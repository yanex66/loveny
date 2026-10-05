import json
import logging
import math
import random
import smtplib
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from django.shortcuts import render, redirect, get_object_or_404
from django.http import Http404, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login, logout, views as auth_views
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
    CallGift,
    CallSession,
    CallSignal,
    ChatMessage,
    CoinPackage,
    CoinTransaction,
    CoinWallet,
    Conversation,
    GiftItem,
    Match,
    PaymentTransaction,
    Profile,
    ProfilePhoto,
    SiteConfiguration,
    SubscriptionPlan,
    Swipe,
)
from .forms import (
    LoginForm,
    SignUpForm,
    ProfileCreationForm,
    ProfileForm,
    SettingsForm,
    SafePasswordResetForm,
)
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
        'name': 'Sex Call (Coins Fueled)',
        'description': 'Sex Call uses Coins — no recurring subscriptions.',
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
    if category == 'SEX_CALL':
        # Sex Call is strictly closed-loop and fueled by Coins — zero recurring subscriptions
        return {'sex_call': []}
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


def _candidate_queryset(user, include_test_profiles=False):
    current_profile = Profile.objects.filter(user=user).first()
    if not current_profile:
        return Profile.objects.none()

    if include_test_profiles:
        # Staff/admin preview mode:
        # Guarantee seeded test profiles immediately appear without being blocked
        # by location, distance, last_active, show_in_discovery, or swipe history filters.
        qs = Profile.objects.exclude(user=user).filter(
            Q(is_test_profile=True) | Q(user__username__startswith='test_user_')
        )
        if current_profile and str(current_profile.relationship_mode).upper() in ('SEX_CALL', 'SEXCALL'):
            sc_qs = qs.filter(Q(relationship_mode__iexact='sex_call') | Q(relationship_mode='SEX_CALL'))
            if sc_qs.exists():
                return sc_qs.select_related('user').prefetch_related('photos', 'tags')
        elif current_profile and current_profile.relationship_mode:
            mode_qs = qs.filter(relationship_mode__iexact=str(current_profile.relationship_mode))
            if mode_qs.exists():
                return mode_qs.select_related('user').prefetch_related('photos', 'tags')
        return qs.select_related('user').prefetch_related('photos', 'tags')

    if not current_profile.age or current_profile.age < 18:
        return Profile.objects.none()

    active_limit = timezone.now() - timedelta(days=60)
    swiped_ids = Swipe.objects.filter(swiper=user).values('swiped_id')
    filters = {
        'show_in_discovery': True,
        'is_test_profile': False,
        'relationship_mode': current_profile.relationship_mode,
        'last_active__gte': active_limit,
        'gender': current_profile.preferred_gender,
        'preferred_gender': current_profile.gender,
        'age__gte': max(18, current_profile.min_age_pref),
        'age__lte': current_profile.max_age_pref,
        'min_age_pref__lte': current_profile.age,
        'max_age_pref__gte': current_profile.age,
    }

    candidates = (
        Profile.objects.exclude(user=user)
        .exclude(user_id__in=Subquery(swiped_ids))
        .filter(**filters)
        .select_related('user')
        .prefetch_related('photos', 'tags')
    )

    if current_profile.relationship_mode == 'HOOKUP' and not include_test_profiles:
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


def get_profile_batch(user, limit=10, include_test_profiles=False):
    current_profile = Profile.objects.filter(user=user).first()
    if not current_profile:
        return []

    candidate_queryset = _candidate_queryset(
        user,
        include_test_profiles=include_test_profiles,
    )
    if (
        current_profile.is_premium()
        and current_profile.premium_tier_for() == 'PLATINUM'
    ):
        candidates = candidate_queryset.order_by('-is_verified', '-last_active')
    else:
        candidates = candidate_queryset.order_by('?')
    if (
        current_profile.relationship_mode != 'HOOKUP'
        or current_profile.latitude is None
        or include_test_profiles
    ):
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
    media_url = ''
    if photo and photo.image:
        try:
            if photo.image.storage.exists(photo.image.name):
                media_url = photo.image.url
        except Exception:
            pass
    if not media_url:
        username = profile.user.username if profile.user else 'user'
        media_url = f"https://api.dicebear.com/7.x/avataaars/svg?seed={username}"
    bio = profile.bio or ''
    config = SiteConfiguration.get_solo()
    return {
        'id': profile.user_id,
        'username': profile.user.username if profile.user else '',
        'age': profile.age or '??',
        'location': profile.location or 'Online',
        'job_title': profile.job_title,
        'bio': bio[:100] + '...' if len(bio) > 100 else bio,
        'first_date_idea': profile.first_date_idea,
        'image_url': media_url,
        'avatar': media_url,
        'is_video': photo.is_video if photo else False,
        'tags': [tag.name for tag in profile.tags.all()[:3]],
        'relationship_mode': profile.relationship_mode,
        'is_verified': profile.is_verified,
        'is_vip': profile.is_vip,
        'is_test_profile': profile.is_test_profile,
        'is_online': profile.is_host_ready or (profile.last_active and (timezone.now() - profile.last_active).total_seconds() < 1800),
        'response_rate': getattr(profile, 'response_rate', 98),
        'total_calls_completed': getattr(profile, 'total_calls_completed', 0),
        'is_host_ready': getattr(profile, 'is_host_ready', True),
        'call_rate_per_minute': config.call_rate_per_minute,
    }

# --- API: Get Profiles JSON for Swipe UI ---
@login_required
def get_profiles_json(request):
    include_test_profiles = bool(
        request.session.get('staff_test_profile_preview')
        or request.session.get('preview_test_profiles')
        or request.GET.get('test_profiles') in ('on', '1', 'true', 'True')
        or request.GET.get('preview_test_profiles') in ('on', '1', 'true', 'True')
        or (
            (request.user.is_staff or request.user.is_superuser)
            and (
                request.session.get('staff_test_profile_preview')
                or request.session.get('preview_test_profiles')
            )
        )
    )
    profiles = get_profile_batch(
        request.user,
        limit=10,
        include_test_profiles=include_test_profiles,
    )
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
    include_test_profiles = bool(
        request.session.get('staff_test_profile_preview')
        or request.session.get('preview_test_profiles')
        or request.GET.get('test_profiles') in ('on', '1', 'true', 'True')
        or request.GET.get('preview_test_profiles') in ('on', '1', 'true', 'True')
    )
    qs = Profile.objects.filter(age__gte=18)
    if not include_test_profiles:
        qs = qs.filter(show_in_discovery=True, is_test_profile=False)
    profile = get_object_or_404(qs, user_id=pk)
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
    toggle = request.GET.get('test_profiles') or request.GET.get('preview_test_profiles')
    if toggle in {'on', 'off'}:
        enabled = (toggle == 'on')
        request.session['staff_test_profile_preview'] = enabled
        request.session['preview_test_profiles'] = enabled
        return redirect('swipe_card')

    profile = Profile.objects.filter(user=request.user).first()
    if not profile:
        return redirect('create_profile')
    if not profile.age or profile.age < 18:
        return redirect('edit_profile')
    profile.last_active = timezone.now()
    profile.save(update_fields=['last_active'])
    product_type = profile.relationship_mode
    if product_type == 'SEX_CALL':
        return redirect('sex_call_hub')
    is_preview = bool(
        request.session.get('staff_test_profile_preview')
        or request.session.get('preview_test_profiles')
        or request.GET.get('test_profiles') in ('on', '1', 'true', 'True')
        or request.GET.get('preview_test_profiles') in ('on', '1', 'true', 'True')
        or (
            (request.user.is_staff or request.user.is_superuser)
            and (
                request.session.get('staff_test_profile_preview')
                or request.session.get('preview_test_profiles')
            )
        )
    )
    premium_url = (
        f"{reverse('premium_checkout')}?mode=COINS"
        if product_type == 'SEX_CALL'
        else f"{reverse('premium_landing')}?product={product_type}"
    )
    return render(request, 'dating/swipe_card.html', {
        'SWIPE_URL': reverse('swipe_action'),
        'PROFILES_URL': reverse('get_profiles_json'),
        'PREMIUM_URL': premium_url,
        'csrf_token': get_token(request),
        'relationship_mode': profile.get_relationship_mode_display(),
        'product_type': product_type,
        'is_premium': profile.is_premium(),
        'staff_test_profile_preview': is_preview,
        'is_staff': request.user.is_staff or request.user.is_superuser,
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

    include_test_profiles = bool(
        request.session.get('staff_test_profile_preview')
        or request.session.get('preview_test_profiles')
        or request.GET.get('test_profiles') in ('on', '1', 'true', 'True')
        or request.GET.get('preview_test_profiles') in ('on', '1', 'true', 'True')
        or (
            (request.user.is_staff or request.user.is_superuser)
            and (
                request.session.get('staff_test_profile_preview')
                or request.session.get('preview_test_profiles')
            )
        )
    )

    if include_test_profiles:
        # Allow testing repeated swipes on test profiles
        Swipe.objects.filter(swiper=request.user, swiped_id=target_id).delete()
    elif Swipe.objects.filter(swiper=request.user, swiped_id=target_id).exists():
        return JsonResponse({'status': 'error', 'message': 'already_swiped'}, status=409)
    target_profile = get_object_or_404(
        _candidate_queryset(
            request.user,
            include_test_profiles=include_test_profiles,
        ),
        user_id=target_id,
    )
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
        if profile.is_test_profile and not (
            request.user.is_staff
            and request.session.get('staff_test_profile_preview')
        ):
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
    if request.GET.get('mode') == 'COINS' or request.GET.get('product') == 'SEX_CALL':
        return redirect(f"{reverse('premium_checkout')}?mode=COINS")
    if product_type == 'SEX_CALL':
        return redirect('sex_call_hub')
    product = PREMIUM_PRODUCTS[product_type]
    tier, expiry = _subscription_details(profile, product_type) if profile else ('', None)
    return render(request, 'dating/premium_landing.html', {
        'profile': profile,
        'product_type': product_type,
        'mode_name': dict(SubscriptionPlan.CATEGORY_CHOICES)[product_type],
        'status_name': f'{dict(SubscriptionPlan.CATEGORY_CHOICES)[product_type]} Premium',
        'product': product,
        'plans_api_url': reverse('subscription_plans_api'),
        'premium_tier': tier,
        'premium_expiry': expiry,
        'is_premium': profile.is_premium(product_type),
    })

@login_required
def premium_checkout(request, mode=None):
    profile = Profile.objects.filter(user=request.user).first()
    if not profile:
        return redirect('create_profile')
    product_type = profile.relationship_mode
    req_mode = str(mode or request.GET.get('mode', '')).upper()
    req_product = str(request.GET.get('product', '')).upper()

    if req_mode == 'COINS' or product_type == 'SEX_CALL' or req_product == 'SEX_CALL':
        packages = list(CoinPackage.objects.filter(is_active=True).order_by('order', 'price'))
        selected_pkg_id = (
            request.GET.get('package_id')
            or request.GET.get('package')
            or request.GET.get('plan')
            or request.GET.get('plan_id')
        )
        selected_package = None
        if selected_pkg_id:
            try:
                selected_package = CoinPackage.objects.filter(pk=int(selected_pkg_id), is_active=True).first()
            except (ValueError, TypeError):
                pass
        if not selected_package and packages:
            selected_package = next((p for p in packages if p.is_popular), packages[0])

        site_config = SiteConfiguration.get_solo()
        return render(request, 'dating/coin_checkout.html', {
            'profile': profile,
            'packages': packages,
            'selected_package': selected_package,
            'coin_balance': profile.coin_balance,
            'earned_diamonds': profile.earned_diamonds,
            'diamond_to_coin_percentage': getattr(site_config, 'diamond_to_coin_percentage', 70),
            'site_config': site_config,
            'call_rate_per_minute': site_config.call_rate_per_minute,
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
            'csrf_token': get_token(request),
        })

    plan_id = request.GET.get('plan_id')
    if not plan_id and request.GET.get('plan'):
        plan_id = request.GET.get('plan')
    plan = get_object_or_404(SubscriptionPlan, pk=plan_id, is_active=True)
    if plan.category != product_type or plan.category == 'SEX_CALL':
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

    if product_type == 'COINS':
        package_id = data.get('package_id')
        if (
            not package_id
            or provider not in ('paystack', 'flutterwave')
            or not reference
            or len(reference) > 100
            or not request.user.email
        ):
            return JsonResponse({'status': 'error', 'message': 'invalid_payment'}, status=400)
        if provider == 'flutterwave' and not transaction_id.isdigit():
            return JsonResponse({'status': 'error', 'message': 'invalid_payment'}, status=400)

        package = CoinPackage.objects.filter(pk=package_id, is_active=True).first()
        if not package:
            return JsonResponse({'status': 'error', 'message': 'invalid_package'}, status=404)

        existing_payment = PaymentTransaction.objects.filter(reference=reference).first()
        if existing_payment:
            if existing_payment.user_id != request.user.id or existing_payment.provider != provider:
                return JsonResponse({'status': 'error', 'message': 'payment_reference_used'}, status=409)
            return JsonResponse({'status': 'success', 'already_applied': True, 'coin_balance': profile.coin_balance})

        try:
            verification = _provider_payment(provider, reference, transaction_id)
        except (ValueError, TypeError):
            verification = None
        if verification is None:
            return JsonResponse({'status': 'error', 'message': 'payment_verification_unavailable'}, status=503)

        verified, amount_kobo, currency, customer_email = verification
        expected_amount = int(package.price * 100)
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
                        'product': 'COINS',
                        'plan_type': package.name,
                        'coin_package': package,
                        'amount_kobo': expected_amount,
                    },
                )
                if not created:
                    return JsonResponse({'status': 'success', 'already_applied': True, 'coin_balance': profile.coin_balance})

                profile = Profile.objects.select_for_update().get(pk=profile.pk)
                profile.coin_balance += package.total_coins
                profile.save(update_fields=['coin_balance'])

                CoinTransaction.objects.create(
                    user=request.user,
                    amount=package.total_coins,
                    transaction_type='PURCHASE',
                    payment=payment,
                    description=f'Purchased {package.name} (+{package.total_coins} coins)',
                )
        except IntegrityError:
            return JsonResponse({'status': 'error', 'message': 'payment_reference_used'}, status=409)

        return JsonResponse({
            'status': 'success',
            'coin_balance': profile.coin_balance,
            'added_coins': package.total_coins,
        })

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
    receiver_profile = Profile.objects.filter(user_id=call.receiver_id).first()
    caller_photo = (
        caller_profile.photos.filter(is_main=True).first() or caller_profile.photos.first()
        if caller_profile else None
    )
    photo_url = ''
    if caller_photo and caller_photo.image and caller_photo.image.storage.exists(caller_photo.image.name):
        photo_url = caller_photo.image.url

    caller_coins = caller_profile.coin_balance if caller_profile else 0
    is_unlimited = bool(caller_profile and (caller_profile.user.is_superuser or caller_profile.is_sex_call_premium))
    config = SiteConfiguration.get_solo()
    rate = getattr(call, 'rate_per_minute', None) or getattr(config, 'call_rate_per_minute', 20) or 20
    max_seconds = 86400 if is_unlimited else ((caller_coins // rate) * 60)

    elapsed_seconds = 0
    if call.started_at and call.status == 'connected':
        elapsed_seconds = max(0, int((timezone.now() - call.started_at).total_seconds()))

    remaining_seconds = 86400 if is_unlimited else max(0, max_seconds - elapsed_seconds)
    grace_period = getattr(config, 'grace_period_seconds', 20)

    return {
        'room_id': str(call.room_id),
        'status': call.status,
        'caller_id': call.caller_id,
        'caller_name': call.caller.first_name or call.caller.username,
        'caller_photo': photo_url,
        'receiver_id': call.receiver_id,
        'receiver_name': call.receiver.first_name or call.receiver.username,
        'rate_per_minute': rate,
        'caller_coins': caller_coins,
        'receiver_diamonds': receiver_profile.earned_diamonds if receiver_profile else 0,
        'is_unlimited': is_unlimited,
        'max_seconds': max_seconds,
        'elapsed_seconds': elapsed_seconds,
        'remaining_seconds': remaining_seconds,
        'grace_period_seconds': grace_period,
        'coins_spent': getattr(call, 'coins_spent', 0),
        'duration_seconds': getattr(call, 'duration_seconds', 0),
        'grace_period_applied': bool(getattr(call, 'duration_seconds', 0) < grace_period and call.status in ('ended', 'declined')),
    }


def _can_use_sex_call(profile_or_user):
    if not profile_or_user:
        return False
    if isinstance(profile_or_user, User):
        profile = getattr(profile_or_user, 'profile', None) or Profile.objects.filter(user=profile_or_user).first()
        user = profile_or_user
    else:
        profile = profile_or_user
        user = getattr(profile, 'user', None)

    if user and user.is_superuser:
        return True
    if not profile:
        return False
    config = SiteConfiguration.get_solo()
    min_coins = getattr(config, 'call_rate_per_minute', 20) or 20
    return bool(
        profile.is_sex_call_premium
        or profile.coin_balance >= min_coins
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
    config = SiteConfiguration.get_solo()
    min_coins = getattr(config, 'call_rate_per_minute', 20) or 20
    if not caller_profile or (not request.user.is_superuser and caller_profile.coin_balance < min_coins):
        return JsonResponse({
            'status': 'error',
            'message': 'insufficient_coins',
            'detail': f'You need at least {min_coins} coins to start a video call.',
            'coin_balance': caller_profile.coin_balance if caller_profile else 0,
            'required_coins': min_coins,
        }, status=402)
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
        rate_per_minute=min_coins,
    )
    return JsonResponse({'status': 'success', 'call': _call_payload(call)}, status=201)
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
            caller_profile = Profile.objects.filter(user=call.caller).first()
            if not _can_use_sex_call(caller_profile):
                call.status = 'declined'
                call.ended_at = timezone.now()
                call.save(update_fields=['status', 'ended_at'])
                return JsonResponse({'status': 'error', 'message': 'caller_insufficient_coins'}, status=409)
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
            if call.started_at:
                duration = int((call.ended_at - call.started_at).total_seconds())
                call.duration_seconds = max(0, duration)
                caller_profile = Profile.objects.select_for_update().filter(user_id=call.caller_id).first()
                receiver_profile = Profile.objects.select_for_update().filter(user_id=call.receiver_id).first()
                is_unlimited = bool(caller_profile and (caller_profile.user.is_superuser or caller_profile.is_sex_call_premium))

                config = SiteConfiguration.get_solo()
                grace_period = getattr(config, 'grace_period_seconds', 20)
                commission = Decimal(getattr(config, 'host_commission_percentage', 70)) / Decimal(100)

                if call.duration_seconds < grace_period:
                    call.coins_spent = 0
                    if caller_profile:
                        CoinTransaction.objects.create(
                            user=call.caller,
                            amount=0,
                            transaction_type='CALL_REFUND',
                            call=call,
                            description=f'Grace period protected: {call.duration_seconds}s < {grace_period}s. 0 coins charged.',
                        )
                elif not is_unlimited and caller_profile:
                    billed_minutes = max(1, math.ceil(call.duration_seconds / 60))
                    rate = getattr(call, 'rate_per_minute', config.call_rate_per_minute) or config.call_rate_per_minute
                    coins_due = billed_minutes * rate
                    coins_deducted = min(caller_profile.coin_balance, coins_due)

                    if coins_deducted > 0:
                        caller_profile.coin_balance = max(0, caller_profile.coin_balance - coins_deducted)
                        caller_profile.save(update_fields=['coin_balance'])
                        call.coins_spent = coins_deducted

                        diamonds_earned = int(Decimal(coins_deducted) * commission)
                        if receiver_profile and diamonds_earned > 0:
                            receiver_profile.earned_diamonds += diamonds_earned
                            receiver_profile.save(update_fields=['earned_diamonds'])
                            CoinTransaction.objects.create(
                                user=call.receiver,
                                amount=diamonds_earned,
                                transaction_type='CALL_EARNING',
                                call=call,
                                description=f'Earned {diamonds_earned} diamonds from {call.duration_seconds}s video call with {call.caller.username}',
                            )

                        CoinTransaction.objects.create(
                            user=call.caller,
                            amount=-coins_deducted,
                            transaction_type='CALL_DEDUCTION',
                            call=call,
                            description=f'Deducted {coins_deducted} coins for {call.duration_seconds}s video call ({billed_minutes}m @ {rate}/min)',
                        )
            call.save(update_fields=['status', 'ended_at', 'duration_seconds', 'coins_spent'])
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
    if signal_type not in ('offer', 'answer', 'candidate', 'gift') or not isinstance(payload, dict):
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


@login_required
@require_POST
def call_heartbeat_api(request, room_id):
    call = get_object_or_404(
        CallSession.objects.select_related('caller', 'receiver'),
        room_id=room_id,
    )
    _call_participants(call, request.user)
    if call.status != 'connected' or not call.started_at:
        return JsonResponse({'status': 'ok', 'call': _call_payload(call)})

    elapsed = max(0, int((timezone.now() - call.started_at).total_seconds()))
    caller_profile = Profile.objects.filter(user_id=call.caller_id).first()
    is_unlimited = bool(caller_profile and (caller_profile.user.is_superuser or caller_profile.is_sex_call_premium))
    site_config = SiteConfiguration.get_solo()
    rate = getattr(call, 'rate_per_minute', site_config.call_rate_per_minute) or site_config.call_rate_per_minute
    max_sec = 86400 if is_unlimited else ((caller_profile.coin_balance // rate) * 60 if caller_profile else 0)

    # If caller exceeded their available coins (after the grace period)
    if not is_unlimited and elapsed >= max_sec and elapsed >= site_config.grace_period_seconds:
        with transaction.atomic():
            call = CallSession.objects.select_for_update().get(pk=call.pk)
            if call.status == 'connected':
                call.status = 'ended'
                call.ended_at = timezone.now()
                call.duration_seconds = max_sec
                if caller_profile:
                    caller_p = Profile.objects.select_for_update().get(pk=caller_profile.pk)
                    receiver_p = Profile.objects.select_for_update().get(user_id=call.receiver_id)
                    coins_to_take = caller_p.coin_balance
                    caller_p.coin_balance = 0
                    caller_p.save(update_fields=['coin_balance'])
                    call.coins_spent = coins_to_take

                    commission = Decimal(str(site_config.host_commission_percentage)) / Decimal('100')
                    diamonds = int(Decimal(coins_to_take) * commission)
                    if diamonds > 0:
                        receiver_p.earned_diamonds += diamonds
                        receiver_p.save(update_fields=['earned_diamonds'])
                        CoinTransaction.objects.create(
                            user=call.receiver,
                            amount=diamonds,
                            transaction_type='CALL_EARNING',
                            call=call,
                            description=f'Earned {diamonds} diamonds from video call with {call.caller.username}',
                        )

                    CoinTransaction.objects.create(
                        user=call.caller,
                        amount=-coins_to_take,
                        transaction_type='CALL_DEDUCTION',
                        call=call,
                        description=f'Coins depleted: {coins_to_take} coins deducted for {call.duration_seconds}s video call.',
                    )
                call.save(update_fields=['status', 'ended_at', 'duration_seconds', 'coins_spent'])
        return JsonResponse({
            'status': 'depleted',
            'reason': 'coins_exhausted',
            'message': 'coins_depleted',
            'call': _call_payload(call),
        })

    return JsonResponse({'status': 'ok', 'call': _call_payload(call)})


@login_required
@require_POST
def call_gift_api(request, room_id):
    call = get_object_or_404(
        CallSession.objects.select_related('caller', 'receiver'),
        room_id=room_id,
    )
    _call_participants(call, request.user)
    if call.status != 'connected':
        return JsonResponse({'status': 'error', 'message': 'call_not_connected'}, status=400)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)

    gift_type = str(data.get('gift_type', '')).lower()
    gift_item = GiftItem.objects.filter(slug=gift_type, is_active=True).first()
    if gift_item:
        cost = gift_item.coin_cost
        gift_name = gift_item.name
        gift_icon = gift_item.icon
    elif gift_type in CallGift.GIFT_PRICES:
        cost = CallGift.GIFT_PRICES[gift_type]
        gift_name = gift_type.title()
        gift_icon = '🎁'
    else:
        return JsonResponse({'status': 'error', 'message': 'invalid_gift_type'}, status=400)

    recipient_user = call.receiver if request.user == call.caller else call.caller
    site_config = SiteConfiguration.get_solo()
    commission = Decimal(str(site_config.host_commission_percentage)) / Decimal('100')
    diamonds = int(Decimal(cost) * commission)

    with transaction.atomic():
        sender_profile = Profile.objects.select_for_update().filter(user=request.user).first()
        if not sender_profile or sender_profile.coin_balance < cost:
            return JsonResponse({
                'status': 'error',
                'message': 'insufficient_coins',
                'required_coins': cost,
                'coin_balance': sender_profile.coin_balance if sender_profile else 0,
            }, status=400)

        sender_profile.coin_balance -= cost
        sender_profile.save(update_fields=['coin_balance'])

        recipient_profile = Profile.objects.select_for_update().filter(user=recipient_user).first()
        if recipient_profile:
            recipient_profile.earned_diamonds += diamonds
            recipient_profile.save(update_fields=['earned_diamonds'])

        gift = CallGift.objects.create(
            call=call,
            sender=request.user,
            receiver=recipient_user,
            gift_type=gift_type,
            coins_cost=cost,
        )

        CoinTransaction.objects.create(
            user=request.user,
            amount=-cost,
            transaction_type='GIFT_SENT',
            call=call,
            description=f'Sent {gift_icon} {gift_name} to {recipient_user.username}',
        )
        if recipient_profile:
            CoinTransaction.objects.create(
                user=recipient_user,
                amount=diamonds,
                transaction_type='GIFT_RECEIVED',
                call=call,
                description=f'Received {gift_icon} {gift_name} from {request.user.username} (+{diamonds} 💎)',
            )

        CallSignal.objects.create(
            call=call,
            sender=request.user,
            signal_type='gift',
            payload={
                'gift_type': gift_type,
                'name': gift_name,
                'icon': gift_icon,
                'coins': cost,
                'diamonds': diamonds,
                'sender_name': request.user.first_name or request.user.username,
            },
        )

    return JsonResponse({
        'status': 'success',
        'remaining_coins': sender_profile.coin_balance,
        'caller_coins': sender_profile.coin_balance,
        'diamond_award': diamonds,
        'gift': {
            'gift_type': gift_type,
            'name': gift_name,
            'icon': gift_icon,
            'coins': cost,
            'diamonds': diamonds,
        },
    })


@login_required
@require_GET
def wallet_balance_api(request):
    profile = Profile.objects.filter(user=request.user).first()
    if not profile:
        return JsonResponse({'status': 'error', 'message': 'profile_required'}, status=400)

    site_config = SiteConfiguration.get_solo()
    # Starter welcome bonus: if 0 coins and no transaction history, give configured free coins!
    if profile.coin_balance == 0 and not CoinTransaction.objects.filter(user=request.user).exists():
        with transaction.atomic():
            p = Profile.objects.select_for_update().get(pk=profile.pk)
            if p.coin_balance == 0 and not CoinTransaction.objects.filter(user=request.user).exists():
                bonus = site_config.welcome_bonus_coins
                p.coin_balance = bonus
                p.save(update_fields=['coin_balance'])
                CoinTransaction.objects.create(
                    user=request.user,
                    amount=bonus,
                    transaction_type='WELCOME_BONUS',
                    description=f'Welcome gift: {bonus} free coins to experience Sex Call!',
                )
                profile.refresh_from_db()

    return JsonResponse({
        'status': 'success',
        'coin_balance': profile.coin_balance,
        'earned_diamonds': profile.earned_diamonds,
        'diamond_to_coin_percentage': getattr(site_config, 'diamond_to_coin_percentage', 70),
        'call_rate_per_minute': site_config.call_rate_per_minute,
        'is_sex_call_premium': profile.is_sex_call_premium,
        'relationship_mode': profile.relationship_mode,
    })


@require_GET
def gifts_api(request):
    gifts = list(GiftItem.objects.filter(is_active=True).order_by('order', 'coin_cost'))
    if not gifts:
        fallback = [
            {'id': 1, 'name': 'Rose', 'slug': 'rose', 'icon': '🌹', 'coin_cost': 5},
            {'id': 2, 'name': 'Kiss', 'slug': 'kiss', 'icon': '💋', 'coin_cost': 15},
            {'id': 3, 'name': 'Champagne', 'slug': 'champagne', 'icon': '🥂', 'coin_cost': 50},
            {'id': 4, 'name': 'Crown', 'slug': 'crown', 'icon': '👑', 'coin_cost': 150},
            {'id': 5, 'name': 'Supercar', 'slug': 'car', 'icon': '🏎️', 'coin_cost': 300},
            {'id': 6, 'name': 'Diamond Ring', 'slug': 'ring', 'icon': '💍', 'coin_cost': 500},
            {'id': 7, 'name': 'Luxury Yacht', 'slug': 'yacht', 'icon': '🛥️', 'coin_cost': 1000},
        ]
        return JsonResponse({'status': 'success', 'gifts': fallback})
    return JsonResponse({
        'status': 'success',
        'gifts': [{
            'id': g.id,
            'name': g.name,
            'slug': g.slug,
            'icon': g.icon,
            'coin_cost': g.coin_cost,
        } for g in gifts],
    })


@require_GET
def site_config_api(request):
    cfg = SiteConfiguration.get_solo()
    return JsonResponse({
        'status': 'success',
        'call_rate_per_minute': cfg.call_rate_per_minute,
        'grace_period_seconds': cfg.grace_period_seconds,
        'host_commission_percentage': cfg.host_commission_percentage,
        'diamond_to_coin_percentage': getattr(cfg, 'diamond_to_coin_percentage', 70),
        'welcome_bonus_coins': cfg.welcome_bonus_coins,
        'announcement_banner': cfg.announcement_banner if cfg.is_announcement_active else '',
        'is_announcement_active': cfg.is_announcement_active,
    })


@require_GET
def online_hosts_api(request):
    """Returns top active and host-ready users for HiiclubChat discovery carousel."""
    site_config = SiteConfiguration.get_solo()
    include_test_profiles = bool(
        request.session.get('staff_test_profile_preview')
        or request.session.get('preview_test_profiles')
        or request.GET.get('test_profiles') in ('on', '1', 'true', 'True')
        or request.GET.get('preview_test_profiles') in ('on', '1', 'true', 'True')
        or (
            (request.user.is_authenticated and (request.user.is_staff or request.user.is_superuser))
            and (
                request.session.get('staff_test_profile_preview')
                or request.session.get('preview_test_profiles')
            )
        )
    )

    if include_test_profiles:
        qs = Profile.objects.filter(Q(is_test_profile=True) | Q(user__username__startswith='test_user_')).select_related('user').prefetch_related('photos')
        if request.user.is_authenticated:
            qs = qs.exclude(user=request.user)
        sc_qs = qs.filter(Q(relationship_mode__iexact='sex_call') | Q(relationship_mode='SEX_CALL'))
        hosts = (sc_qs if sc_qs.exists() else qs)[:24]
    else:
        qs = Profile.objects.filter(age__gte=18, is_test_profile=False).select_related('user').prefetch_related('photos')
        if request.user.is_authenticated:
            qs = qs.exclude(user=request.user)
        hosts = qs.order_by('-is_host_ready', '-last_active', '-response_rate')[:24]

    results = []
    for p in hosts:
        is_active = True if p.is_test_profile else (p.is_host_ready or (p.last_active and (timezone.now() - p.last_active).total_seconds() < 1800))
        photo = p.photos.filter(is_main=True).first() or p.photos.first()
        avatar = photo.image.url if (photo and photo.image and photo.image.storage.exists(photo.image.name)) else f"https://api.dicebear.com/7.x/avataaars/svg?seed={p.user.username}"
        results.append({
            'user_id': p.user_id,
            'username': p.user.username,
            'name': p.user.first_name or p.user.username,
            'age': p.age,
            'gender': p.get_gender_display() if hasattr(p, 'get_gender_display') else p.gender,
            'city': p.location or 'Nearby',
            'avatar': avatar,
            'response_rate': p.response_rate,
            'total_calls': p.total_calls_completed,
            'is_host_ready': p.is_host_ready,
            'is_online': is_active,
            'call_rate': site_config.call_rate_per_minute,
            'bio': (p.bio[:50] + '...') if len(p.bio or '') > 50 else (p.bio or 'Available for video call ✨'),
        })

    return JsonResponse({
        'status': 'success',
        'hosts': results,
        'call_rate': site_config.call_rate_per_minute,
    })


@login_required
@require_POST
def quick_call_match_api(request):
    """Speed Match: Instantly pairs the caller with an available host or active user."""
    caller_profile = Profile.objects.filter(user=request.user).first()
    if not caller_profile:
        return JsonResponse({'status': 'error', 'message': 'profile_required'}, status=400)

    site_config = SiteConfiguration.get_solo()
    min_coins = getattr(site_config, 'call_rate_per_minute', 20) or 20
    if not _can_use_sex_call(caller_profile):
        return JsonResponse({
            'status': 'error',
            'message': 'sex_call_pass_required',
            'coin_balance': caller_profile.coin_balance if caller_profile else 0,
            'required_coins': min_coins,
            'subscribe_url': reverse('premium_landing'),
        }, status=403)

    candidates = Profile.objects.filter(
        age__gte=18
    ).exclude(
        user=request.user
    ).select_related('user').prefetch_related('photos')

    # Exclude busy users
    busy_users = CallSession.objects.filter(
        status__in=['ringing', 'connected']
    ).values_list('caller_id', 'receiver_id')
    busy_ids = set()
    for c_id, r_id in busy_users:
        busy_ids.add(c_id)
        busy_ids.add(r_id)
    if busy_ids:
        candidates = candidates.exclude(user_id__in=busy_ids)

    # Pick candidate (prefer host_ready, or random candidate)
    chosen_profile = (
        candidates.filter(is_host_ready=True).order_by('?').first()
        or candidates.order_by('-last_active', '?').first()
    )

    if not chosen_profile:
        return JsonResponse({
            'status': 'error',
            'message': 'no_hosts_available',
            'detail': 'No hosts are online right now. Please try again in a moment!',
        }, status=404)

    call = CallSession.objects.create(
        caller=request.user,
        receiver=chosen_profile.user,
        rate_per_minute=site_config.call_rate_per_minute,
        status='ringing',
    )

    CallSignal.objects.create(
        call=call,
        sender=request.user,
        signal_type='call_invitation',
        payload={
            'caller_name': request.user.first_name or request.user.username,
            'caller_avatar': _profile_payload(caller_profile).get('avatar', ''),
            'rate_per_minute': site_config.call_rate_per_minute,
            'is_quick_match': True,
        },
    )

    return JsonResponse({
        'status': 'success',
        'room_id': str(call.room_id),
        'call': _call_payload(call),
        'host': _profile_payload(chosen_profile),
    })


@login_required
@require_POST
def convert_diamonds_api(request):
    """
    Closed-Loop Economy:
    Users convert their accumulated Diamonds back into spendable Coins.
    Zero real-money cashout / fiat withdrawals.
    """
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'status': 'error', 'message': 'invalid_request'}, status=400)

    try:
        diamonds = int(data.get('diamonds', 0))
        if diamonds <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'invalid_diamond_amount', 'detail': 'Enter a valid positive number of diamonds.'}, status=400)

    profile = Profile.objects.filter(user=request.user).first()
    if not profile:
        return JsonResponse({'status': 'error', 'message': 'profile_required'}, status=400)

    if profile.earned_diamonds < diamonds:
        return JsonResponse({
            'status': 'error',
            'error': 'insufficient_diamonds',
            'message': 'insufficient_diamonds',
            'detail': f'You only have {profile.earned_diamonds} diamonds available.',
            'earned_diamonds': profile.earned_diamonds,
        }, status=400)

    site_config = SiteConfiguration.get_solo()
    percentage = getattr(site_config, 'diamond_to_coin_percentage', 70) or 70
    coins_awarded = int(Decimal(diamonds) * Decimal(percentage) / Decimal(100))

    if coins_awarded < 1:
        return JsonResponse({
            'status': 'error',
            'message': 'amount_too_low',
            'detail': 'Enter more diamonds to receive at least 1 coin.',
        }, status=400)

    with transaction.atomic():
        p = Profile.objects.select_for_update().get(pk=profile.pk)
        if p.earned_diamonds < diamonds:
            return JsonResponse({'status': 'error', 'message': 'insufficient_diamonds'}, status=400)

        wallet, _ = CoinWallet.objects.select_for_update().get_or_create(
            user=request.user,
            defaults={'coin_balance': p.coin_balance},
        )

        p.earned_diamonds -= diamonds
        p.coin_balance += coins_awarded
        wallet.coin_balance = p.coin_balance

        p.save(update_fields=['earned_diamonds', 'coin_balance'])
        wallet.save(update_fields=['coin_balance'])

        CoinTransaction.objects.create(
            user=request.user,
            amount=coins_awarded,
            transaction_type='DIAMOND_CONVERSION',
            description=f'Exchanged {diamonds} 💎 for {coins_awarded} 🪙 (Rate: {percentage}%)',
        )

    return JsonResponse({
        'status': 'success',
        'success': True,
        'message': f'Successfully exchanged {diamonds} Diamonds for {coins_awarded} Coins!',
        'diamonds_converted': diamonds,
        'diamonds_deducted': diamonds,
        'coins_added': coins_awarded,
        'coins_credited': coins_awarded,
        'coin_balance': p.coin_balance,
        'new_coin_balance': p.coin_balance,
        'remaining_diamonds': p.earned_diamonds,
        'earned_diamonds': p.earned_diamonds,
    })


@login_required
@require_POST
def withdrawal_request_api(request):
    """Closed-loop economy notice: real-money cashouts are permanently removed."""
    return JsonResponse({
        'status': 'error',
        'notice': 'closed_loop_economy',
        'message': 'withdrawals_disabled',
        'detail': 'Cash withdrawals are disabled. You can convert your Diamonds to Coins in the app!',
    }, status=410)


@login_required
@require_GET
def wallet_history_api(request):
    """Returns recent transactions and diamond activity for the logged-in user."""
    txs = CoinTransaction.objects.filter(user=request.user).order_by('-created_at')[:30]
    return JsonResponse({
        'status': 'success',
        'transactions': [{
            'id': t.id,
            'amount': t.amount,
            'type': t.transaction_type,
            'type_display': t.get_transaction_type_display(),
            'description': t.description,
            'created_at': t.created_at.strftime('%b %d, %Y %H:%M'),
        } for t in txs],
        'withdrawals': [],
    })


@require_GET
def coin_packages_api(request):
    packages = CoinPackage.objects.filter(is_active=True).order_by('order', 'price')
    return JsonResponse({
        'status': 'success',
        'packages': [{
            'id': p.pk,
            'name': p.name,
            'coins': p.coins,
            'bonus_coins': p.bonus_coins,
            'total_coins': p.total_coins,
            'price': str(p.price),
            'is_popular': p.is_popular,
            'badge': p.badge,
        } for p in packages],
        'paystack_public_key': getattr(settings, 'PAYSTACK_PUBLIC_KEY', ''),
        'flutterwave_public_key': getattr(settings, 'FLUTTERWAVE_PUBLIC_KEY', ''),
        'paystack_enabled': bool(
            getattr(settings, 'PAYSTACK_PUBLIC_KEY', '')
            and getattr(settings, 'PAYSTACK_SECRET_KEY', '')
        ),
        'flutterwave_enabled': bool(
            getattr(settings, 'FLUTTERWAVE_PUBLIC_KEY', '')
            and getattr(settings, 'FLUTTERWAVE_SECRET_KEY', '')
        ),
        'user_email': request.user.email if request.user.is_authenticated else '',
    })


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


DEFAULT_CHAT_GIFTS = {
    'rose': {'name': 'Red Rose', 'icon': '🌹', 'coins': 10},
    'heart': {'name': 'Love Heart', 'icon': '💖', 'coins': 20},
    'beer': {'name': 'Cold Beer', 'icon': '🍻', 'coins': 25},
    'cocktail': {'name': 'Cocktail', 'icon': '🍸', 'coins': 25},
    'kiss': {'name': 'Sweet Kiss', 'icon': '💋', 'coins': 35},
    'champagne': {'name': 'Champagne', 'icon': '🍾', 'coins': 100},
    'crown': {'name': 'Royal Crown', 'icon': '👑', 'coins': 200},
    'car': {'name': 'Sports Car', 'icon': '🏎️', 'coins': 500},
    'diamond': {'name': 'Diamond', 'icon': '💎', 'coins': 500},
    'ring': {'name': 'Diamond Ring', 'icon': '💍', 'coins': 500},
    'yacht': {'name': 'Luxury Yacht', 'icon': '🛥️', 'coins': 1000},
}

STICKER_PACKS = [
    {
        'id': 'flirty',
        'name': 'Flirty',
        'icon': '💋',
        'stickers': [
            {'id': 'kiss', 'name': 'Kiss', 'icon': '💋', 'caption': 'Smooch!'},
            {'id': 'blow_kiss', 'name': 'Blow Kiss', 'icon': '😘', 'caption': 'Muah!'},
            {'id': 'heart_eyes', 'name': 'Heart Eyes', 'icon': '😍', 'caption': 'In love'},
            {'id': 'love_letter', 'name': 'Love Note', 'icon': '💌', 'caption': 'For you'},
            {'id': 'hug', 'name': 'Warm Hug', 'icon': '🤗', 'caption': 'Hug me'},
            {'id': 'sparkle_heart', 'name': 'Sparkle Heart', 'icon': '💖', 'caption': 'Sweet'},
            {'id': 'wink', 'name': 'Flirty Wink', 'icon': '😉', 'caption': 'Hey you'},
            {'id': 'blush', 'name': 'Blushing', 'icon': '😊', 'caption': 'Shy'},
        ]
    },
    {
        'id': 'spicy',
        'name': 'Spicy',
        'icon': '🔥',
        'stickers': [
            {'id': 'fire', 'name': 'Pure Fire', 'icon': '🔥', 'caption': 'So hot!'},
            {'id': 'devil', 'name': 'Naughty', 'icon': '😈', 'caption': 'Be bad'},
            {'id': 'peach', 'name': 'Peach', 'icon': '🍑', 'caption': 'Juicy'},
            {'id': 'hot_lips', 'name': 'Biting Lips', 'icon': '🫦', 'caption': 'Bite me'},
            {'id': 'smirk', 'name': 'Wild Smirk', 'icon': '😏', 'caption': 'You know it'},
            {'id': 'cherry', 'name': 'Cherries', 'icon': '🍒', 'caption': 'Taste good'},
            {'id': 'sweat', 'name': 'Getting Hot', 'icon': '🥵', 'caption': 'Phew!'},
            {'id': 'drool', 'name': 'Drooling', 'icon': '🤤', 'caption': 'Delicious'},
        ]
    },
    {
        'id': 'party',
        'name': 'Party',
        'icon': '🍾',
        'stickers': [
            {'id': 'dance', 'name': 'Dancing', 'icon': '💃', 'caption': 'Let’s dance'},
            {'id': 'cocktail', 'name': 'Drinks', 'icon': '🍸', 'caption': 'Cheers!'},
            {'id': 'champagne', 'name': 'Champagne', 'icon': '🍾', 'caption': 'Celebrate'},
            {'id': 'sunglasses', 'name': 'Cool Vibe', 'icon': '😎', 'caption': 'Too cool'},
            {'id': 'party', 'name': 'Confetti', 'icon': '🎉', 'caption': 'Party time'},
            {'id': 'music', 'name': 'Vibing', 'icon': '🎶', 'caption': 'Our tune'},
            {'id': 'rose', 'name': 'Red Rose', 'icon': '🌹', 'caption': 'Romantic'},
            {'id': 'clinking', 'name': 'Toast', 'icon': '🥂', 'caption': 'To us'},
        ]
    },
    {
        'id': 'vip',
        'name': 'VIP',
        'icon': '👑',
        'stickers': [
            {'id': 'crown', 'name': 'Crown', 'icon': '👑', 'caption': 'Royalty'},
            {'id': 'diamond', 'name': 'Diamond', 'icon': '💎', 'caption': 'Priceless'},
            {'id': 'ring', 'name': 'Diamond Ring', 'icon': '💍', 'caption': 'Marry me?'},
            {'id': 'car', 'name': 'Supercar', 'icon': '🏎️', 'caption': 'Fast lane'},
            {'id': 'yacht', 'name': 'Mega Yacht', 'icon': '🛥️', 'caption': 'Private cruise'},
            {'id': 'money', 'name': 'Money Bag', 'icon': '💰', 'caption': 'Rich taste'},
            {'id': 'sparkles', 'name': 'Golden Aura', 'icon': '✨', 'caption': 'Stunning'},
            {'id': 'trophy', 'name': 'Winner', 'icon': '🏆', 'caption': 'You won me'},
        ]
    }
]


def _build_chat_context(request, conversation, match, other_user, other_profile, current_profile):
    site_config = SiteConfiguration.get_solo()
    gifts = list(GiftItem.objects.filter(is_active=True).order_by('order', 'coin_cost'))
    gift_list = [{
        'slug': g.slug,
        'name': g.name,
        'icon': g.icon,
        'coins': g.coin_cost,
    } for g in gifts] if gifts else [
        {'slug': k, 'name': v['name'], 'icon': v['icon'], 'coins': v['coins']}
        for k, v in DEFAULT_CHAT_GIFTS.items()
    ]
    other_avatar = ''
    if other_profile:
        photo = other_profile.photos.filter(is_main=True).first() or other_profile.photos.first()
        if photo and photo.image and photo.image.storage.exists(photo.image.name):
            other_avatar = photo.image.url
        else:
            other_avatar = f"https://api.dicebear.com/7.x/avataaars/svg?seed={other_user.username}"

    is_online = bool(
        other_profile and (
            other_profile.is_host_ready
            or (other_profile.last_active and (timezone.now() - other_profile.last_active).total_seconds() < 1800)
        )
    )

    return {
        'conversation': conversation,
        'match': match,
        'other_user': other_user,
        'other_profile': other_profile,
        'other_avatar': other_avatar,
        'is_other_online': is_online,
        'current_profile': current_profile,
        'site_config': site_config,
        'call_rate_per_minute': site_config.call_rate_per_minute,
        'user_coin_balance': current_profile.coin_balance if current_profile else 100,
        'gifts': gift_list,
        'sticker_packs': STICKER_PACKS,
        'messages_url': reverse('conversation_messages_api', args=[conversation.pk]),
        'send_url': reverse('conversation_send_api', args=[conversation.pk]),
        'send_gift_url': reverse('chat_send_gift_api'),
        'stickers_api_url': reverse('chat_stickers_api'),
        'coin_packages_api_url': reverse('coin_packages_api'),
        'wallet_balance_api_url': reverse('wallet_balance_api'),
        'messages_allowed': bool(
            current_profile
            and other_profile
            and current_profile.allow_messages
            and other_profile.allow_messages
        ),
        'max_message_length': MAX_MESSAGE_LENGTH,
    }


@login_required
def chat_room(request, match_id):
    match = _get_active_match(match_id, request.user)
    other_user = match.user2 if match.user1_id == request.user.id else match.user1
    other_profile = Profile.objects.filter(user=other_user).first()
    if not other_profile:
        return redirect('match_list')
    current_profile = Profile.objects.filter(user=request.user).first()
    conversation = _get_or_create_conversation(match)
    context = _build_chat_context(request, conversation, match, other_user, other_profile, current_profile)
    return render(request, 'dating/chat_room.html', context)


@require_GET
def chat_stickers_api(request):
    return JsonResponse({
        'status': 'success',
        'packs': STICKER_PACKS,
    })


@login_required
@require_POST
def chat_send_gift_api(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'status': 'error', 'message': 'invalid_request'}, status=400)

    conversation_id = data.get('conversation_id')
    recipient_id = data.get('recipient_id')
    match_id = data.get('match_id')

    conversation = None
    other_user = None

    if conversation_id:
        conversation, _, other_user = _get_active_conversation(conversation_id, request.user)
    elif match_id:
        match = _get_active_match(match_id, request.user)
        conversation = _get_or_create_conversation(match)
        other_user = match.user2 if match.user1_id == request.user.id else match.user1
    elif recipient_id:
        other_user = get_object_or_404(User, pk=recipient_id)
        match = Match.objects.filter(
            (Q(user1=request.user, user2=other_user) | Q(user1=other_user, user2=request.user)),
            expires_at__gt=timezone.now(),
        ).first()
        if not match:
            return JsonResponse({'status': 'error', 'message': 'active_match_required'}, status=403)
        conversation = _get_or_create_conversation(match)
    else:
        return JsonResponse({'status': 'error', 'message': 'missing_conversation_or_recipient'}, status=400)

    gift_type = str(data.get('gift_type', '')).strip().lower()
    gift_item = GiftItem.objects.filter(slug=gift_type, is_active=True).first()
    if gift_item:
        gift_name = gift_item.name
        gift_icon = gift_item.icon
        gift_cost = gift_item.coin_cost
    elif gift_type in DEFAULT_CHAT_GIFTS:
        gift_meta = DEFAULT_CHAT_GIFTS[gift_type]
        gift_name = gift_meta['name']
        gift_icon = gift_meta['icon']
        gift_cost = gift_meta['coins']
    else:
        return JsonResponse({'status': 'error', 'message': 'invalid_gift_type'}, status=400)

    sender_profile = Profile.objects.filter(user=request.user).first()
    if not sender_profile:
        return JsonResponse({'status': 'error', 'message': 'profile_required'}, status=400)

    if sender_profile.coin_balance < gift_cost:
        return JsonResponse({
            'status': 'error',
            'message': 'insufficient_coins',
            'required_coins': gift_cost,
            'coin_balance': sender_profile.coin_balance,
            'detail': f"You need {gift_cost} coins to send {gift_icon} {gift_name}.",
        }, status=400)

    site_config = SiteConfiguration.get_solo()
    commission = Decimal(str(site_config.host_commission_percentage)) / Decimal('100')
    diamonds_earned = int(Decimal(gift_cost) * commission)

    with transaction.atomic():
        p_sender = Profile.objects.select_for_update().get(pk=sender_profile.pk)
        w_sender, _ = CoinWallet.objects.select_for_update().get_or_create(
            user=request.user,
            defaults={'coin_balance': p_sender.coin_balance},
        )
        if p_sender.coin_balance < gift_cost:
            return JsonResponse({
                'status': 'error',
                'message': 'insufficient_coins',
                'required_coins': gift_cost,
                'coin_balance': p_sender.coin_balance,
            }, status=400)

        p_sender.coin_balance -= gift_cost
        w_sender.coin_balance = p_sender.coin_balance
        p_sender.save(update_fields=['coin_balance'])
        w_sender.save(update_fields=['coin_balance'])

        p_recipient = Profile.objects.select_for_update().filter(user=other_user).first()
        w_recipient, _ = CoinWallet.objects.select_for_update().get_or_create(
            user=other_user,
            defaults={'coin_balance': p_recipient.coin_balance if p_recipient else 100},
        )
        if p_recipient and diamonds_earned > 0:
            p_recipient.earned_diamonds += diamonds_earned
            p_recipient.save(update_fields=['earned_diamonds'])

        CoinTransaction.objects.create(
            user=request.user,
            sender=request.user,
            recipient=other_user,
            amount=-gift_cost,
            gift_type=gift_type,
            transaction_type='GIFT_SENT',
            description=f"Sent {gift_icon} {gift_name} ({gift_cost} coins) in chat to {other_user.username}",
        )

        CoinTransaction.objects.create(
            user=other_user,
            sender=request.user,
            recipient=other_user,
            amount=diamonds_earned,
            gift_type=gift_type,
            transaction_type='GIFT_RECEIVED',
            description=f"Received {gift_icon} {gift_name} from {request.user.username} (+{diamonds_earned}💎)",
        )

        chat_msg = ChatMessage.objects.create(
            conversation=conversation,
            sender=request.user,
            message_type='gift',
            text=f"Sent a {gift_name} ({gift_cost} coins)",
            metadata={
                'gift_type': gift_type,
                'name': gift_name,
                'icon': gift_icon,
                'coins': gift_cost,
                'diamonds': diamonds_earned,
                'sender_name': request.user.first_name or request.user.username,
                'recipient_name': other_user.first_name or other_user.username,
            },
        )
        chat_msg.read_by.add(request.user)
        conversation.save(update_fields=['updated_at'])

    return JsonResponse({
        'status': 'success',
        'sender_balance': p_sender.coin_balance,
        'coin_balance': p_sender.coin_balance,
        'diamonds_awarded': diamonds_earned,
        'gift': {
            'gift_type': gift_type,
            'name': gift_name,
            'icon': gift_icon,
            'coins': gift_cost,
            'diamonds': diamonds_earned,
        },
        'message': serialize_chat_message(chat_msg, other_user.pk),
    }, status=201)


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
    message_type = str(data.get('message_type', 'text')).lower()
    if message_type not in ('text', 'sticker', 'gift'):
        message_type = 'text'
    metadata = data.get('metadata') if isinstance(data.get('metadata'), dict) else {}

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
            message_type=message_type,
            metadata=metadata,
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
    context = _build_chat_context(request, conversation, match, other_user, other_profile, current_profile)
    return render(request, 'dating/chat_room.html', context)

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


class SafePasswordResetView(auth_views.PasswordResetView):
    form_class = SafePasswordResetForm
    template_name = 'registration/password_reset_form.html'
    email_template_name = 'registration/password_reset_email.txt'
    html_email_template_name = 'registration/password_reset_email.html'
    subject_template_name = 'registration/password_reset_subject.txt'

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except Exception as exc:
            logger.exception("Password reset email delivery failed: %s", exc)
            form.add_error(
                None,
                "Unable to deliver the reset email due to a mail server connection issue. "
                "Please verify the email configuration or try again shortly."
            )
            return self.form_invalid(form)



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


# --- SEX CALL DISCOVERY & HOST GRID ---
@login_required
def sex_call_hub(request):
    """
    Sex Call Hub Page (/sex-call/):
    Dual Discovery Modes: 'Swipe View' and '2-Column Grid View'.
    Tabs: 'Hot' and 'Nearby' filter toggle.
    Top-bar: Live Coin Badge (topup trigger) & Diamond Badge (exchange trigger).
    Cosmic Random Match entry point.
    2-column card grid with portrait photos, online badges, and direct video call buttons.
    """
    toggle = request.GET.get('test_profiles') or request.GET.get('preview_test_profiles')
    if toggle in {'on', 'off'}:
        enabled = (toggle == 'on')
        request.session['staff_test_profile_preview'] = enabled
        request.session['preview_test_profiles'] = enabled

    profile = Profile.objects.filter(user=request.user).first()
    if not profile:
        return redirect('create_profile')

    profile.last_active = timezone.now()
    profile.save(update_fields=['last_active'])

    site_config = SiteConfiguration.get_solo()
    CoinWallet.objects.get_or_create(
        user=request.user,
        defaults={'coin_balance': profile.coin_balance},
    )

    is_preview = bool(
        request.session.get('staff_test_profile_preview')
        or request.session.get('preview_test_profiles')
        or request.GET.get('test_profiles') in ('on', '1', 'true', 'True')
        or request.GET.get('preview_test_profiles') in ('on', '1', 'true', 'True')
        or (
            (request.user.is_staff or request.user.is_superuser)
            and (
                request.session.get('staff_test_profile_preview')
                or request.session.get('preview_test_profiles')
            )
        )
    )

    initial_view = request.GET.get('view', 'grid').lower()
    if initial_view not in ('swipe', 'grid'):
        initial_view = 'grid'

    return render(request, 'dating/sex_call_hub.html', {
        'profile': profile,
        'user_coin_balance': profile.coin_balance,
        'user_earned_diamonds': profile.earned_diamonds,
        'site_config': site_config,
        'call_rate_per_minute': site_config.call_rate_per_minute,
        'diamond_to_coin_percentage': getattr(site_config, 'diamond_to_coin_percentage', 70),
        'staff_test_profile_preview': is_preview,
        'is_staff': request.user.is_staff or request.user.is_superuser,
        'initial_view': initial_view,
        'SWIPE_URL': reverse('swipe_action'),
        'PROFILES_URL': reverse('get_profiles_json'),
        'PREMIUM_URL': f"{reverse('premium_checkout')}?mode=COINS",
        'csrf_token': get_token(request),
    })


@login_required
@require_GET
def sex_call_hosts_api(request):
    """
    Returns active hosts for the Sex Call grid.
    Supports ?tab=hot and ?tab=nearby.
    Includes test profiles when staff preview is enabled.
    """
    current_profile = Profile.objects.filter(user=request.user).first()
    tab = request.GET.get('tab', 'hot').lower()

    include_test_profiles = bool(
        request.session.get('staff_test_profile_preview')
        or request.session.get('preview_test_profiles')
        or request.GET.get('test_profiles') in ('on', '1', 'true', 'True')
        or request.GET.get('preview_test_profiles') in ('on', '1', 'true', 'True')
        or (
            (request.user.is_staff or request.user.is_superuser)
            and (
                request.session.get('staff_test_profile_preview')
                or request.session.get('preview_test_profiles')
            )
        )
    )

    qs = Profile.objects.exclude(user=request.user)

    if include_test_profiles:
        test_qs = qs.filter(Q(is_test_profile=True) | Q(user__username__startswith='test_user_'))
        sc_test = test_qs.filter(Q(relationship_mode__iexact='sex_call') | Q(relationship_mode='SEX_CALL'))
        qs = sc_test if sc_test.exists() else test_qs
    else:
        qs = qs.filter(is_test_profile=False, show_in_discovery=True)
        sc_qs = qs.filter(Q(relationship_mode__iexact='sex_call') | Q(relationship_mode='SEX_CALL') | Q(is_host_ready=True))
        if sc_qs.exists():
            qs = sc_qs

    hosts_list = list(qs.select_related('user').prefetch_related('photos')[:50])

    if tab == 'nearby' and current_profile and current_profile.latitude and current_profile.longitude:
        try:
            origin = (float(current_profile.latitude), float(current_profile.longitude))
            hosts_list.sort(key=lambda h: _distance_km(*origin, float(h.latitude or 0), float(h.longitude or 0)) if (h.latitude and h.longitude) else 99999)
        except Exception:
            pass
    else:
        hosts_list.sort(key=lambda h: (not getattr(h, 'is_host_ready', True), -(h.last_active.timestamp() if h.last_active else 0)))

    site_config = SiteConfiguration.get_solo()
    data = []
    for h in hosts_list[:40]:
        payload = _profile_payload(h)
        data.append({
            'id': h.user_id,
            'username': h.user.username,
            'age': h.age or 22,
            'location': h.location or 'Online',
            'avatar_url': payload['image_url'],
            'is_online': True if h.is_test_profile else (h.is_host_ready or (h.last_active and (timezone.now() - h.last_active).total_seconds() < 1800)),
            'rate_per_minute': site_config.call_rate_per_minute,
            'is_verified': h.is_verified,
            'is_vip': h.is_vip,
            'is_test_profile': h.is_test_profile,
        })

    return JsonResponse({'status': 'success', 'success': True, 'tab': tab, 'hosts': data})


@login_required
def call_room_page(request, room_id):
    """
    Dedicated 1-on-1 In-Call Video Room (/call/<room_id>/):
    Full-bleed remote video, PiP local preview, floating live chat, in-call gifts.
    """
    call = get_object_or_404(CallSession, room_id=room_id)
    if request.user not in (call.caller, call.receiver):
        return HttpResponseForbidden("You are not a participant in this call.")

    other_user = call.receiver if request.user == call.caller else call.caller
    other_profile = Profile.objects.filter(user=other_user).first()
    current_profile = Profile.objects.filter(user=request.user).first()
    site_config = SiteConfiguration.get_solo()

    other_payload = _profile_payload(other_profile) if other_profile else {
        'image_url': f"https://api.dicebear.com/7.x/avataaars/svg?seed={other_user.username}",
        'username': other_user.username,
        'age': 22,
    }

    return render(request, 'dating/call_room.html', {
        'call': call,
        'room_id': str(call.room_id),
        'other_user': other_user,
        'other_profile': other_profile,
        'other_avatar': other_payload.get('image_url', ''),
        'other_age': getattr(other_profile, 'age', 22) if other_profile else 22,
        'current_profile': current_profile,
        'user_coin_balance': current_profile.coin_balance if current_profile else 0,
        'user_earned_diamonds': current_profile.earned_diamonds if current_profile else 0,
        'site_config': site_config,
        'rate_per_minute': getattr(call, 'rate_per_minute', site_config.call_rate_per_minute),
        'is_caller': request.user == call.caller,
    })