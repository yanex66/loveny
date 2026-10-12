import json
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.contrib.auth.models import AnonymousUser

from .models import CallSession, HookupMatch
from .services.call_signals import record_call_signal

logger = logging.getLogger(__name__)


class CallSignalingConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get('user', AnonymousUser())
        self.room_id = self.scope['url_route']['kwargs']['room_id']
        if not self.user.is_authenticated:
            await self.close(code=4401)
            return
        if not await self.is_call_participant():
            await self.close(code=4403)
            return

        self.group_name = f'call_{self.room_id.hex}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        acceptance = await self.quick_match_acceptance()
        if acceptance:
            await self.send_json(acceptance)

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    @database_sync_to_async
    def is_call_participant(self):
        return CallSession.objects.filter(room_id=self.room_id).filter(
            caller_id=self.user.pk
        ).exists() or CallSession.objects.filter(room_id=self.room_id).filter(
            receiver_id=self.user.pk
        ).exists()

    @database_sync_to_async
    def quick_match_acceptance(self):
        call = CallSession.objects.filter(
            room_id=self.room_id,
            status__in=('connected', 'active'),
            signals__signal_type='call_accepted',
            signals__payload__is_quick_match=True,
        ).first()
        if not call:
            return None
        signal = call.signals.filter(
            signal_type='call_accepted',
            payload__is_quick_match=True,
        ).first()
        if not signal:
            return None
        return {
            'type': signal.signal_type,
            'payload': signal.payload,
            'id': signal.pk,
        }

    async def receive_json(self, content, **kwargs):
        if not isinstance(content, dict):
            await self.close(code=4400)
            return
        signal_type = content.get('type')
        payload = content.get('payload', {})
        if signal_type not in {
            'offer',
            'answer',
            'candidate',
            'face_verified',
            'privacy_blur_engaged',
            'privacy_blur_lifted',
        } or not isinstance(payload, dict) or len(json.dumps(payload)) > 20000:
            await self.send_json({'type': 'error', 'message': 'invalid_signal'})
            return

        signal, events = await database_sync_to_async(record_call_signal)(
            self.room_id,
            self.user,
            signal_type,
            payload,
        )
        if signal is None:
            await self.send_json({'type': 'error', 'message': 'call_not_active'})
            return

        for event in events:
            await self.channel_layer.group_send(self.group_name, {
                'type': 'call.event',
                'recipient_id': event['recipient_id'],
                'signal_type': event['type'],
                'payload': event['payload'],
                'signal_id': signal.pk,
            })

    async def call_event(self, event):
        if event['recipient_id'] == self.user.pk:
            await self.send_json({
                'type': event['signal_type'],
                'payload': event['payload'],
                'id': event.get('signal_id'),
            })


class HookupEventsConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get('user', AnonymousUser())
        self.match_id = self.scope['url_route']['kwargs']['match_id']
        if not self.user.is_authenticated:
            await self.close(code=4401)
            return
        if not await self.is_match_participant():
            await self.close(code=4403)
            return

        self.group_name = f'hookup_{self.match_id.hex}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    @database_sync_to_async
    def is_match_participant(self):
        return HookupMatch.objects.filter(pk=self.match_id).filter(
            initiator_id=self.user.pk
        ).exists() or HookupMatch.objects.filter(pk=self.match_id).filter(
            target_id=self.user.pk
        ).exists()

    async def hookup_unlocked(self, event):
        await self.send_json({
            'type': 'match_unlocked',
            'match_id': event['match_id'],
            'chat_url': event['chat_url'],
        })
