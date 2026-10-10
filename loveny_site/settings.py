"""
Django settings for loveny_site project.
Updated for Production Deployment.
"""

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env file if present
_env_file = BASE_DIR / '.env'
if _env_file.exists():
    with open(_env_file, 'r', encoding='utf-8') as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))


# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get('DJANGO_DEBUG', 'False').strip().lower() in {
    '1',
    'true',
    'yes',
}

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured(
            'Set DJANGO_SECRET_KEY in the environment when DEBUG is disabled.'
        )
    SECRET_KEY = 'django-insecure-local-development-only-key'

IS_RENDER = os.environ.get('RENDER', '').strip().lower() == 'true'
RENDER_EXTERNAL_HOSTNAME = os.environ.get('RENDER_EXTERNAL_HOSTNAME', '').strip()
CLOUDINARY_URL = os.environ.get('CLOUDINARY_URL', '').strip()
if IS_RENDER and not CLOUDINARY_URL:
    raise ImproperlyConfigured(
        'Set CLOUDINARY_URL to enable persistent profile photo storage on Render.'
    )

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get('DJANGO_ALLOWED_HOSTS', '').split(',')
    if host.strip()
]
ALLOWED_HOSTS += [
    'lovenny.pythonanywhere.com',
    'loveny.pythonanywhere.com',
    '.pythonanywhere.com',
    '.onrender.com',
    '127.0.0.1',
    'localhost',
    '0.0.0.0',
    '.localhost',
]
if RENDER_EXTERNAL_HOSTNAME and RENDER_EXTERNAL_HOSTNAME not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)

ALLOWED_HOSTS = list(dict.fromkeys(ALLOWED_HOSTS))

CSRF_TRUSTED_ORIGINS = [
    'https://*.pythonanywhere.com',
    'http://*.pythonanywhere.com',
    'https://*.onrender.com',
    'http://*.onrender.com',
    'https://lovenny.pythonanywhere.com',
    'http://lovenny.pythonanywhere.com',
    'https://loveny.pythonanywhere.com',
    'http://loveny.pythonanywhere.com',
    'http://127.0.0.1:8000',
    'http://localhost:8000',
    'http://127.0.0.1',
    'http://localhost',
    'https://127.0.0.1:8000',
    'https://localhost:8000',
    'https://127.0.0.1',
    'https://localhost',
    'http://0.0.0.0:8000',
    'http://0.0.0.0',
]
CSRF_TRUSTED_ORIGINS += [
    origin.strip()
    for origin in os.environ.get('DJANGO_CSRF_TRUSTED_ORIGINS', '').split(',')
    if origin.strip()
]
if RENDER_EXTERNAL_HOSTNAME:
    CSRF_TRUSTED_ORIGINS.append(f'https://{RENDER_EXTERNAL_HOSTNAME}')
    CSRF_TRUSTED_ORIGINS.append(f'http://{RENDER_EXTERNAL_HOSTNAME}')

for host in ALLOWED_HOSTS:
    if not host or host == '*':
        continue
    clean_host = host.lstrip('.')
    if host.startswith('.'):
        clean_host = f'*.{clean_host}'
    for proto in ('https://', 'http://'):
        origin = f'{proto}{clean_host}'
        if origin not in CSRF_TRUSTED_ORIGINS:
            CSRF_TRUSTED_ORIGINS.append(origin)

CSRF_TRUSTED_ORIGINS = list(dict.fromkeys(CSRF_TRUSTED_ORIGINS))


# Application definition

INSTALLED_APPS = [
    'jazzmin',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'reversion',
    'dating',
]
if CLOUDINARY_URL:
    INSTALLED_APPS[5:5] = ['cloudinary']

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'dating.middleware.MaintenanceModeMiddleware',
]
if not DEBUG:
    MIDDLEWARE.insert(1, 'whitenoise.middleware.WhiteNoiseMiddleware')

ROOT_URLCONF = 'loveny_site.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'dating' / 'templates'], # Ensure this points to your templates
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'dating.context_processors.site_configuration_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'loveny_site.wsgi.application'


# Use an externally provisioned PostgreSQL database on Render. Local
# development continues to use SQLite unless DATABASE_URL is supplied.
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
if DATABASE_URL:
    DATABASES = {
        'default': dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=600,
            conn_health_checks=True,
            ssl_require=not DEBUG,
        )
    }
elif IS_RENDER:
    raise ImproperlyConfigured(
        'Set DATABASE_URL to the external PostgreSQL database connection string.'
    )
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

AUTHENTICATION_BACKENDS = [
    'dating.backends.EmailAuthBackend',
    'django.contrib.auth.backends.ModelBackend',
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = '/static/'

STATICFILES_DIRS = []
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'default': {
        'BACKEND': (
            'cloudinary_storage.storage.MediaCloudinaryStorage'
            if CLOUDINARY_URL
            else 'django.core.files.storage.FileSystemStorage'
        ),
    },
    'staticfiles': {
        'BACKEND': (
            'whitenoise.storage.CompressedManifestStaticFilesStorage'
            if not DEBUG
            else 'django.contrib.staticfiles.storage.StaticFilesStorage'
        ),
    },
}
# --- Media Files Configuration (User Uploads) ---
MEDIA_URL = '/media/'
MEDIA_ROOT = Path(os.environ.get('MEDIA_ROOT', BASE_DIR / 'media'))

if not DEBUG or IS_RENDER:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

if IS_RENDER:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 3600
else:
    SESSION_COOKIE_SECURE = os.environ.get('DJANGO_SESSION_COOKIE_SECURE', 'false').strip().lower() in {'1', 'true', 'yes'}
    CSRF_COOKIE_SECURE = os.environ.get('DJANGO_CSRF_COOKIE_SECURE', 'false').strip().lower() in {'1', 'true', 'yes'}


# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_REDIRECT_URL = '/app/' 
LOGOUT_REDIRECT_URL = '/' 

# PAYSTACK KEYS
PAYSTACK_PUBLIC_KEY = os.environ.get(
    'PAYSTACK_PUBLIC_KEY',
    'pk_live_6fd34253cf04d94620e50e8c547b5259d052c121',
)
# Add your real secret key here to process payments!
PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY', 'sk_live_PUT_YOUR_REAL_SECRET_KEY_HERE')

# FLUTTERWAVE KEYS
FLUTTERWAVE_PUBLIC_KEY = os.environ.get(
    'FLUTTERWAVE_PUBLIC_KEY',
    'FLWPUBK-9137e1d408bd082a5b9a72987b0e5ce7-X',
)
FLUTTERWAVE_SECRET_KEY = os.environ.get('FLUTTERWAVE_SECRET_KEY', 'FLWSECK-PUT_YOUR_REAL_SECRET_KEY_HERE-X')

# AI MONITORING
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# Use Gmail SMTP in production. Gmail requires an app password; never store it
# in source control. Development defaults to the console email backend.
EMAIL_BACKEND = os.environ.get(
    'DJANGO_EMAIL_BACKEND',
    'django.core.mail.backends.console.EmailBackend' if DEBUG
    else 'django.core.mail.backends.smtp.EmailBackend',
)
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', 'help.hoxobil@gmail.com')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'true').strip().lower() in {
    '1', 'true', 'yes',
}
DEFAULT_FROM_EMAIL = os.environ.get(
    'DEFAULT_FROM_EMAIL',
    'LOVENY Support <help.hoxobil@gmail.com>',
)
# Notifications for new support tickets will be sent to this address.
ADMIN_NOTIFICATION_EMAIL = os.environ.get('ADMIN_NOTIFICATION_EMAIL', EMAIL_HOST_USER)

JAZZMIN_SETTINGS = {
    'site_title': 'LOVENY Admin',
    'site_header': 'LOVENY',
    'site_brand': ' ',
    'site_logo': 'dating/images/loveny-logo.png',
    'login_logo': 'dating/images/loveny-logo.png',
    'site_logo_classes': '',
    'welcome_sign': 'Welcome to LOVENY Admin',
    'copyright': 'LOVENY',
    'search_model': ['auth.User', 'dating.Profile'],
    'show_sidebar': True,
    'navigation_expanded': False,
    'hide_apps': [],
    'hide_models': [],
    'topmenu_links': [
        {'name': 'Dashboard Home', 'url': 'admin:index', 'permissions': ['auth.view_user']},
        {'name': 'View Live Site', 'url': '/', 'new_window': True},
        {'model': 'dating.SiteConfiguration'},
    ],
    'order_with_respect_to': [
        'dating.SiteConfiguration',
        'auth.User',
        'dating.Profile',
        'dating.ProfilePhoto',
        'dating.Match',
        'dating.Swipe',
        'dating.Conversation',
        'dating.ChatMessage',
        'dating.HookupMatch',
        'dating.HookupMessage',
        'dating.CoinWallet',
        'dating.CoinPackage',
        'dating.SubscriptionPlan',
        'dating.PaymentTransaction',
        'dating.CoinTransaction',
        'dating.CallSession',
        'dating.CallGift',
        'dating.GiftItem',
        'dating.CallSignal',
        'dating.UserReport',
        'dating.UserBlock',
    ],
    'icons': {
        'auth.User': 'fas fa-users-cog',
        'auth.Group': 'fas fa-users',
        'dating.SiteConfiguration': 'fas fa-cogs',
        'dating.Profile': 'fas fa-user-circle',
        'dating.ProfilePhoto': 'fas fa-image',
        'dating.Match': 'fas fa-heart',
        'dating.Swipe': 'fas fa-thumbs-up',
        'dating.Conversation': 'fas fa-comments',
        'dating.ChatMessage': 'fas fa-comment-dots',
        'dating.HookupMatch': 'fas fa-fire',
        'dating.HookupMessage': 'fas fa-comment-alt',
        'dating.SubscriptionPlan': 'fas fa-crown',
        'dating.CoinPackage': 'fas fa-coins',
        'dating.PaymentTransaction': 'fas fa-file-invoice-dollar',
        'dating.CallSession': 'fas fa-video',
        'dating.CallGift': 'fas fa-gift',
        'dating.GiftItem': 'fas fa-box-open',
        'dating.CoinTransaction': 'fas fa-exchange-alt',
        'dating.CoinWallet': 'fas fa-wallet',
        'dating.CallSignal': 'fas fa-signal',
        'dating.UserReport': 'fas fa-flag',
        'dating.UserBlock': 'fas fa-ban',
    },
    'default_icon_parents': 'fas fa-folder',
    'default_icon_children': 'fas fa-circle',
    # Enable UI builder so user can tweak or reset themes
    'show_ui_builder': True,
    'custom_css': 'dating/css/admin_custom.css',
    'custom_js': 'dating/js/clear_jazzmin.js',
}

JAZZMIN_UI_TWEAKS = {
    "navbar_small_text": False,
    "footer_small_text": False,
    "body_small_text": False,
    "brand_small_text": False,
    "brand_colour": "navbar-dark",
    "accent": "accent-warning",
    "navbar": "navbar-dark",
    "no_navbar_border": False,
    "navbar_fixed": True,
    "layout_boxed": False,
    "footer_fixed": True,
    "sidebar_fixed": True,
    "sidebar": "sidebar-dark-primary",
    "sidebar_nav_small_text": False,
    "sidebar_disable_expand": False,
    "sidebar_nav_child_indent": True,
    "sidebar_nav_compact_style": False,
    "sidebar_nav_legacy_style": False,
    "sidebar_nav_flat_style": True,
    "theme": "default",
    "dark_mode_theme": None,
    "button_classes": {
        "primary": "btn-warning",
        "secondary": "btn-secondary",
        "info": "btn-info",
        "warning": "btn-warning",
        "danger": "btn-danger",
        "success": "btn-success"
    },
    "actions_sticky_top": True
}
