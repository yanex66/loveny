from django import forms
from django.contrib.auth import authenticate
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm, PasswordResetForm
from django.contrib.auth.models import User
from django.core.mail import EmailMultiAlternatives
from django.template import loader
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
            'bio', 
            'first_date_idea', 
            'tags',
            'is_dnd',
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
            'is_dnd': forms.CheckboxInput(attrs={'class': 'form-checkbox h-5 w-5 text-pink-600 rounded focus:ring-pink-500'}),
        }
        labels = {
            'name': 'Name',
            'preferred_gender': 'Looking for',
            'min_age_pref': 'Min Age Preference',
            'max_age_pref': 'Max Age Preference',
            'max_distance_km': 'Maximum distance (km)',
            'is_dnd': 'Do Not Disturb (DND)',
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
            'relationship_mode',
            'is_dnd',
        ]
        widgets = {
            'is_dnd': forms.CheckboxInput(attrs={'class': 'form-checkbox h-5 w-5 text-pink-600 rounded focus:ring-pink-500'}),
        }
        labels = {
            'relationship_mode': 'Connection mode',
            'is_dnd': 'Do Not Disturb (DND)',
        }


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

