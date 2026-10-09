from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from dating import views as dating_views

# Customize Admin Site Headers
admin.site.site_header = "LOVENY Admin Dashboard"
admin.site.site_title = "LOVENY Admin Portal"
admin.site.index_title = "Welcome to LOVENY Administration"

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/login/', dating_views.login_view, name='account_login'),
    path('accounts/password_reset/', dating_views.SafePasswordResetView.as_view(), name='password_reset'),
    path('accounts/', include('django.contrib.auth.urls')), 
    path('payments/paystack/webhook/', dating_views.paystack_webhook_api, name='root_paystack_webhook'),
    path('', include('dating.urls')), 
]

# Serve collected static assets and media files during development only.
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    