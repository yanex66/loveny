from django.db import migrations


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ('dating', '0037_remove_qa_test_profiles'),
    ]

    operations = [
        migrations.CreateModel(
            name='HookupProfile',
            fields=[],
            options={
                'verbose_name': 'Hookup Profile',
                'verbose_name_plural': 'Hookup Profiles',
                'proxy': True,
                'indexes': [],
                'constraints': [],
            },
            bases=('dating.profile',),
        ),
    ]
