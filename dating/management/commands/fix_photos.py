import os
from django.core.management.base import BaseCommand
from django.conf import settings
from dating.models import ProfilePhoto

class Command(BaseCommand):
    help = 'Removes ProfilePhotos from the database if the actual file is missing'

    def handle(self, *args, **options):
        self.stdout.write("Scanning for broken photo links...")
        
        deleted_count = 0
        all_photos = ProfilePhoto.objects.all()
        
        for photo in all_photos:
            try:
                # Check if file field is empty
                if not photo.image:
                    photo.delete()
                    deleted_count += 1
                    continue
                
                # Check if file actually exists on the computer
                if not os.path.exists(photo.image.path):
                    self.stdout.write(self.style.WARNING(f'Ghost found! Deleting record ID: {photo.id}'))
                    photo.delete()
                    deleted_count += 1
            except Exception as e:
                # If checking the path throws an error (e.g. weird filename), delete it
                self.stdout.write(self.style.ERROR(f'Error on ID {photo.id}: {e}'))
                photo.delete()
                deleted_count += 1

        self.stdout.write(self.style.SUCCESS(f'Successfully removed {deleted_count} broken photo cards.'))