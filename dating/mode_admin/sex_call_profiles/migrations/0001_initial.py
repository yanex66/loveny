from django.db import migrations


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ('dating', '0037_remove_qa_test_profiles'),
    ]

    operations = [
        migrations.CreateModel(
            name='SexCallProfile',
            fields=[],
            options={
                'verbose_name': 'Sex Call Profile',
                'verbose_name_plural': 'Sex Call Profiles',
                'proxy': True,
                'indexes': [],
                'constraints': [],
            },
            bases=('dating.profile',),
        ),
    ]
