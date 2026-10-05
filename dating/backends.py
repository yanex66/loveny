from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q

User = get_user_model()


class EmailAuthBackend(ModelBackend):
    """
    Authenticate against the User model using case-insensitive email address
    or username (allowing seamless login across customer forms and Django admin).
    """

    def authenticate(self, request, username=None, password=None, email=None, **kwargs):
        lookup = email or username or kwargs.get('email')
        if not lookup or not password:
            return None

        clean_lookup = lookup.strip()
        matching_users = User.objects.filter(
            Q(email__iexact=clean_lookup) | Q(username__iexact=clean_lookup)
        )
        for user in matching_users:
            if user.check_password(password) and self.user_can_authenticate(user):
                return user
        return None

    def get_user(self, user_id):
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
        return user if self.user_can_authenticate(user) else None

