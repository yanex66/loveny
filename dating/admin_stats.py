from datetime import timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.http import JsonResponse
from django.utils import timezone

from dating.models import Profile


def get_user_counts():
    total_users = get_user_model().objects.count()
    online_users = Profile.objects.filter(
        user__is_active=True,
        last_active__gte=timezone.now() - timedelta(minutes=15),
    ).count()
    return {
        'total_users': total_users,
        'online_users': online_users,
        'offline_users': max(0, total_users - online_users),
    }


@staff_member_required
def live_user_counts(request):
    return JsonResponse(get_user_counts())
