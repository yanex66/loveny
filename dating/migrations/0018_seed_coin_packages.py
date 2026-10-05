from decimal import Decimal
from django.db import migrations


COIN_PACKAGES = [
    {
        'name': 'Starter Pack',
        'coins': 100,
        'bonus_coins': 0,
        'price': Decimal('1500.00'),
        'is_popular': False,
        'badge': 'Starter',
        'order': 1,
    },
    {
        'name': 'Popular Pack',
        'coins': 300,
        'bonus_coins': 50,
        'price': Decimal('3500.00'),
        'is_popular': True,
        'badge': 'Most Popular',
        'order': 2,
    },
    {
        'name': 'Lover VIP',
        'coins': 800,
        'bonus_coins': 200,
        'price': Decimal('8000.00'),
        'is_popular': False,
        'badge': 'Best Value',
        'order': 3,
    },
    {
        'name': 'Diamond Whale',
        'coins': 2500,
        'bonus_coins': 800,
        'price': Decimal('20000.00'),
        'is_popular': False,
        'badge': 'VIP Pass',
        'order': 4,
    },
]


def seed_coin_packages(apps, schema_editor):
    CoinPackage = apps.get_model('dating', 'CoinPackage')
    for pkg in COIN_PACKAGES:
        CoinPackage.objects.get_or_create(
            name=pkg['name'],
            defaults=pkg,
        )


class Migration(migrations.Migration):
    dependencies = [
        ('dating', '0017_coinpackage_callsession_coins_spent_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_coin_packages, migrations.RunPython.noop),
    ]

