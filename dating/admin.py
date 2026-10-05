from django.contrib import admin
from django import forms
# Import VersionAdmin now that the package is installed
from reversion.admin import VersionAdmin 
from .models import (
    CallGift,
    CallSession,
    CallSignal,
    ChatMessage,
    CoinPackage,
    CoinTransaction,
    Conversation,
    GiftItem,
    Match,
    PaymentTransaction,
    Profile,
    ProfilePhoto,
    SiteConfiguration,
    SubscriptionPlan,
    Swipe,
    Tag,
    WithdrawalRequest,
)


class ProfileAdminForm(forms.ModelForm):
    PREMIUM_TIER_CATEGORIES = {
        'dating_premium_tier': 'DATING',
        'hookup_premium_tier': 'HOOKUP',
        'sex_call_premium_tier': 'SEX_CALL',
    }

    class Meta:
        model = Profile
        exclude = ('premium_tier', 'premium_expiry', 'whatsapp_number')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, category in self.PREMIUM_TIER_CATEGORIES.items():
            tier_names = list(
                SubscriptionPlan.objects.filter(category=category)
                .order_by('duration_days', 'tier_name')
                .values_list('tier_name', flat=True)
            )
            choices = [('', '---------')]
            seen_tiers = set()
            for tier_name in tier_names:
                if tier_name not in seen_tiers:
                    choices.append((tier_name, tier_name))
                    seen_tiers.add(tier_name)

            current_tier = self.initial.get(field_name)
            if current_tier and current_tier not in seen_tiers:
                choices.append((current_tier, f'{current_tier} (currently assigned)'))

            self.fields[field_name] = forms.ChoiceField(
                choices=choices,
                required=False,
            )


# Register Tag model
@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ('name',)
    search_fields = ('name',)

# Stacked inline for photos within the profile editor
class ProfilePhotoInline(admin.TabularInline):
    model = ProfilePhoto
    extra = 1 # Allows one extra empty form for new photo upload

# Register Profile model and enable version control (rewind)
@admin.register(Profile)
class ProfileAdmin(VersionAdmin): # Inherit from VersionAdmin
    form = ProfileAdminForm
    list_display = (
        'user', 'relationship_mode', 'coin_balance', 'earned_diamonds',
        'response_rate', 'is_host_ready', 'is_verified', 'is_vip', 'last_active',
    )
    list_editable = ('is_verified', 'is_vip', 'is_host_ready')
    search_fields = ('user__username', 'bio')
    list_filter = (
        'relationship_mode', 'is_host_ready', 'is_verified', 'is_vip', 'gender',
    )
    readonly_fields = ('is_test_profile',)
    filter_horizontal = ('tags',) # Nicer interface for ManyToMany field
    inlines = [ProfilePhotoInline] # Include photo management inline

# Register Swipe model (Read-only for inspection)
@admin.register(Swipe)
class SwipeAdmin(admin.ModelAdmin):
    list_display = ('swiper', 'swiped', 'type', 'timestamp')
    list_filter = ('type',)
    search_fields = ('swiper__username', 'swiped__username')
    readonly_fields = ('swiper', 'swiped', 'type', 'timestamp')

# Register Match model (Prevent manual creation in Admin)
@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    # Only allow listing and viewing details, not editing or adding
    list_display = ('user1', 'user2', 'created_at', 'expires_at', 'is_active')
    readonly_fields = ('user1', 'user2', 'whatsapp_link_id', 'created_at', 'expires_at')
    search_fields = ('user1__username', 'user2__username')
    
    # Override methods to disable adding and deleting manually
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ('conversation', 'sender', 'created_at')
    search_fields = ('sender__username', 'text')
    readonly_fields = ('conversation', 'sender', 'text', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ('id', 'created_at', 'updated_at')
    filter_horizontal = ('participants',)
    readonly_fields = ('created_at', 'updated_at')


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = ('user', 'provider', 'product', 'plan_type', 'coin_package', 'amount_kobo', 'verified_at')
    list_filter = ('provider', 'product', 'plan_type')
    search_fields = ('user__username', 'reference')
    readonly_fields = ('user', 'reference', 'provider', 'product', 'plan_type', 'coin_package', 'amount_kobo', 'currency', 'verified_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CoinPackage)
class CoinPackageAdmin(admin.ModelAdmin):
    list_display = ('name', 'coins', 'bonus_coins', 'total_coins_display', 'price', 'badge', 'is_popular', 'is_active', 'order')
    list_editable = ('price', 'badge', 'is_popular', 'is_active', 'order')
    list_filter = ('is_popular', 'is_active')
    search_fields = ('name',)
    ordering = ('order', 'price')

    def total_coins_display(self, obj):
        return obj.total_coins
    total_coins_display.short_description = 'Total Coins'


@admin.register(CoinTransaction)
class CoinTransactionAdmin(admin.ModelAdmin):
    list_display = ('user', 'transaction_type', 'amount', 'call', 'payment', 'created_at')
    list_filter = ('transaction_type',)
    search_fields = ('user__username', 'description')
    readonly_fields = ('user', 'transaction_type', 'amount', 'description', 'call', 'payment', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CallGift)
class CallGiftAdmin(admin.ModelAdmin):
    list_display = ('call', 'sender', 'receiver', 'gift_type', 'coins_cost', 'created_at')
    list_filter = ('gift_type',)
    search_fields = ('sender__username', 'receiver__username', 'call__room_id')
    readonly_fields = ('call', 'sender', 'receiver', 'gift_type', 'coins_cost', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    list_display = (
        'tier_name',
        'category',
        'duration_days',
        'price',
        'is_popular',
        'is_active',
    )
    list_editable = ('price', 'is_popular', 'is_active')
    list_filter = ('category', 'is_active', 'is_popular')
    search_fields = ('tier_name',)
    ordering = ('category', 'duration_days')


@admin.register(CallSession)
class CallSessionAdmin(admin.ModelAdmin):
    list_display = ('room_id', 'caller', 'receiver', 'status', 'rate_per_minute', 'duration_seconds', 'coins_spent', 'created_at', 'started_at', 'ended_at')
    list_filter = ('status',)
    search_fields = ('room_id', 'caller__username', 'receiver__username')
    readonly_fields = ('room_id', 'caller', 'receiver', 'status', 'rate_per_minute', 'duration_seconds', 'coins_spent', 'created_at', 'started_at', 'ended_at')

    def has_add_permission(self, request):
        return False


@admin.register(CallSignal)
class CallSignalAdmin(admin.ModelAdmin):
    list_display = ('call', 'sender', 'signal_type', 'created_at')
    list_filter = ('signal_type',)
    readonly_fields = ('call', 'sender', 'signal_type', 'payload', 'created_at')

    def has_add_permission(self, request):
        return False


@admin.register(SiteConfiguration)
class SiteConfigurationAdmin(admin.ModelAdmin):
    fieldsets = (
        ('Sex Call Video Economy', {
            'fields': (
                'call_rate_per_minute',
                'grace_period_seconds',
                'host_commission_percentage',
                'welcome_bonus_coins',
            ),
            'description': 'Adjust live call pricing, free grace periods, and host diamond earnings in real-time.',
        }),
        ('Host Diamond Cashout & Payouts', {
            'fields': (
                'min_diamond_withdrawal',
                'diamond_exchange_rate_naira',
            ),
            'description': 'Configure host withdrawal minimums and exchange rate to Nigerian Naira (₦).',
        }),
        ('Live Announcements & Promotions', {
            'fields': (
                'is_announcement_active',
                'announcement_banner',
            ),
            'description': 'Display promotional announcements or event banners to all users across the app.',
        }),
    )

    def has_add_permission(self, request):
        # Enforce singleton - only 1 configuration object allowed
        return not SiteConfiguration.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(GiftItem)
class GiftItemAdmin(admin.ModelAdmin):
    list_display = ('name', 'icon', 'slug', 'coin_cost', 'is_active', 'order')
    list_editable = ('icon', 'coin_cost', 'is_active', 'order')
    list_filter = ('is_active',)
    search_fields = ('name', 'slug')
    ordering = ('order', 'coin_cost')


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):
    list_display = ('user', 'diamonds_amount', 'naira_amount', 'bank_name', 'account_number', 'account_name', 'status', 'created_at', 'processed_at')
    list_filter = ('status', 'bank_name', 'created_at')
    search_fields = ('user__username', 'account_number', 'account_name', 'bank_name', 'admin_note')
    list_editable = ('status',)
    readonly_fields = ('user', 'diamonds_amount', 'naira_amount', 'bank_name', 'account_number', 'account_name', 'created_at')
    actions = ['mark_as_paid', 'mark_as_approved', 'mark_as_rejected']

    def mark_as_paid(self, request, queryset):
        from django.utils import timezone
        queryset.update(status='PAID', processed_at=timezone.now())
    mark_as_paid.short_description = 'Mark selected requests as PAID OUT'

    def mark_as_approved(self, request, queryset):
        queryset.update(status='APPROVED')
    mark_as_approved.short_description = 'Mark selected requests as APPROVED'

    def mark_as_rejected(self, request, queryset):
        from django.utils import timezone
        queryset.update(status='REJECTED', processed_at=timezone.now())
    mark_as_rejected.short_description = 'Mark selected requests as REJECTED'