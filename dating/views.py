import json
import logging
import math
import random
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from django.shortcuts import render, redirect, get_object_or_404
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login, logout
from django.db import IntegrityError, transaction
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery
from django.utils import timezone
from django.urls import reverse
from django.contrib.auth.models import User
from django.conf import settings
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from .models import (
    CallSession,
    CallSignal,
    ChatMessage,
    Conversation,
    Match,
    PaymentTransaction,
    Profile,
    ProfilePhoto,
    SubscriptionPlan,
    Swipe,
)
from .forms import LoginForm, SignUpForm, ProfileCreationForm, ProfileForm, SettingsForm
from .serializers import serialize_chat_message, serialize_conversation

PREMIUM_PRODUCTS = {
    'DATING': {
        'name': 'Dating Premium',
        'description': 'More meaningful dating matches.',
    },
    'HOOKUP': {
        'name': 'Hookup Premium',
        'description': 'Premium access for Hookup connections.',
    },
    'SEX_CALL': {
        'name': 'Sex Call Premium',
        'description': 'Premium access for Sex Call connections.',
    },
}
PREMIUM_CATEGORY_NAMES = {
    'DATING': 'dating',
    'HOOKUP': 'hookup',
    'SEX_CALL': 'sex_call',
}
DAILY_FREE_LIKES = 100
MAX_MESSAGE_LENGTH = 2000
MESSAGE_RATE_LIMIT = 30
logger = logging.getLogger(__name__)


def _subscription_details(profile, product):
    prefix = {'DATING': 'dating', 'HOOKUP': 'hookup', 'SEX_CALL': 'sex_call'}[product]
    return (
        getattr(profile, f'{prefix}_premium_tier'),
        getattr(profile, f'{prefix}_premium_expiry'),
    )


def _subscription_plans_payload(category):
    category_key = PREMIUM_CATEGORY_NAMES[category]
    grouped = {category_key: []}
    plans = SubscriptionPlan.objects.filter(
        is_active=True,
        category=category,
    ).order_by('duration_days')
    for plan in plans:
        grouped[category_key].append({
            'id': plan.pk,
            'category': plan.category,
            'tier_name': plan.tier_name,
            'duration_days': plan.duration_days,
            'price': str(plan.price),
            'is_popular': plan.is_popular,
            'features': plan.features,
        })
    return grouped


@require_GET
@login_required
def subscription_plans_api(request):
    profile = Profile.objects.filter(user=request.user).first()
    if not profile:
        return JsonResponse({'status': 'error', 'message': 'profile_required'}, status=400)
    return JsonResponse(_subscription_plans_payload(profile.relationship_mode))


def _candidate_queryset(user):
    current_profile = Profile.objects.filter(user=user).first()
    if not current_profile or not current_profile.age or current_profile.age < 18:
        return Profile.objects.none()

    active_limit = timezone.now() - timedelta(days=60)
    swiped_ids = Swipe.objects.filter(swiper=user).values('swiped_id')
    candidates = (
        Profile.objects.exclude(user=user)
        .exclude(user_id__in=Subquery(swiped_ids))
        .filter(
            show_in_discovery=True,
            relationship_mode=current_profile.relationship_mode,
            gender=current_profile.preferred_gender,
            preferred_gender=current_profile.gender,
            age__gte=max(18, current_profile.min_age_pref),
            age__lte=current_profile.max_age_pref,
            min_age_pref__lte=current_profile.age,
            max_age_pref__gte=current_profile.age,
            last_active__gte=active_limit,
        )
        .select_related('user')
        .prefetch_related('photos', 'tags')
    )

    if current_profile.relationship_mode == 'HOOKUP':
        if current_profile.latitude is not None and current_profile.longitude is not None:
            latitude = float(current_profile.latitude)
            longitude = float(current_profile.longitude)
            radius = current_profile.max_distance_km
            latitude_delta = radius / 111.32
            longitude_delta = min(180, radius / (111.32 * max(abs(math.cos(math.radians(latitude))), 0.01)))
            candidates = candidates.filter(
                latitude__range=(
                    max(-90, latitude - latitude_delta),
                    min(90, latitude + latitude_delta),
                )
            )
            lower_longitude = longitude - longitude_delta
            upper_longitude = longitude + longitude_delta
            if lower_longitude < -180:
                candidates = candidates.filter(
                    Q(longitude__gte=lower_longitude + 360) | Q(longitude__lte=upper_longitude)
                )
            elif upper_longitude > 180:
                candidates = candidates.filter(
                    Q(longitude__gte=lower_longitude) | Q(longitude__lte=upper_longitude - 360)
                )
            else:
                candidates = candidates.filter(longitude__range=(lower_longitude, upper_longitude))
        elif current_profile.location:
            candidates = candidates.filter(location__iexact=current_profile.location)
        else:
            return Profile.objects.none()

    return candidates


def _distance_km(latitude_a, longitude_a, latitude_b, longitude_b):
    lat_a, lat_b = math.radians(latitude_a), math.radians(latitude_b)
    delta_lat = lat_b - lat_a
    delta_lon = math.radians(longitude_b - longitude_a)
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    )
    haversine = min(1, max(0, haversine))
    return 6371 * 2 * math.atan2(math.sqrt(haversine), math.sqrt(1 - haversine))


def get_profile_batch(user, limit=10):
    current_profile = Profile.objects.filter(user=user).first()
    if not current_profile:
        return []

    candidate_queryset = _candidate_queryset(user)
    if (
        current_profile.is_premium()
        and current_profile.premium_tier_for() == 'PLATINUM'
    ):
        candidates = candidate_queryset.order_by('-is_verified', '-last_active')
    else:
        candidates = candidate_queryset.order_by('?')
    if current_profile.relationship_mode != 'HOOKUP' or current_profile.latitude is None:
        return list(candidates[:limit])

    origin = (float(current_profile.latitude), float(current_profile.longitude))
    profiles = list(candidates)
    profiles = [
        profile for profile in profiles
        if profile.latitude is not None
        and profile.longitude is not None
        and _distance_km(
            *origin, float(profile.latitude), float(profile.longitude)
        ) <= current_profile.max_distance_km
    ]
    return random.sample(profiles, min(limit, len(profiles)))


def _profile_payload(profile):
    photo = profile.photos.filter(is_main=True).first() or profile.photos.first()
    media_url = (
        photo.image.url
        if photo and photo.image and photo.image.storage.exists(photo.image.name)
        else ''
    )
    bio = profile.bio
    return {
        'id': profile.user_id,
        'username': profile.user.username,
        'age': profile.age or '??',
        'location': profile.location,
        'job_title': profile.job_title,
        'bio': bio[:100] + '...' if len(bio) > 100 else bio,
        'first_date_idea': profile.first_date_idea,
        'image_url': media_url,
        'is_video': photo.is_video if photo else False,
        'tags': [tag.name for tag in profile.tags.all()[:3]],
        'relationship_mode': profile.relationship_mode,
        'is_verified': profile.is_verified,
        'is_vip': profile.is_vip,
        'is_online': profile.last_active >= timezone.now() - timedelta(minutes=10),
    }

# --- API: Get Profiles JSON for Swipe UI ---
@login_required
def get_profiles_json(request):
    profiles = get_profile_batch(request.user, limit=10)
    profile = Profile.objects.filter(user=request.user).only('relationship_mode').first()
    logger.info(
        'Discovery feed returned %d profiles for user_id=%s mode=%s',
        len(profiles),
        request.user.pk,
        profile.relationship_mode if profile else 'unavailable',
    )
    return JsonResponse({'profiles': [_profile_payload(profile) for profile in profiles]})

# --- VIEWS: Profile Management ---
@login_required
def profile_detail(request):
    try:
        profile = request.user.profile
    except Profile.DoesNotExist:
        return redirect('create_profile')
    
    main_photo = profile.photos.filter(is_main=True).first() or profile.photos.first()
    other_photos = profile.photos.exclude(id=main_photo.id) if main_photo else profile.photos.all()
    
    return render(request, 'dating/profile_detail.html', {
        'profile': profile, 
        'main_photo': main_photo, 
        'other_photos': other_photos, 
        'is_own_profile': True,
        'is_online': profile.last_active >= timezone.now() - timedelta(minutes=10),
    })

@login_required
def public_profile(request, pk):
    if request.user.id == pk:
        return redirect('profile')
    profile = get_object_or_404(
        Profile.objects.filter(age__gte=18, show_in_discovery=True),
        user_id=pk,
    )
    main_photo = profile.photos.filter(is_main=True).first() or profile.photos.first()
    other_photos = profile.photos.exclude(id=main_photo.id) if main_photo else profile.photos.all()
    
    return render(request, 'dating/public_profile.html', {
        'profile': profile, 
        'main_photo': main_photo, 
        'other_photos': other_photos, 
        'is_own_profile': False
    })

@login_required
def edit_profile(request):
    try:
        profile = request.user.profile
    except Profile.DoesNotExist:
        return redirect('create_profile')

    if request.method == 'POST':
        action = request.POST.get('action')
        photo_id = request.POST.get('photo_id')
        
        if action and photo_id:
            photo = ProfilePhoto.objects.filter(id=photo_id, profile=profile).first()
            if photo:
                if action == 'set_main':
                    profile.photos.update(is_main=False)
                    photo.is_main = True
                    photo.save()
                elif action == 'delete':
                    photo.delete()
            return redirect('edit_profile')

        form = ProfileForm(
            request.POST,
            request.FILES,
            instance=profile,
            initial={'name': request.user.first_name or request.user.username},
        )
        if form.is_valid():
            more_photos = request.FILES.getlist('more_photos')
            remaining_slots = max(0, 6 - profile.photos.count())
            if len(more_photos) > remaining_slots:
                form.add_error('more_photos', f'You can add up to {remaining_slots} more media file(s).')
            else:
                profile = form.save()
                request.user.first_name = form.cleaned_data['name']
                request.user.save(update_fields=['first_name'])
                request.session['active_connection_mode'] = profile.relationship_mode
                main_index = request.POST.get('new_main_index')
                try:
                    main_index = int(main_index) if main_index else None
                except ValueError:
                    main_index = None
                if main_index is not None and not 0 <= main_index < len(more_photos):
                    main_index = None
                if main_index is not None:
                    profile.photos.update(is_main=False)
                has_main = profile.photos.filter(is_main=True).exists()
                for index, uploaded_file in enumerate(more_photos):
                    is_main = index == main_index if main_index is not None else not has_main and index == 0
                    ProfilePhoto.objects.create(
                        profile=profile,
                        image=uploaded_file,
                        is_main=is_main,
                    )
                return redirect(f"{reverse('edit_profile')}?saved=1")
    else:
        form = ProfileForm(
            instance=profile,
            initial={'name': request.user.first_name or request.user.username},
        )
    
    existing_photos = profile.photos.all().order_by('-is_main', '-uploaded_at')
    
    return render(request, 'dating/edit_profile.html', {
        'form': form, 
        'profile': profile,
        'existing_photos': existing_photos,
        'current_count': profile.photos.count(),
        'media_limit': 6
    })

# --- ACTION: Swiping ---
@login_required
def swipe_view(request):
    profile = Profile.objects.filter(user=request.user).first()
    if not profile:
        return redirect('create_profile')
    if not profile.age or profile.age < 18:
        return redirect('edit_profile')
    profile.last_active = timezone.now()
    profile.save(update_fields=['last_active'])
    product_type = profile.relationship_mode
    return render(request, 'dating/swipe_card.html', {
        'SWIPE_URL': reverse('swipe_action'),
        'PROFILES_URL': reverse('get_profiles_json'),
        'PREMIUM_URL': f"{reverse('premium_landing')}?product={product_type}",
        'csrf_token': get_token(request),
        'relationship_mode': profile.get_relationship_mode_display(),
        'product_type': product_type,
        'is_premium': profile.is_premium(),
    })

@login_required
@require_POST
def swipe_action(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'status': 'error', 'message': 'invalid_request'}, status=400)

    action = data.get('action')
    if action not in ('LIKE', 'PASS', 'DIRECT'):
        return JsonResponse({'status': 'error', 'message': 'invalid_action'}, status=400)
    try:
        target_id = int(data.get('target_user_id'))
    except (TypeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'invalid_target'}, status=400)
    if target_id == request.user.id:
        return JsonResponse({'status': 'error', 'message': 'cannot_swipe_self'}, status=400)
    if Swipe.objects.filter(swiper=request.user, swiped_id=target_id).exists():
        return JsonResponse({'status': 'error', 'message': 'already_swiped'}, status=409)

    target_profile = get_object_or_404(_candidate_queryset(request.user), user_id=target_id)
    current_profile = get_object_or_404(Profile, user=request.user)
    swipe_type = 'PASS' if action == 'PASS' else 'LIKE'
    is_direct = action == 'DIRECT'
    with transaction.atomic():
        if Swipe.objects.filter(swiper=request.user, swiped_id=target_id).exists():
            return JsonResponse({'status': 'error', 'message': 'already_swiped'}, status=409)
        if swipe_type == 'LIKE' and not current_profile.is_premium():
            likes_today = Swipe.objects.filter(
                swiper=request.user,
                type='LIKE',
                mode=current_profile.relationship_mode,
                timestamp__date=timezone.localdate(),
            ).count()
            if likes_today >= DAILY_FREE_LIKES:
                return JsonResponse({'status': 'error', 'message': 'limit_reached_likes'}, status=429)

        _, created = Swipe.objects.get_or_create(
            swiper=request.user,
            swiped=target_profile.user,
            defaults={
                'type': swipe_type,
                'mode': current_profile.relationship_mode,
                'is_direct': is_direct,
            },
        )
        if not created:
            return JsonResponse({'status': 'error', 'message': 'already_swiped'}, status=409)
        reciprocal_like = Swipe.objects.filter(
            swiper=target_profile.user,
            swiped=request.user,
            type='LIKE',
            mode=current_profile.relationship_mode,
        ).first()
        match = None
        if swipe_type == 'LIKE' and reciprocal_like:
            user1, user2 = sorted((request.user, target_profile.user), key=lambda user: user.pk)
            match, created = Match.objects.get_or_create(
                user1=user1,
                user2=user2,
                defaults={
                    'mode': current_profile.relationship_mode,
                    'has_direct_interest': is_direct or reciprocal_like.is_direct,
                    'expires_at': timezone.now() + timedelta(days=7),
                },
            )
            _get_or_create_conversation(match)
            if not created and (is_direct or reciprocal_like.is_direct):
                match.has_direct_interest = True
                match.save(update_fields=['has_direct_interest'])

    return JsonResponse({
        'status': 'success',
        'is_match': match is not None,
        'match_id': match.pk if match else None,
        'is_direct_interest': bool(match and match.has_direct_interest),
    })

# --- LISTS: Matches and Likes ---
@login_required
def match_list(request):
    viewer_profile = Profile.objects.filter(user=request.user).first()
    matches = Match.objects.filter(
        Q(user1=request.user) | Q(user2=request.user),
        expires_at__gt=timezone.now(),
    ).order_by('-created_at')
    data = []
    for m in matches:
        other = m.user2 if m.user1 == request.user else m.user1
        profile = Profile.objects.filter(user=other).first()
        if not profile:
            continue
        data.append({
            'id': m.pk,
            'other_user_id': other.pk,
            'username': other.username,
            'age': profile.age,
            'first_date_idea': profile.first_date_idea,
            'expires_in': max(0, (m.expires_at - timezone.now()).days),
            'has_direct_interest': m.has_direct_interest,
            'mode': m.mode,
        })
    return render(request, 'dating/match_list.html', {
        'matches': data,
        'can_video_call': _can_use_sex_call(viewer_profile),
    })

@login_required
def likes_list(request):
    profile = Profile.objects.filter(user=request.user).first()
    if not profile:
        return redirect('create_profile')
    product_type = profile.relationship_mode
    premium_tier = profile.premium_tier_for()
    if not profile.is_premium():
        return render(request, 'dating/likes_list.html', {
            'likes': [],
            'premium_required': True,
            'csrf_token': get_token(request),
            'product_type': product_type,
            'premium_tier': premium_tier,
            'incoming_like_count': Swipe.objects.filter(
                swiped=request.user,
                type='LIKE',
                mode=profile.relationship_mode,
                swiper_id__in=_candidate_queryset(request.user).values('user_id'),
            ).count(),
        })
    eligible_likers = _candidate_queryset(request.user).values('user_id')
    likers_ids = Swipe.objects.filter(
        swiped=request.user,
        type='LIKE',
        mode=profile.relationship_mode,
        swiper_id__in=eligible_likers,
    ).values_list('swiper_id', flat=True)
    profiles = Profile.objects.filter(user_id__in=likers_ids).prefetch_related('photos')
    return render(request, 'dating/likes_list.html', {
        'likes': profiles,
        'csrf_token': get_token(request),
        'product_type': product_type,
        'premium_tier': premium_tier,
    })

# --- PREMIUM & REWIND ---
@login_required
@require_POST
def rewind_last_swipe(request):
    profile = Profile.objects.filter(user=request.user).first()
    if not profile or not profile.is_premium():
        return JsonResponse({'status': 'error', 'message': 'premium_required'}, status=403)
    last = Swipe.objects.filter(swiper=request.user).last()
    if last:
        swiped_user_id = last.swiped_id
        last.delete()
        swiped_profile = Profile.objects.filter(user_id=swiped_user_id).first()
        if swiped_profile:
            return JsonResponse({'status': 'success', 'profile': _profile_payload(swiped_profile)})
    return JsonResponse({'status': 'error', 'message': 'no_swipes'}, status=404)

@login_required
def premium_landing(request):
    profile = Profile.objects.filter(user=request.user).first()
    if not profile:
        return redirect('create_profile')
    product_type = profile.relationship_mode
    product = PREMIUM_PRODUCTS[product_type]
    tier, expiry = _subscription_details(profile, product_type) if profile else ('', None)
    return render(request, 'dating/premium_landing.html', {
        'profile': profile,
        'product_type': product_type,
        'mode_name': dict(SubscriptionPlan.CATEGORY_CHOICES)[product_type],
        'status_name': 'Sex Call Pass' if product_type == 'SEX_CALL' else f'{dict(SubscriptionPlan.CATEGORY_CHOICES)[product_type]} Premium',
        'product': product,
        'plans_api_url': reverse('subscription_plans_api'),
        'premium_tier': tier,
        'premium_expiry': expiry,
        'is_premium': profile.is_premium(product_type),
    })

@login_required
def premium_checkout(request):
    profile = Profile.objects.filter(user=request.user).first()
    if not profile:
        return redirect('create_profile')
    product_type = profile.relationship_mode
    plan_id = request.GET.get('plan_id')
    if not plan_id and request.GET.get('plan'):
        plan_id = request.GET.get('plan')
    plan = get_object_or_404(SubscriptionPlan, pk=plan_id, is_active=True)
    if plan.category != product_type:
        return HttpResponseBadRequest('The selected pass does not match your connection mode.')
    product = PREMIUM_PRODUCTS[product_type]
    tier, expiry = _subscription_details(profile, product_type) if profile else ('', None)
    return render(request, 'dating/premium_checkout.html', {
        'product_type': product_type,
        'product': product,
        'plan_type': plan.tier_name,
        'plan': plan,
        'premium_tier': tier,
        'premium_expiry': expiry,
        'is_premium': profile.is_premium(product_type),
        'user_email': request.user.email,
        'paystack_key': getattr(settings, 'PAYSTACK_PUBLIC_KEY', ''),
        'flutterwave_key': getattr(settings, 'FLUTTERWAVE_PUBLIC_KEY', ''),
        'paystack_enabled': bool(
            getattr(settings, 'PAYSTACK_PUBLIC_KEY', '')
            and getattr(settings, 'PAYSTACK_SECRET_KEY', '')
        ),
        'flutterwave_enabled': bool(
            getattr(settings, 'FLUTTERWAVE_PUBLIC_KEY', '')
            and getattr(settings, 'FLUTTERWAVE_SECRET_KEY', '')
        ),
        'payment_configured': bool(
            (
                getattr(settings, 'PAYSTACK_PUBLIC_KEY', '')
                and getattr(settings, 'PAYSTACK_SECRET_KEY', '')
            )
            or (
                getattr(settings, 'FLUTTERWAVE_PUBLIC_KEY', '')
                and getattr(settings, 'FLUTTERWAVE_SECRET_KEY', '')
            )
        ),
        'csrf_token': get_token(request),
    })

@login_required
def premium_success(request):
    profile = Profile.objects.filter(user=request.user).first()
    if not profile:
        return redirect('create_profile')
    product_type = profile.relationship_mode
    tier, expiry = _subscription_details(profile, product_type) if profile else ('', None)
    if not profile.is_premium(product_type):
        return redirect('premium_landing')
    return render(request, 'dating/premium_success.html', {
        'profile': profile,
        'product': PREMIUM_PRODUCTS[product_type],
        'product_type': product_type,
        'premium_tier': tier,
        'premium_expiry': expiry,
    })


def _provider_payment(provider, reference, transaction_id=''):
    if provider == 'paystack':
        secret = getattr(settings, 'PAYSTACK_SECRET_KEY', '')
        if not secret:
            return None
        url = f"https://api.paystack.co/transaction/verify/{quote(reference, safe='')}"
        headers = {'Authorization': f'Bearer {secret}'}
    else:
        secret = getattr(settings, 'FLUTTERWAVE_SECRET_KEY', '')
        if not secret or not transaction_id.isdigit():
            return None
        url = f"https://api.flutterwave.com/v3/transactions/{transaction_id}/verify"
        headers = {'Authorization': f'Bearer {secret}'}

    request = Request(url, headers=headers, method='GET')
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except (HTTPError, URLError, OSError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    data = payload.get('data')
    if not isinstance(data, dict):
        return None
    if provider == 'paystack':
        amount_kobo = data.get('amount')
        verified = (
            payload.get('status') is True
            and data.get('status') == 'success'
            and data.get('reference') == reference
        )
        customer = data.get('customer')
        customer_email = customer.get('email', '') if isinstance(customer, dict) else ''
        return verified, amount_kobo, data.get('currency'), customer_email

    verified = (
        payload.get('status') == 'success'
        and data.get('status') == 'successful'
        and data.get('tx_ref') == reference
        and str(data.get('id')) == transaction_id
    )
    customer = data.get('customer')
    customer_email = customer.get('email', '') if isinstance(customer, dict) else ''
    try:
        amount = Decimal(str(data.get('amount'))) * 100
        amount_kobo = int(amount) if amount == amount.to_integral_value() else None
    except (InvalidOperation, TypeError, ValueError):
        amount_kobo = None
    return verified, amount_kobo, data.get('currency'), customer_email

@login_required
@require_POST
def verify_payment(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'status': 'error', 'message': 'invalid_request'}, status=400)

    profile = Profile.objects.filter(user=request.user).first()
    if not profile:
        return JsonResponse({'status': 'error', 'message': 'profile_required'}, status=400)
    plan_id = data.get('plan_id')
    product_type = str(data.get('product', '')).upper()
    provider = data.get('provider')
    reference = str(data.get('reference', '')).strip()
    transaction_id = str(data.get('transaction_id', '')).strip()
    if product_type != profile.relationship_mode:
        return JsonResponse({'status': 'error', 'message': 'plan_mode_mismatch'}, status=400)
    product = PREMIUM_PRODUCTS.get(product_type)
    try:
        plan = SubscriptionPlan.objects.get(pk=plan_id, category=product_type)
    except (SubscriptionPlan.DoesNotExist, TypeError, ValueError):
        plan = None
    if (
        not plan
        or not product
        or provider not in ('paystack', 'flutterwave')
        or not reference
        or len(reference) > 100
        or not request.user.email
    ):
        return JsonResponse({'status': 'error', 'message': 'invalid_payment'}, status=400)
    if provider == 'flutterwave' and not transaction_id.isdigit():
        return JsonResponse({'status': 'error', 'message': 'invalid_payment'}, status=400)
    if plan:
        existing_payment = PaymentTransaction.objects.filter(reference=reference).first()
        if existing_payment:
            if (
                existing_payment.user_id != request.user.id
                or existing_payment.provider != provider
                or existing_payment.plan_type != plan.tier_name
                or existing_payment.product != product_type
                or existing_payment.plan_id != plan.pk
            ):
                return JsonResponse({'status': 'error', 'message': 'payment_reference_used'}, status=409)
            return JsonResponse({'status': 'success', 'already_applied': True})
    if not plan or not plan.is_active:
        return JsonResponse({'status': 'error', 'message': 'invalid_payment'}, status=400)

    try:
        verification = _provider_payment(provider, reference, transaction_id)
    except (ValueError, TypeError):
        verification = None
    if verification is None:
        return JsonResponse({'status': 'error', 'message': 'payment_verification_unavailable'}, status=503)
    verified, amount_kobo, currency, customer_email = verification
    expected_amount = int(plan.price * 100)
    if (
        not verified
        or amount_kobo != expected_amount
        or currency != 'NGN'
        or not isinstance(customer_email, str)
        or customer_email.casefold() != request.user.email.casefold()
    ):
        return JsonResponse({'status': 'error', 'message': 'payment_not_verified'}, status=400)

    try:
        with transaction.atomic():
            payment, created = PaymentTransaction.objects.get_or_create(
                reference=reference,
                defaults={
                    'user': request.user,
                    'provider': provider,
                    'plan_type': plan.tier_name,
                    'product': product_type,
                    'plan': plan,
                    'amount_kobo': expected_amount,
                },
            )
            if not created:
                if (
                    payment.user_id != request.user.id
                    or payment.provider != provider
                    or payment.plan_type != plan.tier_name
                    or payment.product != product_type
                    or payment.plan_id != plan.pk
                ):
                    return JsonResponse({'status': 'error', 'message': 'payment_reference_used'}, status=409)
                return JsonResponse({'status': 'success', 'already_applied': True})

            prefix = {'DATING': 'dating', 'HOOKUP': 'hookup', 'SEX_CALL': 'sex_call'}[product_type]
            expiry_field = f'{prefix}_premium_expiry'
            tier_field = f'{prefix}_premium_tier'
            expiry = getattr(profile, expiry_field)
            start = expiry if expiry and expiry > timezone.now() else timezone.now()
            setattr(profile, tier_field, plan.tier_name)
            setattr(profile, expiry_field, start + timedelta(days=plan.duration_days))
            profile.save(update_fields=[tier_field, expiry_field])
    except IntegrityError:
        return JsonResponse({'status': 'error', 'message': 'payment_reference_used'}, status=409)
    return JsonResponse({'status': 'success'})


def _call_participants(call, user):
    if user.pk not in (call.caller_id, call.receiver_id):
        raise Http404
    return call


def _call_payload(call):
    caller_profile = Profile.objects.filter(user_id=call.caller_id).first()
    caller_photo = (
        caller_profile.photos.filter(is_main=True).first() or caller_profile.photos.first()
        if caller_profile else None
    )
    photo_url = ''
    if caller_photo and caller_photo.image and caller_photo.image.storage.exists(caller_photo.image.name):
        photo_url = caller_photo.image.url
    return {
        'room_id': str(call.room_id),
        'status': call.status,
        'caller_id': call.caller_id,
        'caller_name': call.caller.first_name or call.caller.username,
        'caller_photo': photo_url,
        'receiver_id': call.receiver_id,
    }


def _can_use_sex_call(profile):
    return bool(
        profile
        and (
            profile.relationship_mode == 'SEX_CALL'
            or profile.is_sex_call_premium
        )
    )


@login_required
@require_POST
def call_initiate_api(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'status': 'error', 'message': 'invalid_request'}, status=400)

    caller_profile = Profile.objects.filter(user=request.user).first()
    if not _can_use_sex_call(caller_profile):
        return JsonResponse({
            'status': 'error',
            'message': 'sex_call_pass_required',
            'subscribe_url': reverse('premium_landing'),
        }, status=403)
    try:
        receiver_id = int(data.get('receiver_id'))
    except (TypeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'invalid_receiver'}, status=400)
    if receiver_id == request.user.pk:
        return JsonResponse({'status': 'error', 'message': 'invalid_receiver'}, status=400)

    match = Match.objects.filter(
        Q(user1=request.user, user2_id=receiver_id)
        | Q(user2=request.user, user1_id=receiver_id),
        expires_at__gt=timezone.now(),
    ).first()
    if not match:
        return JsonResponse({'status': 'error', 'message': 'active_sex_call_match_required'}, status=403)
    receiver = match.user2 if match.user1_id == request.user.pk else match.user1
    call = CallSession.objects.create(
        caller=request.user,
        receiver=receiver,
        status='ringing',
    )
    return JsonResponse({'status': 'success', 'call': _call_payload(call)}, status=201)


@login_required
@require_GET
def incoming_calls_api(request):
    current_time = timezone.now()
    CallSession.objects.filter(
        receiver=request.user,
        status='ringing',
        created_at__lt=current_time - timedelta(minutes=5),
    ).update(status='ended', ended_at=current_time)
    call = (
        CallSession.objects.filter(
            receiver=request.user,
            status='ringing',
            created_at__gte=current_time - timedelta(minutes=5),
        )
        .select_related('caller')
        .order_by('-created_at')
        .first()
    )
    return JsonResponse({'call': _call_payload(call) if call else None})


@login_required
@require_POST
def call_respond_api(request, room_id):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)
    action = data.get('action') if isinstance(data, dict) else None
    if action not in ('accept', 'decline'):
        return JsonResponse({'status': 'error', 'message': 'invalid_action'}, status=400)
    with transaction.atomic():
        call = get_object_or_404(
            CallSession.objects.select_for_update().select_related('caller'),
            room_id=room_id,
            receiver=request.user,
        )
        if call.status != 'ringing':
            return JsonResponse({'status': 'error', 'message': 'call_not_ringing'}, status=409)
        if call.created_at < timezone.now() - timedelta(minutes=5):
            call.status = 'ended'
            call.ended_at = timezone.now()
            call.save(update_fields=['status', 'ended_at'])
            return JsonResponse({'status': 'error', 'message': 'call_expired'}, status=409)
        if action == 'accept':
            call.status = 'connected'
            call.started_at = timezone.now()
        else:
            call.status = 'declined'
            call.ended_at = timezone.now()
        call.save(update_fields=['status', 'started_at', 'ended_at'])
    return JsonResponse({'status': 'success', 'call': _call_payload(call)})


@login_required
@require_POST
def call_end_api(request, room_id):
    with transaction.atomic():
        call = get_object_or_404(
            CallSession.objects.select_for_update(),
            room_id=room_id,
        )
        _call_participants(call, request.user)
        if call.status not in ('ended', 'declined'):
            call.status = 'ended'
            call.ended_at = timezone.now()
            call.save(update_fields=['status', 'ended_at'])
    return JsonResponse({'status': 'success', 'call': _call_payload(call)})


@login_required
@require_GET
def call_signals_api(request, room_id):
    call = get_object_or_404(
        CallSession.objects.select_related('caller', 'receiver'),
        room_id=room_id,
    )
    _call_participants(call, request.user)
    try:
        after_id = max(0, int(request.GET.get('after_id', '0')))
    except (TypeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'invalid_cursor'}, status=400)
    signals = call.signals.exclude(sender=request.user).filter(pk__gt=after_id)[:100]
    return JsonResponse({
        'signals': [{
            'id': signal.pk,
            'type': signal.signal_type,
            'payload': signal.payload,
        } for signal in signals],
    })


@login_required
@require_GET
def call_status_api(request, room_id):
    call = get_object_or_404(
        CallSession.objects.select_related('caller', 'receiver'),
        room_id=room_id,
    )
    _call_participants(call, request.user)
    return JsonResponse({'call': _call_payload(call)})


@login_required
@require_POST
def call_signal_send_api(request, room_id):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'status': 'error', 'message': 'invalid_request'}, status=400)
    signal_type = data.get('type')
    payload = data.get('payload')
    if signal_type not in ('offer', 'answer', 'candidate') or not isinstance(payload, dict):
        return JsonResponse({'status': 'error', 'message': 'invalid_signal'}, status=400)
    if len(json.dumps(payload)) > 20000:
        return JsonResponse({'status': 'error', 'message': 'signal_too_large'}, status=400)

    call = get_object_or_404(CallSession.objects.select_related('caller', 'receiver'), room_id=room_id)
    _call_participants(call, request.user)
    if call.status not in ('ringing', 'connected'):
        return JsonResponse({'status': 'error', 'message': 'call_not_active'}, status=409)
    if signal_type == 'offer' and request.user.pk != call.caller_id:
        return JsonResponse({'status': 'error', 'message': 'caller_must_send_offer'}, status=403)
    if signal_type == 'answer' and (
        request.user.pk != call.receiver_id or call.status != 'connected'
    ):
        return JsonResponse({'status': 'error', 'message': 'receiver_must_answer_active_call'}, status=403)
    signal = CallSignal.objects.create(
        call=call,
        sender=request.user,
        signal_type=signal_type,
        payload=payload,
    )
    return JsonResponse({'status': 'success', 'id': signal.pk}, status=201)


def _get_active_match(match_id, user):
    match = get_object_or_404(
        Match.objects.filter(
            Q(user1=user) | Q(user2=user),
            expires_at__gt=timezone.now(),
        ),
        pk=match_id,
    )
    adults = Profile.objects.filter(
        user__in=(match.user1_id, match.user2_id),
        age__gte=18,
    ).count()
    if adults != 2:
        raise Http404
    return match


def _get_or_create_conversation(match):
    with transaction.atomic():
        locked_match = Match.objects.select_for_update().get(pk=match.pk)
        conversation = (
            Conversation.objects
            .filter(participants=locked_match.user1_id)
            .filter(participants=locked_match.user2_id)
            .annotate(participant_count=Count('participants', distinct=True))
            .filter(participant_count=2)
            .first()
        )
        if conversation:
            return conversation

        conversation = Conversation.objects.create()
        conversation.participants.add(locked_match.user1_id, locked_match.user2_id)
        return conversation


def _get_active_conversation(conversation_id, user):
    conversation = get_object_or_404(
        Conversation.objects.filter(participants=user).distinct(),
        pk=conversation_id,
    )
    participants = list(conversation.participants.order_by('pk'))
    if len(participants) != 2:
        raise Http404
    other_user = next((participant for participant in participants if participant.pk != user.pk), None)
    if not other_user:
        raise Http404
    match = Match.objects.filter(
        Q(user1=user, user2=other_user) | Q(user1=other_user, user2=user),
        expires_at__gt=timezone.now(),
    ).first()
    if not match:
        raise Http404
    adults = Profile.objects.filter(
        user__in=(user.pk, other_user.pk),
        age__gte=18,
    ).count()
    if adults != 2:
        raise Http404
    return conversation, match, other_user


@login_required
def chat_room(request, match_id):
    match = _get_active_match(match_id, request.user)
    other_user = match.user2 if match.user1_id == request.user.id else match.user1
    other_profile = Profile.objects.filter(user=other_user).first()
    if not other_profile:
        return redirect('match_list')
    current_profile = Profile.objects.filter(user=request.user).first()
    conversation = _get_or_create_conversation(match)
    return render(request, 'dating/chat_room.html', {
        'conversation': conversation,
        'match': match,
        'other_user': other_user,
        'messages_url': reverse('conversation_messages_api', args=[conversation.pk]),
        'send_url': reverse('conversation_send_api', args=[conversation.pk]),
        'messages_allowed': bool(
            current_profile
            and current_profile.allow_messages
            and other_profile.allow_messages
        ),
        'max_message_length': MAX_MESSAGE_LENGTH,
    })


@login_required
@require_GET
def conversation_messages_api(request, conversation_id):
    conversation, _, other_user = _get_active_conversation(conversation_id, request.user)
    try:
        after_id = max(0, int(request.GET.get('after_id', 0)))
    except (TypeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'invalid_cursor'}, status=400)

    messages_query = conversation.messages.select_related('sender').prefetch_related('read_by')
    if after_id:
        messages = list(messages_query.filter(pk__gt=after_id).order_by('pk')[:100])
    else:
        messages = list(messages_query.order_by('-pk')[:100])
        messages.reverse()

    incoming_ids = [
        message.pk for message in messages
        if message.sender_id != request.user.pk
    ]
    if incoming_ids:
        read_relation = ChatMessage.read_by.through
        read_relation.objects.bulk_create(
            [
                read_relation(chatmessage_id=message_id, user_id=request.user.pk)
                for message_id in incoming_ids
            ],
            ignore_conflicts=True,
        )
        for message in messages:
            if message.pk in incoming_ids:
                message._prefetched_objects_cache['read_by'] = [
                    *message._prefetched_objects_cache['read_by'],
                    request.user,
                ]

    return JsonResponse({
        'messages': [
            serialize_chat_message(message, other_user.pk)
            for message in messages
        ],
    })


@login_required
@require_POST
def conversation_send_api(request, conversation_id):
    conversation, _, other_user = _get_active_conversation(conversation_id, request.user)
    sender_profile = Profile.objects.filter(user=request.user).first()
    recipient_profile = Profile.objects.filter(user=other_user).first()
    if (
        not sender_profile
        or not recipient_profile
        or not sender_profile.allow_messages
        or not recipient_profile.allow_messages
    ):
        return JsonResponse({'status': 'error', 'message': 'messaging_disabled'}, status=403)
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'status': 'error', 'message': 'invalid_request'}, status=400)
    text = str(data.get('text', '')).strip()
    if not text or len(text) > MAX_MESSAGE_LENGTH:
        return JsonResponse({'status': 'error', 'message': 'invalid_message'}, status=400)
    recent_messages = conversation.messages.filter(
        sender=request.user,
        created_at__gte=timezone.now() - timedelta(minutes=1),
    ).count()
    if recent_messages >= MESSAGE_RATE_LIMIT:
        return JsonResponse({'status': 'error', 'message': 'rate_limit'}, status=429)

    with transaction.atomic():
        message = ChatMessage.objects.create(
            conversation=conversation,
            sender=request.user,
            text=text,
        )
        message.read_by.add(request.user)
        conversation.save(update_fields=['updated_at'])

    return JsonResponse({
        'status': 'success',
        'message': serialize_chat_message(message, other_user.pk),
    }, status=201)


@login_required
@require_GET
def conversations_api(request):
    active_matches = Match.objects.filter(
        Q(user1=request.user) | Q(user2=request.user),
        expires_at__gt=timezone.now(),
    ).values_list('user1_id', 'user2_id')
    active_partner_ids = {
        user2_id if user1_id == request.user.pk else user1_id
        for user1_id, user2_id in active_matches
    }
    adult_partner_ids = set(
        Profile.objects.filter(
            user_id__in=active_partner_ids,
            age__gte=18,
        ).values_list('user_id', flat=True)
    )
    own_profile = Profile.objects.filter(user=request.user, age__gte=18).exists()
    if not own_profile or not adult_partner_ids:
        return JsonResponse({'conversations': []})

    unread_messages = (
        ChatMessage.objects
        .filter(conversation_id=OuterRef('pk'))
        .exclude(sender=request.user)
        .exclude(read_by=request.user)
        .order_by()
        .values('conversation_id')
        .annotate(total=Count('pk'))
        .values('total')
    )
    latest_messages = ChatMessage.objects.filter(
        conversation_id=OuterRef('pk'),
    ).order_by('-pk')
    conversations = (
        Conversation.objects
        .filter(participants=request.user)
        .filter(participants__in=adult_partner_ids)
        .annotate(
            unread_count=Subquery(unread_messages, output_field=IntegerField()),
            last_message_id=Subquery(latest_messages.values('pk')[:1]),
            last_message_sender_id=Subquery(latest_messages.values('sender_id')[:1]),
            last_message_text=Subquery(latest_messages.values('text')[:1]),
            last_message_created_at=Subquery(latest_messages.values('created_at')[:1]),
        )
        .prefetch_related('participants')
        .order_by('-updated_at', '-pk')
    )
    results = []
    for conversation in conversations:
        participants = list(conversation.participants.all())
        if len(participants) != 2:
            continue
        other_user = next(
            (participant for participant in participants if participant.pk != request.user.pk),
            None,
        )
        if not other_user:
            continue
        if other_user.pk not in adult_partner_ids:
            continue
        results.append(serialize_conversation(conversation, other_user))
    return JsonResponse({'conversations': results})


@login_required
def conversation_room(request, conversation_id):
    conversation, match, other_user = _get_active_conversation(conversation_id, request.user)
    other_profile = Profile.objects.filter(user=other_user).first()
    current_profile = Profile.objects.filter(user=request.user).first()
    return render(request, 'dating/chat_room.html', {
        'conversation': conversation,
        'match': match,
        'other_user': other_user,
        'messages_url': reverse('conversation_messages_api', args=[conversation.pk]),
        'send_url': reverse('conversation_send_api', args=[conversation.pk]),
        'messages_allowed': bool(
            current_profile
            and other_profile
            and current_profile.allow_messages
            and other_profile.allow_messages
        ),
        'max_message_length': MAX_MESSAGE_LENGTH,
    })

# --- ACCOUNT FLOW & STATIC ---
def index(request): return render(request, 'dating/landing.html')

@ensure_csrf_cookie
def signup(request):
    if request.user.is_authenticated:
        return redirect('swipe_card')
    form = SignUpForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        mode = form.cleaned_data['relationship_mode']
        user = form.save()
        login(request, user)
        request.session['active_connection_mode'] = mode
        return redirect('create_profile')
    return render(request, 'registration/signup.html', {'form': form})


@ensure_csrf_cookie
def login_view(request):
    if request.user.is_authenticated:
        return redirect('swipe_card')
    initial = {
        'relationship_mode': request.session.get('active_connection_mode', 'DATING'),
    }
    form = LoginForm(request, request.POST or None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        mode = form.cleaned_data['relationship_mode']
        user = form.get_user()
        login(request, user)
        request.session['active_connection_mode'] = mode
        profile = Profile.objects.filter(user=user).first()
        if profile:
            profile.relationship_mode = mode
            profile.save(update_fields=['relationship_mode'])

        next_url = request.POST.get('next') or request.GET.get('next', '')
        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect(next_url)
        return redirect('swipe_card' if profile else 'create_profile')
    return render(request, 'registration/login.html', {'form': form, 'next': request.GET.get('next', '')})


@login_required
def create_profile(request):
    try:
        if request.user.profile: return redirect('swipe_card')
    except Profile.DoesNotExist: pass

    form = ProfileCreationForm(
        request.POST or None,
        request.FILES or None,
        initial={
            'relationship_mode': request.session.get('active_connection_mode', 'DATING'),
        },
    )
    if request.method == 'POST' and form.is_valid():
        p = form.save(commit=False)
        p.user = request.user
        if form.cleaned_data.get('name'):
            request.user.first_name = form.cleaned_data['name']
            request.user.save(update_fields=['first_name'])
        p.relationship_mode = request.session.get('active_connection_mode', 'DATING')
        p.save()
        form.save_m2m()
        request.session['active_connection_mode'] = p.relationship_mode
        if request.FILES.get('photo'):
            ProfilePhoto.objects.create(profile=p, image=request.FILES.get('photo'), is_main=True)
        return redirect('swipe_card')
    return render(request, 'dating/create_profile.html', {'form': form})

@login_required
def delete_account(request):
    if request.method == 'POST':
        user = request.user
        logout(request)
        user.delete()
        return render(request, 'dating/account_deleted.html')
    return render(request, 'dating/confirm_delete.html')

@login_required
def settings_view(request):
    try:
        profile = request.user.profile
    except Profile.DoesNotExist:
        return redirect('create_profile')
        
    form = SettingsForm(request.POST or None, instance=profile)
    if request.method == 'POST' and form.is_valid():
        profile = form.save()
        request.session['active_connection_mode'] = profile.relationship_mode
        return redirect('swipe_card')
    return render(request, 'dating/settings.html', {'form': form, 'profile': profile})

def privacy(request): return render(request, 'dating/privacy.html')
def terms(request): return render(request, 'dating/terms.html')
def contact(request):
    return render(request, 'dating/contact.html')