import hashlib
import hmac
import json
import logging
import math
import random
import smtplib
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from django.shortcuts import render, redirect, get_object_or_404
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login, logout, views as auth_views
from django.db import IntegrityError, transaction
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery
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
    CallGift,
    CallSession,
    CallSignal,
    ChatMessage,
    CoinPackage,
    CoinTransaction,
    CoinWallet,
    Conversation,
    GiftItem,
    HookupMatch,
    HookupMessage,
    Match,
    PaymentTransaction,
    Profile,
    ProfilePhoto,
    SiteConfiguration,
    SubscriptionPlan,
    Swipe,
    Tag,
    UserBlock,
    UserReport,
)
from .services.content_filter import ContentFilter
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

    is_sex_call_mode = str(current_profile.relationship_mode).upper() in ('SEX_CALL', 'SEXCALL')
    if is_sex_call_mode:
        target_gender = 'F' if current_profile.gender == 'M' else ('M' if current_profile.gender == 'F' else None)
    else:
        target_gender = current_profile.preferred_gender or ('F' if current_profile.gender == 'M' else ('M' if current_profile.gender == 'F' else None))

    if include_test_profiles:
        # Staff/admin preview mode:
        # Guarantee seeded test profiles immediately appear without being blocked
        # by location, distance, last_active, show_in_discovery, or swipe history filters.
        qs = Profile.objects.exclude(user=user).filter(
            Q(is_test_profile=True)
            | Q(user__username__startswith='test_')
            | Q(user__username__startswith='testuser_')
        )
        if target_gender and qs.filter(gender=target_gender).exists():
            qs = qs.filter(gender=target_gender)

        if is_sex_call_mode:
            sc_qs = qs.filter(Q(relationship_mode__iexact='sex_call') | Q(relationship_mode='SEX_CALL'))
            if sc_qs.exists():
                return sc_qs.select_related('user').prefetch_related('photos', 'tags')
        elif current_profile and current_profile.relationship_mode:
            mode_qs = qs.filter(relationship_mode__iexact=str(current_profile.relationship_mode))
            if mode_qs.exists():
                return mode_qs.select_related('user').prefetch_related('photos', 'tags')
        return qs.select_related('user').prefetch_related('photos', 'tags')

    user_age = current_profile.age or 22
    if user_age < 18:
        user_age = 22

    active_limit = timezone.now() - timedelta(days=60)
    swiped_ids = Swipe.objects.filter(swiper=user).values('swiped_id')
    filters = {
        'is_test_profile': False,
        'user__is_active': True,
        'last_active__gte': active_limit,
        'incognito_mode': False,
    }
    if not is_sex_call_mode:
        filters['relationship_mode'] = current_profile.relationship_mode
    if target_gender:
        filters['gender'] = target_gender
    if not is_sex_call_mode and current_profile.gender in ('M', 'F'):
        filters['preferred_gender'] = current_profile.gender

    candidates = (
        Profile.objects.exclude(user=user)
        .exclude(user_id__in=Subquery(swiped_ids))
        .filter(**filters)
        .select_related('user')
        .prefetch_related('photos', 'tags')
    )

    if not candidates.exists():
        real_fallback = (
            Profile.objects.exclude(user=user)
            .exclude(user_id__in=Subquery(swiped_ids))
            .filter(is_test_profile=False, user__is_active=True)
            .select_related('user')
            .prefetch_related('photos', 'tags')
        )
        if target_gender and real_fallback.filter(gender=target_gender).exists():
            candidates = real_fallback.filter(gender=target_gender)
        elif real_fallback.exists():
            candidates = real_fallback
        elif is_sex_call_mode:
            test_qs = Profile.objects.exclude(user=user).filter(
                Q(is_test_profile=True)
                | Q(user__username__startswith='test_')
                | Q(user__username__startswith='testuser_')
            )
            if target_gender and test_qs.filter(gender=target_gender).exists():
                test_qs = test_qs.filter(gender=target_gender)
            sc_qs = test_qs.filter(Q(relationship_mode__iexact='sex_call') | Q(relationship_mode='SEX_CALL'))
            if sc_qs.exists():
                return sc_qs.select_related('user').prefetch_related('photos', 'tags')
            return test_qs.select_related('user').prefetch_related('photos', 'tags')

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
        'main_photo': main_photo, 
        'other_photos': other_photos, 
        'is_own_profile': True,
        'is_online': True,
        'numeric_id': numeric_id,
        'following_count': following_count,
        'followers_count': followers_count,
        'has_checked_in_today': has_checked_in_today,
        'customer_service_whatsapp_url': 'https://wa.me/2349130273282?text=Hello%20Loveny%20Support,%20I%20need%20help%20with%20my%20account',
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

    is_sex_call = (
        profile.relationship_mode == 'SEX_CALL'
        or (viewer_profile and viewer_profile.relationship_mode == 'SEX_CALL')
    )
    site_config = SiteConfiguration.get_solo()
    rate_per_minute = getattr(site_config, 'call_rate_per_minute', 20) or 20

    # Chamet / HiiClub Standards Host Indicators
    host_levels = ['⭐ New Star', '🔥 Level 2 Host', '💎 Level 3 Star', '👑 VIP Host']
    host_level = host_levels[(profile.user.id % len(host_levels))]

    flirty_moods = [
        "Looking for fun video chats tonight 💕",
        "Let's vibe on call! Say hi 💬",
        "Free to chat right now, let's talk ✨",
        "Looking for real energy & good laughs 🥂",
        "Ready to connect. Hit instant call! 📹",
    ]
    mood_line = profile.bio if (profile.bio and len(profile.bio) > 10) else flirty_moods[(profile.user.id % len(flirty_moods))]

    languages = ['English', 'Pidgin']
    if profile.user.id % 3 == 0:
        languages.append('Yoruba')
    elif profile.user.id % 3 == 1:
        languages.append('Hausa')
    else:
        languages.append('Igbo')

    # Gift showcase: count real gifts or show standard showcase
    real_gifts = (
        CallGift.objects.filter(receiver=profile.user)
        .values('gift_type')
        .annotate(total=Count('id'))
    )
    real_map = {g['gift_type']: g['total'] for g in real_gifts}
    
    base_seed = (profile.user.id * 7) % 50
    gifts_showcase = [
        {'icon': '🌹', 'name': 'Rose', 'count': real_map.get('rose', 12 + base_seed % 15)},
        {'icon': '💋', 'name': 'Kiss', 'count': real_map.get('kiss', 5 + (base_seed * 2) % 10)},
        {'icon': '🥂', 'name': 'Champagne', 'count': real_map.get('champagne', 2 + base_seed % 6)},
        {'icon': '👑', 'name': 'Crown', 'count': real_map.get('crown', 1 + base_seed % 3)},
        {'icon': '💎', 'name': 'Diamond', 'count': real_map.get('car', 1 + (base_seed % 2))},
    ]

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
        'is_sex_call': is_sex_call,
        'site_config': site_config,
        'rate_per_minute': rate_per_minute,
        'host_level': host_level,
        'mood_line': mood_line,
        'languages': languages,
        'gifts_showcase': gifts_showcase,
        'is_matched': is_matched,
        'response_status': '⚡ Fast Pickup' if (profile.user.id % 2 == 0) else '🟢 Available Now',
        'is_online': profile.last_active >= timezone.now() - timedelta(minutes=15),
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

    if not conversation.messages.exists():
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

            media_limit = 9
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
        'media_limit': 9,
    })

# --- ACTION: Swiping ---
@ensure_csrf_cookie
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
            and (
                request.session.get('staff_test_profile_preview')
                or request.session.get('preview_test_profiles')
            )
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
            'location': profile.location or 'Nearby',
            'first_date_idea': profile.first_date_idea,
            'expires_in': max(0, (m.expires_at - timezone.now()).days),
            'has_direct_interest': m.has_direct_interest,
            'mode': m.mode,
            'is_online': bool(profile.is_host_ready or (profile.last_active and (timezone.now() - profile.last_active).total_seconds() < 1800)),
        })
    return render(request, 'dating/match_list.html', {
        'matches': data,
        'can_video_call': _can_use_sex_call(viewer_profile),
    })

@login_required
def messages_inbox(request):
    viewer_profile = Profile.objects.filter(user=request.user).first()
    if not viewer_profile:
        return redirect('create_profile')

    matches = Match.objects.filter(
        Q(user1=request.user) | Q(user2=request.user),
        expires_at__gt=timezone.now(),
    ).order_by('-created_at')

    for m in matches:
        _get_or_create_conversation(m)

    conversations = (
        Conversation.objects
        .filter(participants=request.user)
        .prefetch_related('participants', 'messages')
        .order_by('-updated_at')
    )

    threads = []
    for conv in conversations:
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
            else:
                last_preview = last_msg.text
        else:
            last_preview = "Tap to open chat..."

        is_online = True if other_profile.is_test_profile else (
            other_profile.is_host_ready or (
                other_profile.last_active and (timezone.now() - other_profile.last_active).total_seconds() < 1800
            )
        )

        threads.append({
            'id': conv.pk,
            'chat_url': reverse('conversation_room', args=[conv.pk]),
            'other_avatar': payload['image_url'],
            'other_username': other_user.username,
            'other_name': other_user.first_name or other_user.username,
            'other_age': other_profile.age,
            'is_online': is_online,
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
    
    swiped_me = Swipe.objects.filter(swiped=request.user, is_like=True).values_list('swiper_id', flat=True)
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

    if not profile:
        return False
    if profile.relationship_mode != 'SEX_CALL':
        return False
    if user and user.is_superuser:
        return True
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

    receiver = User.objects.filter(pk=receiver_id).first()
    if not receiver:
        return JsonResponse({'status': 'error', 'message': 'invalid_receiver'}, status=400)

    receiver_profile = Profile.objects.filter(user=receiver).first()
    if receiver_profile and getattr(receiver_profile, 'is_dnd', False):
        return JsonResponse({
            'status': 'error',
            'message': 'user_dnd',
            'detail': f"{receiver.first_name or receiver.username} is currently on Do Not Disturb."
        }, status=403)

    match = Match.objects.filter(
        Q(user1=request.user, user2_id=receiver_id)
        | Q(user2=request.user, user1_id=receiver_id),
    ).first()

    caller_is_sex_call = bool(
        caller_profile
        and (
            str(caller_profile.relationship_mode).lower() == 'sex_call'
            or caller_profile.is_test_profile
            or getattr(caller_profile, 'is_host_ready', False)
        )
    )
    receiver_is_sex_call = bool(
        receiver_profile
        and (
            str(receiver_profile.relationship_mode).lower() == 'sex_call'
            or receiver_profile.is_test_profile
            or getattr(receiver_profile, 'is_host_ready', False)
        )
    )

    if match:
        if match.expires_at and match.expires_at <= timezone.now():
            return JsonResponse({'status': 'error', 'message': 'active_sex_call_match_required'}, status=403)
    else:
        if not (caller_is_sex_call or receiver_is_sex_call):
            return JsonResponse({'status': 'error', 'message': 'active_sex_call_match_required'}, status=403)

    call = CallSession.objects.create(
        caller=request.user,
        receiver=receiver,
        status='ringing',
        rate_per_minute=min_coins,
    )
    return JsonResponse({
        'status': 'success',
        'room_id': str(call.room_id),
        'call': _call_payload(call),
    }, status=201)


@login_required
@require_GET
def incoming_calls_api(request):
    current_time = timezone.now()
    CallSession.objects.filter(
        receiver=request.user,
        status='ringing',
        created_at__lt=current_time - timedelta(seconds=30),
    ).update(status='missed', ended_at=current_time)
    call = (
        CallSession.objects.filter(
            receiver=request.user,
            status='ringing',
            created_at__gte=current_time - timedelta(seconds=30),
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
        if call.created_at < timezone.now() - timedelta(seconds=30):
            call.status = 'missed'
            call.ended_at = timezone.now()
            call.save(update_fields=['status', 'ended_at'])
            return JsonResponse({'status': 'error', 'message': 'call_missed_or_timeout'}, status=409)
        if action == 'accept':
            caller_profile = Profile.objects.select_for_update().filter(user=call.caller).first()
            if not _can_use_sex_call(caller_profile):
                call.status = 'declined'
                call.ended_at = timezone.now()
                call.save(update_fields=['status', 'ended_at'])
                return JsonResponse({'status': 'error', 'message': 'caller_insufficient_coins'}, status=409)
            call.status = 'connected'
            now = timezone.now()
            call.started_at = now
            call.connected_at = now

            # Billing Starts Only After Pickup:
            # Deduct the first minute's rate upon connection
            site_config = SiteConfiguration.get_solo()
            rate = getattr(call, 'rate_per_minute', site_config.call_rate_per_minute) or site_config.call_rate_per_minute
            is_unlimited = bool(caller_profile and (caller_profile.user.is_superuser or caller_profile.is_sex_call_premium))
            if not is_unlimited and caller_profile:
                deposit = min(caller_profile.coin_balance, rate)
                caller_profile.coin_balance = max(0, caller_profile.coin_balance - deposit)
                caller_profile.save(update_fields=['coin_balance'])
                call.coins_spent = deposit
                CoinTransaction.objects.create(
                    user=call.caller,
                    amount=-deposit,
                    transaction_type='CALL_DEDUCTION',
                    call=call,
                    description=f'Minute 1 video call deposit ({deposit} coins)',
                )
        else:
            call.status = 'declined'
            call.ended_at = timezone.now()
        call.save(update_fields=['status', 'started_at', 'connected_at', 'ended_at', 'coins_spent'])
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
        now = timezone.now()

        # If call is still ringing or initiated, cancel or decline without any coin deduction
        if call.status in ('ringing', 'initiated'):
            call.status = 'cancelled' if request.user == call.caller else 'declined'
            call.ended_at = now
            call.save(update_fields=['status', 'ended_at'])
            return JsonResponse({'status': 'success', 'call': _call_payload(call)})

        if call.status not in ('ended', 'declined', 'missed', 'cancelled'):
            call.status = 'ended'
            call.ended_at = now
            if call.started_at:
                duration = int((call.ended_at - call.started_at).total_seconds())
                call.duration_seconds = max(0, duration)
                caller_profile = Profile.objects.select_for_update().filter(user_id=call.caller_id).first()
                receiver_profile = Profile.objects.select_for_update().filter(user_id=call.receiver_id).first()
                is_unlimited = bool(caller_profile and (caller_profile.user.is_superuser or caller_profile.is_sex_call_premium))

                config = SiteConfiguration.get_solo()
                grace_period = getattr(config, 'grace_period_seconds', 20)
                commission = Decimal(getattr(config, 'host_commission_percentage', 70)) / Decimal(100)
                rate = getattr(call, 'rate_per_minute', config.call_rate_per_minute) or config.call_rate_per_minute

                if call.duration_seconds < grace_period:
                    refund = call.coins_spent
                    if caller_profile and refund > 0:
                        caller_profile.coin_balance += refund
                        caller_profile.save(update_fields=['coin_balance'])
                    CoinTransaction.objects.filter(
                        user=call.caller,
                        transaction_type='CALL_DEDUCTION',
                        call=call,
                    ).delete()
                    CoinTransaction.objects.create(
                        user=call.caller,
                        amount=0,
                        transaction_type='CALL_REFUND',
                        call=call,
                        description=f'Grace period protected: {call.duration_seconds}s < {grace_period}s. 0 coins charged.',
                    )
                    call.coins_spent = 0
                elif not is_unlimited and caller_profile:
                    billed_minutes = max(1, math.ceil(call.duration_seconds / 60))
                    total_due = billed_minutes * rate
                    additional_due = max(0, total_due - call.coins_spent)
                    additional_deducted = min(caller_profile.coin_balance, additional_due)

                    if additional_deducted > 0:
                        caller_profile.coin_balance = max(0, caller_profile.coin_balance - additional_deducted)
                        caller_profile.save(update_fields=['coin_balance'])
                        call.coins_spent += additional_deducted

                    tx = CoinTransaction.objects.filter(
                        user=call.caller,
                        transaction_type='CALL_DEDUCTION',
                        call=call,
                    ).first()
                    if tx:
                        tx.amount = -call.coins_spent
                        tx.description = f'Deducted {call.coins_spent} coins for {call.duration_seconds}s video call ({billed_minutes}m @ {rate}/min)'
                        tx.save(update_fields=['amount', 'description'])
                    else:
                        CoinTransaction.objects.create(
                            user=call.caller,
                            amount=-call.coins_spent,
                            transaction_type='CALL_DEDUCTION',
                            call=call,
                            description=f'Deducted {call.coins_spent} coins for {call.duration_seconds}s video call ({billed_minutes}m @ {rate}/min)',
                        )

                    diamonds_earned = int(Decimal(call.coins_spent) * commission)
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
                elif is_unlimited and receiver_profile:
                    billed_minutes = max(1, math.ceil(call.duration_seconds / 60))
                    simulated_coins = billed_minutes * rate
                    diamonds_earned = int(Decimal(simulated_coins) * commission)
                    if diamonds_earned > 0:
                        receiver_profile.earned_diamonds += diamonds_earned
                        receiver_profile.save(update_fields=['earned_diamonds'])

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
    signal_type = data.get('type') or data.get('signal_type')
    payload = data.get('payload')
    allowed_signals = (
        'offer', 'answer', 'candidate', 'gift', 'chat', 'follow',
        'face_verified', 'privacy_blur_engaged', 'privacy_blur_lifted', 'feeds_unlocked',
    )
    if signal_type not in allowed_signals or not isinstance(payload, dict):
        return JsonResponse({'status': 'error', 'message': 'invalid_signal'}, status=400)
    if len(json.dumps(payload)) > 20000:
        return JsonResponse({'status': 'error', 'message': 'signal_too_large'}, status=400)

    call = get_object_or_404(CallSession.objects.select_related('caller', 'receiver'), room_id=room_id)
    _call_participants(call, request.user)
    if call.status not in ('ringing', 'connected', 'active'):
        return JsonResponse({'status': 'error', 'message': 'call_not_active'}, status=409)
    if signal_type == 'offer' and request.user.pk != call.caller_id:
        return JsonResponse({'status': 'error', 'message': 'caller_must_send_offer'}, status=403)
    if signal_type == 'answer' and (
        request.user.pk != call.receiver_id or call.status not in ('connected', 'ringing')
    ):
        return JsonResponse({'status': 'error', 'message': 'receiver_must_answer_active_call'}, status=403)
    signal = CallSignal.objects.create(
        call=call,
        sender=request.user,
        signal_type=signal_type,
        payload=payload,
    )

    # Mutual pre-call face verification trigger
    if signal_type == 'face_verified':
        peer_id = call.receiver_id if request.user.pk == call.caller_id else call.caller_id
        peer_verified = CallSignal.objects.filter(
            call=call,
            sender_id=peer_id,
            signal_type='face_verified',
        ).exists()
        if peer_verified:
            CallSignal.objects.create(
                call=call,
                sender=request.user,
                signal_type='feeds_unlocked',
                payload={'unlocked': True, 'timestamp': timezone.now().isoformat()},
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
        'call_rate_per_minute': site_config.call_rate_per_minute,
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
        'call_rate_per_minute': cfg.call_rate_per_minute,
        'grace_period_seconds': cfg.grace_period_seconds,
        'host_commission_percentage': cfg.host_commission_percentage,
        'diamond_to_coin_percentage': getattr(cfg, 'diamond_to_coin_percentage', 70),
        'welcome_bonus_coins': cfg.welcome_bonus_coins,
        'daily_checkin_coins': getattr(cfg, 'daily_checkin_coins', 5),
        'day_7_bonus_coins': getattr(cfg, 'day_7_bonus_coins', 10),
        'day_7_bonus_diamonds': getattr(cfg, 'day_7_bonus_diamonds', 20),
        'announcement_banner': cfg.announcement_banner if cfg.is_announcement_active else '',
        'is_announcement_active': cfg.is_announcement_active,
        'mode_switch_fee': cfg.mode_switch_fee,
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

    base_qs = Profile.objects.select_related('user').prefetch_related('photos')
    current_profile = None
    target_gender = None
    if request.user.is_authenticated:
        base_qs = base_qs.exclude(user=request.user)
        current_profile = Profile.objects.filter(user=request.user).first()
        user_gender = getattr(current_profile, 'gender', None) if current_profile else None
        if user_gender == 'M':
            target_gender = 'F'
        elif user_gender == 'F':
            target_gender = 'M'

    if include_test_profiles:
        test_qs = base_qs.filter(Q(is_test_profile=True) | Q(user__username__startswith='test_user_'))
        if target_gender and test_qs.filter(gender=target_gender).exists():
            test_qs = test_qs.filter(gender=target_gender)
        sc_qs = test_qs.filter(Q(relationship_mode__iexact='sex_call') | Q(relationship_mode='SEX_CALL'))
        hosts = list((sc_qs if sc_qs.exists() else (test_qs if test_qs.exists() else base_qs))[:24])
    else:
        real_qs = base_qs.filter(is_test_profile=False, user__is_active=True)
        if real_qs.exists():
            if target_gender and real_qs.filter(gender=target_gender).exists():
                matched_real = real_qs.filter(gender=target_gender)
            else:
                matched_real = real_qs
            hosts = list(matched_real.order_by('-is_host_ready', '-last_active')[:24])
            if len(hosts) < 8:
                test_qs = base_qs.filter(
                    Q(is_test_profile=True)
                    | Q(user__username__startswith='test_')
                    | Q(user__username__startswith='testuser_')
                )
                if target_gender and test_qs.filter(gender=target_gender).exists():
                    test_qs = test_qs.filter(gender=target_gender)
                hosts.extend(list(test_qs[:(8 - len(hosts))]))
        else:
            test_qs = base_qs.filter(
                Q(is_test_profile=True)
                | Q(user__username__startswith='test_')
                | Q(user__username__startswith='testuser_')
            )
            if target_gender and test_qs.filter(gender=target_gender).exists():
                test_qs = test_qs.filter(gender=target_gender)
            sc_qs = test_qs.filter(Q(relationship_mode__iexact='sex_call') | Q(relationship_mode='SEX_CALL'))
            hosts = list((sc_qs if sc_qs.exists() else (test_qs if test_qs.exists() else base_qs))[:24])

    results = []
    for p in hosts:
        is_active = True if p.is_test_profile else (p.is_host_ready or (p.last_active and (timezone.now() - p.last_active).total_seconds() < 1800))
        photo = p.photos.filter(is_main=True).first() or p.photos.first()
        avatar = ''
        if photo and photo.image:
            try:
                avatar = photo.image.url
            except Exception:
                pass
        if not avatar:
            avatar = f"https://api.dicebear.com/7.x/avataaars/svg?seed={p.user.username}"
        loc = (p.location.strip() if p.location else '') or 'Lekki, Lagos'
        results.append({
            'user_id': p.user_id,
            'username': p.user.username,
            'name': p.user.first_name or p.user.username,
            'age': p.age or 22,
            'gender': p.get_gender_display() if hasattr(p, 'get_gender_display') else p.gender,
            'city': loc,
            'avatar': avatar,
            'is_host_ready': getattr(p, 'is_host_ready', True),
            'is_online': is_active,
            'response_rate': getattr(p, 'response_rate', 98),
            'total_calls': getattr(p, 'total_calls_completed', 0),
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
        "Are you free for a call? 📹",
    ]

    return {
        'conversation': conversation,
        'match': match,
        'other_user': other_user,
        'other_profile': other_profile,
        'other_avatar': other_avatar,
        'is_other_online': is_online,
        'current_profile': current_profile,
        'can_video_call': _can_use_sex_call(current_profile),
        'site_config': site_config,
        'call_rate_per_minute': site_config.call_rate_per_minute,
        'user_coin_balance': current_profile.coin_balance if current_profile else 100,
        'gifts': gift_list,
        'sticker_packs': STICKER_PACKS,
        'languages': languages,
        'quick_icebreakers': quick_icebreakers,
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

    # Automated Contact-Info Leak Filter: Prevent chunking across recent messages
    recent_qs = conversation.messages.filter(
        sender=request.user,
        created_at__gte=timezone.now() - timedelta(minutes=3),
    ).order_by('-created_at')[:20]
    recent_texts = [m.text for m in reversed(recent_qs) if m.text]
    combined_text = " ".join(recent_texts) + " " + text

    is_allowed, warning_error = ContentFilter.inspect_and_record(request.user, combined_text)
    if not is_allowed:
        return JsonResponse({
            'status': 'error',
            'message': 'prohibited_content',
            'detail': warning_error,
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
@ensure_csrf_cookie
def index(request): return render(request, 'dating/landing.html')

@ensure_csrf_cookie
def signup(request):
    if request.user.is_authenticated:
        return redirect('swipe_card')
    form = SignUpForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        mode = form.cleaned_data['relationship_mode']
        user = form.save()
        login(request, user, backend='dating.backends.EmailAuthBackend')
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
    existing_profile = Profile.objects.filter(user=request.user).first()
    if existing_profile and existing_profile.age and existing_profile.age >= 18 and existing_profile.photos.exists():
        if existing_profile.relationship_mode == 'SEX_CALL':
            return redirect('sex_call_hub')
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
        p = form.save(commit=False)
        p.user = request.user
        if form.cleaned_data.get('name'):
            request.user.first_name = form.cleaned_data['name']
            request.user.save(update_fields=['first_name'])
        chosen_mode = form.cleaned_data.get('relationship_mode') or request.session.get('active_connection_mode', 'DATING')
        p.relationship_mode = chosen_mode
        p.save()
        form.save_m2m()
        request.session['active_connection_mode'] = p.relationship_mode
        if request.FILES.get('photo'):
            ProfilePhoto.objects.create(profile=p, user=p.user, mode=p.relationship_mode, image=request.FILES.get('photo'), is_main=True)
        if p.relationship_mode == 'SEX_CALL':
            return redirect('sex_call_hub')
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
    
    site_config = SiteConfiguration.objects.first()
    return render(request, 'dating/settings.html', {
        'form': form, 
        'profile': profile,
        'site_config': site_config,
        'paystack_public_key': settings.PAYSTACK_PUBLIC_KEY
    })

def privacy(request): return render(request, 'dating/privacy.html')
def terms(request): return render(request, 'dating/terms.html')
def contact(request):
    return render(request, 'dating/contact.html')


# --- SEX CALL DISCOVERY & HOST GRID ---
@ensure_csrf_cookie
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
        profile, _ = Profile.objects.get_or_create(
            user=request.user,
            defaults={'relationship_mode': 'SEX_CALL', 'is_host_ready': True, 'age': 22, 'gender': 'O', 'location': 'Lagos, Nigeria'}
        )

    profile.last_active = timezone.now()
    if profile.relationship_mode != 'SEX_CALL':
        profile.relationship_mode = 'SEX_CALL'
        request.session['active_connection_mode'] = 'SEX_CALL'
        profile.save(update_fields=['last_active', 'relationship_mode'])
    else:
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

    base_qs = Profile.objects.exclude(user=request.user)
    user_gender = getattr(current_profile, 'gender', None) if current_profile else None
    target_gender = 'F' if user_gender == 'M' else ('M' if user_gender == 'F' else None)

    if include_test_profiles:
        test_qs = base_qs.filter(
            Q(is_test_profile=True)
            | Q(user__username__startswith='test_')
            | Q(user__username__startswith='testuser_')
        )
        if target_gender and test_qs.filter(gender=target_gender).exists():
            test_qs = test_qs.filter(gender=target_gender)
        sc_test = test_qs.filter(Q(relationship_mode__iexact='sex_call') | Q(relationship_mode='SEX_CALL'))
        hosts_list = list((sc_test if sc_test.exists() else (test_qs if test_qs.exists() else base_qs)).select_related('user').prefetch_related('photos')[:50])
    else:
        real_qs = base_qs.filter(is_test_profile=False, user__is_active=True)
        if real_qs.exists():
            if target_gender and real_qs.filter(gender=target_gender).exists():
                matched_real = real_qs.filter(gender=target_gender)
            else:
                matched_real = real_qs
            hosts_list = list(
                matched_real.select_related('user').prefetch_related('photos')
                .order_by('-is_host_ready', '-last_active')[:50]
            )
            if len(hosts_list) < 12:
                test_qs = base_qs.filter(
                    Q(is_test_profile=True)
                    | Q(user__username__startswith='test_')
                    | Q(user__username__startswith='testuser_')
                )
                if target_gender and test_qs.filter(gender=target_gender).exists():
                    test_qs = test_qs.filter(gender=target_gender)
                sc_test = test_qs.filter(Q(relationship_mode__iexact='sex_call') | Q(relationship_mode='SEX_CALL'))
                supplement_qs = sc_test if sc_test.exists() else test_qs
                supplement = list(supplement_qs.select_related('user').prefetch_related('photos')[:(12 - len(hosts_list))])
                hosts_list.extend(supplement)
        else:
            test_qs = base_qs.filter(
                Q(is_test_profile=True)
                | Q(user__username__startswith='test_')
                | Q(user__username__startswith='testuser_')
            )
            if target_gender and test_qs.filter(gender=target_gender).exists():
                test_qs = test_qs.filter(gender=target_gender)
            sc_test = test_qs.filter(Q(relationship_mode__iexact='sex_call') | Q(relationship_mode='SEX_CALL'))
            hosts_list = list((sc_test if sc_test.exists() else (test_qs if test_qs.exists() else base_qs)).select_related('user').prefetch_related('photos')[:50])

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
        loc = (h.location.strip() if h.location else '') or 'Lekki, Lagos'
        data.append({
            'id': h.user_id,
            'username': h.user.username,
            'age': h.age or 22,
            'location': loc,
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


# =====================================================================
# HOOKUP MODE & DUAL-FIAT ESCROW PAYMENT BACKEND
# =====================================================================

@csrf_exempt
@require_POST
def paystack_webhook_api(request):
    """
    Paystack Webhook Listener for real-time payment confirmation.
    Validates HMAC SHA512 signature against PAYSTACK_SECRET_KEY.
    Unlocks HookupMatch when both participants pay their dual-fiat connection fee.
    """
    secret = getattr(settings, 'PAYSTACK_SECRET_KEY', '')
    signature = request.headers.get('x-paystack-signature') or request.META.get('HTTP_X_PAYSTACK_SIGNATURE', '')

    if secret:
        computed_hash = hmac.new(secret.encode('utf-8'), request.body, hashlib.sha512).hexdigest()
        if not hmac.compare_digest(computed_hash, signature):
            return HttpResponse(status=400)

    try:
        event_data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponseBadRequest("Invalid JSON")

    event = event_data.get('event')
    data = event_data.get('data', {})

    if event == 'charge.success':
        reference = data.get('reference', '')
        amount_kobo = data.get('amount', 0)
        metadata = data.get('metadata') or {}
        customer_email = data.get('customer', {}).get('email', '')

        # Check for Hookup Match payment
        match_id = metadata.get('match_id')
        user_id = metadata.get('user_id')

        if match_id:
            hookup_match = HookupMatch.objects.filter(pk=match_id).first()
            if hookup_match:
                paying_user = None
                if user_id:
                    paying_user = User.objects.filter(pk=user_id).first()
                if not paying_user and customer_email:
                    paying_user = User.objects.filter(email__iexact=customer_email).first()

                if paying_user:
                    with transaction.atomic():
                        PaymentTransaction.objects.get_or_create(
                            reference=reference,
                            defaults={
                                'user': paying_user,
                                'provider': 'paystack',
                                'product': 'HOOKUP_MATCH',
                                'plan_type': 'hookup_connection_fee',
                                'amount_kobo': amount_kobo,
                                'currency': 'NGN',
                            }
                        )

                        if paying_user.pk == hookup_match.initiator_id:
                            hookup_match.initiator_paid = True
                        elif paying_user.pk == hookup_match.target_id:
                            hookup_match.target_paid = True

                        if hookup_match.is_fully_paid():
                            hookup_match.status = 'unlocked'
                            hookup_match.unlocked_at = timezone.now()
                            hookup_match.expires_at = timezone.now() + timedelta(hours=24)
                        else:
                            if hookup_match.status == 'pending':
                                hookup_match.status = 'accepted'
                        hookup_match.save()

    return HttpResponse(status=200)


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

    if profile.relationship_mode != 'HOOKUP':
        profile.relationship_mode = 'HOOKUP'
        request.session['active_connection_mode'] = 'HOOKUP'
        profile.save(update_fields=['relationship_mode', 'last_active'])

    now = timezone.now()

    # Get blocked user IDs
    blocked_by_me = UserBlock.objects.filter(blocker=request.user).values_list('blocked_id', flat=True)
    blocking_me = UserBlock.objects.filter(blocked=request.user).values_list('blocker_id', flat=True)
    blocked_ids = set(blocked_by_me).union(set(blocking_me))

    # Available profiles for hookup
    base_qs = Profile.objects.exclude(user=request.user).exclude(user_id__in=blocked_ids).filter(show_in_discovery=True, incognito_mode=False)
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

    # Inbox: Incoming pending requests
    pending_incoming = list(
        HookupMatch.objects.filter(target=request.user, status='pending')
        .select_related('initiator', 'initiator__profile')
        .prefetch_related('initiator__profile__photos')
    )

    # Outgoing pending requests
    pending_outgoing = list(
        HookupMatch.objects.filter(initiator=request.user, status='pending')
        .select_related('target', 'target__profile')
        .prefetch_related('target__profile__photos')
    )

    # Accepted / Escrow payment stage matches
    accepted_matches = list(
        HookupMatch.objects.filter(
            Q(initiator=request.user) | Q(target=request.user),
            status='accepted'
        ).select_related('initiator', 'target', 'initiator__profile', 'target__profile')
    )
    for m in accepted_matches:
        m.is_initiator = (m.initiator_id == request.user.id)
        m.has_paid = m.initiator_paid if m.is_initiator else m.target_paid

    # Active unlocked 24-hr chats
    active_unlocked = list(
        HookupMatch.objects.filter(
            Q(initiator=request.user) | Q(target=request.user),
            status='unlocked',
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
        'candidates': available_candidates,
        'pending_incoming': pending_incoming,
        'pending_outgoing': pending_outgoing,
        'accepted_matches': accepted_matches,
        'active_unlocked': active_unlocked,
        'paystack_public_key': getattr(settings, 'PAYSTACK_PUBLIC_KEY', ''),
        'connection_fee': SiteConfiguration.get_solo().hookup_connection_fee,
    })


@login_required
@require_POST
def hookup_request_api(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)

    target_id = data.get('target_id')
    if not target_id or target_id == request.user.pk:
        return JsonResponse({'status': 'error', 'message': 'invalid_target'}, status=400)

    target_user = get_object_or_404(User, pk=target_id)
    target_profile = Profile.objects.filter(user=target_user).first()
    if not target_profile:
        return JsonResponse({'status': 'error', 'message': 'profile_not_found'}, status=404)

    # Check for existing active/pending match
    existing = HookupMatch.objects.filter(
        Q(initiator=request.user, target=target_user) |
        Q(initiator=target_user, target=request.user)
    ).exclude(status__in=('declined', 'expired')).first()

    if existing:
        return JsonResponse({
            'status': 'exists',
            'match_id': str(existing.id),
            'match_status': existing.status,
            'message': 'A connection request already exists between you and this user.',
        })

    match = HookupMatch.objects.create(
        initiator=request.user,
        target=target_user,
        status='pending',
        connection_fee=SiteConfiguration.get_solo().hookup_connection_fee,
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
    if request.user not in (match.initiator, match.target):
        return HttpResponseForbidden("Not authorized")

    if action == 'accept':
        match.status = 'accepted'
    elif action == 'decline':
        match.status = 'declined'
    match.save(update_fields=['status'])

    return JsonResponse({
        'status': 'success',
        'match_id': str(match.id),
        'match_status': match.status,
    })


@login_required
@require_POST
def hookup_initialize_payment_api(request, match_id):
    match = get_object_or_404(HookupMatch, pk=match_id)
    if request.user not in (match.initiator, match.target):
        return HttpResponseForbidden("Not authorized")

    fee = match.connection_fee or SiteConfiguration.get_solo().hookup_connection_fee
    amount_kobo = fee * 100
    ref = f"HOOKUP_{match.id}_{request.user.id}_{int(timezone.now().timestamp())}"

    return JsonResponse({
        'status': 'success',
        'match_id': str(match.id),
        'amount': fee,
        'amount_kobo': amount_kobo,
        'reference': ref,
        'email': request.user.email or f"{request.user.username}@loveny.com",
        'public_key': getattr(settings, 'PAYSTACK_PUBLIC_KEY', ''),
        'flutterwave_public_key': getattr(settings, 'FLUTTERWAVE_PUBLIC_KEY', ''),
        'metadata': {
            'match_id': str(match.id),
            'user_id': request.user.id,
            'product': 'HOOKUP_MATCH',
        }
    })


@login_required
@require_POST
def hookup_verify_payment_api(request, match_id):
    match = get_object_or_404(HookupMatch, pk=match_id)
    if request.user not in (match.initiator, match.target):
        return HttpResponseForbidden("Not authorized")

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = {}

    reference = str(data.get('reference', '')).strip()
    provider = str(data.get('provider', 'paystack')).strip()
    if not reference:
        return JsonResponse({'status': 'error', 'message': 'reference_required'}, status=400)

    with transaction.atomic():
        PaymentTransaction.objects.get_or_create(
            reference=reference,
            defaults={
                'user': request.user,
                'provider': provider,
                'product': 'HOOKUP_MATCH',
                'plan_type': 'hookup_connection_fee',
                'amount_kobo': (match.connection_fee or SiteConfiguration.get_solo().hookup_connection_fee) * 100,
                'currency': 'NGN',
            }
        )

        if request.user.pk == match.initiator_id:
            match.initiator_paid = True
        elif request.user.pk == match.target_id:
            match.target_paid = True

        if match.is_fully_paid():
            match.status = 'unlocked'
            match.unlocked_at = timezone.now()
            match.expires_at = timezone.now() + timedelta(hours=24)
        else:
            match.status = 'accepted'
        match.save()

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
    is_expired = match.status == 'expired' or (match.expires_at and match.expires_at <= timezone.now())

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

    # Automated Contact-Info Leak Filter: Prevent chunking across recent messages
    recent_qs = match.messages.filter(
        sender=request.user,
        created_at__gte=timezone.now() - timedelta(minutes=3),
    ).order_by('-created_at')[:20]
    recent_texts = [m.text for m in reversed(recent_qs) if m.text]
    combined_text = " ".join(recent_texts) + " " + text

    is_allowed, warning_error = ContentFilter.inspect_and_record(request.user, combined_text)
    if not is_allowed:
        return JsonResponse({
            'status': 'error',
            'message': 'prohibited_content',
            'detail': warning_error,
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
    try:
        last_id_str = request.GET.get('last_id', '0')
        last_id = int(last_id_str) if last_id_str.isdigit() else 0
        
        # Only return new messages if last_id > 0, otherwise just find max_id
        if last_id > 0:
            new_messages = ChatMessage.objects.filter(
                conversation__participants=request.user,
                id__gt=last_id
            ).exclude(sender=request.user).order_by('id')[:10]
        else:
            new_messages = []
        
        messages_data = []
        for msg in new_messages:
            messages_data.append({
                'id': msg.id,
                'text': msg.text,
                'message_type': msg.message_type,
                'sender_name': msg.sender.first_name or msg.sender.username,
                'sender_avatar': _profile_payload(msg.sender.profile).get('image_url') if hasattr(msg.sender, 'profile') else None,
                'conversation_url': reverse('conversation_room', args=[msg.conversation.id])
            })
            
        unread_count = ChatMessage.objects.filter(
            conversation__participants=request.user
        ).exclude(sender=request.user).exclude(read_by=request.user).count()
        
        max_id = last_id
        if messages_data:
            max_id = messages_data[-1]['id']
        elif last_id == 0:
            latest_msg = ChatMessage.objects.filter(
                conversation__participants=request.user
            ).order_by('-id').first()
            if latest_msg:
                max_id = latest_msg.id
            
        profile = request.user.profile if hasattr(request.user, 'profile') else None
        coin_balance = profile.coin_balance if profile else 0
        diamond_balance = profile.earned_diamonds if profile else 0
            
        return JsonResponse({
            'status': 'success',
            'unread_count': unread_count,
            'new_messages': messages_data,
            'last_id': max_id,
            'coin_balance': coin_balance,
            'diamond_balance': diamond_balance
        })
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
import json
import requests
from django.conf import settings
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from dating.models import ProfileStateBackup, ProfilePhoto, SiteConfiguration, RELATIONSHIP_MODE_CHOICES

@login_required
@require_POST
def switch_profile_mode_api(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid_json'}, status=400)
    
    target_mode = data.get('target_mode')
    reference = data.get('reference')
    if target_mode not in dict(RELATIONSHIP_MODE_CHOICES).keys():
        return JsonResponse({'status': 'error', 'message': 'invalid_mode'}, status=400)
        
    profile = request.user.profile
    if profile.relationship_mode == target_mode:
        return JsonResponse({'status': 'error', 'message': 'already_in_mode'}, status=400)
        
    # Verify Payment if reference provided
    if reference:
        url = f"https://api.paystack.co/transaction/verify/{reference}"
        headers = {
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"
        }
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            res_data = response.json()
            if not res_data.get('status') or res_data['data']['status'] != 'success':
                return JsonResponse({"status": "error", "message": "Payment failed or incomplete"}, status=400)
            
            # verify amount
            expected_amount = SiteConfiguration.objects.first().mode_switch_fee * 100
            if res_data['data']['amount'] < expected_amount:
                return JsonResponse({"status": "error", "message": "Insufficient payment"}, status=400)
        else:
            return JsonResponse({"status": "error", "message": "Verification failed"}, status=400)
            
    # Perform Mode Switch (save current to backup, restore from backup if exists, update photos)
    current_mode = profile.relationship_mode
    
    # 1. Backup current mode
    backup, _ = ProfileStateBackup.objects.get_or_create(user=request.user, mode=current_mode)
    backup.bio = profile.bio
    backup.first_date_idea = profile.first_date_idea
    backup.job_title = profile.job_title
    backup.save()
    backup.tags.set(profile.tags.all())
    
    # 2. Detach current photos (they retain user and mode)
    profile.photos.all().update(profile=None)
    
    # 3. Restore target mode from backup if it exists
    target_backup = ProfileStateBackup.objects.filter(user=request.user, mode=target_mode).first()
    if target_backup:
        profile.bio = target_backup.bio
        profile.first_date_idea = target_backup.first_date_idea
        profile.job_title = target_backup.job_title
        profile.save()
        profile.tags.set(target_backup.tags.all())
    else:
        # Defaults if new
        profile.bio = ''
        profile.first_date_idea = ''
        profile.job_title = ''
        profile.save()
        profile.tags.clear()
        
    profile.relationship_mode = target_mode
    profile.save()
    
    # 4. Attach photos for target mode
    ProfilePhoto.objects.filter(user=request.user, mode=target_mode).update(profile=profile)
    
    request.session['active_connection_mode'] = target_mode
    
    return JsonResponse({'status': 'success'})

