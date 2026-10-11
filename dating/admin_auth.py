from django.contrib import admin
from django.contrib.auth import logout
from django.contrib.admin.forms import AdminAuthenticationForm
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


class DedicatedAdminAuthenticationForm(AdminAuthenticationForm):
    error_messages = {
        **AdminAuthenticationForm.error_messages,
        'invalid_login': _(
            'Please enter a correct administrative username and password.'
        ),
    }

    def confirm_login_allowed(self, user):
        if not user.is_active or not user.is_staff:
            raise ValidationError(
                self.error_messages['invalid_login'],
                code='invalid_login',
            )


def admin_login_view(request):
    if request.user.is_authenticated and not request.user.is_staff:
        logout(request)
    return admin.site.login(request)
