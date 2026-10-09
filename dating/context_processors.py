from datetime import timedelta
from django.utils import timezone
from .models import SiteConfiguration, CoinTransaction


def site_configuration_context(request):
    """
    Exposes `site_config` and user gamification/reward status globally to all templates.
    """
    try:
        config = SiteConfiguration.get_solo()
    except Exception:
        config = None

    daily_coins = getattr(config, 'daily_checkin_coins', 5) if config else 5
    d7_coins = getattr(config, 'day_7_bonus_coins', 10) if config else 10
    d7_diamonds = getattr(config, 'day_7_bonus_diamonds', 20) if config else 20

    ctx = {
        'site_config': config,
        'user_has_claimed_welcome': True,
        'user_has_checked_in_today': True,
        'user_checkin_streak': 0,
        'user_cycle_day': 1,
        'user_welcome_coins': getattr(config, 'welcome_bonus_coins', 30) if config else 30,
        'reward_calendar_days': [
            {'day': 1, 'coins': daily_coins, 'diamonds': 0, 'is_grand': False},
            {'day': 2, 'coins': daily_coins, 'diamonds': 0, 'is_grand': False},
            {'day': 3, 'coins': daily_coins, 'diamonds': 0, 'is_grand': False},
            {'day': 4, 'coins': daily_coins, 'diamonds': 0, 'is_grand': False},
            {'day': 5, 'coins': daily_coins, 'diamonds': 0, 'is_grand': False},
            {'day': 6, 'coins': daily_coins, 'diamonds': 0, 'is_grand': False},
            {'day': 7, 'coins': d7_coins, 'diamonds': d7_diamonds, 'is_grand': True},
        ],
    }

    user = getattr(request, 'user', None)
    if user and getattr(user, 'is_authenticated', False):
        profile = getattr(user, 'profile', None)
        if profile:
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

            # Determine which day of the 1..7 streak cycle is active
            streak = profile.checkin_streak or 0
            if has_checked_in_today:
                cycle_day = max(1, min(7, ((streak - 1) % 7) + 1)) if streak > 0 else 1
            else:
                if profile.last_checkin_date == yesterday and streak > 0:
                    cycle_day = (streak % 7) + 1
                else:
                    cycle_day = 1

            ctx.update({
                'user_has_claimed_welcome': has_claimed_welcome,
                'user_has_checked_in_today': has_checked_in_today,
                'user_checkin_streak': streak,
                'user_cycle_day': cycle_day,
                'user_coin_balance': profile.coin_balance,
                'user_earned_diamonds': profile.earned_diamonds,
            })

    return ctx

