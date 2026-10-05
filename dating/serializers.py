from django.urls import reverse


def serialize_chat_message(message, other_user_id):
    read_by_ids = [reader.pk for reader in message.read_by.all()]
    return {
        'id': message.pk,
        'sender_id': message.sender_id,
        'sender': message.sender.username,
        'text': message.text,
        'message_type': getattr(message, 'message_type', 'text') or 'text',
        'metadata': getattr(message, 'metadata', {}) or {},
        'created_at': message.created_at.isoformat(),
        'read_by': read_by_ids,
        'is_read': other_user_id in read_by_ids,
    }


def serialize_conversation(conversation, other_user):
    last_message = conversation.last_message_id
    return {
        'id': conversation.pk,
        'other_user': {
            'id': other_user.pk,
            'username': other_user.username,
        },
        'updated_at': conversation.updated_at.isoformat(),
        'unread_count': conversation.unread_count or 0,
        'last_message': ({
            'id': last_message,
            'sender_id': conversation.last_message_sender_id,
            'text': conversation.last_message_text[:160],
            'created_at': conversation.last_message_created_at.isoformat(),
        } if last_message else None),
        'url': reverse('conversation_room', args=[conversation.pk]),
    }
