"""
Support ticket and messaging models for the in-app support chat widget.
These are kept in a separate file and imported into models.py to keep
the codebase tidy.
"""
import uuid
from django.conf import settings
from django.db import models


class SupportTicket(models.Model):
    STATUS_CHOICES = [
        ('open', 'Open'),
        ('in_progress', 'In Progress'),
        ('resolved', 'Resolved'),
        ('closed', 'Closed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='support_tickets',
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    current_page = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Support Ticket'
        verbose_name_plural = 'Support Tickets'

    def __str__(self):
        return f'Ticket #{str(self.id)[:8]} – {self.user.username} [{self.status}]'

    @property
    def unread_count(self):
        """Number of user messages not yet read by admin staff."""
        return self.messages.filter(is_read_by_admin=False, is_from_support=False).count()

    @property
    def has_unread_for_admin(self):
        return self.messages.filter(is_read_by_admin=False, is_from_support=False).exists()


class SupportMessage(models.Model):
    ticket = models.ForeignKey(
        SupportTicket,
        on_delete=models.CASCADE,
        related_name='messages',
    )
    # null = True means this is a system / auto-reply message
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='support_messages_sent',
    )
    is_from_support = models.BooleanField(default=False)
    is_auto_reply = models.BooleanField(default=False)
    message = models.TextField()
    is_read_by_admin = models.BooleanField(default=False)
    is_read_by_user = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = 'Support Message'
        verbose_name_plural = 'Support Messages'

    def __str__(self):
        sender_label = 'Support' if self.is_from_support else (
            self.sender.username if self.sender else 'System'
        )
        return f'[{self.ticket.id!s:.8}] {sender_label}: {self.message[:60]}'
