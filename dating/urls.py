from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('app/', views.swipe_view, name='swipe_card'), 
    path('signup/', views.signup, name='signup'),
    path('create-profile/', views.create_profile, name='create_profile'),
    path('profile/', views.profile_detail, name='profile'),
    path('profile/edit/', views.edit_profile, name='edit_profile'),
    
    path('settings/', views.settings_view, name='settings'),

    path('api/get_profiles/', views.get_profiles_json, name='get_profiles_json'),
    path('action/', views.swipe_action, name='swipe_action'),
    path('matches/', views.match_list, name='match_list'),
    path('likes/', views.likes_list, name='likes_list'),
    path('privacy-policy/', views.privacy, name='privacy'),
    path('terms-of-service/', views.terms, name='terms'),
    
    path('users/<int:pk>/', views.public_profile, name='public_profile'),

    # --- ADD THESE NEW LINES TO FIX THE ERROR ---
    path('premium/', views.premium_landing, name='premium_landing'),
    path('premium/checkout/', views.premium_checkout, name='premium_checkout'),
    path('premium/success/', views.premium_success, name='premium_success'),
    path('premium/verify/', views.verify_payment, name='verify_payment'),
    path('contact/', views.contact, name='contact'),
    path('swipe/rewind/', views.rewind_last_swipe, name='rewind_swipe'),
]