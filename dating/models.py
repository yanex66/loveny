import uuid
import os
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.contrib.auth import get_user_model 
from django.utils import timezone
from django.core.validators import MaxValueValidator, MinValueValidator
from django.core.exceptions import ValidationError
from django.db.models import F, Q

User = get_user_model() 

# --- Choices ---
GENDER_CHOICES = (
    ('M', 'Male'),
    ('F', 'Female'),
)

SWIPE_CHOICES = (
    ('LIKE', 'Like'),
    ('PASS', 'Pass'),
)

RELATIONSHIP_MODE_CHOICES = (
    ('DATING', 'Dating'),
    ('HOOKUP', 'Hookup'),
    ('SEX_CALL', 'Sex Call'),
)

class Tag(models.Model):
    """
    Model for mandatory passion tags (e.g., #Hiking, #Gamer).
    """
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name

class Profile(models.Model):
    """
    Extends the Django User model to hold dating profile data.
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    
    # Core fields - Fix: Age cannot be below 18
    age = models.IntegerField(
        null=True, 
        blank=True, 
        validators=[MinValueValidator(18)]
    )
    gender = models.CharField(max_length=1, choices=GENDER_CHOICES, default='M')
    preferred_gender = models.CharField(max_length=1, choices=GENDER_CHOICES, default='F', help_text="Gender preference for profiles to see.")
    relationship_mode = models.CharField(max_length=12, choices=RELATIONSHIP_MODE_CHOICES, default='DATING')

    # Age Preferences - Fix: Minimum preference cannot be below 18
    min_age_pref = models.IntegerField(
        default=18, 
        validators=[MinValueValidator(18)],
        help_text="Minimum age of people you want to see."
    )
    max_age_pref = models.IntegerField(default=50, help_text="Maximum age of people you want to see.")

    # Detailed info
    bio = models.CharField(max_length=200, blank=True)
    location = models.CharField(max_length=100, blank=True)
    latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )
    longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )
    max_distance_km = models.PositiveSmallIntegerField(
        default=100, validators=[MinValueValidator(1), MaxValueValidator(500)],
    )
    job_title = models.CharField(max_length=100, blank=True)

    whatsapp_number = models.CharField(
        max_length=20,
        blank=False,
        default='',
        help_text='Shared with your mutual match after both participants unlock the contact.',
    )
    match_credits = models.PositiveIntegerField(default=0)
    first_date_idea = models.CharField(max_length=255, blank=True, help_text="My ideal first date is...")
    
    # Tags
    tags = models.ManyToManyField(Tag, blank=True, help_text="Select up to 5 passion tags.")

    # Status
    last_active = models.DateTimeField(default=timezone.now)
    show_in_discovery = models.BooleanField(default=True)
    incognito_mode = models.BooleanField(default=False, help_text="Hide profile unless you like someone first (Premium).")
    allow_messages = models.BooleanField(default=True)
    last_checkin_date = models.DateField(null=True, blank=True, help_text="Last daily coin reward check-in date")
    checkin_streak = models.PositiveSmallIntegerField(default=0, help_text="Consecutive 7-day daily check-in streak (0 to 7)")
    has_claimed_welcome = models.BooleanField(default=False, help_text="Whether newcomer joining gift has been claimed")
    free_trial_mode = models.CharField(
        max_length=12,
        choices=RELATIONSHIP_MODE_CHOICES,
        blank=True,
        help_text="Mode covered by this user's new-account free trial",
    )
    free_trial_expires_at = models.DateTimeField(null=True, blank=True)
    has_paid_mode_switch_fee = models.BooleanField(
        default=False,
        help_text="Whether this account has paid once to permanently unlock profile-mode switching",
    )
    off_platform_attempts = models.PositiveIntegerField(default=0, help_text="Count of detected contact-info off-platform leak attempts")
    is_test_profile = models.BooleanField(default=False, editable=False)

    # --- PREMIUM FEATURES ---
    premium_tier = models.CharField(max_length=20, blank=True, null=True) 
    premium_expiry = models.DateTimeField(null=True, blank=True)
    dating_premium_tier = models.CharField(max_length=20, blank=True)
    dating_premium_expiry = models.DateTimeField(null=True, blank=True)
    hookup_premium_tier = models.CharField(max_length=50, blank=True)
    hookup_premium_expiry = models.DateTimeField(null=True, blank=True)
    sex_call_premium_tier = models.CharField(max_length=50, blank=True)
    sex_call_premium_expiry = models.DateTimeField(null=True, blank=True)
    is_verified = models.BooleanField(default=False)
    is_vip = models.BooleanField(default=False)

    # --- COIN & DIAMOND WALLET ---
    coin_balance = models.PositiveIntegerField(default=0, help_text="Available coin balance for gifts")
    earned_diamonds = models.PositiveIntegerField(default=0, help_text="Diamonds earned from received gifts")

    @property
    def verification_photo_count(self):
        return sum(1 for photo in self.photos.all() if not photo.is_video)

    @property
    def is_verification_eligible(self):
        return self.verification_photo_count >= 4

    @property
    def has_unlocked_mode_switching(self):
        return bool(self.has_paid_mode_switch_fee)

    @property
    def is_dating_premium(self):
        return bool(
            (
                self.dating_premium_expiry
                and self.dating_premium_expiry > timezone.now()
            )
            or self.has_free_trial_access('DATING')
        )

    @property
    def is_hookup_premium(self):
        return bool(
            (
                self.hookup_premium_expiry
                and self.hookup_premium_expiry > timezone.now()
            )
            or self.has_free_trial_access('HOOKUP')
        )

    @property
    def is_sex_call_premium(self):
        return bool(
            (
                self.sex_call_premium_expiry
                and self.sex_call_premium_expiry > timezone.now()
            )
            or self.has_free_trial_access('SEX_CALL')
        )

    def has_free_trial_access(self, mode=None):
        active_mode = mode or self.relationship_mode
        return bool(
            self.free_trial_mode == active_mode
            and self.free_trial_expires_at
            and self.free_trial_expires_at > timezone.now()
        )

    @property
    def is_hookup_sexcall_premium(self):
        return self.is_hookup_premium or self.is_sex_call_premium

    def is_premium(self, mode=None):
        active_mode = mode or self.relationship_mode
        if self.has_free_trial_access(active_mode):
            return True
        if active_mode == 'DATING':
            return self.is_dating_premium
        if active_mode == 'HOOKUP':
            return self.is_hookup_premium
        if active_mode == 'SEX_CALL':
            return self.is_sex_call_premium
        return False

    def premium_tier_for(self, mode=None):
        active_mode = mode or self.relationship_mode
        if active_mode == 'DATING':
            return self.dating_premium_tier
        if active_mode == 'HOOKUP':
            return self.hookup_premium_tier
        if active_mode == 'SEX_CALL':
            return self.sex_call_premium_tier
        return ''

    def clean(self):
        super().clean()
        if self.min_age_pref > self.max_age_pref:
            raise ValidationError({'max_age_pref': 'Maximum age must be at least the minimum age.'})

    class Meta:
        verbose_name = 'Dating Profile'
        verbose_name_plural = 'Dating Profiles'

    def __str__(self):
        return self.user.username

class ProfilePhoto(models.Model):
    user = models.ForeignKey(User, related_name='all_profile_photos', on_delete=models.CASCADE, null=True, blank=True)
    mode = models.CharField(max_length=12, choices=RELATIONSHIP_MODE_CHOICES, default='DATING')
    profile = models.ForeignKey(Profile, related_name='photos', on_delete=models.SET_NULL, null=True, blank=True)
    image = models.FileField(upload_to='profile_media/')
    is_main = models.BooleanField(default=False)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['uploaded_at']
        verbose_name_plural = 'Profile Media'

    def __str__(self):
        username = self.user.username if self.user else (self.profile.user.username if self.profile else "Unknown")
        return f"Media for {username} in {self.mode} (Main: {self.is_main})"

    @property
    def is_video(self):
        name, extension = os.path.splitext(self.image.name)
        return extension.lower() in ['.mp4', '.mov', '.avi', '.webm', '.mkv']

class Swipe(models.Model):
    swiper = models.ForeignKey(User, related_name='given_swipes', on_delete=models.CASCADE)
    swiped = models.ForeignKey(User, related_name='received_swipes', on_delete=models.CASCADE)
    type = models.CharField(max_length=5, choices=SWIPE_CHOICES)
    mode = models.CharField(max_length=12, choices=RELATIONSHIP_MODE_CHOICES, default='DATING')
    is_direct = models.BooleanField(default=False)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('swiper', 'swiped')
        indexes = [
            models.Index(fields=['swiped', 'swiper']),
        ]

    def __str__(self):
        return f"{self.swiper.username} -> {self.type} -> {self.swiped.username}"

class Match(models.Model):
    user1 = models.ForeignKey(User, related_name='matches_as_user1', on_delete=models.CASCADE)
    user2 = models.ForeignKey(User, related_name='matches_as_user2', on_delete=models.CASCADE)
    whatsapp_link_id = models.UUIDField(default=uuid.uuid4, unique=True)
    mode = models.CharField(max_length=12, choices=RELATIONSHIP_MODE_CHOICES, default='DATING')
    has_direct_interest = models.BooleanField(default=False)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user1', 'user2')

    def __str__(self):
        return f"Match: {self.user1.username} & {self.user2.username}"

    def is_active(self):
        return self.expires_at > timezone.now()


class Conversation(models.Model):
    participants = models.ManyToManyField(
        User,
        related_name='conversations',
        db_table='dating_conversation_participants',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ('-updated_at', '-id')

    def __str__(self):
        return f'Conversation {self.pk}'


class ChatMessage(models.Model):
    MESSAGE_TYPES = (
        ('text', 'Text'),
        ('gift', 'Gift'),
        ('sticker', 'Sticker'),
        ('image', 'Image'),
    )

    conversation = models.ForeignKey(
        Conversation,
        related_name='messages',
        on_delete=models.CASCADE,
    )
    sender = models.ForeignKey(User, related_name='chat_messages', on_delete=models.CASCADE)
    text = models.TextField()
    message_type = models.CharField(max_length=20, choices=MESSAGE_TYPES, default='text', db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    read_by = models.ManyToManyField(
        User,
        related_name='read_chat_messages',
        blank=True,
    )
    notification_delivered_to = models.ManyToManyField(
        User,
        related_name='delivered_chat_notifications',
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ('created_at', 'id')
        indexes = [
            models.Index(fields=('conversation', 'created_at')),
        ]

    def __str__(self):
        return f"[{self.message_type}] Message from {self.sender.username} in conversation {self.conversation_id}"


class SubscriptionPlan(models.Model):
    CATEGORY_CHOICES = (
        ('DATING', 'Dating'),
        ('HOOKUP', 'Hookup'),
        ('SEX_CALL', 'Sex Call'),
    )

    category = models.CharField(max_length=12, choices=CATEGORY_CHOICES)
    tier_name = models.CharField(max_length=50)
    duration_days = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1)],
    )
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(0.01)],
        help_text='Price in Nigerian naira (₦).',
    )
    is_popular = models.BooleanField(default=False)
    features = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ('category', 'duration_days')
        constraints = [
            models.UniqueConstraint(
                fields=('category', 'duration_days'),
                name='unique_subscription_plan_category_duration',
            ),
            models.CheckConstraint(
                condition=Q(price__gt=0),
                name='subscription_plan_price_positive',
            ),
        ]

    def clean(self):
        super().clean()
        if not isinstance(self.features, list) or any(
            not isinstance(feature, str) or not feature.strip()
            for feature in self.features
        ):
            raise ValidationError({
                'features': 'Features must be a list of non-empty text items.',
            })

    def __str__(self):
        return f'{self.get_category_display()} {self.tier_name} ({self.duration_days} days)'


class CoinPackage(models.Model):
    name = models.CharField(max_length=50)
    coins = models.PositiveIntegerField(help_text="Base coins")
    bonus_coins = models.PositiveIntegerField(default=0, help_text="Extra free bonus coins")
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(0.01)],
        help_text='Price in Nigerian naira (₦).',
    )
    is_popular = models.BooleanField(default=False)
    badge = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ('order', 'price')

    @property
    def total_coins(self):
        return self.coins + self.bonus_coins

    def __str__(self):
        return f"{self.name}: {self.total_coins} Coins (₦{self.price})"


class PaymentTransaction(models.Model):
    PROVIDER_CHOICES = (
        ('paystack', 'Paystack'),
        ('flutterwave', 'Flutterwave'),
    )
    PRODUCT_CHOICES = (
        ('DATING', 'Dating Premium'),
        ('HOOKUP', 'Hookup Premium'),
        ('SEX_CALL', 'Sex Call Premium'),
        ('COINS', 'Coin Pack'),
        ('HOOKUP_MATCH', 'Hookup Match Connection Fee'),
        ('CONTACT_MATCH', 'WhatsApp Contact Match Unlock'),
        ('MODE_SWITCH', 'Connection Mode Switch'),
    )

    user = models.ForeignKey(User, related_name='premium_payments', on_delete=models.CASCADE)
    reference = models.CharField(max_length=100, unique=True)
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES)
    product = models.CharField(max_length=20, choices=PRODUCT_CHOICES, default='DATING')
    plan_type = models.CharField(max_length=50)
    plan = models.ForeignKey(
        SubscriptionPlan,
        related_name='payments',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    coin_package = models.ForeignKey(
        CoinPackage,
        related_name='payments',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    amount_kobo = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default='NGN')
    verified_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-verified_at']

    def __str__(self):
        return f"{self.provider} payment {self.reference} for {self.user.username}"


class ConnectionFeeSetting(models.Model):
    dating_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('1000.00'))
    hookup_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('2000.00'))
    sexcall_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('1500.00'))

    class Meta:
        verbose_name = 'Connection Fee Setting'
        verbose_name_plural = 'Connection Fee Settings'

    def __str__(self):
        return 'Connection fees'

    def save(self, *args, **kwargs):
        self.pk = 1
        kwargs.pop('force_insert', None)
        super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        setting, _ = cls.objects.get_or_create(pk=1)
        return setting


class ContactMatch(models.Model):
    MODE_CHOICES = (
        ('DATING', 'Dating'),
        ('HOOKUP', 'Hookup'),
        ('SEXCALL', 'Sex Call'),
    )

    user_a = models.ForeignKey(User, related_name='contact_matches_as_a', on_delete=models.CASCADE)
    user_b = models.ForeignKey(User, related_name='contact_matches_as_b', on_delete=models.CASCADE)
    mode = models.CharField(max_length=10, choices=MODE_CHOICES)
    user_a_paid = models.BooleanField(default=False)
    user_b_paid = models.BooleanField(default=False)
    user_a_paid_with_credit = models.BooleanField(default=False)
    user_b_paid_with_credit = models.BooleanField(default=False)
    is_unlocked = models.BooleanField(default=False)
    is_cancelled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    unlocked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-created_at',)
        constraints = [
            models.UniqueConstraint(
                fields=('user_a', 'user_b'),
                condition=Q(is_cancelled=False),
                name='unique_active_contact_match_pair',
            ),
            models.CheckConstraint(condition=~Q(user_a=F('user_b')), name='contact_match_users_differ'),
        ]

    def __str__(self):
        return f'{self.user_a} ↔ {self.user_b} ({self.mode})'

    def mark_paid(self, user, *, used_credit=False):
        if user.pk == self.user_a_id:
            self.user_a_paid = True
            self.user_a_paid_with_credit = used_credit
        elif user.pk == self.user_b_id:
            self.user_b_paid = True
            self.user_b_paid_with_credit = used_credit
        else:
            raise ValueError('Only a contact match participant can pay.')
        if self.user_a_paid and self.user_b_paid:
            self.is_unlocked = True
            self.unlocked_at = timezone.now()


class CoinTransaction(models.Model):
    TRANSACTION_TYPES = (
        ('PURCHASE', 'Coin Purchase'),
        ('WELCOME_BONUS', 'Welcome Bonus'),
        ('GIFT_SENT', 'Gift Sent'),
        ('GIFT_RECEIVED', 'Gift Received'),
        ('DIAMOND_CONVERSION', 'Diamond to Coin Exchange'),
        ('DAILY_CHECKIN', 'Daily Check-In Reward'),
        ('REFERRAL_REWARD', 'Referral Reward'),
    )

    user = models.ForeignKey(User, related_name='coin_transactions', on_delete=models.CASCADE)
    sender = models.ForeignKey(User, null=True, blank=True, related_name='sent_coin_transactions', on_delete=models.SET_NULL)
    recipient = models.ForeignKey(User, null=True, blank=True, related_name='received_coin_transactions', on_delete=models.SET_NULL)
    gift_type = models.CharField(max_length=50, blank=True, help_text="e.g. rose, heart, cocktail, car, diamond, yacht, ring")
    amount = models.IntegerField(help_text="Coins (positive for credit, negative for debit) or Diamonds")
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    description = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    payment = models.ForeignKey(PaymentTransaction, null=True, blank=True, on_delete=models.SET_NULL, related_name='coin_transactions')

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('user', 'created_at')),
        ]

    @property
    def timestamp(self):
        return self.created_at

    def __str__(self):
        return f"{self.user.username}: {self.amount} ({self.transaction_type})"


def get_default_wallet_coins():
    try:
        return SiteConfiguration.get_solo().default_user_coins
    except Exception:
        return 100

class CoinWallet(models.Model):
    user = models.OneToOneField(User, related_name='coin_wallet', on_delete=models.CASCADE)
    coin_balance = models.PositiveIntegerField(default=get_default_wallet_coins, help_text="Total spendable coin balance")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f"{self.user.username}'s Wallet: {self.coin_balance} coins"


class SiteConfiguration(models.Model):
    """
    Global system settings configurable directly in Admin.
    Singleton pattern - automatically retrieves or creates pk=1.
    """
    host_commission_percentage = models.PositiveIntegerField(
        default=70,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
        help_text="Percentage of gift coins awarded to recipients in diamonds (e.g. 70%)",
    )
    welcome_bonus_coins = models.PositiveIntegerField(
        default=30,
        help_text="Free starter coins granted to new users",
    )
    daily_checkin_coins = models.PositiveIntegerField(
        default=5,
        help_text="Coins awarded daily on Days 1 through 6 of the check-in streak",
    )
    day_7_bonus_coins = models.PositiveIntegerField(
        default=10,
        help_text="Bonus coins awarded on Day 7 of the check-in streak (Grand Prize)",
    )
    day_7_bonus_diamonds = models.PositiveIntegerField(
        default=20,
        help_text="Diamonds awarded on Day 7 of the check-in streak (Grand Prize)",
    )
    mode_switch_fee = models.PositiveIntegerField(
        default=200,
        help_text="Amount in Naira to charge when switching profile modes",
    )
    diamond_to_coin_percentage = models.PositiveIntegerField(
        default=70,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
        help_text="Coins received per 100 Diamonds exchanged (e.g. 70 means 100💎 = 70🪙). Closed loop - no real-money cashout.",
    )
    announcement_banner = models.CharField(
        max_length=255,
        blank=True,
        help_text="Top promotional banner text (e.g. '🔥 Weekend Special: 2x Host Diamonds!')",
    )
    is_announcement_active = models.BooleanField(
        default=False,
        help_text="Enable announcement banner across the app",
    )
    
    # --- Advanced Controls ---
    referral_reward_coins = models.PositiveIntegerField(
        default=100,
        help_text="Coins credited to the referrer for each successful new referral",
    )
    free_trial_enabled = models.BooleanField(
        default=True,
        help_text="Give new users a free premium-access trial in their selected mode",
    )
    free_trial_duration_hours = models.PositiveSmallIntegerField(
        default=24,
        validators=[MinValueValidator(1)],
        help_text="Length of the new-user free premium-access trial in hours",
    )
    default_user_coins = models.PositiveIntegerField(
        default=100,
        help_text="Default coin balance for new user wallets",
    )
    
    # --- System Status ---
    is_maintenance_mode = models.BooleanField(
        default=False,
        help_text="Enable maintenance mode to display maintenance page to non-admins",
    )
    maintenance_message = models.TextField(
        default="We are currently undergoing maintenance. Please check back later.",
        help_text="Message displayed when maintenance mode is active",
    )
    
    # --- Contact & Legal ---
    support_email = models.EmailField(
        blank=True,
        help_text="Contact email for support",
    )
    support_whatsapp = models.CharField(
        max_length=20,
        blank=True,
        help_text="WhatsApp number for support",
    )
    terms_of_service_url = models.URLField(
        blank=True,
        help_text="URL to Terms of Service",
    )
    privacy_policy_url = models.URLField(
        blank=True,
        help_text="URL to Privacy Policy",
    )

    class Meta:
        verbose_name = 'Site Configuration'
        verbose_name_plural = 'Site Configuration'

    def __str__(self):
        return "LOVENY System Configuration"

    def save(self, *args, **kwargs):
        self.pk = 1
        kwargs.pop('force_insert', None)
        super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        config, _ = cls.objects.get_or_create(pk=1)
        return config


class GiftItem(models.Model):
    """
    Customizable in-call gifts manageable directly from Admin.
    """
    name = models.CharField(max_length=50)
    slug = models.SlugField(max_length=50, unique=True, help_text="Unique key (e.g. rose, kiss, champagne)")
    icon = models.CharField(max_length=20, default='🎁', help_text="Emoji symbol (e.g. 🌹, 💋, 🥂, 👑, 🏎️)")
    coin_cost = models.PositiveIntegerField(default=10, help_text="Coin cost to send")
    is_active = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(default=0, help_text="Display order in gift drawer")

    class Meta:
        ordering = ('order', 'coin_cost')

    def __str__(self):
        return f"{self.icon} {self.name} ({self.coin_cost} Coins)"


class HookupSettings(models.Model):
    connection_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('1500.00'),
        validators=[MinValueValidator(Decimal('0.01'))],
        help_text='NGN each participant must pay after accepting a Hookup request',
    )

    class Meta:
        verbose_name = 'Hookup Settings'
        verbose_name_plural = 'Hookup Settings'

    def __str__(self):
        return f'Hookup connection fee: ₦{self.connection_fee}'

    def save(self, *args, **kwargs):
        self.pk = 1
        kwargs.pop('force_insert', None)
        super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        settings, _ = cls.objects.get_or_create(pk=1)
        return settings


class HookupMatch(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending Acceptance'),
        ('accepted', 'Accepted - Awaiting Payment'),
        ('unlocked', 'Unlocked / Active Chat'),
        ('expired', 'Expired (24h Elapsed)'),
        ('declined', 'Declined'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    initiator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name='hookup_requests_sent',
        on_delete=models.CASCADE,
    )
    target = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name='hookup_requests_received',
        on_delete=models.CASCADE,
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    target_notification_delivered_at = models.DateTimeField(null=True, blank=True)
    initiator_paid = models.BooleanField(default=False)
    target_paid = models.BooleanField(default=False)
    connection_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('1500.00'),
        help_text="NGN connection fee each participant must pay for this match",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    unlocked_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('initiator', 'status')),
            models.Index(fields=('target', 'status')),
        ]

    def __str__(self):
        return f"HookupMatch {self.id}: {self.initiator.username} <-> {self.target.username} ({self.status})"

    def is_fully_paid(self):
        return self.initiator_paid and self.target_paid

    @property
    def is_active(self):
        if self.status != 'unlocked':
            return False
        return bool(self.expires_at and self.expires_at > timezone.now())

    @property
    def time_remaining_seconds(self):
        if not self.expires_at or self.status != 'unlocked':
            return 0
        delta = (self.expires_at - timezone.now()).total_seconds()
        return max(0, int(delta))

    def check_and_update_expiry(self):
        if self.status == 'unlocked' and self.expires_at and self.expires_at <= timezone.now():
            self.status = 'expired'
            self.save(update_fields=['status'])
            return True
        return False


class HookupMessage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    match = models.ForeignKey(HookupMatch, related_name='messages', on_delete=models.CASCADE)
    sender = models.ForeignKey(User, related_name='hookup_messages', on_delete=models.CASCADE)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ('created_at',)
        indexes = [
            models.Index(fields=('match', 'created_at')),
        ]

    def __str__(self):
        return f"Message by {self.sender.username} in {self.match.id}"


User.add_to_class(
    'datingprofile',
    property(lambda u: getattr(u, 'profile', None) or Profile.objects.filter(user=u).first())
)

class UserBlock(models.Model):
    blocker = models.ForeignKey(User, related_name='blocking', on_delete=models.CASCADE)
    blocked = models.ForeignKey(User, related_name='blockers', on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('blocker', 'blocked')
        verbose_name = "User Block"
        verbose_name_plural = "User Blocks"

    def __str__(self):
        return f"{self.blocker.username} blocked {self.blocked.username}"

class UserReport(models.Model):
    REASON_CHOICES = [
        ('spam', 'Spam or Scam'),
        ('inappropriate', 'Inappropriate Content'),
        ('harassment', 'Harassment or Abuse'),
        ('fake', 'Fake Profile'),
        ('underage', 'Underage User'),
        ('off_platform', 'Soliciting Off-Platform'),
        ('other', 'Other'),
    ]
    reporter = models.ForeignKey(User, related_name='reports_made', on_delete=models.CASCADE)
    reported = models.ForeignKey(User, related_name='reports_received', on_delete=models.CASCADE)
    reason = models.CharField(max_length=20, choices=REASON_CHOICES)
    details = models.TextField(blank=True, null=True)
    is_resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "User Report"
        verbose_name_plural = "User Reports"

    def __str__(self):
        return f"Report: {self.reported.username} ({self.reason})"

class ProfileView(models.Model):
    viewer = models.ForeignKey(User, related_name='viewed_profiles', on_delete=models.CASCADE)
    viewed = models.ForeignKey(User, related_name='profile_views', on_delete=models.CASCADE)
    timestamp = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('viewer', 'viewed')
        ordering = ['-timestamp']

    def __str__(self):
        return f'{self.viewer.username} viewed {self.viewed.username}'
class ProfileStateBackup(models.Model):
    user = models.ForeignKey(User, related_name='profile_backups', on_delete=models.CASCADE)
    mode = models.CharField(max_length=12, choices=RELATIONSHIP_MODE_CHOICES)
    bio = models.CharField(max_length=200, blank=True)
    first_date_idea = models.CharField(max_length=255, blank=True)
    tags = models.ManyToManyField(Tag, blank=True)
    job_title = models.CharField(max_length=100, blank=True)
    
    class Meta:
        unique_together = ('user', 'mode')
        
    def __str__(self):
        return f"Backup for {self.user.username} in {self.mode} mode"


class Referral(models.Model):
    referrer = models.ForeignKey(
        User,
        related_name='referrals_made',
        on_delete=models.CASCADE,
    )
    referred_user = models.OneToOneField(
        User,
        related_name='referral_attribution',
        on_delete=models.CASCADE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    coins_awarded = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f'{self.referred_user.username} referred by {self.referrer.username}'


class ModeSwitchPaymentIntent(models.Model):
    user = models.ForeignKey(
        User,
        related_name='mode_switch_payment_intents',
        on_delete=models.CASCADE,
    )
    reference = models.CharField(max_length=100, unique=True)
    source_mode = models.CharField(max_length=12, choices=RELATIONSHIP_MODE_CHOICES)
    target_mode = models.CharField(max_length=12, choices=RELATIONSHIP_MODE_CHOICES)
    amount_kobo = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-created_at',)


# ── Support Chat Widget Models ─────────────────────────────────────────────────
# Imported here so Django migrations pick them up automatically from the
# 'dating' app without needing a separate app.
from .support_models import SupportKnowledgeBase, SupportMessage, SupportTicket  # noqa: E402, F401
