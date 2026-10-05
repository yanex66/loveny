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
        'price': Decimal('4000.00'),
        'is_popular': True,
        'badge': 'Popular',
        'order': 2,
    },
    {
        'name': 'Value Pack',
        'coins': 750,
        'bonus_coins': 100,
        'price': Decimal('9500.00'),
        'is_popular': False,
        'badge': 'High Value',
        'order': 3,
    },
    {
        'name': 'VIP Best Value',
        'coins': 2000,
        'bonus_coins': 300,
        'price': Decimal('24000.00'),
        'is_popular': False,
        'badge': 'Best Value',
        'order': 4,
    },
]


def update_coin_packages(apps, schema_editor):
    CoinPackage = apps.get_model('dating', 'CoinPackage')
    CoinPackage.objects.all().delete()
    for pkg in COIN_PACKAGES:
        CoinPackage.objects.create(**pkg)


class Migration(migrations.Migration):
    dependencies = [
        ('dating', '0022_remove_siteconfiguration_diamond_exchange_rate_naira_and_more'),
    ]

    operations = [
        migrations.RunPython(update_coin_packages, migrations.RunPython.noop),
    ]

