import uuid
import os
from django.db import models
from django.contrib.auth import get_user_model 
from django.utils import timezone
import reversion 

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
    
    # Core fields
    age = models.IntegerField(null=True, blank=True)
    gender = models.CharField(max_length=1, choices=GENDER_CHOICES, default='O')
    preferred_gender = models.CharField(max_length=1, choices=GENDER_CHOICES, default='F', help_text="Gender preference for profiles to see.")
    
    # Age Preferences
    min_age_pref = models.IntegerField(default=18, help_text="Minimum age of people you want to see.")
    max_age_pref = models.IntegerField(default=50, help_text="Maximum age of people you want to see.")

    # Detailed info
    bio = models.CharField(max_length=200, blank=True)
    location = models.CharField(max_length=100, blank=True)
    job_title = models.CharField(max_length=100, blank=True)

    whatsapp_number = models.CharField(max_length=20, unique=True, help_text="Required for sharing upon a match.")
    first_date_idea = models.CharField(max_length=255, blank=True, help_text="My ideal first date is...")
    
    # Tags
    tags = models.ManyToManyField(Tag, blank=True, help_text="Select up to 5 passion tags.")

    # Status
    last_active = models.DateTimeField(default=timezone.now)

    # --- PREMIUM FEATURES ---
    # Stores the Plan Name: 'SILVER', 'GOLD', 'PLATINUM'
    premium_tier = models.CharField(max_length=20, blank=True, null=True) 
    # Stores exactly when the plan runs out
    premium_expiry = models.DateTimeField(null=True, blank=True)

    def is_premium(self):
        """
        Returns True if the user has an active plan that hasn't expired.
        Used by templates to lock/unlock features.
        """
        if self.premium_expiry and self.premium_expiry > timezone.now():
            return True
        return False

    class Meta:
        verbose_name = 'Dating Profile'
        verbose_name_plural = 'Dating Profiles'

    def __str__(self):
        return self.user.username

class ProfilePhoto(models.Model):
    """
    Stores individual photos or videos for a profile.
    """
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
    """
    Tracks every swipe action (Like or Pass).
    """
    swiper = models.ForeignKey(User, related_name='given_swipes', on_delete=models.CASCADE)
    swiped = models.ForeignKey(User, related_name='received_swipes', on_delete=models.CASCADE)
    type = models.CharField(max_length=5, choices=SWIPE_CHOICES)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('swiper', 'swiped')
        indexes = [
            models.Index(fields=['swiped', 'swiper']),
        ]

    def __str__(self):
        return f"{self.swiper.username} -> {self.type} -> {self.swiped.username}"

class Match(models.Model):
    """
    Stores successful mutual matches.
    """
    user1 = models.ForeignKey(User, related_name='matches_as_user1', on_delete=models.CASCADE)
    user2 = models.ForeignKey(User, related_name='matches_as_user2', on_delete=models.CASCADE)
    whatsapp_link_id = models.UUIDField(default=uuid.uuid4, unique=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user1', 'user2')

    def __str__(self):
        return f"Match: {self.user1.username} & {self.user2.username}"

    def is_active(self):
        return self.expires_at > timezone.now()

reversion.register(Profile)