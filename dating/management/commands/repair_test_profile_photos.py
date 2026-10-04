from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand

from dating.management.commands.create_test_data import make_avatar
from dating.models import ProfilePhoto


class Command(BaseCommand):
    help = 'Ensure numbered test profiles have between one and three usable local photos.'

    def handle(self, *args, **options):
        profiles = User.objects.filter(
            username__startswith='test_profile_',
            profile__isnull=False,
        ).select_related('profile').order_by('username')
        repaired = 0
        removed = 0

        for user in profiles.iterator():
            profile = user.profile
            photos = list(profile.photos.all().order_by('is_main', 'uploaded_at'))
            valid_photos = [
                photo for photo in photos
                if photo.image
                and default_storage.exists(photo.image.name)
                and default_storage.size(photo.image.name) > 0
            ]

            for photo in photos:
                if photo not in valid_photos:
                    photo.delete()
                    removed += 1

            for photo in valid_photos[3:]:
                photo.delete()
                removed += 1
            valid_photos = valid_photos[:3]

            if not valid_photos:
                suffix = user.username.removeprefix('test_profile_')
                try:
                    index = int(suffix)
                except ValueError:
                    index = user.pk
                photo = ProfilePhoto(profile=profile, is_main=True)
                photo.image.save(
                    f'{user.username}-avatar.svg',
                    ContentFile(make_avatar(index, profile.gender).encode('utf-8')),
                    save=True,
                )
                repaired += 1
            elif not any(photo.is_main for photo in valid_photos):
                valid_photos[0].is_main = True
                valid_photos[0].save(update_fields=['is_main'])

        self.stdout.write(self.style.SUCCESS(
            f'Checked {profiles.count()} test profiles; created {repaired} replacement avatars '
            f'and removed {removed} invalid or excess photo records.'
        ))
