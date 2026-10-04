from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User
from .models import Profile, RELATIONSHIP_MODE_CHOICES

# --- CUSTOM WIDGET TO FIX SERVER CRASH ---
class MultipleFileInput(forms.FileInput):
    """
    Explicitly allows multiple files to bypass the ValueError 
    triggered by some Django versions.
    """
    allow_multiple_selected = True

class SignUpForm(UserCreationForm):
    email = forms.EmailField(required=True, help_text='Required. A valid email address.')
    first_name = forms.CharField(max_length=30, required=True)
    relationship_mode = forms.ChoiceField(
        choices=RELATIONSHIP_MODE_CHOICES,
        required=True,
        label='Connection mode',
    )

    class Meta:
        model = User
        fields = ('username', 'email', 'first_name')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['password1'].help_text = (
            'Use 8+ characters and avoid common or personal-info-based passwords.'
        )
        self.fields['password2'].help_text = 'Enter the same password again.'
        for field in self.fields.values():
            field.widget.attrs.update({
                'class': 'block w-full rounded-xl border-2 border-rose-100 bg-white px-4 py-3 font-bold tracking-wide text-slate-900 shadow-inner focus:border-pink-400 focus:outline-none',
            })
            if field.widget.input_type == 'password':
                field.widget.attrs.update({
                    'autocomplete': 'new-password',
                    'class': 'block w-full rounded-xl border-2 border-rose-100 bg-white px-4 py-3 pr-12 font-medium tracking-normal text-slate-900 shadow-inner focus:border-pink-400 focus:outline-none',
                })


class LoginForm(AuthenticationForm):
    relationship_mode = forms.ChoiceField(
        choices=RELATIONSHIP_MODE_CHOICES,
        required=True,
        label='Connection mode',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({
                'class': 'block w-full rounded-xl border-2 border-rose-100 bg-white px-4 py-3 font-bold tracking-wide text-slate-900 focus:border-pink-400 focus:outline-none',
            })
            if field.widget.input_type == 'password':
                field.widget.attrs.update({
                    'autocomplete': 'current-password',
                    'class': 'block w-full rounded-xl border-2 border-rose-100 bg-white px-4 py-3 font-medium tracking-normal text-slate-900 focus:border-pink-400 focus:outline-none',
                })


class ProfileForm(forms.ModelForm):
    """
    Main form for editing the user's profile.
    """
    name = forms.CharField(max_length=150, required=True, label='Name')
    photo = forms.ImageField(required=False, label="Main Profile Picture")
    age = forms.IntegerField(min_value=18, max_value=120, required=True)
    
    # FIXED: Using our Custom Widget to bypass the 'multiple' ValueError
    more_photos = forms.FileField(
        widget=MultipleFileInput(attrs={
            'multiple': True, 
            'class': 'form-input'
        }), 
        required=False,
        label="Add more photos to your gallery"
    )

    class Meta:
        model = Profile
        fields = [
            'age', 
            'gender', 
            'preferred_gender', 
            'min_age_pref', 
            'max_age_pref',
            'location', 
            'latitude',
            'longitude',
            'max_distance_km',
            'job_title', 
            'whatsapp_number', 
            'bio', 
            'first_date_idea', 
            'tags',
            'show_in_discovery',
            'allow_messages',
        ]
        widgets = {
            # Enforce 18+ Rule in the UI
            'age': forms.NumberInput(attrs={'min': '18', 'class': 'form-input'}),
            'min_age_pref': forms.NumberInput(attrs={'min': '18', 'class': 'form-input'}),
            'max_age_pref': forms.NumberInput(attrs={'min': '18', 'class': 'form-input'}),
            'latitude': forms.NumberInput(attrs={
                'step': '0.000001',
                'min': '-90',
                'max': '90',
                'placeholder': 'e.g. 6.5244',
                'class': 'form-input',
            }),
            'longitude': forms.NumberInput(attrs={
                'step': '0.000001',
                'min': '-180',
                'max': '180',
                'placeholder': 'e.g. 3.3792',
                'class': 'form-input',
            }),
            'max_distance_km': forms.NumberInput(attrs={'min': '1', 'max': '500', 'class': 'form-input'}),
            'bio': forms.Textarea(attrs={'rows': 3, 'maxlength': '200', 'class': 'form-input'}),
            'tags': forms.CheckboxSelectMultiple(),
            'location': forms.TextInput(attrs={'placeholder': 'e.g. Lagos, Nigeria', 'class': 'form-input'}),
            'whatsapp_number': forms.TextInput(attrs={'placeholder': '+234...', 'class': 'form-input'}),
        }
        labels = {
            'name': 'Name',
            'preferred_gender': 'Looking for',
            'min_age_pref': 'Min Age Preference',
            'max_age_pref': 'Max Age Preference',
            'max_distance_km': 'Maximum distance (km)',
            'show_in_discovery': 'Show my profile in discovery',
            'allow_messages': 'Allow messages from matches',
        }

    def clean_tags(self):
        tags = self.cleaned_data['tags']
        if tags.count() > 5:
            raise forms.ValidationError('Choose up to five interests.')
        return tags


class ProfileCreationForm(ProfileForm):
    class Meta(ProfileForm.Meta):
        fields = ['relationship_mode', *ProfileForm.Meta.fields]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['name'].required = False


class SettingsForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = [
            'preferred_gender', 'relationship_mode',
            'min_age_pref', 'max_age_pref', 'max_distance_km',
            'latitude', 'longitude', 'show_in_discovery', 'allow_messages',
        ]
        widgets = {
            'min_age_pref': forms.NumberInput(attrs={'min': '18'}),
            'max_age_pref': forms.NumberInput(attrs={'min': '18'}),
            'max_distance_km': forms.NumberInput(attrs={'min': '1', 'max': '500'}),
            'latitude': forms.NumberInput(attrs={
                'step': '0.000001',
                'min': '-90',
                'max': '90',
                'placeholder': 'e.g. 6.5244',
            }),
            'longitude': forms.NumberInput(attrs={
                'step': '0.000001',
                'min': '-180',
                'max': '180',
                'placeholder': 'e.g. 3.3792',
            }),
        }
        labels = {
            'preferred_gender': 'I want to see',
            'relationship_mode': 'Connection mode',
            'min_age_pref': 'Minimum Age',
            'max_age_pref': 'Maximum Age',
            'max_distance_km': 'Maximum distance (km)',
            'show_in_discovery': 'Show my profile in discovery',
            'allow_messages': 'Allow messages from matches',
        }
