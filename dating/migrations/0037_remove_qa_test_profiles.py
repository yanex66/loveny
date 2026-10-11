from django.conf import settings
from django.db import migrations
from django.db.models import Q


def remove_qa_test_profiles(apps, schema_editor):
    database = schema_editor.connection.alias
    Profile = apps.get_model('dating', 'Profile')
    ProfilePhoto = apps.get_model('dating', 'ProfilePhoto')
    app_label, model_name = settings.AUTH_USER_MODEL.split('.')
    User = apps.get_model(app_label, model_name)

    profiles = Profile.objects.using(database).filter(
        Q(is_test_profile=True)
        | Q(user__username__startswith='qa_demo')
        | Q(user__username__startswith='test_user_')
    )
    profile_ids = list(profiles.values_list('pk', flat=True))
    user_ids = set(profiles.values_list('user_id', flat=True))
    user_ids.update(
        User.objects.using(database).filter(
            Q(username__startswith='qa_demo')
            | Q(username__startswith='test_user_')
        ).values_list('pk', flat=True)
    )

    photos = ProfilePhoto.objects.using(database).filter(
        Q(profile_id__in=profile_ids) | Q(user_id__in=user_ids)
    )
    photo_ids = list(photos.values_list('pk', flat=True))
    image_names = photos.exclude(image='').values_list(
        'image',
        flat=True,
    ).distinct()
    storage = ProfilePhoto._meta.get_field('image').storage
    for image_name in image_names:
        shared_image = ProfilePhoto.objects.using(database).exclude(
            pk__in=photo_ids
        ).filter(image=image_name).exists()
        if not shared_image:
            storage.delete(image_name)

    User.objects.using(database).filter(pk__in=user_ids).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('dating', '0036_supportknowledgebase_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [migrations.RunPython(remove_qa_test_profiles)]
