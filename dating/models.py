import uuid
import os
from django.db import models
from django.contrib.auth import get_user_model 
from django.utils import timezone
from django.core.validators import MaxValueValidator, MinValueValidator
from django.core.exceptions import ValidationError
from django.db.models import Q

User = get_user_model() 

# --- Choices ---
GENDER_CHOICES = (
    ('M', 'Male'),
    ('F', 'Female'),
    ('O', 'Other'),
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
    gender = models.CharField(max_length=1, choices=GENDER_CHOICES, default='O')
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
        unique=True,
        null=True,
        blank=True,
        help_text='Private legacy contact field; not shown or used for external contact.',
    )
    first_date_idea = models.CharField(max_length=255, blank=True, help_text="My ideal first date is...")
    
    # Tags
    tags = models.ManyToManyField(Tag, blank=True, help_text="Select up to 5 passion tags.")

    # Status
    last_active = models.DateTimeField(default=timezone.now)
    show_in_discovery = models.BooleanField(default=True)
    allow_messages = models.BooleanField(default=True)
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
    coin_balance = models.PositiveIntegerField(default=0, help_text="Available coin balance for video calls & gifts")
    earned_diamonds = models.PositiveIntegerField(default=0, help_text="Diamonds earned by hosts from received calls & gifts")

    def can_call_with_coins(self, min_coins=20):
        return self.coin_balance >= min_coins

    @property
    def is_dating_premium(self):
        return bool(
            self.dating_premium_expiry
            and self.dating_premium_expiry > timezone.now()
        )

    @property
    def is_hookup_premium(self):
        return bool(
            self.hookup_premium_expiry
            and self.hookup_premium_expiry > timezone.now()
        )

    @property
    def is_sex_call_premium(self):
        return bool(
            self.sex_call_premium_expiry
            and self.sex_call_premium_expiry > timezone.now()
        )

    @property
    def is_hookup_sexcall_premium(self):
        return self.is_hookup_premium or self.is_sex_call_premium

    def is_premium(self, mode=None):
        active_mode = mode or self.relationship_mode
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
        if (self.latitude is None) != (self.longitude is None):
            raise ValidationError('Latitude and longitude must be provided together.')
        if self.min_age_pref > self.max_age_pref:
            raise ValidationError({'max_age_pref': 'Maximum age must be at least the minimum age.'})

    class Meta:
        verbose_name = 'Dating Profile'
        verbose_name_plural = 'Dating Profiles'

    def __str__(self):
        return self.user.username

class ProfilePhoto(models.Model):
    profile = models.ForeignKey(Profile, related_name='photos', on_delete=models.CASCADE)
    image = models.FileField(upload_to='profile_media/')
    is_main = models.BooleanField(default=False)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['uploaded_at']
        verbose_name_plural = 'Profile Media'

    def __str__(self):
        return f"Media for {self.profile.user.username} (Main: {self.is_main})"

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
    conversation = models.ForeignKey(
        Conversation,
        related_name='messages',
        on_delete=models.CASCADE,
    )
    sender = models.ForeignKey(User, related_name='chat_messages', on_delete=models.CASCADE)
    text = models.TextField()
    read_by = models.ManyToManyField(
        User,
        related_name='read_chat_messages',
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ('created_at', 'id')
        indexes = [
            models.Index(fields=('conversation', 'created_at')),
        ]

    def __str__(self):
        return f"Message from {self.sender.username} in conversation {self.conversation_id}"


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
    )

    user = models.ForeignKey(User, related_name='premium_payments', on_delete=models.CASCADE)
    reference = models.CharField(max_length=100, unique=True)
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES)
    product = models.CharField(max_length=12, choices=PRODUCT_CHOICES, default='DATING')
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


class CallSession(models.Model):
    STATUS_CHOICES = (
        ('initiated', 'Initiated'),
        ('ringing', 'Ringing'),
        ('connected', 'Connected'),
        ('ended', 'Ended'),
        ('declined', 'Declined'),
    )

    caller = models.ForeignKey(
        User,
        related_name='outgoing_call_sessions',
        on_delete=models.CASCADE,
    )
    receiver = models.ForeignKey(
        User,
        related_name='incoming_call_sessions',
        on_delete=models.CASCADE,
    )
    room_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='initiated', db_index=True)
    rate_per_minute = models.PositiveIntegerField(default=20, help_text="Coins per minute")
    coins_spent = models.PositiveIntegerField(default=0, help_text="Total coins deducted for this call")
    duration_seconds = models.PositiveIntegerField(default=0, help_text="Connected call duration in seconds")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=('receiver', 'status', 'created_at')),
            models.Index(fields=('caller', 'status', 'created_at')),
        ]

    def __str__(self):
        return f'Call {self.room_id}: {self.caller} → {self.receiver} ({self.status})'


class CallGift(models.Model):
    GIFT_CHOICES = (
        ('rose', 'Rose 🌹'),
        ('kiss', 'Kiss 💋'),
        ('champagne', 'Champagne 🥂'),
        ('crown', 'Crown 👑'),
        ('car', 'Supercar 🏎️'),
    )
    GIFT_PRICES = {
        'rose': 10,
        'kiss': 25,
        'champagne': 50,
        'crown': 100,
        'car': 300,
    }

    call = models.ForeignKey(CallSession, related_name='gifts', on_delete=models.CASCADE)
    sender = models.ForeignKey(User, related_name='sent_call_gifts', on_delete=models.CASCADE)
    receiver = models.ForeignKey(User, related_name='received_call_gifts', on_delete=models.CASCADE)
    gift_type = models.CharField(max_length=20, choices=GIFT_CHOICES)
    coins_cost = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f"{self.sender.username} sent {self.gift_type} ({self.coins_cost} coins) in Call {self.call.room_id}"


class CoinTransaction(models.Model):
    TRANSACTION_TYPES = (
        ('PURCHASE', 'Coin Purchase'),
        ('WELCOME_BONUS', 'Welcome Bonus'),
        ('CALL_DEDUCTION', 'Call Minute Deduction'),
        ('CALL_EARNING', 'Host Call Diamond Earning'),
        ('CALL_REFUND', 'Call Refund / Grace Period'),
        ('GIFT_SENT', 'Gift Sent'),
        ('GIFT_RECEIVED', 'Gift Received'),
    )

    user = models.ForeignKey(User, related_name='coin_transactions', on_delete=models.CASCADE)
    amount = models.IntegerField(help_text="Coins (positive for credit, negative for debit) or Diamonds")
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    description = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    call = models.ForeignKey(CallSession, null=True, blank=True, on_delete=models.SET_NULL, related_name='coin_transactions')
    payment = models.ForeignKey(PaymentTransaction, null=True, blank=True, on_delete=models.SET_NULL, related_name='coin_transactions')

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('user', 'created_at')),
        ]

    def __str__(self):
        return f"{self.user.username}: {self.amount} ({self.transaction_type})"


class CallSignal(models.Model):
    SIGNAL_TYPES = (
        ('offer', 'Offer'),
        ('answer', 'Answer'),
        ('candidate', 'ICE candidate'),
        ('gift', 'In-call gift'),
    )

    call = models.ForeignKey(CallSession, related_name='signals', on_delete=models.CASCADE)
    sender = models.ForeignKey(User, related_name='call_signals', on_delete=models.CASCADE)
    signal_type = models.CharField(max_length=12, choices=SIGNAL_TYPES)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ('id',)
        indexes = [
            models.Index(fields=('call', 'id')),
        ]

    def __str__(self):
        return f'{self.signal_type} for call {self.call.room_id}'