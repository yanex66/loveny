from django.db import migrations
from django.utils import timezone


def backfill_active_contact_matches(apps, schema_editor):
    Match = apps.get_model('dating', 'Match')
    ContactMatch = apps.get_model('dating', 'ContactMatch')
    database = schema_editor.connection.alias

    for match in Match.objects.using(database).filter(expires_at__gt=timezone.now()).iterator():
        user_a_id, user_b_id = sorted((match.user1_id, match.user2_id))
        mode = 'SEXCALL' if match.mode == 'SEX_CALL' else match.mode
        ContactMatch.objects.using(database).get_or_create(
            user_a_id=user_a_id,
            user_b_id=user_b_id,
            is_cancelled=False,
            defaults={'mode': mode},
        )


class Migration(migrations.Migration):

    dependencies = [
        ('dating', '0054_remove_retired_call_profile_fields'),
    ]

    operations = [
        migrations.RunPython(
            backfill_active_contact_matches,
            migrations.RunPython.noop,
        ),
    ]
