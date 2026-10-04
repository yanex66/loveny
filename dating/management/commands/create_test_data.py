import random
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from dating.models import GENDER_CHOICES, RELATIONSHIP_MODE_CHOICES, Profile, ProfilePhoto, Tag


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

LOCATIONS = [
    ('Lagos', 6.5244, 3.3792),
    ('Abuja', 9.0765, 7.3986),
    ('Port Harcourt', 4.8156, 7.0498),
    ('Ibadan', 7.3775, 3.9470),
    ('Enugu', 6.4584, 7.5464),
    ('Benin City', 6.3350, 5.6037),
    ('Kano', 12.0022, 8.5920),
    ('Abeokuta', 7.1475, 3.3619),
]

BIOS = [
    'Here for good conversation and new experiences.',
    'Easygoing, curious, and always up for good food.',
    'Looking to meet someone kind and genuine.',
    'Balancing work, friends, and finding new places.',
    'A little adventurous and big on laughter.',
    'Music, good company, and a positive outlook.',
]

JOBS = [
    'Designer', 'Teacher', 'Software Developer', 'Nurse', 'Chef',
    'Entrepreneur', 'Accountant', 'Photographer', 'Marketing Manager',
    'Architect', 'Student', 'Researcher', 'Product Manager',
    'Fashion Designer', 'Consultant', 'Writer',
]


def make_avatar(index, gender):
    palette = [
        ('#f9a8d4', '#701a75', '#fef3c7'),
        ('#fdba74', '#9f1239', '#ffedd5'),
        ('#c4b5fd', '#312e81', '#fce7f3'),
        ('#86efac', '#14532d', '#dcfce7'),
        ('#93c5fd', '#1e3a8a', '#dbeafe'),
        ('#fda4af', '#881337', '#ffe4e6'),
        ('#fde68a', '#78350f', '#fef3c7'),
        ('#a5f3fc', '#164e63', '#cffafe'),
    ]
    background, hair, shirt = palette[index % len(palette)]
    hair_path = (
        'M135 226c0-78 31-126 105-126s105 48 105 126v30H135z'
        if gender == 'F'
        else 'M150 203c0-69 31-103 90-103s90 34 90 103v20H150z'
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 420 560">
<defs><linearGradient id="bg" x2="1" y2="1"><stop stop-color="{background}"/><stop offset="1" stop-color="#fff7ed"/></linearGradient></defs>
<rect width="420" height="560" fill="url(#bg)"/>
<circle cx="70" cy="100" r="52" fill="#fff" opacity=".24"/>
<circle cx="350" cy="430" r="82" fill="#fff" opacity=".2"/>
<path d="M65 560c8-139 55-211 145-211s137 72 145 211" fill="{shirt}"/>
<path d="{hair_path}" fill="{hair}"/>
<ellipse cx="240" cy="225" rx="81" ry="105" fill="#b97854"/>
<path d="M159 210c10-70 37-108 81-108 54 0 81 39 81 108-25-18-45-42-57-69-27 31-62 50-105 58z" fill="{hair}"/>
<ellipse cx="211" cy="230" rx="6" ry="8" fill="#35162f"/>
<ellipse cx="270" cy="230" rx="6" ry="8" fill="#35162f"/>
<path d="M220 272c13 12 28 12 41 0" fill="none" stroke="#7f1d1d" stroke-width="6" stroke-linecap="round"/>
<text x="210" y="515" text-anchor="middle" font-family="Arial,sans-serif" font-size="17" font-weight="700" fill="#35162f" opacity=".72">TEST PROFILE</text>
</svg>'''


class Command(BaseCommand):
    help = 'Create opt-in dummy profiles with generated local SVG avatars for development.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--count',
            type=int,
            default=1000,
            help='Number of test profiles to create (default: 1000).',
        )

    def handle(self, *args, **options):
        count = options['count']
        if count < 1:
            raise CommandError('--count must be a positive integer.')

        tags = [Tag.objects.get_or_create(name=name)[0] for name in INTEREST_NAMES]
        modes = [choice[0] for choice in RELATIONSHIP_MODE_CHOICES]
        genders = [choice[0] for choice in GENDER_CHOICES]
        created_profiles = 0
        created_photos = 0

        for index in range(1, count + 1):
            rng = random.Random(index)
            username = f'test_profile_{index:04d}'
            gender = genders[((index - 1) // len(modes)) % len(genders)]
            preferred_gender = genders[((index - 1) // (len(modes) * len(genders))) % len(genders)]
            location, latitude, longitude = rng.choice(LOCATIONS)
            user, user_created = User.objects.get_or_create(
                username=username,
                defaults={
                    'email': f'{username}@example.invalid',
                    'first_name': f'Test{index}',
                },
            )
            if user_created:
                user.set_unusable_password()
                user.save(update_fields=['password'])

            profile, profile_created = Profile.objects.get_or_create(
                user=user,
                defaults={
                    'age': rng.randint(21, 45),
                    'gender': gender,
                    'preferred_gender': preferred_gender,
                    'relationship_mode': modes[(index - 1) % len(modes)],
                    'min_age_pref': 18,
                    'max_age_pref': 65,
                    'bio': rng.choice(BIOS),
                    'location': location,
                    'latitude': latitude,
                    'longitude': longitude,
                    'max_distance_km': 100,
                    'job_title': rng.choice(JOBS),
                    'whatsapp_number': f'+1999{index:07d}',
                    'first_date_idea': 'Coffee and good conversation.',
                    'last_active': timezone.now() - timedelta(minutes=rng.randint(0, 60 * 24 * 14)),
                },
            )
            if profile_created:
                profile.tags.set(rng.sample(tags, 5))
                created_profiles += 1
            else:
                profile.gender = gender
                profile.preferred_gender = preferred_gender
                profile.relationship_mode = modes[(index - 1) % len(modes)]
                profile.last_active = timezone.now()
                profile.show_in_discovery = True
                profile.save(update_fields=[
                    'gender',
                    'preferred_gender',
                    'relationship_mode',
                    'last_active',
                    'show_in_discovery',
                ])

            if not profile.photos.exists():
                photo = ProfilePhoto(profile=profile, is_main=True)
                photo.image.save(
                    f'{username}.svg',
                    ContentFile(make_avatar(index, gender).encode('utf-8')),
                    save=True,
                )
                created_photos += 1

            if index % 100 == 0:
                self.stdout.write(f'Processed {index} of {count} test profiles.')

        self.stdout.write(self.style.SUCCESS(
            f'Created {created_profiles} profiles and {created_photos} local test avatars. '
            f'Processed {count} numbered profiles.'
        ))
