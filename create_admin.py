import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "loveny_site.settings")

import django

django.setup()

from django.contrib.auth import get_user_model


def main():
    admin_username = os.environ.get("ADMIN_USERNAME", "loveny")
    admin_password = os.environ.get("ADMIN_PASSWORD", "loveny2025")
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@loveny.com")
    user_model = get_user_model()
    username_field = user_model.USERNAME_FIELD

    user, _ = user_model.objects.get_or_create(
        **{username_field: admin_username},
        defaults={"email": admin_email},
    )
    user.email = admin_email
    user.is_staff = True
    user.is_superuser = True
    user.is_active = True
    user.set_password(admin_password)
    user.save()

    print(f"==> Admin user '{admin_username}' configured successfully.")


if __name__ == "__main__":
    main()
