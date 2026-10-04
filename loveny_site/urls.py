from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from dating import views as dating_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/login/', dating_views.login_view, name='account_login'),
    path('accounts/', include('django.contrib.auth.urls')), 
    path('', include('dating.urls')), 
]

# Serve collected static assets and media files during development only.
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    