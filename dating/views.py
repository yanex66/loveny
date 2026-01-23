import random
import json
import os
from datetime import timedelta
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponseBadRequest
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth import login
from django.db.models import Q, Subquery
from django.utils import timezone
from django.urls import reverse
from django.contrib.auth.models import User
from django.conf import settings
from .models import Profile, Swipe, Match, SWIPE_CHOICES, ProfilePhoto
from .forms import SignUpForm, ProfileForm, SettingsForm

# --- HELPER: Get Batch of Profiles ---
def get_profile_batch(user, limit=10):
    try:
        current_profile = user.profile
    except Profile.DoesNotExist:
        return Profile.objects.none()

    swiped_ids = Swipe.objects.filter(swiper=user).values('swiped_id')
    active_limit = timezone.now() - timedelta(days=60)
    
    candidates = Profile.objects.exclude(user=user) \
                                .exclude(user_id__in=Subquery(swiped_ids)) \
                                .filter(
                                    gender=current_profile.preferred_gender,
                                    age__gte=current_profile.min_age_pref,
                                    age__lte=current_profile.max_age_pref,
                                    last_active__gte=active_limit
                                ) \
                                .select_related('user') \
                                .prefetch_related('photos', 'tags') \
                                .order_by('?')[:limit]

    return candidates

# --- API: Get Profiles JSON (Updated with Online Status) ---
@login_required
def get_profiles_json(request):
    profiles = get_profile_batch(request.user, limit=10)
    data = []
    
    # Define "Online" as active within the last 10 minutes
    online_threshold = timezone.now() - timedelta(minutes=10)
    
    for profile in profiles:
        photo_obj = profile.photos.filter(is_main=True).first() or profile.photos.first()
        
        if photo_obj:
            try:
                media_url = photo_obj.image.url
                is_video = photo_obj.is_video
            except Exception:
                media_url = "https://placehold.co/600x800/F9A8D4/ffffff?text=Error"
                is_video = False
        else:
            media_url = "https://placehold.co/600x800/F9A8D4/ffffff?text=No+Photo"
            is_video = False
            
        tags = [t.name for t in profile.tags.all()[:3]]
        
        # Calculate Online Status
        is_online = False
        if profile.last_active and profile.last_active >= online_threshold:
            is_online = True
        
        data.append({
            'id': profile.user.id,
            'username': profile.user.username,
            'age': profile.age if profile.age else '??',
            'location': profile.location,
            'job_title': profile.job_title,
            'bio': profile.bio[:100] + '...' if len(profile.bio) > 100 else profile.bio,
            'first_date_idea': profile.first_date_idea,
            'image_url': media_url,
            'is_video': is_video,
            'tags': tags,
            'is_online': is_online, # <--- Added Field
        })
        
    return JsonResponse({'profiles': data})

# --- VIEW: Swipe Interface ---
@login_required
def swipe_view(request):
    base_context = {
        'SWIPE_URL': reverse('swipe_action'),
        'PROFILES_URL': reverse('get_profiles_json'), 
        'MATCHES_URL': reverse('match_list'),
    }

    try:
        request.user.profile
    except Profile.DoesNotExist:
        return redirect('create_profile')

    return render(request, 'dating/swipe_card.html', base_context)

# --- ACTION: Handle Swipes (Updated with Like Limit) ---
@login_required
def swipe_action(request):
    if request.method != 'POST':
        return HttpResponseBadRequest("Invalid request method.")

    try:
        data = json.loads(request.body)
        target_user_id = data.get('target_user_id')
        action = data.get('action')
    except Exception:
        return HttpResponseBadRequest("Invalid request data.")

    if action not in [c[0] for c in SWIPE_CHOICES]:
        return HttpResponseBadRequest("Invalid swipe action.")

    # --- LIKE LIMIT CHECK ---
    if action == 'LIKE' and not request.user.profile.is_premium:
        # Check likes made TODAY
        today = timezone.now().date()
        likes_today = Swipe.objects.filter(
            swiper=request.user, 
            type='LIKE', 
            created_at__date=today
        ).count()
        
        if likes_today >= 100:
            return JsonResponse({'status': 'error', 'message': 'limit_reached_likes'})
    # ------------------------

    target_user = get_object_or_404(User, id=target_user_id)
    swiper = request.user
    
    swipe, created = Swipe.objects.get_or_create(
        swiper=swiper,
        swiped=target_user,
        defaults={'type': action}
    )
    
    # Enable Rewind (One step)
    request.session['can_rewind'] = True

    try:
        swiper.profile.last_active = timezone.now()
        swiper.profile.save()
    except Exception:
        pass

    is_match = False
    
    if action == 'LIKE':
        reverse_swipe = Swipe.objects.filter(
            swiper=target_user, 
            swiped=swiper, 
            type='LIKE'
        ).exists()

        if reverse_swipe:
            is_match = True
            user1, user2 = sorted([swiper, target_user], key=lambda u: u.id)
            Match.objects.get_or_create(
                user1=user1,
                user2=user2,
                defaults={'expires_at': timezone.now() + timedelta(days=7)}
            )

    return JsonResponse({'status': 'success', 'is_match': is_match})

# --- ACTION: Rewind Last Swipe ---
@login_required
def rewind_last_swipe(request):
    if not request.user.profile.is_premium:
        return JsonResponse({'status': 'error', 'message': 'premium_required'}, status=403)

    if not request.session.get('can_rewind', False):
        return JsonResponse({'status': 'error', 'message': 'limit_reached'})

    try:
        last_swipe = Swipe.objects.filter(swiper=request.user).order_by('-id').first()
        
        if not last_swipe:
            return JsonResponse({'status': 'error', 'message': 'no_swipes'})

        target_user = last_swipe.swiped
        
        if last_swipe.type == 'LIKE':
             Match.objects.filter(
                (Q(user1=request.user) & Q(user2=target_user)) | 
                (Q(user1=target_user) & Q(user2=request.user))
            ).delete()

        last_swipe.delete()
        request.session['can_rewind'] = False
        
        profile = target_user.profile
        photo_obj = profile.photos.filter(is_main=True).first() or profile.photos.first()
        
        if photo_obj:
            media_url = photo_obj.image.url
            is_video = photo_obj.is_video
        else:
            media_url = "https://placehold.co/600x800/F9A8D4/ffffff?text=No+Photo"
            is_video = False
        
        data = {
            'id': target_user.id,
            'username': target_user.username,
            'age': profile.age,
            'location': profile.location,
            'job_title': profile.job_title,
            'bio': profile.bio[:100] + '...' if len(profile.bio) > 100 else profile.bio,
            'first_date_idea': profile.first_date_idea,
            'image_url': media_url,
            'is_video': is_video,
            'tags': [t.name for t in profile.tags.all()[:3]],
            'is_online': False # Default for rewound profile
        }
        
        return JsonResponse({'status': 'success', 'profile': data})

    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

# --- Standard Views (Match, Likes, Profile, etc) ---
@login_required
def match_list(request):
    user = request.user
    active_matches = Match.objects.filter(
        Q(user1=user) | Q(user2=user),
        expires_at__gt=timezone.now()
    ).select_related('user1', 'user2')

    matches_data = []
    for match in active_matches:
        matched_user = match.user2 if match.user1 == user else match.user1
        try:
            matched_profile = matched_user.profile
            whatsapp_url = f"https://wa.me/{matched_profile.whatsapp_number}"
            matches_data.append({
                'username': matched_user.username,
                'age': matched_profile.age,
                'first_date_idea': matched_profile.first_date_idea,
                'whatsapp_url': whatsapp_url,
                'expires_in': (match.expires_at - timezone.now()).days,
            })
        except Profile.DoesNotExist:
            continue

    return render(request, 'dating/match_list.html', {'matches': matches_data})

@login_required
def likes_list(request):
    user = request.user
    likers_ids = Swipe.objects.filter(swiped=user, type='LIKE').values_list('swiper_id', flat=True)
    swiped_ids = Swipe.objects.filter(swiper=user).values_list('swiped_id', flat=True)
    
    pending_likes = Profile.objects.filter(user__id__in=likers_ids)\
                                   .exclude(user__id__in=swiped_ids)\
                                   .select_related('user')\
                                   .prefetch_related('photos')

    likes_data = []
    for profile in pending_likes:
        photo_obj = profile.photos.filter(is_main=True).first() or profile.photos.first()
        if photo_obj:
            try:
                image_url = photo_obj.image.url
                is_video = photo_obj.is_video
            except Exception:
                 image_url = None
                 is_video = False
        else:
            image_url = None
            is_video = False

        likes_data.append({
            'id': profile.user.id,
            'username': profile.user.username,
            'age': profile.age,
            'image_url': image_url,
            'is_video': is_video,
            'bio': profile.bio,
            'first_date_idea': profile.first_date_idea
        })

    return render(request, 'dating/likes_list.html', {'likes': likes_data})

@login_required
def profile_detail(request):
    try:
        profile = request.user.profile
    except Profile.DoesNotExist:
        return redirect('create_profile')
    
    main_photo = profile.photos.filter(is_main=True).first() 
    if not main_photo:
        main_photo = profile.photos.first()
    other_photos = profile.photos.exclude(id=main_photo.id) if main_photo else profile.photos.all()

    return render(request, 'dating/profile_detail.html', {
        'profile': profile, 
        'main_photo': main_photo,
        'other_photos': other_photos
    })

@login_required
def public_profile(request, pk):
    profile = get_object_or_404(Profile, user_id=pk)
    main_photo = profile.photos.filter(is_main=True).first() 
    if not main_photo:
        main_photo = profile.photos.first()
    other_photos = profile.photos.exclude(id=main_photo.id) if main_photo else profile.photos.all()

    return render(request, 'dating/public_profile.html', {
        'profile': profile,
        'main_photo': main_photo,
        'other_photos': other_photos
    })

@login_required
def edit_profile(request):
    try:
        profile = request.user.profile
    except Profile.DoesNotExist:
        return redirect('create_profile')

    for photo in list(profile.photos.all()): 
        try:
            if not photo.image or not os.path.exists(photo.image.path):
                photo.delete()
        except Exception:
            photo.delete()

    if request.method == 'POST':
        action = request.POST.get('action')
        photo_id = request.POST.get('photo_id')

        if action and photo_id:
            try:
                photo = ProfilePhoto.objects.filter(id=photo_id, profile=profile).first()
                if photo:
                    if action == 'set_main':
                        ProfilePhoto.objects.filter(profile=profile).update(is_main=False)
                        photo.is_main = True
                        photo.save()
                    elif action == 'delete':
                        was_main = photo.is_main
                        photo.delete()
                        if was_main:
                            next_p = ProfilePhoto.objects.filter(profile=profile).first()
                            if next_p:
                                next_p.is_main = True
                                next_p.save()
            except Exception as e:
                print(f"Photo action error: {e}")
            return redirect('edit_profile')

        form = ProfileForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            form.save() 
            more_photos = request.FILES.getlist('more_photos')
            new_main_index = request.POST.get('new_main_index')
            try:
                main_idx = int(new_main_index)
            except (ValueError, TypeError):
                main_idx = -1

            for i, f in enumerate(more_photos):
                is_main_status = False
                if i == main_idx:
                    is_main_status = True
                    ProfilePhoto.objects.filter(profile=profile).update(is_main=False)
                elif not ProfilePhoto.objects.filter(profile=profile).exists() and i == 0:
                    is_main_status = True
                ProfilePhoto.objects.create(profile=profile, image=f, is_main=is_main_status)
            return redirect('profile')
    else:
        form = ProfileForm(instance=profile)
    
    existing_photos = profile.photos.all().order_by('-is_main', '-uploaded_at')
    current_count = profile.photos.count()
    return render(request, 'dating/edit_profile.html', {
        'form': form, 
        'existing_photos': existing_photos,
        'media_limit': 20,
        'current_count': current_count
    })

@login_required
def settings_view(request):
    try:
        profile = request.user.profile
    except Profile.DoesNotExist:
        return redirect('create_profile')
    if request.method == 'POST':
        form = SettingsForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            return redirect('swipe_card')
    else:
        form = SettingsForm(instance=profile)
    return render(request, 'dating/settings.html', {'form': form})

def signup(request):
    if request.user.is_authenticated:
        return redirect('swipe_card')
    if request.method == 'POST':
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect('create_profile')
    else:
        form = SignUpForm()
    return render(request, 'registration/signup.html', {'form': form})

@login_required
def create_profile(request):
    try:
        if request.user.profile:
            return redirect('swipe_card')
    except Profile.DoesNotExist:
        pass
    if request.method == 'POST':
        form = ProfileForm(request.POST, request.FILES)
        if form.is_valid():
            profile = form.save(commit=False)
            profile.user = request.user
            profile.save()
            form.save_m2m()
            photo = form.cleaned_data.get('photo')
            if photo:
                ProfilePhoto.objects.create(profile=profile, image=photo, is_main=True)
            more_photos = request.FILES.getlist('more_photos')
            for f in more_photos[:20]:
                has_main = ProfilePhoto.objects.filter(profile=profile, is_main=True).exists()
                ProfilePhoto.objects.create(profile=profile, image=f, is_main=not has_main)
            return redirect('swipe_card')
    else:
        form = ProfileForm()
    return render(request, 'dating/create_profile.html', {'form': form})

def index(request):
    if request.user.is_authenticated:
        return redirect('swipe_card')
    return render(request, 'dating/landing.html')

def privacy(request): return render(request, 'dating/privacy.html')
def terms(request): return render(request, 'dating/terms.html')
def contact(request): return render(request, 'dating/contact.html')

# --- PREMIUM ---
@login_required
def premium_landing(request):
    return render(request, 'dating/premium_landing.html')

@login_required
def premium_checkout(request):
    plan_type = request.GET.get('plan', 'GOLD').upper()
    plans = {
        'SILVER': {'price': 200, 'name': 'Silver Plan', 'days': 3, 'color': 'gray-400'},
        'GOLD': {'price': 500, 'name': 'Gold Plan', 'days': 7, 'color': 'yellow-400'},
        'PLATINUM': {'price': 1000, 'name': 'Platinum Plan', 'days': 30, 'color': 'pink-500'},
    }
    selected_plan = plans.get(plan_type, plans['GOLD'])
    return render(request, 'dating/premium_checkout.html', {
        'plan': selected_plan, 
        'plan_type': plan_type,
        'paystack_key': settings.PAYSTACK_PUBLIC_KEY,
        'flutterwave_key': settings.FLUTTERWAVE_PUBLIC_KEY,
        'user_email': request.user.email or f"{request.user.username}@loveny.com"
    })

@login_required
@csrf_exempt 
def verify_payment(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            plan_type = data.get('plan_type')
            days_map = {'SILVER': 3, 'GOLD': 7, 'PLATINUM': 30}
            days = days_map.get(plan_type, 7)
            profile = request.user.profile
            profile.premium_tier = plan_type
            if profile.premium_expiry and profile.premium_expiry > timezone.now():
                profile.premium_expiry += timedelta(days=days)
            else:
                profile.premium_expiry = timezone.now() + timedelta(days=days)
            profile.save()
            return JsonResponse({'status': 'success'})
        except Exception as e:
            return JsonResponse({'status': 'error'}, status=400)
    return JsonResponse({'status': 'error'}, status=400)

@login_required
def premium_success(request):
    return render(request, 'dating/premium_success.html')