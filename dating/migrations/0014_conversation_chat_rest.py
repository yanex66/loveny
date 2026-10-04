import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

def migrate_existing_chats(apps, schema_editor):
    alias = schema_editor.connection.alias
    Match = apps.get_model('dating', 'Match')
    Conversation = apps.get_model('dating', 'Conversation')
    ChatMessage = apps.get_model('dating', 'ChatMessage')
    participant_model = Conversation.participants.through
    read_by_model = ChatMessage.read_by.through

    conversations = {}
    for match in Match.objects.using(alias).all().iterator():
        conversation = Conversation.objects.using(alias).create(
            created_at=match.created_at,
            updated_at=match.created_at,
        )
        participant_model.objects.using(alias).bulk_create([
            participant_model(
                conversation_id=conversation.pk,
                user_id=match.user1_id,
            ),
            participant_model(
                conversation_id=conversation.pk,
                user_id=match.user2_id,
            ),
        ])
        conversations[match.pk] = conversation.pk

    for match_id, conversation_id in conversations.items():
        ChatMessage.objects.using(alias).filter(match_id=match_id).update(
            conversation_id=conversation_id,
        )

    read_by_model.objects.using(alias).bulk_create(
        [
            read_by_model(chatmessage_id=message.pk, user_id=message.sender_id)
            for message in ChatMessage.objects.using(alias).only('pk', 'sender_id').iterator()
        ],
        ignore_conflicts=True,
    )

    for conversation_id in conversations.values():
        last_message = (
            ChatMessage.objects.using(alias)
            .filter(conversation_id=conversation_id)
            .order_by('-sent_at', '-pk')
            .values_list('sent_at', flat=True)
            .first()
        )
        if last_message:
            Conversation.objects.using(alias).filter(pk=conversation_id).update(
                updated_at=last_message,
            )


class Migration(migrations.Migration):

    dependencies = [
        ('dating', '0013_dynamic_subscription_plans'),
    ]

    operations = [
        migrations.CreateModel(
            name='Conversation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True, db_index=True)),
                ('participants', models.ManyToManyField(db_table='dating_conversation_participants', related_name='conversations', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ('-updated_at', '-id'),
            },
        ),
        migrations.AddField(
            model_name='chatmessage',
            name='conversation',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='messages', to='dating.conversation'),
        ),
        migrations.AddField(
            model_name='chatmessage',
            name='read_by',
            field=models.ManyToManyField(blank=True, related_name='read_chat_messages', to=settings.AUTH_USER_MODEL),
        ),
        migrations.RunPython(migrate_existing_chats, migrations.RunPython.noop),
        migrations.RemoveIndex(
            model_name='chatmessage',
            name='dating_chat_match_i_af84b9_idx',
        ),
        migrations.RemoveField(
            model_name='chatmessage',
            name='match',
        ),
        migrations.RenameField(
            model_name='chatmessage',
            old_name='body',
            new_name='text',
        ),
        migrations.AlterField(
            model_name='chatmessage',
            name='text',
            field=models.TextField(),
        ),
        migrations.RenameField(
            model_name='chatmessage',
            old_name='sent_at',
            new_name='created_at',
        ),
        migrations.AlterField(
            model_name='chatmessage',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, db_index=True),
        ),
        migrations.AlterField(
            model_name='chatmessage',
            name='conversation',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='messages', to='dating.conversation'),
        ),
        migrations.AlterModelOptions(
            name='chatmessage',
            options={'ordering': ('created_at', 'id')},
        ),
        migrations.AddIndex(
            model_name='chatmessage',
            index=models.Index(fields=['conversation', 'created_at'], name='dating_chat_convers_3817ed_idx'),
        ),
    ]
