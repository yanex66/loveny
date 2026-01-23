from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from .models import Profile, Tag

class SignUpForm(UserCreationForm):
    email = forms.EmailField(required=True, help_text='Required. A valid email address.')
    first_name = forms.CharField(max_length=30, required=True)

    class Meta:
        model = User
        fields = ('username', 'email', 'first_name')

class ProfileForm(forms.ModelForm):
    """
    Main form for editing the user's profile, including details and preferences.
    """
    class Meta:
        model = Profile
        fields = [
            'age', 
            'gender', 
            'preferred_gender', 
            'min_age_pref', 
            'max_age_pref',
            'location',     # Added
            'job_title',    # Added
            'whatsapp_number', 
            'bio', 
            'first_date_idea', 
            'tags'
        ]
        widgets = {
            'bio': forms.Textarea(attrs={'rows': 3}),
            'tags': forms.CheckboxSelectMultiple(),
        }
        labels = {
            'preferred_gender': 'Looking for',
            'min_age_pref': 'Min Age',
            'max_age_pref': 'Max Age',
            'location': 'City / Location',
            'job_title': 'Job / Industry',
        }

class SettingsForm(forms.ModelForm):
    """
    Secondary form if you want to edit settings separately.
    """
    class Meta:
        model = Profile
        fields = ['preferred_gender', 'min_age_pref', 'max_age_pref']
        labels = {
            'preferred_gender': 'I want to see',
            'min_age_pref': 'Minimum Age',
            'max_age_pref': 'Maximum Age',
        }