import re

from django import forms
from django.contrib.auth import authenticate
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm, PasswordResetForm
from django.contrib.auth.models import User
from django.core.mail import EmailMultiAlternatives
from django.template import loader
from .models import Profile, RELATIONSHIP_MODE_CHOICES, GENDER_CHOICES

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


class LoginForm(forms.Form):
    email = forms.EmailField(
        label='Email Address',
        widget=forms.EmailInput(attrs={
            'autocomplete': 'email',
            'placeholder': 'name@example.com',
            'autofocus': True,
        }),
    )
    password = forms.CharField(
        label='Password',
        strip=False,
        widget=forms.PasswordInput(attrs={
            'autocomplete': 'current-password',
        }),
    )
    relationship_mode = forms.ChoiceField(
        choices=RELATIONSHIP_MODE_CHOICES,
        required=True,
        label='Connection mode',
    )

    error_messages = {
        'invalid_login': 'Please enter a correct email address and password.',
        'inactive': 'This account is inactive.',
    }

    def __init__(self, request=None, *args, **kwargs):
        if request is not None and not hasattr(request, 'META') and not hasattr(request, 'method') and not args and 'data' not in kwargs:
            data = request
            request = None
            super().__init__(data, *args, **kwargs)
        else:
            super().__init__(*args, **kwargs)
        self.request = request
        self.user_cache = None

        for field in self.fields.values():
            field.widget.attrs.update({
                'class': 'block w-full rounded-xl border-2 border-rose-100 bg-white px-4 py-3 font-bold tracking-wide text-slate-900 focus:border-pink-400 focus:outline-none',
            })
            if field.widget.input_type == 'password':
                field.widget.attrs.update({
                    'autocomplete': 'current-password',
                    'class': 'block w-full rounded-xl border-2 border-rose-100 bg-white px-4 py-3 font-medium tracking-normal text-slate-900 focus:border-pink-400 focus:outline-none',
                })

    def clean(self):
        email = self.cleaned_data.get('email')
        password = self.cleaned_data.get('password')

        if email is not None and password:
            self.user_cache = authenticate(
                self.request,
                email=email,
                password=password,
            )
            if self.user_cache is None:
                raise forms.ValidationError(
                    self.error_messages['invalid_login'],
                    code='invalid_login',
                )
            else:
                if not getattr(self.user_cache, 'is_active', True):
                    raise forms.ValidationError(
                        self.error_messages['inactive'],
                        code='inactive',
                    )

        return self.cleaned_data

    def get_user(self):
        return self.user_cache


class MultipleFileField(forms.FileField):
    def clean(self, data, initial=None):
        if data in self.empty_values:
            return []
        files = data if isinstance(data, (list, tuple)) else [data]
        clean_file = super().clean
        return [clean_file(uploaded_file) for uploaded_file in files]


class ProfileForm(forms.ModelForm):
    """
    Main form for editing the user's profile.
    """
    name = forms.CharField(max_length=150, required=False, label='Name')
    photo = forms.ImageField(required=False, label="Main Profile Picture")
    age = forms.IntegerField(min_value=18, max_value=99, required=True)
    min_age_pref = forms.IntegerField(min_value=18, max_value=99, required=False, initial=18)
    max_age_pref = forms.IntegerField(min_value=18, max_value=99, required=False, initial=50)
    max_distance_km = forms.IntegerField(min_value=1, max_value=500, required=False, initial=100)
    latitude = forms.DecimalField(
        max_digits=9,
        decimal_places=6,
        min_value=-90,
        max_value=90,
        required=False,
        widget=forms.NumberInput(attrs={
            'min': '-90',
            'max': '90',
            'step': '0.000001',
            'class': 'form-input',
        }),
    )
    longitude = forms.DecimalField(
        max_digits=9,
        decimal_places=6,
        min_value=-180,
        max_value=180,
        required=False,
        widget=forms.NumberInput(attrs={
            'min': '-180',
            'max': '180',
            'step': '0.000001',
            'class': 'form-input',
        }),
    )
    
    location = forms.CharField(
        max_length=100,
        required=False,
        label='City / Location',
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'autocomplete': 'address-level2',
            'placeholder': 'Enter your city or state',
        }),
    )
    gender = forms.ChoiceField(
        choices=GENDER_CHOICES,
        required=False,
        label='Identify As',
        widget=forms.Select(attrs={'class': 'form-input'}),
    )
    preferred_gender = forms.ChoiceField(
        choices=GENDER_CHOICES,
        required=False,
        label='Looking For',
        widget=forms.Select(attrs={'class': 'form-input'}),
    )
    
    # FIXED: Using our Custom Widget to bypass the 'multiple' ValueError
    more_photos = MultipleFileField(
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
            'max_distance_km',
            'whatsapp_number',
            'job_title', 
            'bio', 
            'first_date_idea', 
            'tags',
        ]
        widgets = {
            # Enforce 18+ Rule in the UI
            'age': forms.NumberInput(attrs={'min': '18', 'max': '99', 'class': 'form-input'}),
            'min_age_pref': forms.NumberInput(attrs={'min': '18', 'max': '99', 'class': 'form-input'}),
            'max_age_pref': forms.NumberInput(attrs={'min': '18', 'max': '99', 'class': 'form-input'}),
            'max_distance_km': forms.NumberInput(attrs={'min': '1', 'max': '500', 'class': 'form-input'}),
            'whatsapp_number': forms.TextInput(attrs={
                'type': 'tel',
                'autocomplete': 'tel',
                'placeholder': '+2348012345678',
                'class': 'form-input',
            }),
            'bio': forms.Textarea(attrs={'rows': 3, 'maxlength': '200', 'class': 'form-input'}),
            'tags': forms.CheckboxSelectMultiple(),
        }
        labels = {
            'name': 'Name',
            'preferred_gender': 'Looking for',
            'min_age_pref': 'Min Age Preference',
            'max_age_pref': 'Max Age Preference',
            'max_distance_km': 'Maximum distance (km)',
            'whatsapp_number': 'WhatsApp number (include country code)',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        mode = (
            self.data.get('relationship_mode')
            or self.initial.get('relationship_mode')
            or getattr(self.instance, 'relationship_mode', 'DATING')
        )
        mode = str(mode).upper()
        self.fields.pop('latitude', None)
        self.fields.pop('longitude', None)
        if mode != 'DATING':
            self.fields.pop('job_title', None)
            self.fields.pop('first_date_idea', None)
        if mode == 'SEX_CALL':
            self.fields.pop('location', None)
            self.fields.pop('max_distance_km', None)

    def clean_name(self):
        val = (self.cleaned_data.get('name') or '').strip()
        if val:
            return val
        if not val and self.instance and hasattr(self.instance, 'user') and self.instance.user:
            return self.instance.user.first_name or self.instance.user.username
        return ''

    def clean_min_age_pref(self):
        val = self.cleaned_data.get('min_age_pref')
        return val if val is not None else 18

    def clean_max_age_pref(self):
        val = self.cleaned_data.get('max_age_pref')
        return val if val is not None else 50

    def clean_max_distance_km(self):
        val = self.cleaned_data.get('max_distance_km')
        return val if val is not None else 100

    def clean_whatsapp_number(self):
        value = (self.cleaned_data.get('whatsapp_number') or '').strip()
        if not re.fullmatch(r'\+?[\d\s().-]+', value):
            raise forms.ValidationError('Enter a valid WhatsApp number with country code.')
        digits = re.sub(r'\D', '', value)
        if not 7 <= len(digits) <= 15:
            raise forms.ValidationError('Enter a WhatsApp number containing 7 to 15 digits.')
        return value

    def clean_tags(self):
        tags = self.cleaned_data.get('tags')
        if tags and hasattr(tags, 'count') and tags.count() > 5:
            raise forms.ValidationError('Choose up to five interests.')
        return tags

    def clean_gender(self):
        val = self.cleaned_data.get('gender')
        if not val and self.instance and self.instance.gender:
            return self.instance.gender
        return val or 'M'

    def clean_preferred_gender(self):
        val = self.cleaned_data.get('preferred_gender')
        if not val and self.instance and self.instance.preferred_gender:
            return self.instance.preferred_gender
        return val or 'F'


class ProfileCreationForm(ProfileForm):
    class Meta(ProfileForm.Meta):
        fields = ['relationship_mode', *ProfileForm.Meta.fields]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['gender'].required = True
        self.fields['name'].required = False
        optional_fields = [
            'min_age_pref', 'max_age_pref', 'max_distance_km',
            'latitude', 'longitude', 'location', 'job_title',
            'bio', 'first_date_idea', 'tags', 'more_photos', 'photo'
        ]
        for field_name in optional_fields:
            if field_name in self.fields:
                self.fields[field_name].required = False

        if 'min_age_pref' in self.fields:
            self.fields['min_age_pref'].initial = 18
        if 'max_age_pref' in self.fields:
            self.fields['max_age_pref'].initial = 50
        if 'max_distance_km' in self.fields:
            self.fields['max_distance_km'].initial = 100


class SafePasswordResetForm(PasswordResetForm):
    def send_mail(
        self,
        subject_template_name,
        email_template_name,
        context,
        from_email,
        to_email,
        html_email_template_name=None,
    ):
        subject = loader.render_to_string(subject_template_name, context)
        subject = "".join(subject.splitlines())
        body = loader.render_to_string(email_template_name, context)

        email_message = EmailMultiAlternatives(subject, body, from_email, [to_email])
        if html_email_template_name is not None:
            html_email = loader.render_to_string(html_email_template_name, context)
            email_message.attach_alternative(html_email, "text/html")

        email_message.send()
