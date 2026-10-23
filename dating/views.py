import base64
import hashlib
import hmac
import json
import logging
import math
import random
import re
import smtplib
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from django.shortcuts import render, redirect, get_object_or_404
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login, logout, views as auth_views
from django.db import IntegrityError, transaction
from django.db.models import Count, F, IntegerField, OuterRef, Q, Subquery
from django.utils import timezone
from django.urls import reverse
from django.contrib.auth.models import User
from django.conf import settings
from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from .models import (
    ChatMessage,
    CoinPackage,
    CoinTransaction,
    CoinWallet,
    ConnectionFeeSetting,
    ContactMatch,
    Conversation,
    GiftItem,
    HookupSettings,
    HookupMatch,
    HookupMessage,
    Match,
    ModeSwitchPaymentIntent,
    PaymentTransaction,
    Profile,
    ProfilePhoto,
    ProfileStateBackup,
    Referral,
    RELATIONSHIP_MODE_CHOICES,
    SiteConfiguration,
    SubscriptionPlan,
    Swipe,
    Tag,
    UserBlock,
    UserReport,
)
from .services.content_filter import ContentFilter
from .utils import detect_contact_leak
from .forms import (
    LoginForm,
    SignUpForm,
    ProfileCreationForm,
    ProfileForm,
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
FREE_SEX_CALL_DURATION_SECONDS = 180
PREMIUM_CATEGORY_NAMES = {
    'DATING': 'dating',
    'HOOKUP': 'hookup',
    'SEX_CALL': 'sex_call',
}
DAILY_FREE_LIKES = 100
MAX_MESSAGE_LENGTH = 2000
MESSAGE_RATE_LIMIT = 30
logger = logging.getLogger(__name__)


def _paystack_keys_configured():
    public_key = getattr(settings, 'PAYSTACK_PUBLIC_KEY', '').strip()
    secret_key = getattr(settings, 'PAYSTACK_SECRET_KEY', '').strip()
    public_mode = next(
        (mode for mode in ('live', 'test') if public_key.startswith(f'pk_{mode}_')),
        None,
    )
    secret_mode = next(
        (mode for mode in ('live', 'test') if secret_key.startswith(f'sk_{mode}_')),
        None,
    )
    return bool(public_mode and public_mode == secret_mode)


def _flutterwave_keys_configured():
    public_key = getattr(settings, 'FLUTTERWAVE_PUBLIC_KEY', '').strip()
    secret_key = getattr(settings, 'FLUTTERWAVE_SECRET_KEY', '').strip()
    return bool(
        public_key
        and public_key.startswith('FLWPUBK-')
        and secret_key
        and secret_key.startswith('FLWSECK-')
        and 'PUT_YOUR_REAL' not in secret_key
    )


def _profile_is_online(profile, now=None):
    now = now or timezone.now()
    return bool(profile.last_active and profile.last_active >= now - timedelta(minutes=15))


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


def _exclude_test_profiles(queryset):
    return queryset.exclude(
        Q(is_test_profile=True)
        | Q(user__username__startswith='qa_demo')
        | Q(user__username__startswith='test_user_')
    )


def _candidate_queryset(user):
    current_profile = Profile.objects.filter(user=user).first()
    if not current_profile:
        return Profile.objects.none()

    target_gender = current_profile.preferred_gender or (
        'F' if current_profile.gender == 'M'
        else ('M' if current_profile.gender == 'F' else None)
    )

    active_limit = timezone.now() - timedelta(days=60)
    swiped_ids = Swipe.objects.filter(swiper=user).values('swiped_id')
    filters = Profile.objects.exclude(user=user)
    filters = _exclude_test_profiles(filters).filter(
        user__is_active=True,
        last_active__gte=active_limit,
        incognito_mode=False,
    )
    filters = filters.filter(relationship_mode=current_profile.relationship_mode)
    if target_gender:
        filters = filters.filter(gender=target_gender)
    if current_profile.gender in ('M', 'F'):
        filters = filters.filter(preferred_gender=current_profile.gender)
    candidates = filters.exclude(user_id__in=Subquery(swiped_ids))
    candidates = candidates.select_related('user').prefetch_related('photos', 'tags')

    if not candidates.exists():
        real_fallback = _exclude_test_profiles(
            Profile.objects.exclude(user=user)
        ).exclude(
            user_id__in=Subquery(swiped_ids)
        ).filter(
            user__is_active=True,
            is_test_profile=False,
        ).select_related('user').prefetch_related('photos', 'tags')
        if target_gender and real_fallback.filter(gender=target_gender).exists():
            candidates = real_fallback.filter(gender=target_gender)
        elif real_fallback.exists():
            candidates = real_fallback

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
    if (
        current_profile.relationship_mode != 'HOOKUP'
        or current_profile.latitude is None
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


def _profile_avatar_url(profile):
    if profile:
        photo = profile.photos.filter(is_main=True).first() or profile.photos.first()
        if photo and photo.image:
            try:
                if photo.image.storage.exists(photo.image.name):
                    return photo.image.url
            except (OSError, ValueError):
                logger.warning(
                    'Could not resolve avatar image for user %s.',
                    profile.user_id,
                    exc_info=True,
                )
        username = profile.user.username if profile.user else 'user'
    else:
        username = 'user'
    return f"https://api.dicebear.com/7.x/avataaars/svg?seed={quote(username)}"


def _profile_payload(profile):
    photo = profile.photos.filter(is_main=True).first() or profile.photos.first()
    media_url = _profile_avatar_url(profile)
    bio = profile.bio or ''
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
        'gender': (
            profile.get_gender_display()
            if profile.gender in {'M', 'F'}
            else ''
        ),
        'is_verified': profile.is_verified,
        'is_vip': profile.is_vip,
        'is_online': _profile_is_online(profile),
        'last_seen': profile.last_active.isoformat() if profile.last_active else None,
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

    display_name = (request.user.first_name or '').strip()
    if not display_name or display_name.casefold() == 'member':
        display_name = request.user.username.strip()
    if not display_name or display_name.casefold() in {'member', 'user', 'unknown'}:
        display_name = f"User {317000000 + request.user.pk}"

    main_photo = profile.photos.filter(is_main=True).first() or profile.photos.first()
    other_photos = profile.photos.exclude(id=main_photo.id) if main_photo else profile.photos.all()
    
    today = timezone.localdate()
    has_checked_in_today = bool(
        profile.last_checkin_date == today
        or CoinTransaction.objects.filter(
            user=request.user,
            transaction_type='DAILY_CHECKIN',
            created_at__date=today,
        ).exists()
    )
    following_count = Swipe.objects.filter(swiper=request.user, type='LIKE').count()
    followers_count = Swipe.objects.filter(swiped=request.user, type='LIKE').count()
    numeric_id = 317000000 + request.user.pk
    return render(request, 'dating/profile_detail.html', {
        'profile': profile, 
        'display_name': display_name,
        'main_photo': main_photo, 
        'other_photos': other_photos, 
        'is_own_profile': True,
        'is_online': True,
        'numeric_id': numeric_id,
        'following_count': following_count,
        'followers_count': followers_count,
        'has_checked_in_today': has_checked_in_today,
    })

@login_required
def public_profile(request, pk):
    if request.user.id == pk:
        return redirect('profile')
    qs = Profile.objects.filter(age__gte=18)
    profile = get_object_or_404(qs, user_id=pk)
    viewer_profile = Profile.objects.filter(user=request.user).first()
    all_photos = list(profile.photos.all())
    main_photo = profile.photos.filter(is_main=True).first() or (all_photos[0] if all_photos else None)
    other_photos = [p for p in all_photos if not main_photo or p.id != main_photo.id]

    flirty_moods = [
        'Looking for a genuine connection',
        "Let's connect and see where it goes",
        'Looking for real energy and good laughs',
        'Here to meet someone kind and interesting.',
    ]
    mood_line = profile.bio if (profile.bio and len(profile.bio) > 10) else flirty_moods[profile.user.id % len(flirty_moods)]

    languages = ['English', 'Pidgin']
    if profile.user.id % 3 == 0:
        languages.append('Yoruba')
    elif profile.user.id % 3 == 1:
        languages.append('Hausa')
    else:
        languages.append('Igbo')

    is_matched = Match.objects.filter(
        (Q(user1=request.user, user2=profile.user) | Q(user2=request.user, user1=profile.user)),
        expires_at__gt=timezone.now()
    ).exists()
    return render(request, 'dating/public_profile.html', {
        'profile': profile, 
        'main_photo': main_photo, 
        'all_photos': all_photos,
        'other_photos': other_photos, 
        'is_own_profile': False,
        'mood_line': mood_line,
        'languages': languages,
        'is_matched': is_matched,
        'is_online': _profile_is_online(profile),
        'last_seen': profile.last_active,
    })


@login_required
@require_POST
def quick_connect_api(request, user_id):
    """
    ⚡ Match / Connect action from Profile View:
    Instantly creates or activates a Match record with the target user without swiping.
    """
    if request.user.pk == user_id:
        return JsonResponse({'status': 'error', 'message': 'cannot_match_self'}, status=400)

    target_user = get_object_or_404(User, pk=user_id)
    target_profile = Profile.objects.filter(user=target_user, age__gte=18).first()
    if not target_profile:
        return JsonResponse({'status': 'error', 'message': 'target_not_found'}, status=404)

    user1, user2 = sorted((request.user, target_user), key=lambda user: user.pk)
    now = timezone.now()
    with transaction.atomic():
        match = Match.objects.select_for_update().filter(
            user1=user1,
            user2=user2,
            expires_at__gt=now,
        ).first()
        if match:
            match.expires_at = now
            match.save(update_fields=['expires_at'])
            Swipe.objects.filter(
                Q(swiper=request.user, swiped=target_user)
                | Q(swiper=target_user, swiped=request.user),
            ).delete()
            return JsonResponse({
                'status': 'success',
                'is_matched': False,
                'message': f'Disconnected from {target_user.username}.',
            })

        Swipe.objects.get_or_create(
            swiper=request.user,
            swiped=target_user,
            defaults={'type': 'LIKE', 'is_direct': True}
        )
        Swipe.objects.get_or_create(
            swiper=target_user,
            swiped=request.user,
            defaults={'type': 'LIKE', 'is_direct': True}
        )

        match, _ = Match.objects.get_or_create(
            user1=user1,
            user2=user2,
            defaults={
                'mode': 'SEX_CALL' if target_profile.relationship_mode.upper() == 'SEX_CALL' else 'DATING',
                'expires_at': now + timedelta(days=365),
            }
        )
        if match.expires_at <= now:
            match.expires_at = now + timedelta(days=365)
            match.save(update_fields=['expires_at'])

    return JsonResponse({
        'status': 'success',
        'is_matched': True,
        'match_id': match.id,
        'message': f'Connected with {target_user.first_name or target_user.username}!'
    })


@login_required
@require_POST
def quick_say_hi_api(request, user_id):
    """
    💬 Say Hi action from Profile View:
    Ensures a match & conversation exist and sends an opening intro greeting.
    Redirects to the chat room.
    """
    if request.user.pk == user_id:
        return JsonResponse({'status': 'error', 'message': 'cannot_chat_self'}, status=400)

    target_user = get_object_or_404(User, pk=user_id)
    target_profile = Profile.objects.filter(user=target_user, age__gte=18).first()
    if not target_profile:
        return JsonResponse({'status': 'error', 'message': 'target_not_found'}, status=404)

    match, _ = Match.objects.get_or_create(
        user1=min(request.user, target_user, key=lambda u: u.pk),
        user2=max(request.user, target_user, key=lambda u: u.pk),
        defaults={
            'mode': 'SEX_CALL' if getattr(target_profile, 'relationship_mode', '').upper() == 'SEX_CALL' else 'DATING',
            'expires_at': timezone.now() + timedelta(days=365),
        }
    )
    if match.expires_at and match.expires_at <= timezone.now():
        match.expires_at = timezone.now() + timedelta(days=365)
        match.save(update_fields=['expires_at'])

    conversation = _get_or_create_conversation(match)

    if not conversation.messages.exists() and (
        match.mode != 'DATING'
        or _has_paid_dating_premium(
            Profile.objects.filter(user=request.user).first(),
        )
    ):
        ChatMessage.objects.create(
            conversation=conversation,
            sender=request.user,
            text=f"Hey {target_user.first_name or target_user.username}! 👋 Let's connect.",
            message_type='TEXT'
        )

    return JsonResponse({
        'status': 'success',
        'conversation_id': conversation.id,
        'redirect_url': reverse('conversation_room', args=[conversation.id]),
        'chat_url': reverse('chat_room', args=[match.id])
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
            single_photo = request.FILES.get('photo')
            if single_photo and single_photo not in more_photos:
                more_photos.append(single_photo)

            media_limit = 14
            remaining_slots = max(0, media_limit - profile.photos.count())
            if len(more_photos) > remaining_slots:
                form.add_error('more_photos', f'You can add up to {remaining_slots} more media file(s). Maximum total is {media_limit}.')
            else:
                profile = form.save()
                name_val = form.cleaned_data.get('name')
                if name_val:
                    request.user.first_name = name_val
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
                    ProfilePhoto.objects.create(profile=profile, user=profile.user, mode=profile.relationship_mode, image=uploaded_file, is_main=is_main)
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
        'verification_photo_count': profile.verification_photo_count,
        'verification_photos_needed': max(0, 4 - profile.verification_photo_count),
        'media_limit': 14,
    })

# --- ACTION: Swiping ---
@ensure_csrf_cookie
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
    premium_url = (
        f"{reverse('premium_checkout')}?mode=COINS"
        if product_type == 'SEX_CALL'
        else f"{reverse('premium_landing')}?product={product_type}"
    )
    return render(request, 'dating/swipe_card.html', {
        'profile': profile,
        'SWIPE_URL': reverse('swipe_action'),
        'PROFILES_URL': reverse('get_profiles_json'),
        'PREMIUM_URL': premium_url,
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
    target_profile = get_object_or_404(
        _candidate_queryset(request.user),
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
            mode = {
                'SEX_CALL': 'SEXCALL',
            }.get(current_profile.relationship_mode, current_profile.relationship_mode)
            ContactMatch.objects.get_or_create(
                user_a=user1,
                user_b=user2,
                is_cancelled=False,
                defaults={'mode': mode},
            )

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
    active_mode = request.session.get(
        'active_connection_mode',
        viewer_profile.relationship_mode if viewer_profile else 'DATING',
    )
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
        if profile.is_test_profile or other.username.startswith(
            ('qa_demo', 'test_user_')
        ):
            continue
        payload = _profile_payload(profile)
        data.append({
            'id': m.pk,
            'other_user_id': other.pk,
            'username': other.username,
            'name': other.first_name or other.username,
            'avatar': payload['image_url'],
            'age': profile.age,
            'gender': (
                profile.get_gender_display()
                if profile.gender in {'M', 'F'}
                else ''
            ),
            'location': profile.location or 'Nearby',
            'first_date_idea': profile.first_date_idea,
            'expires_in': max(0, (m.expires_at - timezone.now()).days),
            'has_direct_interest': m.has_direct_interest,
            'mode': m.mode,
            'is_online': _profile_is_online(profile),
            'last_seen': profile.last_active,
        })
    fee_settings = ConnectionFeeSetting.get_solo()
    fee_by_mode = {
        'DATING': fee_settings.dating_fee,
        'HOOKUP': fee_settings.hookup_fee,
        'SEXCALL': fee_settings.sexcall_fee,
    }
    contact_matches = []
    contact_rows = ContactMatch.objects.filter(
        Q(user_a=request.user) | Q(user_b=request.user),
        is_cancelled=False,
    ).select_related('user_a', 'user_b', 'user_a__profile', 'user_b__profile')
    for contact_match in contact_rows:
        is_user_a = contact_match.user_a_id == request.user.pk
        partner = contact_match.user_b if is_user_a else contact_match.user_a
        partner_profile = partner.profile
        viewer_paid = contact_match.user_a_paid if is_user_a else contact_match.user_b_paid
        partner_paid = contact_match.user_b_paid if is_user_a else contact_match.user_a_paid
        if contact_match.is_unlocked:
            status = 'Unlocked - Chat on WhatsApp'
        elif viewer_paid:
            status = 'You Paid - Waiting for Partner'
        elif partner_paid:
            status = f'Partner Paid - Pay ₦{fee_by_mode[contact_match.mode]} to Reveal'
        else:
            status = 'Mutual Match - Unlock Contact'

        partner_data = _profile_payload(partner_profile)
        item = {
            'id': contact_match.pk,
            'mode': contact_match.mode,
            'status': status,
            'fee': fee_by_mode[contact_match.mode],
            'viewer_paid': viewer_paid,
            'partner_paid': partner_paid,
            'is_unlocked': contact_match.is_unlocked,
            'can_cancel': viewer_paid and not partner_paid and not contact_match.is_unlocked,
            'partner_name': partner.first_name or partner.username,
            'partner_username': partner.username,
            'partner_avatar': partner_data['image_url'],
        }
        if contact_match.is_unlocked:
            viewer_phone = re.sub(r'\D', '', request.user.profile.whatsapp_number)
            partner_phone = re.sub(r'\D', '', partner_profile.whatsapp_number)
            item['viewer_whatsapp'] = request.user.profile.whatsapp_number
            item['partner_whatsapp'] = partner_profile.whatsapp_number
            item['partner_whatsapp_url'] = (
                f'https://wa.me/{partner_phone}?text=Hi%20from%20LOVENY!'
            )
            item['viewer_whatsapp_url'] = (
                f'https://wa.me/{viewer_phone}?text=Hi%20from%20LOVENY!'
            )
        contact_matches.append(item)
    return render(request, 'dating/match_list.html', {
        'matches': data,
        'contact_matches': contact_matches,
        'match_credits': viewer_profile.match_credits if viewer_profile else 0,
        'paystack_enabled': _paystack_keys_configured(),
        'flutterwave_enabled': _flutterwave_keys_configured(),
        'active_connection_mode': active_mode,
    })


def _contact_match_fee(contact_match):
    fee_settings = ConnectionFeeSetting.get_solo()
    return {
        'DATING': fee_settings.dating_fee,
        'HOOKUP': fee_settings.hookup_fee,
        'SEXCALL': fee_settings.sexcall_fee,
    }[contact_match.mode]


def _contact_match_payer_state(contact_match, user):
    if user.pk == contact_match.user_a_id:
        return contact_match.user_a_paid, contact_match.user_b_paid
    if user.pk == contact_match.user_b_id:
        return contact_match.user_b_paid, contact_match.user_a_paid
    return None, None


def _contact_match_json(contact_match, user):
    viewer_paid, partner_paid = _contact_match_payer_state(contact_match, user)
    payload = {
        'status': 'success',
        'match_id': contact_match.pk,
        'viewer_paid': viewer_paid,
        'partner_paid': partner_paid,
        'is_unlocked': contact_match.is_unlocked,
        'is_cancelled': contact_match.is_cancelled,
    }
    if contact_match.is_unlocked:
        for participant in (contact_match.user_a, contact_match.user_b):
            participant_profile = Profile.objects.filter(user=participant).first()
            if not participant_profile:
                continue
            phone = re.sub(r'\D', '', participant_profile.whatsapp_number)
            payload[f'user_{participant.pk}_whatsapp'] = participant_profile.whatsapp_number
            payload[f'user_{participant.pk}_whatsapp_url'] = (
                f'https://wa.me/{phone}?text=Hi%20from%20LOVENY!'
            )
    return payload


@login_required
@require_POST
def contact_match_payment_api(request, match_id):
    contact_match = get_object_or_404(ContactMatch, pk=match_id)
    if request.user.pk not in (contact_match.user_a_id, contact_match.user_b_id):
        return HttpResponseForbidden('Not authorized')
    if contact_match.is_cancelled or contact_match.is_unlocked:
        return JsonResponse({'status': 'error', 'message': 'match_not_payable'}, status=409)
    viewer_paid, _ = _contact_match_payer_state(contact_match, request.user)
    if viewer_paid:
        return JsonResponse({'status': 'error', 'message': 'already_paid'}, status=409)

    profile = Profile.objects.filter(user=request.user).first()
    partner_id = (
        contact_match.user_b_id
        if contact_match.user_a_id == request.user.pk
        else contact_match.user_a_id
    )
    partner_profile = Profile.objects.filter(user_id=partner_id).first()
    if not profile or not profile.whatsapp_number or not partner_profile or not partner_profile.whatsapp_number:
        return JsonResponse({'status': 'error', 'message': 'whatsapp_number_required'}, status=409)

    try:
        data = json.loads(request.body or '{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'status': 'error', 'message': 'invalid_request'}, status=400)

    if profile.match_credits > 0:
        with transaction.atomic():
            match_locked = ContactMatch.objects.select_for_update().get(pk=contact_match.pk)
            profile_locked = Profile.objects.select_for_update().get(pk=profile.pk)
            paid, _ = _contact_match_payer_state(match_locked, request.user)
            if match_locked.is_cancelled or match_locked.is_unlocked or paid:
                return JsonResponse({'status': 'error', 'message': 'match_not_payable'}, status=409)
            if profile_locked.match_credits < 1:
                return JsonResponse({'status': 'error', 'message': 'no_match_credits'}, status=409)
            profile_locked.match_credits -= 1
            profile_locked.save(update_fields=['match_credits'])
            match_locked.mark_paid(request.user, used_credit=True)
            match_locked.save(update_fields=[
                'user_a_paid', 'user_b_paid',
                'user_a_paid_with_credit', 'user_b_paid_with_credit',
                'is_unlocked', 'unlocked_at',
            ])
        return JsonResponse(_contact_match_json(match_locked, request.user))

    provider = data.get('provider')
    if provider not in ('paystack', 'flutterwave'):
        return JsonResponse({'status': 'error', 'message': 'invalid_payment_provider'}, status=400)
    if provider == 'paystack':
        if not _paystack_keys_configured():
            return JsonResponse({'status': 'error', 'message': 'payment_provider_unavailable'}, status=503)
        public_key = settings.PAYSTACK_PUBLIC_KEY
    else:
        if not _flutterwave_keys_configured():
            return JsonResponse({'status': 'error', 'message': 'payment_provider_unavailable'}, status=503)
        public_key = settings.FLUTTERWAVE_PUBLIC_KEY

    fee = _contact_match_fee(contact_match)
    amount_kobo = int(fee * 100)
    reference = f'CONTACT_{contact_match.pk}_{request.user.pk}_{uuid.uuid4().hex}'
    return JsonResponse({
        'status': 'success',
        'match_id': contact_match.pk,
        'provider': provider,
        'reference': reference,
        'amount': str(fee),
        'amount_kobo': amount_kobo,
        'currency': 'NGN',
        'public_key': public_key,
        'email': request.user.email or f'{request.user.username}@loveny.com',
        'metadata': {
            'match_id': str(contact_match.pk),
            'payer_id': request.user.pk,
            'product': 'CONTACT_MATCH',
        },
    })


@login_required
@require_POST
def contact_match_verify_payment_api(request, match_id):
    contact_match = get_object_or_404(ContactMatch, pk=match_id)
    if request.user.pk not in (contact_match.user_a_id, contact_match.user_b_id):
        return HttpResponseForbidden('Not authorized')
    if contact_match.is_cancelled:
        return JsonResponse({'status': 'error', 'message': 'match_cancelled'}, status=409)
    viewer_paid, _ = _contact_match_payer_state(contact_match, request.user)
    if viewer_paid:
        return JsonResponse(_contact_match_json(contact_match, request.user))

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'status': 'error', 'message': 'invalid_request'}, status=400)
    provider = data.get('provider')
    reference = str(data.get('reference', '')).strip()
    transaction_id = str(data.get('transaction_id', '')).strip()
    if provider not in ('paystack', 'flutterwave') or not reference or len(reference) > 100:
        return JsonResponse({'status': 'error', 'message': 'invalid_payment'}, status=400)
    if provider == 'flutterwave' and not transaction_id.isdigit():
        return JsonResponse({'status': 'error', 'message': 'invalid_payment'}, status=400)

    if provider == 'paystack':
        verified, error_message, error_status = _verify_paystack_reference(reference)
        if not verified:
            return JsonResponse({'status': 'error', 'message': error_message}, status=error_status)
        payment_data = verified.get('data') if isinstance(verified, dict) else None
        metadata = payment_data.get('metadata') if isinstance(payment_data, dict) else None
        try:
            payer_id = int(metadata.get('payer_id', 0))
        except (AttributeError, TypeError, ValueError):
            payer_id = 0
        amount_kobo = payment_data.get('amount') if isinstance(payment_data, dict) else None
        currency = payment_data.get('currency') if isinstance(payment_data, dict) else None
        valid = (
            isinstance(payment_data, dict)
            and isinstance(metadata, dict)
            and payment_data.get('status') == 'success'
            and payment_data.get('reference') == reference
            and metadata.get('match_id') == str(contact_match.pk)
            and metadata.get('product') == 'CONTACT_MATCH'
            and payer_id == request.user.pk
            and type(amount_kobo) is int
            and amount_kobo == int(_contact_match_fee(contact_match) * 100)
            and currency == 'NGN'
        )
    else:
        payment_data, error_message, error_status = _verify_flutterwave_hookup_transaction(
            transaction_id,
            reference,
        )
        if not payment_data:
            return JsonResponse({'status': 'error', 'message': error_message}, status=error_status)
        metadata = payment_data.get('meta', payment_data.get('metadata'))
        if isinstance(metadata, list):
            metadata = {
                item.get('metaname'): item.get('metavalue')
                for item in metadata
                if isinstance(item, dict)
            }
        if not isinstance(metadata, dict):
            metadata = {}
        try:
            payer_id = int(metadata.get('payer_id', 0))
            amount = Decimal(str(payment_data.get('amount'))) * 100
            amount_kobo = int(amount) if amount == amount.to_integral_value() else None
        except (InvalidOperation, TypeError, ValueError):
            payer_id = 0
            amount_kobo = None
        currency = payment_data.get('currency')
        valid = (
            metadata.get('match_id') == str(contact_match.pk)
            and metadata.get('product') == 'CONTACT_MATCH'
            and payer_id == request.user.pk
            and currency == 'NGN'
            and amount_kobo == int(_contact_match_fee(contact_match) * 100)
        )

    if not valid:
        return JsonResponse({'status': 'error', 'message': 'payment_not_verified'}, status=400)

    try:
        with transaction.atomic():
            match_locked = ContactMatch.objects.select_for_update().get(pk=contact_match.pk)
            if match_locked.is_cancelled:
                return JsonResponse({'status': 'error', 'message': 'match_cancelled'}, status=409)
            already_paid, _ = _contact_match_payer_state(match_locked, request.user)
            if already_paid:
                return JsonResponse(_contact_match_json(match_locked, request.user))
            existing = PaymentTransaction.objects.filter(reference=reference).first()
            if existing and (
                existing.user_id != request.user.pk
                or existing.product != 'CONTACT_MATCH'
                or existing.plan_type != f'CONTACT_MATCH_{match_locked.pk}'
            ):
                return JsonResponse({'status': 'error', 'message': 'payment_reference_used'}, status=409)
            if not existing:
                PaymentTransaction.objects.create(
                    user=request.user,
                    reference=reference,
                    provider=provider,
                    product='CONTACT_MATCH',
                    plan_type=f'CONTACT_MATCH_{match_locked.pk}',
                    amount_kobo=amount_kobo,
                    currency='NGN',
                )
            match_locked.mark_paid(request.user)
            match_locked.save(update_fields=[
                'user_a_paid', 'user_b_paid',
                'user_a_paid_with_credit', 'user_b_paid_with_credit',
                'is_unlocked', 'unlocked_at',
            ])
    except IntegrityError:
        return JsonResponse({'status': 'error', 'message': 'payment_reference_used'}, status=409)
    return JsonResponse(_contact_match_json(match_locked, request.user))


@login_required
@require_POST
def contact_match_cancel_api(request, match_id):
    with transaction.atomic():
        contact_match = get_object_or_404(
            ContactMatch.objects.select_for_update(),
            pk=match_id,
        )
        if request.user.pk not in (contact_match.user_a_id, contact_match.user_b_id):
            return HttpResponseForbidden('Not authorized')
        viewer_paid, partner_paid = _contact_match_payer_state(contact_match, request.user)
        if contact_match.is_cancelled or contact_match.is_unlocked or not viewer_paid or partner_paid:
            return JsonResponse({'status': 'error', 'message': 'match_not_cancellable'}, status=409)
        contact_match.is_cancelled = True
        contact_match.save(update_fields=['is_cancelled'])
        profile = Profile.objects.select_for_update().get(user=request.user)
        profile.match_credits += 1
        profile.save(update_fields=['match_credits'])
    return JsonResponse({
        'status': 'success',
        'message': 'Your match credit is saved for your next match.',
        'match_credits': profile.match_credits,
    })


@login_required
def messages_inbox(request):
    viewer_profile = Profile.objects.filter(user=request.user).first()
    if not viewer_profile:
        return redirect('create_profile')

    matches = _active_mode_matches_for_user(request.user, viewer_profile)

    for m in matches:
        _get_or_create_conversation(m)

    conversations = (
        Conversation.objects
        .filter(participants=request.user)
        .filter(participants__in=_match_partner_ids(matches, request.user.pk))
        .distinct()
        .prefetch_related('participants', 'messages')
        .order_by('-updated_at')
    )

    threads = []
    for conv in conversations:
        if conv.participants.count() != 2:
            continue
        other_user = conv.participants.exclude(id=request.user.id).first()
        if not other_user:
            continue
        other_profile = Profile.objects.filter(user=other_user).first()
        if not other_profile:
            continue

        last_msg = conv.messages.order_by('-created_at').first()
        unread_count = conv.messages.exclude(sender=request.user).exclude(read_by=request.user).count()
        payload = _profile_payload(other_profile)

        time_str = ""
        if last_msg:
            diff = timezone.now() - last_msg.created_at
            if diff.days > 0:
                time_str = f"{diff.days}d ago"
            elif diff.seconds >= 3600:
                time_str = f"{diff.seconds // 3600}h ago"
            elif diff.seconds >= 60:
                time_str = f"{diff.seconds // 60}m ago"
            else:
                time_str = "just now"
        elif conv.updated_at:
            diff = timezone.now() - conv.updated_at
            if diff.days > 0:
                time_str = f"{diff.days}d ago"
            elif diff.seconds >= 3600:
                time_str = f"{diff.seconds // 3600}h ago"
            else:
                time_str = "new"

        if last_msg:
            if last_msg.message_type == 'gift':
                last_preview = f"🎁 Sent a gift: {last_msg.text}"
            elif last_msg.message_type == 'sticker':
                last_preview = "Sent a sticker"
            elif last_msg.message_type == 'image':
                last_preview = last_msg.text or 'Sent a photo'
            else:
                last_preview = last_msg.text[:100]
        else:
            last_preview = "No messages yet. Say hello!"

        is_online = _profile_is_online(other_profile)

        threads.append({
            'id': conv.pk,
            'chat_url': reverse('conversation_room', args=[conv.pk]),
            'other_avatar': payload['image_url'],
            'other_username': other_user.username,
            'other_name': other_user.first_name or other_user.username,
            'other_age': other_profile.age,
            'is_online': is_online,
            'last_seen': other_profile.last_active,
            'time_ago': time_str,
            'last_message': last_preview,
            'unread_count': unread_count,
            'updated_at': conv.updated_at,
        })

    return render(request, 'dating/messages_inbox.html', {
        'conversations': threads,
    })


@login_required
def likes_list(request):
    """Shows profiles who liked the current user (Secret Admirers)."""
    profile = Profile.objects.filter(user=request.user).first()
    if not profile:
        return redirect('create_profile')
    if not profile.is_premium('DATING'):
        return redirect('messages_inbox')

    swiped_me = Swipe.objects.filter(swiped=request.user, type='LIKE').values_list('swiper_id', flat=True)
    i_swiped = Swipe.objects.filter(swiper=request.user).values_list('swiped_id', flat=True)
    blocked_by_me = UserBlock.objects.filter(blocker=request.user).values_list('blocked_id', flat=True)
    
    admirer_ids = set(swiped_me) - set(i_swiped) - set(blocked_by_me)
    likes = Profile.objects.filter(user_id__in=admirer_ids).select_related('user').prefetch_related('photos')
    
    return render(request, 'dating/likes_list.html', {
        'likes': likes,
        'incoming_like_count': len(admirer_ids),
        'premium_required': False,
        'product_type': 'DATING',
        'premium_tier': profile.premium_tier if profile else 'FREE',
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
        return redirect('swipe_card')
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
            'user_email': request.user.email,
            'paystack_key': getattr(settings, 'PAYSTACK_PUBLIC_KEY', ''),
            'flutterwave_key': getattr(settings, 'FLUTTERWAVE_PUBLIC_KEY', ''),
            'paystack_enabled': _paystack_keys_configured(),
            'flutterwave_enabled': _flutterwave_keys_configured(),
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
        'paystack_enabled': _paystack_keys_configured(),
        'flutterwave_enabled': _flutterwave_keys_configured(),
        'payment_configured': (
            _paystack_keys_configured() or _flutterwave_keys_configured()
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
        if not _paystack_keys_configured():
            return None
        url = f"https://api.paystack.co/transaction/verify/{quote(reference, safe='')}"
        headers = {'Authorization': f'Bearer {secret}'}
    else:
        secret = getattr(settings, 'FLUTTERWAVE_SECRET_KEY', '')
        if not _flutterwave_keys_configured() or not transaction_id.isdigit():
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

        if reference.startswith('MOCK_DEV_'):
            verification = (True, int(package.price * 100), 'NGN', request.user.email)
        else:
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

    if reference.startswith('MOCK_DEV_'):
        verification = (True, int(plan.price * 100), 'NGN', request.user.email)
    else:
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


def _has_paid_dating_premium(profile):
    if not profile or not profile.dating_premium_expiry:
        return False
    if profile.dating_premium_expiry <= timezone.now():
        return False
    if (
        profile.free_trial_mode == 'DATING'
        and profile.free_trial_expires_at
        and profile.has_free_trial_access('DATING')
        and profile.dating_premium_expiry
        <= profile.free_trial_expires_at + timedelta(seconds=1)
    ):
        return False
    return True


@login_required
@require_GET
def wallet_balance_api(request):
    profile = Profile.objects.filter(user=request.user).first()
    if not profile:
        return JsonResponse({'status': 'error', 'message': 'profile_required'}, status=400)

    site_config = SiteConfiguration.get_solo()
    today = timezone.localdate()
    yesterday = today - timedelta(days=1)

    has_claimed_welcome = bool(
        profile.has_claimed_welcome
        or CoinTransaction.objects.filter(
            user=request.user,
            transaction_type='WELCOME_BONUS'
        ).exists()
    )

    has_checked_in_today = bool(
        profile.last_checkin_date == today
        or CoinTransaction.objects.filter(
            user=request.user,
            transaction_type='DAILY_CHECKIN',
            created_at__date=today
        ).exists()
    )

    streak = profile.checkin_streak or 0
    if has_checked_in_today:
        cycle_day = max(1, min(7, ((streak - 1) % 7) + 1)) if streak > 0 else 1
    else:
        if profile.last_checkin_date == yesterday and streak > 0:
            cycle_day = (streak % 7) + 1
        else:
            cycle_day = 1

    return JsonResponse({
        'status': 'success',
        'coin_balance': profile.coin_balance,
        'earned_diamonds': profile.earned_diamonds,
        'diamond_to_coin_percentage': getattr(site_config, 'diamond_to_coin_percentage', 70),
        'is_sex_call_premium': profile.is_sex_call_premium,
        'relationship_mode': profile.relationship_mode,
        'has_claimed_welcome': has_claimed_welcome,
        'has_checked_in_today': has_checked_in_today,
        'checkin_streak': streak,
        'cycle_day': cycle_day,
        'welcome_bonus_coins': getattr(site_config, 'welcome_bonus_coins', 30),
    })


@login_required
@require_POST
def wallet_visit_rewards_api(request):
    """Grant the one-time welcome reward; daily check-ins are explicitly claimed."""
    profile = Profile.objects.filter(user=request.user).first()
    if not profile:
        return JsonResponse(
            {'status': 'error', 'message': 'profile_required'},
            status=400,
        )

    today = timezone.localdate()
    site_config = SiteConfiguration.get_solo()
    welcome_coins = getattr(site_config, 'welcome_bonus_coins', 30) or 30

    with transaction.atomic():
        profile = Profile.objects.select_for_update().get(pk=profile.pk)
        welcome_awarded = not (
            profile.has_claimed_welcome
            or CoinTransaction.objects.filter(
                user=request.user,
                transaction_type='WELCOME_BONUS',
            ).exists()
        )
        if welcome_awarded:
            profile.coin_balance += welcome_coins
            profile.has_claimed_welcome = True
            CoinTransaction.objects.create(
                user=request.user,
                amount=welcome_coins,
                transaction_type='WELCOME_BONUS',
                description=f'Newcomer Joining Reward (+{welcome_coins} Coins)',
            )

        if welcome_awarded:
            profile.save(update_fields=[
                'coin_balance',
                'has_claimed_welcome',
            ])
            wallet, _ = CoinWallet.objects.select_for_update().get_or_create(
                user=request.user,
                defaults={'coin_balance': profile.coin_balance},
            )
            wallet.coin_balance = profile.coin_balance
            wallet.save(update_fields=['coin_balance'])

    return JsonResponse({
        'status': 'success',
        'welcome_awarded': welcome_coins if welcome_awarded else 0,
        'daily_awarded': 0,
        'diamonds_awarded': 0,
        'coin_balance': profile.coin_balance,
        'earned_diamonds': profile.earned_diamonds,
        'has_claimed_welcome': profile.has_claimed_welcome,
        'has_checked_in_today': bool(
            profile.last_checkin_date == today
            or CoinTransaction.objects.filter(
                user=request.user,
                transaction_type='DAILY_CHECKIN',
                created_at__date=today,
            ).exists()
        ),
        'checkin_streak': profile.checkin_streak,
        'cycle_day': profile.checkin_streak or 1,
    })


@login_required
@require_POST
def claim_welcome_bonus_api(request):
    """
    Claims newcomer joining starter reward (e.g. +30 coins).
    Callable once per account via the Welcome Joining Reward popup modal.
    """
    profile = Profile.objects.filter(user=request.user).first()
    if not profile:
        return JsonResponse({'status': 'error', 'message': 'profile_required'}, status=400)

    site_config = SiteConfiguration.get_solo()
    bonus_coins = getattr(site_config, 'welcome_bonus_coins', 30) or 30

    already_claimed = bool(
        profile.has_claimed_welcome
        or CoinTransaction.objects.filter(
            user=request.user,
            transaction_type='WELCOME_BONUS'
        ).exists()
    )
    if already_claimed:
        return JsonResponse({
            'status': 'already_claimed',
            'message': 'You have already claimed your newcomer joining reward!',
            'coin_balance': profile.coin_balance,
            'earned_diamonds': profile.earned_diamonds,
            'has_claimed_welcome': True,
        }, status=400)

    with transaction.atomic():
        p = Profile.objects.select_for_update().get(pk=profile.pk)
        p.coin_balance += bonus_coins
        p.has_claimed_welcome = True
        p.save(update_fields=['coin_balance', 'has_claimed_welcome'])

        wallet, _ = CoinWallet.objects.select_for_update().get_or_create(
            user=request.user,
            defaults={'coin_balance': p.coin_balance},
        )
        wallet.coin_balance = p.coin_balance
        wallet.save(update_fields=['coin_balance'])

        CoinTransaction.objects.create(
            user=request.user,
            amount=bonus_coins,
            transaction_type='WELCOME_BONUS',
            description=f'Newcomer Joining Reward (+{bonus_coins} Coins)',
        )

    return JsonResponse({
        'status': 'success',
        'coins_awarded': bonus_coins,
        'coin_balance': p.coin_balance,
        'earned_diamonds': p.earned_diamonds,
        'has_claimed_welcome': True,
        'message': f'🎉 Welcome bonus claimed! +{bonus_coins} 🪙 added to your wallet.',
    })


@login_required
@require_POST
def daily_checkin_api(request):
    """
    Awards daily check-in rewards.
    Days 1-6: +5 coins.
    Day 7 Grand Prize: +10 coins and +20 diamonds for completing a full 7-day streak!
    Tracks consecutive 7-day check-in streak.
    """
    profile = Profile.objects.filter(user=request.user).first()
    if not profile:
        return JsonResponse({'status': 'error', 'message': 'profile_required'}, status=400)

    today = timezone.localdate()
    yesterday = today - timedelta(days=1)

    already_claimed = (
        profile.last_checkin_date == today
        or CoinTransaction.objects.filter(
            user=request.user,
            transaction_type='DAILY_CHECKIN',
            created_at__date=today,
        ).exists()
    )
    if already_claimed:
        cycle_day = max(1, min(7, ((profile.checkin_streak - 1) % 7) + 1)) if profile.checkin_streak else 1
        return JsonResponse({
            'status': 'already_claimed',
            'message': 'You have already checked in today! Come back tomorrow for more rewards.',
            'coin_balance': profile.coin_balance,
            'earned_diamonds': profile.earned_diamonds,
            'checkin_streak': profile.checkin_streak,
            'cycle_day': cycle_day,
            'has_checked_in': True,
        }, status=400)

    with transaction.atomic():
        p = Profile.objects.select_for_update().get(pk=profile.pk)

        # Calculate streak progression
        if p.last_checkin_date == yesterday and p.checkin_streak > 0:
            new_streak = (p.checkin_streak % 7) + 1
        else:
            new_streak = 1

        site_config = SiteConfiguration.get_solo()
        daily_coins = getattr(site_config, 'daily_checkin_coins', 5) or 5
        d7_coins = getattr(site_config, 'day_7_bonus_coins', 10) or 10
        d7_diamonds = getattr(site_config, 'day_7_bonus_diamonds', 20) or 20

        is_grand_prize = (new_streak == 7)
        if is_grand_prize:
            coins_awarded = d7_coins
            diamonds_awarded = d7_diamonds
        else:
            coins_awarded = daily_coins
            diamonds_awarded = 0

        p.coin_balance += coins_awarded
        p.earned_diamonds += diamonds_awarded
        p.checkin_streak = new_streak
        p.last_checkin_date = today
        p.save(update_fields=['coin_balance', 'earned_diamonds', 'checkin_streak', 'last_checkin_date'])

        wallet, _ = CoinWallet.objects.select_for_update().get_or_create(
            user=request.user,
            defaults={'coin_balance': p.coin_balance},
        )
        wallet.coin_balance = p.coin_balance
        wallet.save(update_fields=['coin_balance'])

        desc = (
            f"Day 7 Streak Grand Prize (+{coins_awarded} Coins, +{diamonds_awarded} Diamonds 💎)"
            if is_grand_prize else
            f"Day {new_streak} Daily Check-In (+{coins_awarded} Coins)"
        )
        CoinTransaction.objects.create(
            user=request.user,
            amount=coins_awarded,
            transaction_type='DAILY_CHECKIN',
            description=desc,
        )

    return JsonResponse({
        'status': 'success',
        'coins_awarded': coins_awarded,
        'diamonds_awarded': diamonds_awarded,
        'coin_balance': p.coin_balance,
        'earned_diamonds': p.earned_diamonds,
        'checkin_streak': new_streak,
        'cycle_day': new_streak,
        'is_grand_prize': is_grand_prize,
        'has_checked_in': True,
        'message': f"Claimed Day {new_streak} reward! +{coins_awarded} 🪙" + (f" and +{diamonds_awarded} 💎 GRAND PRIZE!" if is_grand_prize else ""),
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
        'diamond_to_coin_percentage': getattr(cfg, 'diamond_to_coin_percentage', 70),
        'welcome_bonus_coins': cfg.welcome_bonus_coins,
        'daily_checkin_coins': getattr(cfg, 'daily_checkin_coins', 5),
        'day_7_bonus_coins': getattr(cfg, 'day_7_bonus_coins', 10),
        'day_7_bonus_diamonds': getattr(cfg, 'day_7_bonus_diamonds', 20),
        'host_commission_percentage': cfg.host_commission_percentage,
        'announcement_banner': cfg.announcement_banner if cfg.is_announcement_active else '',
        'is_announcement_active': cfg.is_announcement_active,
        'mode_switch_fee': cfg.mode_switch_fee,
    })


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
        'paystack_enabled': _paystack_keys_configured(),
        'flutterwave_enabled': _flutterwave_keys_configured(),
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
        conversations = (
            Conversation.objects
            .filter(participants=locked_match.user1_id)
            .filter(participants=locked_match.user2_id)
            .distinct()
            .prefetch_related('participants')
        )
        for conversation in conversations:
            if conversation.participants.count() == 2:
                return conversation

        conversation = Conversation.objects.create()
        conversation.participants.add(locked_match.user1_id, locked_match.user2_id)
        return conversation


def _active_mode_matches_for_user(user, profile=None):
    profile = profile or Profile.objects.filter(user=user).first()
    if not profile:
        return Match.objects.none()

    mode = profile.relationship_mode
    return Match.objects.filter(
        Q(user1=user, user2__profile__relationship_mode=mode)
        | Q(user2=user, user1__profile__relationship_mode=mode),
        mode=mode,
        expires_at__gt=timezone.now(),
    )


def _match_partner_ids(matches, user_id):
    return {
        match.user2_id if match.user1_id == user_id else match.user1_id
        for match in matches
    }


def _active_mode_conversations_for_user(user, profile=None):
    partner_ids = _match_partner_ids(
        _active_mode_matches_for_user(user, profile),
        user.pk,
    )
    if not partner_ids:
        return Conversation.objects.none()

    candidates = (
        Conversation.objects
        .filter(participants=user)
        .filter(participants__in=partner_ids)
        .distinct()
        .prefetch_related('participants')
    )
    conversation_ids = [
        conversation.pk
        for conversation in candidates
        if conversation.participants.count() == 2
    ]
    return Conversation.objects.filter(pk__in=conversation_ids)


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
    match = _active_mode_matches_for_user(user).filter(
        Q(user1=user, user2=other_user) | Q(user1=other_user, user2=user),
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
        other_profile
        and other_profile.last_active
        and (timezone.now() - other_profile.last_active).total_seconds() < 1800
    )

    languages = ['English', 'Pidgin']
    if other_user.id % 3 == 0:
        languages.append('Yoruba')
    elif other_user.id % 3 == 1:
        languages.append('Hausa')
    else:
        languages.append('Igbo')

    quick_icebreakers = [
        "Hello, how are you? 😍",
        "How old are you?",
        "Nice to meet you ✨",
    ]
    return {
        'conversation': conversation,
        'match': match,
        'other_user': other_user,
        'other_profile': other_profile,
        'other_avatar': other_avatar,
        'is_other_online': is_online,
        'current_profile': current_profile,
        'site_config': site_config,
        'user_coin_balance': current_profile.coin_balance if current_profile else 100,
        'gifts': gift_list,
        'sticker_packs': STICKER_PACKS,
        'languages': languages,
        'quick_icebreakers': quick_icebreakers,
        'can_send_messages': (
            match.mode != 'DATING'
            or _has_paid_dating_premium(current_profile)
        ),
        'dating_premium_url': f"{reverse('premium_landing')}?product=DATING",
        'other_numeric_id': 317000000 + other_user.pk,
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
    match = None

    if conversation_id:
        conversation, match, other_user = _get_active_conversation(conversation_id, request.user)
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
    if match and match.mode == 'DATING' and not _has_paid_dating_premium(sender_profile):
        return JsonResponse({
            'status': 'error',
            'message': 'dating_premium_required',
            'detail': 'Upgrade to Dating Premium to send messages and gifts.',
            'premium_url': f"{reverse('premium_landing')}?product=DATING",
        }, status=403)

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
            p_recipient.coin_balance += diamonds_earned
            w_recipient.coin_balance = p_recipient.coin_balance
            p_recipient.save(update_fields=['earned_diamonds', 'coin_balance'])
            w_recipient.save(update_fields=['coin_balance'])

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

    profile = request.user.profile if hasattr(request.user, 'profile') else None
    
    return JsonResponse({
        'messages': [
            serialize_chat_message(message, other_user.pk)
            for message in messages
        ],
        'coin_balance': profile.coin_balance if profile else 0,
    })


@login_required
@require_POST
def conversation_send_api(request, conversation_id):
    conversation, match, other_user = _get_active_conversation(conversation_id, request.user)
    sender_profile = Profile.objects.filter(user=request.user).first()
    recipient_profile = Profile.objects.filter(user=other_user).first()
    if match.mode == 'DATING' and not _has_paid_dating_premium(sender_profile):
        return JsonResponse({
            'status': 'error',
            'message': 'dating_premium_required',
            'detail': 'Upgrade to Dating Premium to send messages.',
            'premium_url': f"{reverse('premium_landing')}?product=DATING",
        }, status=403)
    if (
        not sender_profile
        or not recipient_profile
        or not sender_profile.allow_messages
        or not recipient_profile.allow_messages
    ):
        return JsonResponse({'status': 'error', 'message': 'messaging_disabled'}, status=403)
    content_type = request.content_type or ''
    if 'multipart/form-data' in content_type:
        text = str(request.POST.get('text', '')).strip()
        message_type = str(request.POST.get('message_type', 'text')).lower()
        metadata = {}
        uploaded_image = request.FILES.get('image')
        if uploaded_image:
            filename = f"chat_media/chat_{conversation.pk}_{int(timezone.now().timestamp())}_{uploaded_image.name}"
            saved_name = default_storage.save(filename, uploaded_image)
            image_url = default_storage.url(saved_name)
            message_type = 'image'
            metadata['image_url'] = image_url
            if not text:
                text = '📷 Photo'
    else:
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)
        if not isinstance(data, dict):
            return JsonResponse({'status': 'error', 'message': 'invalid_request'}, status=400)
        text = str(data.get('text', '')).strip()
        message_type = str(data.get('message_type', 'text')).lower()
        metadata = data.get('metadata') if isinstance(data.get('metadata'), dict) else {}

    if message_type not in ('text', 'sticker', 'gift', 'image'):
        message_type = 'text'

    if not text or len(text) > MAX_MESSAGE_LENGTH:
        return JsonResponse({'status': 'error', 'message': 'invalid_message'}, status=400)

    # Off-Platform Contact-Leak Detection (sliding-window: last 10 msgs / 10 min)
    is_leak, detected_pattern = detect_contact_leak(
        text,
        sender=request.user,
        conversation=conversation,
    )
    if is_leak:
        profile = request.user.profile
        profile.off_platform_attempts = (profile.off_platform_attempts or 0) + 1
        profile.save(update_fields=['off_platform_attempts'])
        logger.warning(
            'Off-platform contact leak blocked: user=%s conversation=%s pattern=%s',
            request.user.pk, conversation.pk, detected_pattern,
        )
        return JsonResponse({
            'status': 'error',
            'message': 'Sharing contact details off-platform is strictly prohibited.',
        }, status=400)

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
    own_profile = Profile.objects.filter(user=request.user, age__gte=18).first()
    if not own_profile:
        return JsonResponse({'conversations': []})

    active_partner_ids = _match_partner_ids(
        _active_mode_matches_for_user(request.user, own_profile),
        request.user.pk,
    )
    adult_partner_ids = set(
        Profile.objects.filter(
            user_id__in=active_partner_ids,
            age__gte=18,
        ).values_list('user_id', flat=True)
    )
    if not adult_partner_ids:
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
        .distinct()
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
    try:
        conversation, match, other_user = _get_active_conversation(conversation_id, request.user)
    except Http404:
        if Conversation.objects.filter(
            pk=conversation_id,
            participants=request.user,
        ).exists():
            return redirect(f"{reverse('messages_inbox')}?conversation_unavailable=1")
        raise
    other_profile = Profile.objects.filter(user=other_user).first()
    current_profile = Profile.objects.filter(user=request.user).first()
    context = _build_chat_context(request, conversation, match, other_user, other_profile, current_profile)
    return render(request, 'dating/chat_room.html', context)

# --- ACCOUNT FLOW & STATIC ---
@ensure_csrf_cookie
def index(request): return render(request, 'dating/landing.html')


def _award_referral_signup_rewards(referrer_id, referred_user):
    if not referrer_id or str(referrer_id) == str(referred_user.pk):
        return

    with transaction.atomic():
        referrer_profile = Profile.objects.select_for_update().filter(
            user_id=referrer_id,
        ).first()
        if not referrer_profile:
            logger.warning(
                'Referral attribution ignored because referrer %s has no profile.',
                referrer_id,
            )
            return

        referral, created = Referral.objects.get_or_create(
            referred_user=referred_user,
            defaults={'referrer_id': referrer_id},
        )
        if not created:
            return

        config = SiteConfiguration.get_solo()
        coin_reward = config.referral_reward_coins
        referrer_profile.coin_balance += coin_reward
        referrer_profile.save(update_fields=['coin_balance'])
        CoinTransaction.objects.create(
            user=referrer_profile.user,
            sender=referred_user,
            recipient=referrer_profile.user,
            amount=coin_reward,
            transaction_type='REFERRAL_REWARD',
            description=f'Referral reward for inviting {referred_user.username}',
        )
        referral.coins_awarded = coin_reward

        referral.save(update_fields=['coins_awarded'])


@ensure_csrf_cookie
def signup(request):
    if request.user.is_authenticated:
        return redirect('swipe_card')
    referral_username = request.GET.get('ref', '').strip()
    if referral_username:
        referrer = User.objects.filter(
            username=referral_username,
            profile__isnull=False,
        ).first()
        if referrer:
            request.session['pending_referrer_id'] = referrer.pk

    form = SignUpForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        mode = form.cleaned_data['relationship_mode']
        with transaction.atomic():
            user = form.save()
            site_config = SiteConfiguration.get_solo()
            profile = Profile.objects.create(
                user=user,
                relationship_mode=mode,
                has_claimed_welcome=True,
                coin_balance=site_config.welcome_bonus_coins,
            )
            if site_config.welcome_bonus_coins:
                CoinTransaction.objects.create(
                    user=user,
                    amount=site_config.welcome_bonus_coins,
                    transaction_type='WELCOME_BONUS',
                    description='Newcomer Joining Reward',
                )
        referrer_id = request.session.pop('pending_referrer_id', None)
        if not referrer_id:
            posted_referral = request.POST.get('ref', '').strip()
            if posted_referral:
                referrer_id = User.objects.filter(
                    username=posted_referral,
                    profile__isnull=False,
                ).values_list('pk', flat=True).first()
        if referrer_id:
            _award_referral_signup_rewards(referrer_id, user)
        login(request, user, backend='dating.backends.EmailAuthBackend')
        request.session['active_connection_mode'] = mode
        return redirect('create_profile')
    return render(request, 'registration/signup.html', {
        'form': form,
        'referral_username': referral_username,
    })


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
        profile = Profile.objects.filter(user=user).first()
        if profile:
            if profile.relationship_mode != mode:
                with transaction.atomic():
                    profile = Profile.objects.select_for_update().get(user=user)
                    if profile.relationship_mode != mode:
                        if profile.has_paid_mode_switch_fee:
                            _apply_profile_mode_switch(profile, mode, user)
                        else:
                            request.session['active_connection_mode'] = profile.relationship_mode
                            return redirect(
                                f"{reverse('settings')}?mode_switch_required=1&target_mode={mode}"
                            )
            request.session['active_connection_mode'] = profile.relationship_mode
        else:
            request.session['active_connection_mode'] = mode

        next_url = request.POST.get('next') or request.GET.get('next', '')
        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect(next_url)
        return redirect('swipe_card' if profile else 'create_profile')
    return render(request, 'registration/login.html', {
        'form': form,
        'next': request.GET.get('next', ''),
        'welcome_message': request.GET.get('welcome') == '1',
        'suspension_notice': request.session.pop(
            'live_call_suspension_notice',
            '',
        ),
    })


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
    existing_profile = Profile.objects.filter(user=request.user).first()
    existing_mode = existing_profile.relationship_mode if existing_profile else None
    if existing_profile and existing_profile.age and existing_profile.age >= 18 and existing_profile.photos.exists():
        if existing_profile.relationship_mode == 'SEX_CALL':
            return redirect('swipe_card')
        return redirect('swipe_card')

    form = ProfileCreationForm(
        request.POST or None,
        request.FILES or None,
        instance=existing_profile,
        initial={
            'relationship_mode': request.session.get('active_connection_mode', 'DATING'),
        },
    )
    if request.method == 'POST' and form.is_valid():
        chosen_mode = form.cleaned_data.get('relationship_mode') or request.session.get('active_connection_mode', 'DATING')
        if (
            existing_profile
            and chosen_mode != existing_mode
            and not existing_profile.has_paid_mode_switch_fee
        ):
            form.add_error(
                'relationship_mode',
                'Pay the one-time mode-switch fee from Settings to switch modes.',
            )
            return render(request, 'dating/create_profile.html', {'form': form})

        p = form.save(commit=False)
        p.user = request.user
        if form.cleaned_data.get('name'):
            request.user.first_name = form.cleaned_data['name']
            request.user.save(update_fields=['first_name'])
        if (
            existing_profile
            and chosen_mode != existing_mode
            and existing_profile.has_paid_mode_switch_fee
        ):
            current_profile = Profile.objects.get(user=request.user)
            _apply_profile_mode_switch(current_profile, chosen_mode, request.user)
        p.relationship_mode = chosen_mode
        trial_config = SiteConfiguration.get_solo()
        if not p.has_claimed_welcome:
            welcome_coins = trial_config.welcome_bonus_coins
            p.coin_balance = max(p.coin_balance, welcome_coins)
            p.has_claimed_welcome = True
        p.save()
        form.save_m2m()
        request.session['active_connection_mode'] = p.relationship_mode
        if request.FILES.get('photo'):
            ProfilePhoto.objects.create(profile=p, user=p.user, mode=p.relationship_mode, image=request.FILES.get('photo'), is_main=True)
        if p.relationship_mode == 'SEX_CALL':
            return redirect('swipe_card')
        return redirect('swipe_card')
    return render(request, 'dating/create_profile.html', {'form': form})

@login_required
def delete_account(request):
    if request.method == 'POST':
        user = request.user
        with transaction.atomic():
            user.delete()
        logout(request)
        return render(request, 'dating/account_deleted.html')
    return render(request, 'dating/confirm_delete.html')

@login_required
def settings_view(request):
    try:
        profile = request.user.profile
    except Profile.DoesNotExist:
        return redirect('create_profile')
        
    site_config = SiteConfiguration.objects.first()
    requested_mode = request.GET.get('target_mode', '')
    if requested_mode not in dict(RELATIONSHIP_MODE_CHOICES):
        requested_mode = ''
    return render(request, 'dating/settings.html', {
        'profile': profile,
        'site_config': site_config,
        'paystack_public_key': getattr(settings, 'PAYSTACK_PUBLIC_KEY', ''),
        'mode_switch_fee': site_config.mode_switch_fee if site_config else 200,
        'mode_switch_unlocked': profile.has_paid_mode_switch_fee,
        'mode_switch_required': request.GET.get('mode_switch_required') == '1',
        'requested_mode': requested_mode,
        'mode_switch_modes': RELATIONSHIP_MODE_CHOICES,
        'mode_switch_payments_enabled': _paystack_keys_configured(),
    })

def privacy(request): return render(request, 'dating/privacy.html')
def terms(request): return render(request, 'dating/terms.html')
def contact(request):
    return render(request, 'dating/contact.html')


# --- SEX CALL DISCOVERY & HOST GRID ---
@ensure_csrf_cookie
@login_required
def paystack_webhook_api(request):
    secret = getattr(settings, 'PAYSTACK_SECRET_KEY', '')
    if not secret or 'PUT_YOUR_REAL_SECRET_KEY_HERE' in secret:
        logger.error('Paystack webhook received without a configured secret key.')
        return HttpResponse(status=503)

    signature = request.headers.get('x-paystack-signature', '')
    expected_signature = hmac.new(
        secret.encode('utf-8'),
        request.body,
        hashlib.sha512,
    ).hexdigest()
    if not signature or not hmac.compare_digest(expected_signature, signature):
        return HttpResponse(status=400)

    try:
        event_data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponseBadRequest('Invalid JSON')
    if not isinstance(event_data, dict) or event_data.get('event') != 'charge.success':
        return HttpResponse(status=200)

    data = event_data.get('data')
    if not isinstance(data, dict) or data.get('status') != 'success':
        return HttpResponseBadRequest('Invalid payment data')
    metadata = data.get('metadata')
    if not isinstance(metadata, dict) or not metadata.get('match_id'):
        return HttpResponse(status=200)
    if metadata.get('product') != 'HOOKUP_MATCH':
        return HttpResponseBadRequest('Invalid Hookup payment product')

    match_id = metadata.get('match_id')
    payer_id = metadata.get('payer_id', metadata.get('user_id'))
    reference = data.get('reference')
    amount_kobo = data.get('amount')
    currency = data.get('currency')
    if not payer_id or not isinstance(reference, str) or type(amount_kobo) is not int:
        return HttpResponseBadRequest('Missing payment details')

    match = _record_hookup_payment(
        match_id=match_id,
        payer_id=payer_id,
        reference=reference,
        amount_kobo=amount_kobo,
        currency=currency,
        provider='paystack',
    )
    if not match:
        return HttpResponseBadRequest('Payment does not match the Hookup request')
    return HttpResponse(status=200)


def _record_hookup_payment(
    *,
    match_id,
    payer_id,
    reference,
    amount_kobo,
    currency,
    provider,
):
    try:
        match_id = uuid.UUID(str(match_id))
    except (TypeError, ValueError, AttributeError):
        return None

    with transaction.atomic():
        match = HookupMatch.objects.select_for_update().filter(pk=match_id).first()
        if (
            not match
            or match.status not in ('accepted', 'unlocked')
            or provider not in ('paystack', 'flutterwave')
            or currency != 'NGN'
            or amount_kobo != int(match.connection_fee * 100)
        ):
            return None

        try:
            payer_id = int(payer_id)
        except (TypeError, ValueError):
            return None
        if payer_id == match.initiator_id:
            payer_field = 'initiator_paid'
        elif payer_id == match.target_id:
            payer_field = 'target_paid'
        else:
            return None

        payment, created = PaymentTransaction.objects.get_or_create(
            reference=reference,
            defaults={
                'user_id': payer_id,
                'provider': provider,
                'product': 'HOOKUP_MATCH',
                'plan_type': 'hookup_connection_fee',
                'amount_kobo': amount_kobo,
                'currency': currency,
            },
        )
        if not created and (
            payment.user_id != payer_id
            or payment.product != 'HOOKUP_MATCH'
            or payment.amount_kobo != amount_kobo
            or payment.currency != currency
            or payment.provider != provider
        ):
            return None

        if not getattr(match, payer_field):
            setattr(match, payer_field, True)
        if match.is_fully_paid() and match.status != 'unlocked':
            match.status = 'unlocked'
            match.unlocked_at = timezone.now()
            match.expires_at = match.unlocked_at + timedelta(hours=24)
            match.save(update_fields=[
                payer_field,
                'status',
                'unlocked_at',
                'expires_at',
            ])
        else:
            match.save(update_fields=[payer_field])
        return match


@login_required
def hookup_discovery_view(request):
    """
    Hookup Mode Discovery Feed:
    - Mobile-first grid ("⚡ Available Tonight", "Lekki, Lagos", [ ⚡ Meet Today ] button).
    - Match inbox with [ Accept ] / [ Decline ] / [ Escrow Checkout ].
    - Coins completely hidden (no coin balance / counters).
    """
    profile = Profile.objects.filter(user=request.user).first()
    if not profile:
        return redirect('create_profile')

    is_hookup_mode = profile.relationship_mode == 'HOOKUP'
    if not is_hookup_mode:
        return redirect('settings')

    pending_incoming = list(
        HookupMatch.objects.filter(
            target=request.user,
            status='pending',
            initiator__profile__relationship_mode='HOOKUP',
        )
        .select_related('initiator', 'initiator__profile')
        .prefetch_related('initiator__profile__photos')
    )

    now = timezone.now()

    # Get blocked user IDs
    blocked_by_me = UserBlock.objects.filter(blocker=request.user).values_list('blocked_id', flat=True)
    blocking_me = UserBlock.objects.filter(blocked=request.user).values_list('blocker_id', flat=True)
    blocked_ids = set(blocked_by_me).union(set(blocking_me))

    # Available profiles for hookup
    if is_hookup_mode:
        base_qs = _exclude_test_profiles(
            Profile.objects.exclude(user=request.user)
            .exclude(user_id__in=blocked_ids)
            .filter(
                relationship_mode='HOOKUP',
                show_in_discovery=True,
                incognito_mode=False,
            )
        )
        target_gender = 'F' if profile.gender == 'M' else ('M' if profile.gender == 'F' else None)
        if target_gender:
            candidates_qs = base_qs.filter(gender=target_gender)
            if not candidates_qs.exists():
                candidates_qs = base_qs
        else:
            candidates_qs = base_qs
        available_candidates = list(
            candidates_qs.select_related('user').prefetch_related('photos')
            .order_by('-last_active')[:30]
        )
        for candidate in available_candidates:
            first_name = candidate.user.first_name.strip()
            candidate.display_name = (
                first_name
                if first_name and first_name.casefold() != 'member'
                else candidate.user.username
            )
            candidate.is_online = bool(
                candidate.last_active
                and now - candidate.last_active < timedelta(minutes=15)
            )
    else:
        available_candidates = []

    # Outgoing pending requests
    pending_outgoing = list(
        HookupMatch.objects.filter(
            initiator=request.user,
            status='pending',
            target__profile__relationship_mode='HOOKUP',
        )
        .select_related('target', 'target__profile')
        .prefetch_related('target__profile__photos')
    )

    # Accepted / Escrow payment stage matches
    accepted_matches = list(
        HookupMatch.objects.filter(
            Q(initiator=request.user) | Q(target=request.user),
            status='accepted',
            initiator__profile__relationship_mode='HOOKUP',
            target__profile__relationship_mode='HOOKUP',
        ).select_related('initiator', 'target', 'initiator__profile', 'target__profile')
    )
    for m in accepted_matches:
        m.is_initiator = (m.initiator_id == request.user.id)
        m.has_paid = m.initiator_paid if m.is_initiator else m.target_paid
        m.partner_paid = m.target_paid if m.is_initiator else m.initiator_paid

    # Active unlocked 24-hr chats
    active_unlocked = list(
        HookupMatch.objects.filter(
            Q(initiator=request.user) | Q(target=request.user),
            status='unlocked',
            initiator__profile__relationship_mode='HOOKUP',
            target__profile__relationship_mode='HOOKUP',
        ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .select_related('initiator', 'target', 'initiator__profile', 'target__profile')
    )
    for m in active_unlocked:
        m.partner = m.target if m.initiator_id == request.user.id else m.initiator

    # Auto-expire matches where 24h passed
    HookupMatch.objects.filter(
        status='unlocked',
        expires_at__lte=now
    ).update(status='expired')

    return render(request, 'dating/hookup/discovery.html', {
        'profile': profile,
        'is_hookup_mode': is_hookup_mode,
        'candidates': available_candidates,
        'pending_incoming': pending_incoming,
        'pending_outgoing': pending_outgoing,
        'accepted_matches': accepted_matches,
        'active_unlocked': active_unlocked,
        'paystack_public_key': getattr(settings, 'PAYSTACK_PUBLIC_KEY', ''),
        'paystack_enabled': _paystack_keys_configured(),
        'flutterwave_enabled': _flutterwave_keys_configured(),
        'connection_fee': HookupSettings.get_solo().connection_fee,
    })


@login_required
@require_POST
def hookup_request_api(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'status': 'error', 'message': 'invalid_request'}, status=400)

    initiator_profile = Profile.objects.filter(user=request.user).first()
    if not initiator_profile:
        return JsonResponse({'status': 'error', 'message': 'profile_required'}, status=400)
    if initiator_profile.relationship_mode != 'HOOKUP':
        return JsonResponse({'status': 'error', 'message': 'hookup_mode_required'}, status=403)

    target_id = data.get('target_id')
    if not target_id or target_id == request.user.pk:
        return JsonResponse({'status': 'error', 'message': 'invalid_target'}, status=400)

    target_user = get_object_or_404(User, pk=target_id)
    target_profile = Profile.objects.filter(user=target_user).first()
    if not target_profile:
        return JsonResponse({'status': 'error', 'message': 'profile_not_found'}, status=404)
    if target_profile.relationship_mode != 'HOOKUP':
        return JsonResponse({'status': 'error', 'message': 'target_not_in_hookup_mode'}, status=400)

    # Check for existing active/pending match
    existing = HookupMatch.objects.filter(
        Q(initiator=request.user, target=target_user) |
        Q(initiator=target_user, target=request.user)
    ).exclude(status__in=('declined', 'expired')).first()

    if existing:
        request_direction = 'incoming' if existing.target_id == request.user.pk else 'outgoing'
        return JsonResponse({
            'status': 'exists',
            'match_id': str(existing.id),
            'match_status': existing.status,
            'request_direction': request_direction,
            'message': 'A connection request already exists between you and this user.',
        })

    with transaction.atomic():
        profiles = Profile.objects.select_for_update().filter(
            user_id__in=(request.user.pk, target_user.pk),
        ).in_bulk(field_name='user_id')
        initiator_profile = profiles.get(request.user.pk)
        target_profile = profiles.get(target_user.pk)
        if not initiator_profile or initiator_profile.relationship_mode != 'HOOKUP':
            return JsonResponse({'status': 'error', 'message': 'hookup_mode_required'}, status=403)
        if not target_profile or target_profile.relationship_mode != 'HOOKUP':
            return JsonResponse({'status': 'error', 'message': 'target_not_in_hookup_mode'}, status=400)
        match = HookupMatch.objects.create(
            initiator=request.user,
            target=target_user,
            status='pending',
            connection_fee=HookupSettings.get_solo().connection_fee,
        )
    return JsonResponse({
        'status': 'success',
        'match_id': str(match.id),
        'message': f'Connection request sent to {target_user.username}!',
    }, status=201)


@login_required
@require_POST
def hookup_respond_api(request, match_id):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)

    action = data.get('action')
    if action not in ('accept', 'decline'):
        return JsonResponse({'status': 'error', 'message': 'invalid_action'}, status=400)

    match = get_object_or_404(HookupMatch, pk=match_id)
    if request.user.pk != match.target_id:
        return HttpResponseForbidden("Not authorized")
    if not Profile.objects.filter(
        user_id=match.target_id,
        relationship_mode='HOOKUP',
    ).exists() or not Profile.objects.filter(
        user_id=match.initiator_id,
        relationship_mode='HOOKUP',
    ).exists():
        return JsonResponse({'status': 'error', 'message': 'hookup_mode_required'}, status=403)
    if match.status != 'pending':
        return JsonResponse({'status': 'error', 'message': 'request_not_pending'}, status=409)

    if action == 'accept':
        match.status = 'accepted'
        match.connection_fee = HookupSettings.get_solo().connection_fee
    elif action == 'decline':
        with transaction.atomic():
            match = HookupMatch.objects.select_for_update().get(pk=match.pk)
            if match.status != 'pending':
                return JsonResponse({'status': 'error', 'message': 'request_not_pending'}, status=409)
            match.status = 'declined'
            match.save(update_fields=['status'])
    if action == 'accept':
        update_fields = ['status']
        update_fields.append('connection_fee')
        match.save(update_fields=update_fields)

    return JsonResponse({
        'status': 'success',
        'match_id': str(match.id),
        'match_status': match.status,
    })


def _verify_flutterwave_hookup_transaction(transaction_id, reference):
    if not _flutterwave_keys_configured():
        return None, 'Flutterwave verification is not configured.', 503
    if not transaction_id.isdigit():
        return None, 'Invalid Flutterwave transaction ID.', 400

    verify_request = Request(
        f'https://api.flutterwave.com/v3/transactions/{transaction_id}/verify',
        headers={'Authorization': f'******'},
        method='GET',
    )
    try:
        with urlopen(verify_request, timeout=10) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        logger.warning('Flutterwave verification returned HTTP %s.', exc.code)
        return None, 'Flutterwave could not verify this payment.', 502
    except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning('Flutterwave verification failed: %s', exc)
        return None, 'Flutterwave verification is temporarily unavailable.', 503

    data = payload.get('data') if isinstance(payload, dict) else None
    if (
        not isinstance(data, dict)
        or payload.get('status') != 'success'
        or data.get('status') != 'successful'
        or str(data.get('id')) != transaction_id
        or data.get('tx_ref') != reference
    ):
        return None, 'Flutterwave could not verify this payment.', 400
    return data, None, None


@login_required
@require_POST
def hookup_initialize_payment_api(request, match_id):
    match = get_object_or_404(HookupMatch, pk=match_id)
    if request.user not in (match.initiator, match.target):
        return HttpResponseForbidden("Not authorized")
    if match.status not in ('accepted', 'unlocked'):
        return JsonResponse({'status': 'error', 'message': 'request_not_accepted'}, status=409)
    payer_has_paid = (
        match.initiator_paid
        if request.user.pk == match.initiator_id
        else match.target_paid
    )
    if payer_has_paid:
        return JsonResponse({'status': 'error', 'message': 'fee_already_paid'}, status=409)

    try:
        data = json.loads(request.body or '{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'status': 'error', 'message': 'invalid_request'}, status=400)
    provider = data.get('provider', 'paystack')
    if provider not in ('paystack', 'flutterwave'):
        return JsonResponse({'status': 'error', 'message': 'invalid_payment_provider'}, status=400)

    if provider == 'paystack':
        public_key = getattr(settings, 'PAYSTACK_PUBLIC_KEY', '')
        if not _paystack_keys_configured() or not public_key:
            return JsonResponse({'status': 'error', 'message': 'payment_provider_unavailable'}, status=503)
    else:
        public_key = getattr(settings, 'FLUTTERWAVE_PUBLIC_KEY', '')
        if not _flutterwave_keys_configured():
            return JsonResponse({'status': 'error', 'message': 'payment_provider_unavailable'}, status=503)

    fee = match.connection_fee
    amount_kobo = int(fee * 100)
    ref = f'HOOKUP_{match.id}_{request.user.id}_{uuid.uuid4().hex}'

    return JsonResponse({
        'status': 'success',
        'match_id': str(match.id),
        'amount': str(fee),
        'amount_kobo': amount_kobo,
        'reference': ref,
        'provider': provider,
        'email': request.user.email or f"{request.user.username}@loveny.com",
        'public_key': public_key,
        'metadata': {
            'match_id': str(match.id),
            'payer_id': request.user.id,
            'product': 'HOOKUP_MATCH',
        }
    })


def _verify_paystack_reference(reference):
    secret = getattr(settings, 'PAYSTACK_SECRET_KEY', '')
    if not _paystack_keys_configured():
        return None, 'Payment verification is not configured.', 503
    verify_request = Request(
        f'https://api.paystack.co/transaction/verify/{quote(reference)}',
        headers={'Authorization': f'Bearer {secret}'},
    )
    try:
        with urlopen(verify_request, timeout=10) as response:
            return json.loads(response.read().decode('utf-8')), None, None
    except HTTPError as exc:
        logger.warning('Paystack verification returned HTTP %s.', exc.code)
        if exc.code in (401, 403):
            return (
                None,
                'Payment verification is misconfigured. Please contact support before retrying.',
                502,
            )
        return None, 'Paystack could not verify this payment.', 502
    except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning('Paystack verification failed: %s', exc)
        return None, 'Paystack verification is temporarily unavailable.', 503


@login_required
@require_POST
def hookup_verify_payment_api(request, match_id):
    match = get_object_or_404(HookupMatch, pk=match_id)
    if request.user not in (match.initiator, match.target):
        return HttpResponseForbidden("Not authorized")
    if match.status not in ('accepted', 'unlocked'):
        return JsonResponse({'status': 'error', 'message': 'request_not_accepted'}, status=409)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'status': 'error', 'message': 'invalid_request'}, status=400)

    provider = data.get('provider', 'paystack')
    reference = str(data.get('reference', '')).strip()
    transaction_id = str(data.get('transaction_id', '')).strip()
    if provider not in ('paystack', 'flutterwave') or not reference:
        return JsonResponse({'status': 'error', 'message': 'reference_required'}, status=400)

    payer_has_paid = (
        match.initiator_paid
        if request.user.pk == match.initiator_id
        else match.target_paid
    )
    if payer_has_paid:
        return JsonResponse({'status': 'error', 'message': 'fee_already_paid'}, status=409)

    if provider == 'paystack':
        verified, verification_error, error_status = _verify_paystack_reference(reference)
        if not verified:
            return JsonResponse(
                {'status': 'error', 'message': verification_error},
                status=error_status,
            )
        payment_data = (
            verified.get('data')
            if isinstance(verified, dict) and verified.get('status') is True
            else None
        )
        metadata = payment_data.get('metadata') if isinstance(payment_data, dict) else None
        try:
            payer_id = int(metadata.get('payer_id', metadata.get('user_id', 0)))
        except (AttributeError, TypeError, ValueError):
            payer_id = 0
        amount_kobo = payment_data.get('amount') if isinstance(payment_data, dict) else None
        currency = payment_data.get('currency') if isinstance(payment_data, dict) else None
        payment_valid = (
            isinstance(payment_data, dict)
            and payment_data.get('status') == 'success'
            and payment_data.get('reference') == reference
            and metadata.get('match_id') == str(match.pk)
            and metadata.get('product') == 'HOOKUP_MATCH'
            and payer_id == request.user.pk
            and currency == 'NGN'
            and type(amount_kobo) is int
            and amount_kobo == int(match.connection_fee * 100)
        )
    else:
        payment_data, verification_error, error_status = _verify_flutterwave_hookup_transaction(
            transaction_id,
            reference,
        )
        if not payment_data:
            return JsonResponse(
                {'status': 'error', 'message': verification_error},
                status=error_status,
            )
        metadata = payment_data.get('meta', payment_data.get('metadata'))
        if isinstance(metadata, list):
            metadata = {
                item.get('metaname'): item.get('metavalue')
                for item in metadata
                if isinstance(item, dict)
            }
        if not isinstance(metadata, dict):
            metadata = {}
        try:
            payer_id = int(metadata.get('payer_id', 0))
            amount_naira = Decimal(str(payment_data.get('amount')))
            amount_minor = amount_naira * 100
            amount_kobo = int(amount_minor) if amount_minor == amount_minor.to_integral_value() else None
        except (InvalidOperation, TypeError, ValueError):
            payer_id = 0
            amount_kobo = None
        currency = payment_data.get('currency')
        payment_valid = (
            metadata.get('match_id') == str(match.pk)
            and metadata.get('product') == 'HOOKUP_MATCH'
            and payer_id == request.user.pk
            and currency == 'NGN'
            and amount_kobo == int(match.connection_fee * 100)
        )

    if not payment_valid:
        return JsonResponse({'status': 'error', 'message': 'payment_not_verified'}, status=400)

    match = _record_hookup_payment(
        match_id=match.pk,
        payer_id=payer_id,
        reference=reference,
        amount_kobo=amount_kobo,
        currency=currency,
        provider=provider,
    )
    if not match:
        return JsonResponse({'status': 'error', 'message': 'payment_not_verified'}, status=400)
    return JsonResponse({
        'status': 'success',
        'match_id': str(match.id),
        'initiator_paid': match.initiator_paid,
        'target_paid': match.target_paid,
        'is_fully_paid': match.is_fully_paid(),
        'is_unlocked': match.status == 'unlocked',
        'match_status': match.status,
    })


@login_required
def hookup_chat_room_view(request, match_id):
    match = get_object_or_404(HookupMatch, pk=match_id)
    if request.user not in (match.initiator, match.target):
        return HttpResponseForbidden("You are not a participant in this hookup connection.")

    match.check_and_update_expiry()
    if not match.is_active:
        return HttpResponseForbidden("This Hookup chat is not active.")
    is_expired = False

    other_user = match.target if request.user == match.initiator else match.initiator
    other_profile = Profile.objects.filter(user=other_user).first()
    current_profile = Profile.objects.filter(user=request.user).first()

    messages = match.messages.select_related('sender').order_by('created_at')

    other_avatar = ''
    if other_profile:
        photo = other_profile.photos.filter(is_main=True).first() or other_profile.photos.first()
        if photo and photo.image:
            other_avatar = photo.image.url
    if not other_avatar:
        other_avatar = f"https://api.dicebear.com/7.x/avataaars/svg?seed={other_user.username}"

    return render(request, 'dating/hookup/chat_room.html', {
        'match': match,
        'other_user': other_user,
        'other_profile': other_profile,
        'other_avatar': other_avatar,
        'current_profile': current_profile,
        'messages': messages,
        'is_expired': is_expired,
        'time_remaining_seconds': match.time_remaining_seconds,
        'connection_fee': match.connection_fee,
        'paystack_public_key': getattr(settings, 'PAYSTACK_PUBLIC_KEY', ''),
    })


@login_required
@require_GET
def hookup_messages_api(request, match_id):
    match = get_object_or_404(HookupMatch, pk=match_id)
    if request.user not in (match.initiator, match.target):
        return HttpResponseForbidden("Not authorized")

    match.check_and_update_expiry()
    if not match.is_active:
        return JsonResponse({'status': 'error', 'message': 'window_expired'}, status=403)
    messages = match.messages.select_related('sender').order_by('created_at')

    return JsonResponse({
        'status': 'success',
        'is_active': match.is_active,
        'is_expired': match.status == 'expired',
        'time_remaining_seconds': match.time_remaining_seconds,
        'messages': [{
            'id': m.pk,
            'sender_id': m.sender_id,
            'sender_name': m.sender.first_name or m.sender.username,
            'is_self': m.sender_id == request.user.pk,
            'text': m.text,
            'created_at': m.created_at.strftime('%H:%M'),
        } for m in messages]
    })


@login_required
@require_POST
def hookup_send_message_api(request, match_id):
    match = get_object_or_404(HookupMatch, pk=match_id)
    if request.user not in (match.initiator, match.target):
        return HttpResponseForbidden("Not authorized")

    match.check_and_update_expiry()
    if not match.is_active:
        return JsonResponse({
            'status': 'error',
            'message': 'window_expired',
            'detail': 'The 24-hour hookup chat window has expired or is not yet unlocked.'
        }, status=403)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)

    text = str(data.get('text', '')).strip()
    if not text or len(text) > 500:
        return JsonResponse({'status': 'error', 'message': 'invalid_message'}, status=400)

    # Off-Platform Contact-Leak Detection (sliding-window: last 10 msgs / 10 min)
    other_user = match.target if request.user == match.initiator else match.initiator
    is_leak, detected_pattern = detect_contact_leak(
        text,
        sender=request.user,
        recipient=other_user,
    )
    if is_leak:
        profile = request.user.profile
        profile.off_platform_attempts = (profile.off_platform_attempts or 0) + 1
        profile.save(update_fields=['off_platform_attempts'])
        logger.warning(
            'Off-platform contact leak blocked (hookup): user=%s match=%s pattern=%s',
            request.user.pk, match.pk, detected_pattern,
        )
        return JsonResponse({
            'status': 'error',
            'message': 'Sharing contact details off-platform is strictly prohibited.',
        }, status=400)

    msg = HookupMessage.objects.create(
        match=match,
        sender=request.user,
        text=text,
    )

    return JsonResponse({
        'status': 'success',
        'message': {
            'id': msg.pk,
            'sender_id': msg.sender_id,
            'sender_name': request.user.first_name or request.user.username,
            'is_self': True,
            'text': msg.text,
            'created_at': msg.created_at.strftime('%H:%M'),
        }
    }, status=201)


def csrf_failure_view(request, reason=""):
    """
    Custom branded CSRF failure view returning luxury 403 error page.
    """
    return render(request, '403_csrf.html', {'reason': reason}, status=403)


@require_POST
@login_required
def report_user_api(request):
    try:
        data = json.loads(request.body)
        target_id = data.get('target_id')
        reason = data.get('reason', 'other')
        details = data.get('details', '')
        
        target = get_object_or_404(User, pk=target_id)
        
        # Create Report
        UserReport.objects.create(
            reporter=request.user,
            reported=target,
            reason=reason,
            details=details
        )
        
        # Automatically block the user when reported
        UserBlock.objects.get_or_create(blocker=request.user, blocked=target)
        
        return JsonResponse({'status': 'success', 'message': 'User reported and blocked successfully. They will no longer appear in your feed.'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


def _apply_profile_mode_switch(profile, target_mode, user):
    source_mode = profile.relationship_mode
    backup, _ = ProfileStateBackup.objects.get_or_create(
        user=user,
        mode=source_mode,
    )
    backup.bio = profile.bio
    backup.first_date_idea = profile.first_date_idea
    backup.job_title = profile.job_title
    backup.save()
    backup.tags.set(profile.tags.all())
    profile.photos.all().update(profile=None)

    target_backup = ProfileStateBackup.objects.filter(
        user=user,
        mode=target_mode,
    ).first()
    if target_backup:
        profile.bio = target_backup.bio
        profile.first_date_idea = target_backup.first_date_idea
        profile.job_title = target_backup.job_title
        profile.tags.set(target_backup.tags.all())
    else:
        profile.bio = ''
        profile.first_date_idea = ''
        profile.job_title = ''
        profile.tags.clear()

    profile.relationship_mode = target_mode
    profile.save()
    ProfilePhoto.objects.filter(
        user=user,
        mode=target_mode,
    ).update(profile=profile)


@login_required
@require_POST
def create_mode_switch_payment_intent_api(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'status': 'error', 'message': 'invalid_request'}, status=400)

    try:
        profile = Profile.objects.get(user=request.user)
    except Profile.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'profile_required'}, status=400)

    target_mode = data.get('target_mode')
    if not target_mode or target_mode not in dict(RELATIONSHIP_MODE_CHOICES):
        # Default to another mode if not provided
        target_mode = 'HOOKUP' if profile.relationship_mode == 'DATING' else 'DATING'
    if profile.relationship_mode == target_mode and profile.has_paid_mode_switch_fee:
        return JsonResponse({'status': 'error', 'message': 'already_in_mode'}, status=400)
    if profile.has_paid_mode_switch_fee:
        return JsonResponse({
            'status': 'success',
            'switching_unlocked': True,
            'target_mode': target_mode,
        })

    public_key = getattr(settings, 'PAYSTACK_PUBLIC_KEY', '').strip()
    if not _paystack_keys_configured():
        logger.error('Mode-switch payment requested with missing or mismatched Paystack keys.')
        return JsonResponse({'status': 'error', 'message': 'payments_unavailable'}, status=503)

    try:
        with transaction.atomic():
            profile = Profile.objects.select_for_update().get(user=request.user)
            if profile.relationship_mode == target_mode:
                return JsonResponse({'status': 'error', 'message': 'already_in_mode'}, status=400)
            if profile.has_paid_mode_switch_fee:
                return JsonResponse({
                    'status': 'success',
                    'switching_unlocked': True,
                    'target_mode': target_mode,
                })
            existing_intent = ModeSwitchPaymentIntent.objects.filter(
                user=request.user,
                completed_at__isnull=True,
                cancelled_at__isnull=True,
                expires_at__gt=timezone.now(),
            ).first()
            if existing_intent:
                return JsonResponse({'status': 'error', 'message': 'payment_intent_in_progress'}, status=409)

            site_config = SiteConfiguration.objects.first()
            fee = site_config.mode_switch_fee if site_config else 200
            intent = ModeSwitchPaymentIntent.objects.create(
                user=request.user,
                reference=str(uuid.uuid4()),
                source_mode=profile.relationship_mode,
                target_mode=target_mode,
                amount_kobo=fee * 100,
                expires_at=timezone.now() + timedelta(minutes=30),
            )
    except Profile.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'profile_required'}, status=400)

    return JsonResponse({
        'status': 'success',
        'switching_unlocked': False,
        'reference': intent.reference,
        'amount_kobo': intent.amount_kobo,
        'email': request.user.email or f'{request.user.username}@loveny.com',
        'public_key': public_key,
        'target_mode': intent.target_mode,
    })


@login_required
@require_POST
def cancel_mode_switch_payment_intent_api(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'status': 'error', 'message': 'invalid_request'}, status=400)
    reference = data.get('reference')
    if not isinstance(reference, str) or not reference.strip():
        return JsonResponse({'status': 'error', 'message': 'reference_required'}, status=400)

    updated = ModeSwitchPaymentIntent.objects.filter(
        reference=reference.strip(),
        user=request.user,
        completed_at__isnull=True,
        cancelled_at__isnull=True,
    ).update(cancelled_at=timezone.now())
    return JsonResponse({'status': 'success', 'cancelled': bool(updated)})


@login_required
@require_POST
def switch_profile_mode_api(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'status': 'error', 'message': 'invalid_request'}, status=400)

    target_mode = data.get('target_mode')
    if target_mode is not None and target_mode not in dict(RELATIONSHIP_MODE_CHOICES):
        return JsonResponse({'status': 'error', 'message': 'invalid_mode'}, status=400)

    reference = data.get('reference')
    if target_mode is not None:
        try:
            with transaction.atomic():
                profile = Profile.objects.select_for_update().get(user=request.user)
                if profile.relationship_mode == target_mode:
                    return JsonResponse({'status': 'error', 'message': 'already_in_mode'}, status=400)
                if not profile.has_paid_mode_switch_fee:
                    if not isinstance(reference, str) or not reference.strip():
                        return JsonResponse({
                            'status': 'error',
                            'message': 'mode_switch_payment_required',
                        }, status=402)
                else:
                    _apply_profile_mode_switch(profile, target_mode, request.user)
                    request.session['active_connection_mode'] = target_mode
                    return JsonResponse({
                        'status': 'success',
                        'target_mode': target_mode,
                        'switching_unlocked': True,
                    })
        except Profile.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': 'profile_required'}, status=400)

    if not isinstance(reference, str) or not reference.strip():
        return JsonResponse({'status': 'error', 'message': 'reference_required'}, status=400)
    reference = reference.strip()
    intent = ModeSwitchPaymentIntent.objects.filter(
        reference=reference,
        user=request.user,
        completed_at__isnull=True,
        cancelled_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).first()
    if not intent:
        return JsonResponse({'status': 'error', 'message': 'payment_intent_invalid'}, status=400)
    if target_mode is not None and target_mode != intent.target_mode:
        return JsonResponse({'status': 'error', 'message': 'payment_target_mode_mismatch'}, status=400)

    verified, verification_error, error_status = _verify_paystack_reference(reference)
    if not verified:
        return JsonResponse(
            {'status': 'error', 'message': verification_error},
            status=error_status,
        )
    payment_data = (
        verified.get('data')
        if isinstance(verified, dict) and verified.get('status') is True
        else None
    )
    if (
        not isinstance(payment_data, dict)
        or payment_data.get('status') != 'success'
        or payment_data.get('reference') != reference
        or payment_data.get('currency') != 'NGN'
        or type(payment_data.get('amount')) is not int
        or payment_data['amount'] != intent.amount_kobo
    ):
        return JsonResponse({'status': 'error', 'message': 'payment_not_verified'}, status=400)

    try:
        with transaction.atomic():
            intent = ModeSwitchPaymentIntent.objects.select_for_update().get(
                pk=intent.pk,
                user=request.user,
                completed_at__isnull=True,
                cancelled_at__isnull=True,
                expires_at__gt=timezone.now(),
            )
            profile = Profile.objects.select_for_update().get(user=request.user)
            if profile.relationship_mode != intent.source_mode:
                return JsonResponse({'status': 'error', 'message': 'profile_mode_changed'}, status=409)

            PaymentTransaction.objects.create(
                user=request.user,
                reference=reference,
                provider='paystack',
                product='MODE_SWITCH',
                plan_type=f'{intent.source_mode}_TO_{intent.target_mode}',
                amount_kobo=intent.amount_kobo,
                currency='NGN',
                verified_at=timezone.now(),
            )

            _apply_profile_mode_switch(profile, intent.target_mode, request.user)
            profile.has_paid_mode_switch_fee = True
            profile.save(update_fields=['has_paid_mode_switch_fee'])
            intent.completed_at = timezone.now()
            intent.save(update_fields=['completed_at'])
    except (ModeSwitchPaymentIntent.DoesNotExist, Profile.DoesNotExist):
        return JsonResponse({'status': 'error', 'message': 'payment_intent_invalid'}, status=409)
    except IntegrityError:
        return JsonResponse({'status': 'error', 'message': 'payment_reference_already_used'}, status=409)

    request.session['active_connection_mode'] = intent.target_mode
    return JsonResponse({
        'status': 'success',
        'target_mode': intent.target_mode,
        'switching_unlocked': True,
    })

@login_required
def blocked_users_view(request):
    blocks = UserBlock.objects.filter(blocker=request.user).select_related('blocked', 'blocked__profile')
    
    if request.method == 'POST':
        # Handle unblocking
        target_id = request.POST.get('unblock_id')
        if target_id:
            UserBlock.objects.filter(blocker=request.user, blocked_id=target_id).delete()
            return redirect('blocked_users')

    return render(request, 'dating/blocked_users.html', {'blocks': blocks})


@login_required
@require_GET
def global_notifications_api(request):
    last_id_str = request.GET.get('last_id', '0')
    try:
        last_id = max(0, int(last_id_str))
    except (TypeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'invalid_cursor'}, status=400)

    profile = Profile.objects.filter(user=request.user).first()
    user_mode = profile.relationship_mode if profile else 'DATING'
    active_conversations = _active_mode_conversations_for_user(request.user, profile)

    new_messages = []
    if last_id > 0:
        new_messages = list(
            ChatMessage.objects.filter(
                conversation__in=active_conversations,
                id__gt=last_id,
            )
            .exclude(sender=request.user)
            .exclude(notification_delivered_to=request.user)
            .select_related('sender', 'conversation')
            .order_by('id')[:10]
        )

    messages_data = [{
        'id': msg.id,
        'text': msg.text,
        'message_type': msg.message_type,
        'sender_name': msg.sender.first_name or msg.sender.username,
        'sender_avatar': (
            _profile_payload(msg.sender.profile).get('image_url')
            if hasattr(msg.sender, 'profile') else None
        ),
        'conversation_url': reverse('conversation_room', args=[msg.conversation_id]),
    } for msg in new_messages]

    unread_count = ChatMessage.objects.filter(
        conversation__in=active_conversations,
    ).exclude(sender=request.user).exclude(read_by=request.user).count()

    max_id = last_id
    if messages_data:
        max_id = messages_data[-1]['id']
    elif last_id == 0:
        latest_message = ChatMessage.objects.filter(
            conversation__in=active_conversations,
        ).order_by('-id').first()
        if latest_message:
            max_id = latest_message.id

    incoming_hookup_requests = []
    if profile and user_mode == 'HOOKUP':
        incoming_hookup_requests = list(
            HookupMatch.objects.filter(
                target=request.user,
                target_notification_delivered_at__isnull=True,
                status='pending',
                initiator__profile__relationship_mode='HOOKUP',
            )
            .select_related('initiator')
            .order_by('-created_at')[:5]
        )

    now = timezone.now()
    incoming_likes = list(
        Swipe.objects.filter(
            swiped=request.user,
            type='LIKE',
            mode=user_mode,
            timestamp__gte=now - timedelta(minutes=15),
        )
        .select_related('swiper')
        .order_by('-timestamp')[:10]
    )
    active_match_user_ids = set(
        Match.objects.filter(
            Q(user1=request.user) | Q(user2=request.user),
            mode=user_mode,
            expires_at__gt=now,
        ).values_list('user1_id', 'user2_id')
    )
    connected_user_ids = {
        second_id if first_id == request.user.pk else first_id
        for first_id, second_id in active_match_user_ids
    }

    return JsonResponse({
        'status': 'success',
        'unread_count': unread_count,
        'new_messages': messages_data,
        'last_id': max_id,
        'coin_balance': profile.coin_balance if profile else 0,
        'diamond_balance': profile.earned_diamonds if profile else 0,
        'user_mode': user_mode,
        'incoming_hookup_requests': [{
            'id': str(match.pk),
            'initiator_name': match.initiator.first_name or match.initiator.username,
        } for match in incoming_hookup_requests],
        'incoming_social_events': [{
            'id': swipe.pk,
            'type': 'connection' if swipe.swiper_id in connected_user_ids else 'follow',
            'sender_name': swipe.swiper.first_name or swipe.swiper.username,
        } for swipe in incoming_likes],
    })


@login_required
@require_POST
def acknowledge_notifications_api(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'status': 'error', 'message': 'invalid_request'}, status=400)

    message_ids = data.get('message_ids', [])
    hookup_request_ids = data.get('hookup_request_ids', [])
    if (
        not isinstance(message_ids, list)
        or not isinstance(hookup_request_ids, list)
        or len(message_ids) > 100
        or len(hookup_request_ids) > 100
    ):
        return JsonResponse({'status': 'error', 'message': 'invalid_notification_ids'}, status=400)

    try:
        parsed_message_ids = {int(message_id) for message_id in message_ids}
    except (TypeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'invalid_message_ids'}, status=400)
    if any(message_id < 1 for message_id in parsed_message_ids):
        return JsonResponse({'status': 'error', 'message': 'invalid_message_ids'}, status=400)

    try:
        parsed_hookup_ids = {uuid.UUID(str(request_id)) for request_id in hookup_request_ids}
    except (TypeError, ValueError, AttributeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_hookup_request_ids'}, status=400)

    profile = Profile.objects.filter(user=request.user).first()
    active_conversations = _active_mode_conversations_for_user(request.user, profile)
    messages = ChatMessage.objects.filter(
        pk__in=parsed_message_ids,
        conversation__in=active_conversations,
    ).exclude(sender=request.user)
    eligible_message_ids = list(messages.values_list('pk', flat=True))
    message_through = ChatMessage.notification_delivered_to.through
    with transaction.atomic():
        message_through.objects.bulk_create(
            [
                message_through(chatmessage_id=message_id, user_id=request.user.pk)
                for message_id in eligible_message_ids
            ],
            ignore_conflicts=True,
        )

        hookup_requests_updated = 0
        if profile and profile.relationship_mode == 'HOOKUP' and parsed_hookup_ids:
            hookup_requests_updated = HookupMatch.objects.filter(
                pk__in=parsed_hookup_ids,
                target=request.user,
                target_notification_delivered_at__isnull=True,
                status='pending',
                initiator__profile__relationship_mode='HOOKUP',
            ).update(target_notification_delivered_at=timezone.now())

    return JsonResponse({
        'status': 'success',
        'messages_acknowledged': len(eligible_message_ids),
        'hookup_requests_acknowledged': hookup_requests_updated,
    })
