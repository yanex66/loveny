import random
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from dating.management.commands.create_test_data import (
    BIOS,
    INTEREST_NAMES,
    JOBS,
    make_avatar,
)
from dating.models import Profile, ProfilePhoto, Tag


QA_PROFILE_COUNT = 50
QA_USERNAME_PREFIX = 'qa_demo_'
LOCATIONS = (
    ('Lekki, Lagos', 6.4698, 3.5852),
    ('Ikeja, Lagos', 6.6018, 3.3515),
    ('Victoria Island, Lagos', 6.4281, 3.4219),
    ('Yaba, Lagos', 6.5158, 3.3890),
    ('Wuse, Abuja', 9.0579, 7.4951),
    ('GRA, Port Harcourt', 4.8156, 7.0498),
)
MODES = ('DATING', 'HOOKUP', 'SEX_CALL')


class Command(BaseCommand):
    help = 'Create 50 clearly labelled, active profiles for pre-launch QA.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--confirm-qa-data',
            action='store_true',
            help='Confirm creation of public-facing, clearly labelled QA profiles.',
        )

    def handle(self, *args, **options):
        from django.conf import settings

        if not settings.ENABLE_QA_TEST_PROFILES:
            raise CommandError(
                'Set ENABLE_QA_TEST_PROFILES=true before seeding QA profiles.'
            )
        if not options['confirm_qa_data']:
            raise CommandError(
                'Pass --confirm-qa-data to create active, public QA profiles.'
            )
        if not User.objects.filter(
            is_active=True,
            is_staff=True,
            is_superuser=True,
        ).exists():
            raise CommandError(
                'An active administrator must exist before seeding QA profiles.'
            )

        tags = [Tag.objects.get_or_create(name=name)[0] for name in INTEREST_NAMES]
        now = timezone.now()
        created_users = 0
        created_profiles = 0
        created_photos = 0

        with transaction.atomic():
            for index in range(1, QA_PROFILE_COUNT + 1):
                rng = random.Random(index)
                username = f'{QA_USERNAME_PREFIX}{index:04d}'
                gender = 'F' if index % 2 else 'M'
                preferred_gender = 'M' if gender == 'F' else 'F'
                mode = MODES[(index - 1) % len(MODES)]
                location, latitude, longitude = LOCATIONS[(index - 1) % len(LOCATIONS)]
                user, user_created = User.objects.get_or_create(
                    username=username,
                    defaults={
                        'email': f'{username}@example.invalid',
                        'first_name': f'[TEST] Demo User {index:04d}',
                        'is_active': True,
                    },
                )
                if user_created:
                    user.set_unusable_password()
                    created_users += 1
                elif user.is_staff or user.is_superuser:
                    raise CommandError(
                        f'Refusing to replace privileged account {username}.'
                    )
                elif hasattr(user, 'profile') and not user.profile.is_test_profile:
                    raise CommandError(
                        f'Refusing to replace non-test account {username}.'
                    )
                user.first_name = f'[TEST] Demo User {index:04d}'
                user.email = f'{username}@example.invalid'
                user.is_active = True
                update_fields = ['first_name', 'email', 'is_active']
                if user_created:
                    update_fields.append('password')
                user.save(update_fields=update_fields)

                profile, profile_created = Profile.objects.get_or_create(
                    user=user,
                )
                if not profile_created and not profile.is_test_profile:
                    raise CommandError(
                        f'Refusing to overwrite the non-test profile for {username}.'
                    )
                profile.age = rng.randint(20, 34)
                profile.gender = gender
                profile.preferred_gender = preferred_gender
                profile.relationship_mode = mode
                profile.min_age_pref = 18
                profile.max_age_pref = rng.randint(40, 60)
                profile.bio = (
                    '[TEST PROFILE] Synthetic QA account, not a real person. '
                    f'{rng.choice(BIOS)}'
                )[:200]
                profile.location = location
                profile.latitude = latitude
                profile.longitude = longitude
                profile.max_distance_km = rng.randint(50, 100)
                profile.job_title = rng.choice(JOBS)
                profile.first_date_idea = rng.choice((
                    'Coffee and conversation in a public place.',
                    'A relaxed walk and a local meal.',
                    'Trying a new restaurant together.',
                ))
                profile.last_active = now
                profile.show_in_discovery = True
                profile.allow_messages = True
                profile.is_test_profile = True
                profile.is_host_ready = True
                profile.is_verified = True
                profile.is_vip = index % 10 == 0
                profile.coin_balance = rng.randint(500, 5000)
                profile.response_rate = rng.randint(85, 99)
                profile.total_calls_completed = rng.randint(0, 120)
                profile.checkin_streak = 0
                for tier_field in (
                    'dating_premium_tier',
                    'hookup_premium_tier',
                    'sex_call_premium_tier',
                ):
                    setattr(profile, tier_field, '')
                    setattr(profile, tier_field.replace('_tier', '_expiry'), None)
                if profile.is_vip:
                    tier_field = {
                        'DATING': 'dating_premium_tier',
                        'HOOKUP': 'hookup_premium_tier',
                        'SEX_CALL': 'sex_call_premium_tier',
                    }[mode]
                    setattr(profile, tier_field, 'Gold')
                    setattr(
                        profile,
                        tier_field.replace('_tier', '_expiry'),
                        now + timedelta(days=30),
                    )
                profile.save()
                profile.tags.set(rng.sample(tags, rng.randint(3, 5)))
                created_profiles += int(profile_created)

                if not profile.photos.exists():
                    image_name = f'qa_test_profiles/{username}.svg'
                    if not default_storage.exists(image_name):
                        default_storage.save(
                            image_name,
                            ContentFile(
                                make_avatar(index, gender).encode('utf-8')
                            ),
                        )
                    ProfilePhoto.objects.create(
                        profile=profile,
                        image=image_name,
                        is_main=True,
                        mode=mode,
                    )
                    created_photos += 1

        mode_counts = {
            mode: Profile.objects.filter(
                user__username__in=[
                    f'{QA_USERNAME_PREFIX}{index:04d}'
                    for index in range(1, QA_PROFILE_COUNT + 1)
                ],
                relationship_mode=mode,
                is_test_profile=True,
            ).count()
            for mode in MODES
        }
        total = sum(mode_counts.values())
        if total != QA_PROFILE_COUNT:
            raise CommandError(
                f'Expected {QA_PROFILE_COUNT} QA profiles; found {total}.'
            )
        self.stdout.write(self.style.SUCCESS(
            f'Configured {total} labelled QA profiles '
            f'(created {created_users} users, {created_profiles} profiles, '
            f'{created_photos} avatars). Modes: '
            + ', '.join(f'{mode}={count}' for mode, count in mode_counts.items())
        ))
