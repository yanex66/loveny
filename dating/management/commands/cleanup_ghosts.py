import os
from django.core.management.base import BaseCommand
from django.conf import settings
from dating.models import ProfilePhoto

class Command(BaseCommand):
    help = 'Removes ProfilePhotos from the database if the actual file is missing'

    def handle(self, *args, **options):
        self.stdout.write("Checking for ghost photos...")
        deleted_count = 0
        
        for photo in ProfilePhoto.objects.all():
            # Construct full file path
            try:
                if not photo.image:
                    photo.delete()
                    deleted_count += 1
                    continue
                    
                file_path = photo.image.path
                if not os.path.exists(file_path):
                    self.stdout.write(self.style.WARNING(f'Missing file for Photo ID {photo.id}. Deleting record.'))
                    photo.delete()
                    deleted_count += 1
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Error checking Photo ID {photo.id}: {e}'))
                # Optional: delete if path is corrupted
                # photo.delete()
                # deleted_count += 1    

        self.stdout.write(self.style.SUCCESS(f'Done! Deleted {deleted_count} ghost photo records.'))