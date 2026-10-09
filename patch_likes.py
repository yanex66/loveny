import os

with open('dating/views.py', 'r', encoding='utf-8') as f:
    content = f.read()

target = """@login_required
def likes_list(request):
    \"\"\"Deprecated: redirects directly to messages inbox.\"\"\"
    return redirect('messages_inbox')"""

replacement = """@login_required
def likes_list(request):
    \"\"\"Shows profiles who liked the current user (Secret Admirers).\"\"\"
    profile = Profile.objects.filter(user=request.user).first()
    
    swiped_me = Swipe.objects.filter(swiped=request.user, is_like=True).values_list('swiper_id', flat=True)
    i_swiped = Swipe.objects.filter(swiper=request.user).values_list('swiped_id', flat=True)
    blocked_by_me = UserBlock.objects.filter(blocker=request.user).values_list('blocked_id', flat=True)
    
    admirer_ids = set(swiped_me) - set(i_swiped) - set(blocked_by_me)
    admirers = Profile.objects.filter(user_id__in=admirer_ids).select_related('user').prefetch_related('photos')
    
    is_premium = profile and profile.is_premium()
    
    return render(request, 'dating/likes_list.html', {
        'admirers': admirers,
        'is_premium': is_premium
    })"""

if target in content:
    content = content.replace(target, replacement)
    with open('dating/views.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Success")
else:
    print("Target not found")

