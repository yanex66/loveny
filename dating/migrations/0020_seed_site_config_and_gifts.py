from decimal import Decimal
from django.db import migrations


def seed_site_config_and_gifts(apps, schema_editor):
    SiteConfiguration = apps.get_model('dating', 'SiteConfiguration')
    GiftItem = apps.get_model('dating', 'GiftItem')

    SiteConfiguration.objects.get_or_create(
        pk=1,
        defaults={
            'call_rate_per_minute': 20,
            'grace_period_seconds': 20,
            'host_commission_percentage': 70,
            'welcome_bonus_coins': 30,
            'min_diamond_withdrawal': 500,
            'diamond_exchange_rate_naira': Decimal('5.00'),
            'announcement_banner': '🔥 Welcome to LOVENY Video Hub! 20 Free Coins on Signup ✨',
            'is_announcement_active': True,
        }
    )

    gifts_data = [
        {'name': 'Rose', 'slug': 'rose', 'icon': '🌹', 'coin_cost': 5, 'order': 1},
        {'name': 'Kiss', 'slug': 'kiss', 'icon': '💋', 'coin_cost': 15, 'order': 2},
        {'name': 'Champagne', 'slug': 'champagne', 'icon': '🥂', 'coin_cost': 50, 'order': 3},
        {'name': 'Crown', 'slug': 'crown', 'icon': '👑', 'coin_cost': 150, 'order': 4},
        {'name': 'Supercar', 'slug': 'car', 'icon': '🏎️', 'coin_cost': 300, 'order': 5},
        {'name': 'Diamond Ring', 'slug': 'ring', 'icon': '💍', 'coin_cost': 500, 'order': 6},
        {'name': 'Luxury Yacht', 'slug': 'yacht', 'icon': '🛥️', 'coin_cost': 1000, 'order': 7},
    ]

    for item in gifts_data:
        GiftItem.objects.update_or_create(
            slug=item['slug'],
            defaults=item,
        )


def reverse_func(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('dating', '0019_giftitem_siteconfiguration_profile_is_host_ready_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_site_config_and_gifts, reverse_func),
    ]
