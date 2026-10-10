from django.urls import path

from .consumers import CallSignalingConsumer, HookupEventsConsumer

websocket_urlpatterns = [
    path('ws/calls/<uuid:room_id>/', CallSignalingConsumer.as_asgi()),
    path('ws/hookup/<uuid:match_id>/', HookupEventsConsumer.as_asgi()),
]
