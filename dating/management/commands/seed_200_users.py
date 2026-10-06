import random

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from dating.management.commands.create_test_data import INTEREST_NAMES, JOBS, make_avatar
from dating.models import Profile, ProfilePhoto, Tag


TEST_USER_START = 1001
TEST_USER_COUNT = 200
LAGOS_AREAS = (
    ('Ikeja', 6.6018, 3.3515),
    ('Lekki', 6.4698, 3.5852),
    ('Victoria Island', 6.4281, 3.4219),
    ('Yaba', 6.5158, 3.3890),
    ('Surulere', 6.4969, 3.3502),
)
BIOS = (
    'TEST PROFILE - Fictional account for staff-only discovery testing.',
    'TEST PROFILE - Synthetic profile used to check matching and filters.',
    'TEST PROFILE - Not a real member; staff preview only.',
)
DATE_IDEAS = (
    'Dinner and drinks',
    'Late night vibes',
    'Looking for genuine connection',
    'Coffee and a relaxed conversation',
    'A walk by the water and good food',
)


def mode_for_index(index):
    if index < 80:
        return 'DATING'
    if index < 140:
        return 'HOOKUP'
    return 'SEX_CALL'


class Command(BaseCommand):
    help = (
        'Create 200 clearly labelled, inactive test profiles for staff-only '
        'discovery preview. Run with --staff-only to confirm isolation.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--staff-only',
            action='store_true',
            help='Required confirmation: test profiles stay hidden from regular members.',
        )

    def handle(self, *args, **options):
        if not options['staff_only']:
            raise CommandError(
                'This command is restricted to staff-only test profiles. '
                'Re-run with --staff-only; test users will be inactive and unverified.'
            )
        if not User.objects.filter(is_staff=True, is_active=True).exists():
            raise CommandError('Create an active staff account before seeding test profiles.')

        tags = [Tag.objects.get_or_create(name=name)[0] for name in INTEREST_NAMES]
        created_users = 0
        created_profiles = 0
        created_photos = 0
        now = timezone.now()
        avatar_names = {}

        for gender in ('F', 'M'):
            avatar_name = f'profile_media/staff_test_{gender.lower()}.svg'
            if not default_storage.exists(avatar_name):
                avatar_name = default_storage.save(
                    avatar_name,
                    ContentFile(make_avatar(TEST_USER_START, gender).encode('utf-8')),
                )
            avatar_names[gender] = avatar_name

        with transaction.atomic():
            for offset in range(TEST_USER_COUNT):
                number = TEST_USER_START + offset
                username = f'test_user_{number}'
                rng = random.Random(number)
                gender = 'F' if offset % 2 else 'M'
                preferred_gender = 'M' if gender == 'F' else 'F'
                location, latitude, longitude = LAGOS_AREAS[offset % len(LAGOS_AREAS)]
                user, user_created = User.objects.get_or_create(
                    username=username,
                    defaults={
                        'email': f'{username}@example.invalid',
                        'first_name': f'Test {number}',
                    },
                )
                if user_created:
                    created_users += 1

                user.is_active = False
                user.set_unusable_password()
                user.save(update_fields=['is_active', 'password'])

                profile, profile_created = Profile.objects.get_or_create(
                    user=user,
                    defaults={
                        'age': 20 + (offset % 26),
                        'gender': gender,
                        'preferred_gender': preferred_gender,
                        'relationship_mode': mode_for_index(offset),
                        'min_age_pref': 20,
                        'max_age_pref': 45,
                        'bio': rng.choice(BIOS),
                        'location': location,
                        'latitude': latitude,
                        'longitude': longitude,
                        'max_distance_km': 500,
                        'job_title': rng.choice(JOBS),
                        'whatsapp_number': f'+1998{user.pk:07d}',
                        'first_date_idea': rng.choice(DATE_IDEAS),
                    },
                )
                if profile_created:
                    created_profiles += 1

                profile.age = 20 + (offset % 26)
                profile.gender = gender
                profile.preferred_gender = preferred_gender
                profile.relationship_mode = mode_for_index(offset)
                profile.min_age_pref = 20
                profile.max_age_pref = 45
                profile.bio = rng.choice(BIOS)
                profile.location = location
                profile.latitude = latitude
                profile.longitude = longitude
                profile.max_distance_km = 500
                profile.job_title = rng.choice(JOBS)
                profile.first_date_idea = rng.choice(DATE_IDEAS)
                profile.last_active = now
                profile.show_in_discovery = True
                profile.is_verified = False
                profile.is_test_profile = True
                profile.save()
                profile.tags.set(rng.sample(tags, 5))

                desired_photos = 2 + (offset % 3)  # 2 to 4 diverse photos per profile
                current_photos = profile.photos.count()
                if current_photos < desired_photos:
                    has_main = profile.photos.filter(is_main=True).exists()
                    for p_idx in range(current_photos, desired_photos):
                        p_name = f'profile_media/staff_test_{number}_{p_idx}.svg'
                        if not default_storage.exists(p_name):
                            default_storage.save(
                                p_name,
                                ContentFile(make_avatar(number * 10 + p_idx, gender).encode('utf-8')),
                            )
                        ProfilePhoto.objects.create(
                            profile=profile,
                            image=p_name,
                            is_main=(not has_main and p_idx == 0),
                        )
                        created_photos += 1

        total_test_profiles = Profile.objects.filter(is_test_profile=True).count()
        self.stdout.write(self.style.SUCCESS(
            f'Processed {TEST_USER_COUNT} staff-only test profiles '
            f'({created_users} users, {created_profiles} profiles, '
            f'{created_photos} photos created). '
            f'Total isolated test profiles: {total_test_profiles}.'
        ))
