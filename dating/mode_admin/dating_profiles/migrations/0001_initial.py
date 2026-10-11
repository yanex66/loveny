from django.db import migrations


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ('dating', '0037_remove_qa_test_profiles'),
    ]

    operations = [
        migrations.CreateModel(
            name='DatingProfile',
            fields=[],
            options={
                'verbose_name': 'Dating Profile',
                'verbose_name_plural': 'Dating Profiles',
                'proxy': True,
                'indexes': [],
                'constraints': [],
            },
            bases=('dating.profile',),
        ),
    ]
