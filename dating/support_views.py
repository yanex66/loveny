"""
Support Chat Widget — Django views.
Endpoints:
  POST /api/support/send/      – Send a message (creates ticket if needed)
  GET  /api/support/messages/  – Load chat history for the current user's ticket
  POST /api/support/mark-read/ – Mark support messages as read by user
"""
import json
import logging
import threading
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import SupportTicket, SupportMessage

logger = logging.getLogger(__name__)

AUTO_REPLY_TEXT = (
    "Thanks for reaching out! 💬 We have received your message. "
    "Our support team is reviewing it and will get back to you within 1 hour."
)


def _send_admin_email_async(ticket, message_body):
    """Fire-and-forget admin notification email on a background thread."""
    try:
        admin_email = getattr(settings, 'ADMIN_NOTIFICATION_EMAIL', None) or getattr(settings, 'EMAIL_HOST_USER', None)
        if not admin_email:
            return

        admin_url = 'https://' + getattr(settings, 'RENDER_EXTERNAL_HOSTNAME', '127.0.0.1:8000')
        try:
            ticket_admin_path = reverse('admin:dating_supportticket_change', args=[str(ticket.id)])
            admin_url = admin_url + ticket_admin_path
        except Exception:
            pass

        subject = f"[LOVENY Support] New message from {ticket.user.username}"
        body = (
            f"A user has submitted a support message.\n\n"
            f"User: {ticket.user.username} ({ticket.user.email})\n"
            f"Ticket ID: {ticket.id}\n"
            f"Status: {ticket.status}\n"
            f"Page: {ticket.current_page or 'N/A'}\n\n"
            f"Message:\n{message_body}\n\n"
            f"View ticket in admin:\n{admin_url}\n"
        )
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[admin_email],
            fail_silently=True,
        )
    except Exception as exc:
        logger.warning("Support admin email failed: %s", exc)


def _serialize_message(msg):
    return {
        'id': msg.id,
        'message': msg.message,
        'is_from_support': msg.is_from_support,
        'is_auto_reply': msg.is_auto_reply,
        'is_read_by_user': msg.is_read_by_user,
        'created_at': msg.created_at.strftime('%I:%M %p'),
        'created_at_iso': msg.created_at.isoformat(),
        'sender': 'Support' if msg.is_from_support else (msg.sender.username if msg.sender else 'System'),
    }


@login_required
@require_http_methods(["POST"])
def support_send_api(request):
    """
    POST /api/support/send/
    Body: { "message": "...", "current_page": "..." }
    Creates or reuses the user's open/in_progress ticket, saves the user
    message, generates the auto-reply on first message, and notifies admin.
    """
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON.'}, status=400)

    user_message_text = (data.get('message') or '').strip()
    current_page = (data.get('current_page') or '').strip()[:255]

    if not user_message_text:
        return JsonResponse({'status': 'error', 'message': 'Message cannot be empty.'}, status=400)

    # Find an existing open/in-progress ticket for this user, or create one
    ticket = (
        SupportTicket.objects
        .filter(user=request.user, status__in=['open', 'in_progress'])
        .order_by('-created_at')
        .first()
    )
    is_new_ticket = ticket is None

    if is_new_ticket:
        ticket = SupportTicket.objects.create(
            user=request.user,
            status='open',
            current_page=current_page,
        )
    elif current_page and not ticket.current_page:
        ticket.current_page = current_page
        ticket.save(update_fields=['current_page', 'updated_at'])

    # Save the user's message
    user_msg = SupportMessage.objects.create(
        ticket=ticket,
        sender=request.user,
        is_from_support=False,
        is_auto_reply=False,
        message=user_message_text,
        is_read_by_admin=False,
        is_read_by_user=True,
    )

    response_messages = [_serialize_message(user_msg)]

    # Generate the auto-reply only on the very first message in the ticket
    auto_msg = None
    if ticket.messages.count() == 1:  # only the message we just created
        auto_msg = SupportMessage.objects.create(
            ticket=ticket,
            sender=None,
            is_from_support=True,
            is_auto_reply=True,
            message=AUTO_REPLY_TEXT,
            is_read_by_admin=True,
            is_read_by_user=False,
        )
        response_messages.append(_serialize_message(auto_msg))

    # Notify admin via email (non-blocking)
    t = threading.Thread(
        target=_send_admin_email_async,
        args=(ticket, user_message_text),
        daemon=True,
    )
    t.start()

    return JsonResponse({
        'status': 'success',
        'ticket_id': str(ticket.id),
        'messages': response_messages,
    })


@login_required
@require_http_methods(["GET"])
def support_messages_api(request):
    """
    GET /api/support/messages/
    Returns all messages for the user's active support ticket.
    Also marks support replies as read by user.
    """
    ticket = (
        SupportTicket.objects
        .filter(user=request.user, status__in=['open', 'in_progress'])
        .order_by('-created_at')
        .first()
    )

    if not ticket:
        return JsonResponse({'status': 'success', 'messages': [], 'ticket_id': None, 'has_unread': False})

    messages = ticket.messages.order_by('created_at')

    # Count unread support replies for the badge
    unread_count = messages.filter(is_from_support=True, is_read_by_user=False).count()

    return JsonResponse({
        'status': 'success',
        'ticket_id': str(ticket.id),
        'ticket_status': ticket.status,
        'has_unread': unread_count > 0,
        'unread_count': unread_count,
        'messages': [_serialize_message(m) for m in messages],
    })


@login_required
@require_http_methods(["POST"])
def support_mark_read_api(request):
    """
    POST /api/support/mark-read/
    Marks all support replies in the user's active ticket as read.
    """
    ticket = (
        SupportTicket.objects
        .filter(user=request.user, status__in=['open', 'in_progress'])
        .order_by('-created_at')
        .first()
    )
    if ticket:
        ticket.messages.filter(is_from_support=True, is_read_by_user=False).update(is_read_by_user=True)

    return JsonResponse({'status': 'success'})
