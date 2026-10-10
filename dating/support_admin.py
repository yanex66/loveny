"""
Support Chat Admin configuration — appended to dating/admin.py registration.
"""
from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse

from .models import SupportTicket, SupportMessage


# ── Inline ─────────────────────────────────────────────────────────────────────

class SupportMessageInline(admin.TabularInline):
    model = SupportMessage
    extra = 1
    fields = ('sender', 'message', 'is_from_support', 'is_auto_reply', 'is_read_by_admin', 'is_read_by_user', 'created_at')
    readonly_fields = ('sender', 'is_auto_reply', 'created_at')
    ordering = ('created_at',)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('sender')

    def save_model(self, request, obj, form, change):
        # When admin writes a reply via inline, auto-mark it as from support
        if not obj.pk and not obj.is_auto_reply:
            obj.is_from_support = True
            obj.is_read_by_admin = True
            obj.is_read_by_user = False
        super().save_model(request, obj, form, change)


# ── Custom Admin Filter ─────────────────────────────────────────────────────────

class UnreadByAdminFilter(admin.SimpleListFilter):
    title = 'Admin Unread Messages'
    parameter_name = 'unread_admin'

    def lookups(self, request, model_admin):
        return [
            ('yes', '🔴 Unread (needs reply)'),
            ('no', 'All read'),
        ]

    def queryset(self, qs, request):
        if self.value() == 'yes':
            from django.db.models import Exists, OuterRef
            unread_msgs = SupportMessage.objects.filter(
                ticket=OuterRef('pk'),
                is_read_by_admin=False,
                is_from_support=False,
            )
            return qs.filter(Exists(unread_msgs))
        if self.value() == 'no':
            from django.db.models import Exists, OuterRef
            unread_msgs = SupportMessage.objects.filter(
                ticket=OuterRef('pk'),
                is_read_by_admin=False,
                is_from_support=False,
            )
            return qs.exclude(Exists(unread_msgs))
        return qs


# ── Main Ticket Admin ──────────────────────────────────────────────────────────

@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ('short_id', 'user_link', 'status_badge', 'unread_badge', 'current_page', 'created_at', 'updated_at')
    list_filter = (UnreadByAdminFilter, 'status', 'created_at')
    search_fields = ('user__username', 'user__email', 'current_page', 'id')
    readonly_fields = ('id', 'created_at', 'updated_at', 'user')
    ordering = ('-updated_at',)
    inlines = [SupportMessageInline]

    fieldsets = (
        ('Ticket Info', {
            'fields': ('id', 'user', 'status', 'current_page'),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    def short_id(self, obj):
        return str(obj.id)[:8].upper()
    short_id.short_description = 'Ticket ID'

    def user_link(self, obj):
        url = reverse('admin:auth_user_change', args=[obj.user.pk])
        return format_html('<a href="{}">{}</a>', url, obj.user.username)
    user_link.short_description = 'User'
    user_link.admin_order_field = 'user__username'

    def status_badge(self, obj):
        colors = {
            'open': '#e53e3e',
            'in_progress': '#dd6b20',
            'resolved': '#276749',
            'closed': '#718096',
        }
        color = colors.get(obj.status, '#718096')
        return format_html(
            '<span style="background:{};color:white;padding:2px 8px;border-radius:9999px;font-size:11px;font-weight:bold;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = 'Status'
    status_badge.admin_order_field = 'status'

    def unread_badge(self, obj):
        count = obj.messages.filter(is_read_by_admin=False, is_from_support=False).count()
        if count:
            return format_html(
                '<span style="background:#e53e3e;color:white;padding:2px 8px;border-radius:9999px;font-size:11px;font-weight:bold;">&#128276; {} Unread</span>',
                count,
            )
        return format_html('<span style="color:#48bb78;font-size:11px;">&#10003; All Read</span>')
    unread_badge.short_description = 'Unread by Admin'

    def save_formset(self, request, form, formset, change):
        """When admin saves inline replies, mark them as from support and update ticket."""
        instances = formset.save(commit=False)
        for obj in instances:
            if isinstance(obj, SupportMessage):
                # New message added by admin → it's a support reply
                if not obj.pk:
                    obj.is_from_support = True
                    obj.is_read_by_admin = True
                    obj.is_read_by_user = False
                    obj.sender = request.user
                obj.save()
                # Upgrade ticket status when admin replies
                if obj.is_from_support and not obj.is_auto_reply:
                    SupportTicket.objects.filter(pk=obj.ticket_id, status='open').update(status='in_progress')
        formset.save_m2m()
        # Mark user messages as read once admin views ticket
        if change:
            obj_id = form.instance.pk
            SupportMessage.objects.filter(
                ticket_id=obj_id,
                is_from_support=False,
                is_read_by_admin=False,
            ).update(is_read_by_admin=True)


# ── Standalone Message Admin ───────────────────────────────────────────────────

@admin.register(SupportMessage)
class SupportMessageAdmin(admin.ModelAdmin):
    list_display = ('short_ticket', 'sender_label', 'is_from_support', 'is_auto_reply', 'is_read_by_admin', 'created_at')
    list_filter = ('is_from_support', 'is_auto_reply', 'is_read_by_admin', 'is_read_by_user')
    search_fields = ('message', 'ticket__user__username', 'sender__username')
    readonly_fields = ('ticket', 'sender', 'is_auto_reply', 'created_at')
    ordering = ('-created_at',)

    def short_ticket(self, obj):
        url = reverse('admin:dating_supportticket_change', args=[obj.ticket_id])
        return format_html('<a href="{}">#{}</a>', url, str(obj.ticket_id)[:8].upper())
    short_ticket.short_description = 'Ticket'

    def sender_label(self, obj):
        if obj.is_auto_reply:
            return '🤖 Auto-reply'
        if obj.is_from_support:
            return '🛡️ Support'
        return obj.sender.username if obj.sender else '—'
    sender_label.short_description = 'From'
