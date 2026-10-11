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
from functools import wraps
from django.conf import settings
from django.core.mail import send_mail
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_http_methods

from .models import SupportTicket, SupportMessage
from .services.support_bot import process_support_inquiry

logger = logging.getLogger(__name__)

ACTIVE_TICKET_STATUSES = ['open', 'in_progress', 'needs_admin']


def _unauthenticated_response():
    return JsonResponse(
        {'status': 'unauthenticated', 'messages': []},
        status=401,
    )


def authenticated_support_api(view):
    @csrf_exempt
    @wraps(view)
    def wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _unauthenticated_response()
        return csrf_protect(view)(request, *args, **kwargs)

    return wrapped_view


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


@require_http_methods(["POST"])
@authenticated_support_api
def support_send_api(request):
    """
    POST /api/support/send/
    Body: { "message": "...", "current_page": "..." }
    Creates or reuses the user's open/in_progress ticket, saves the user
    message, generates the auto-reply on first message, and notifies admin.
    """
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON.'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'status': 'error', 'message': 'Invalid request.'}, status=400)

    message = data.get('message')
    current_page = data.get('current_page', '')
    if not isinstance(message, str) or not isinstance(current_page, str):
        return JsonResponse({'status': 'error', 'message': 'Invalid request.'}, status=400)

    user_message_text = message.strip()
    current_page = current_page.strip()[:255]

    if not user_message_text:
        return JsonResponse({'status': 'error', 'message': 'Message cannot be empty.'}, status=400)

    # Find an existing open/in-progress ticket for this user, or create one
    ticket = (
        SupportTicket.objects
        .filter(user=request.user, status__in=ACTIVE_TICKET_STATUSES)
        .order_by('-created_at')
        .first()
    )
    if ticket is None:
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

    bot_answer = process_support_inquiry(ticket, user_message_text)
    auto_msg = SupportMessage.objects.create(
        ticket=ticket,
        sender=None,
        is_from_support=True,
        is_auto_reply=True,
        message=bot_answer,
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
        'ticket_status': ticket.status,
        'messages': response_messages,
    })


@require_http_methods(["GET"])
@authenticated_support_api
def support_messages_api(request):
    """
    GET /api/support/messages/
    Returns all messages for the user's active support ticket.
    Also marks support replies as read by user.
    """
    ticket = (
        SupportTicket.objects
        .filter(user=request.user, status__in=ACTIVE_TICKET_STATUSES)
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


@require_http_methods(["POST"])
@authenticated_support_api
def support_mark_read_api(request):
    """
    POST /api/support/mark-read/
    Marks all support replies in the user's active ticket as read.
    """
    ticket = (
        SupportTicket.objects
        .filter(user=request.user, status__in=ACTIVE_TICKET_STATUSES)
        .order_by('-created_at')
        .first()
    )
    if ticket:
        ticket.messages.filter(is_from_support=True, is_read_by_user=False).update(is_read_by_user=True)

    return JsonResponse({'status': 'success'})
