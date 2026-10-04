from django.db import migrations


INTEREST_NAMES = [
    'Books', 'Movies', 'Travel', 'Fitness', 'Food', 'Cooking', 'Coffee',
    'Music', 'Live Music', 'Afrobeats', 'Art', 'Photography', 'Gaming',
    'Technology', 'Fashion', 'Dancing', 'Hiking', 'Nature', 'Beach Days',
    'Swimming', 'Running', 'Yoga', 'Football', 'Basketball', 'Animals',
    'Dogs', 'Cats', 'Volunteering', 'Faith', 'Family', 'Entrepreneurship',
    'Startups', 'Design', 'Writing', 'Podcasts', 'Comedy', 'Theatre',
    'Museums', 'History', 'Languages', 'Learning', 'Board Games',
    'Skincare', 'Wellness', 'Gardening', 'DIY', 'Cars', 'Road Trips',
    'Camping', 'Brunch', 'Street Food', 'Restaurants', 'Tea', 'Concerts',
    'Festivals', 'Nollywood', 'Documentaries', 'Personal Growth',
    'Meditation', 'Fashion Design', 'Makeup', 'Interior Design',
    'Architecture', 'Crypto', 'Science', 'Space', 'Badminton', 'Tennis',
    'Cycling', 'Pilates', 'Baking', 'Beach Walks',
]


def seed_interest_tags(apps, schema_editor):
    Tag = apps.get_model('dating', 'Tag')
    Tag.objects.bulk_create(
        [Tag(name=name) for name in INTEREST_NAMES],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ('dating', '0011_remove_profile_sugar_allowance_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_interest_tags, migrations.RunPython.noop),
    ]
