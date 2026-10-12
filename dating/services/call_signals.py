from django.db import transaction
from django.utils import timezone

from dating.models import CallSession, CallSignal


def record_call_signal(room_id, user, signal_type, payload):
    with transaction.atomic():
        call = CallSession.objects.select_for_update().filter(room_id=room_id).first()
        if not call or user.pk not in (call.caller_id, call.receiver_id):
            return None, []
        if call.status not in ('ringing', 'connected', 'active'):
            return None, []

        if (
            signal_type == 'offer' and user.pk != call.caller_id
            or signal_type == 'answer' and user.pk != call.receiver_id
        ):
            return None, []

        peer_id = call.receiver_id if user.pk == call.caller_id else call.caller_id
        if signal_type in ('face_verified', 'speech_monitor_ready'):
            if not call.signals.filter(sender=user, signal_type=signal_type).exists():
                signal = CallSignal.objects.create(
                    call=call,
                    sender=user,
                    signal_type=signal_type,
                    payload={
                        'ready': True,
                        'user_id': user.pk,
                        'call_id': str(room_id),
                    },
                )
            else:
                signal = call.signals.filter(
                    sender=user,
                    signal_type=signal_type,
                ).first()
        else:
            signal = CallSignal.objects.create(
                call=call,
                sender=user,
                signal_type=signal_type,
                payload={**payload, 'user_id': user.pk, 'call_id': str(room_id)},
            )

        events = []
        if signal_type not in ('face_verified', 'speech_monitor_ready'):
            events.append({
                'recipient_id': peer_id,
                'type': signal_type,
                'payload': signal.payload,
            })
            return signal, events

        events.append({
            'recipient_id': peer_id,
            'type': signal_type,
            'payload': signal.payload,
        })
        participants_verified = all({
            call.caller_id,
            call.receiver_id,
        } <= set(call.signals.filter(
            signal_type=required_signal,
            sender_id__in=(call.caller_id, call.receiver_id),
        ).values_list('sender_id', flat=True))
            for required_signal in ('face_verified', 'speech_monitor_ready')
        )

        if participants_verified and not call.signals.filter(
            signal_type='feeds_unlocked',
        ).exists():
            payload = {'unlocked': True, 'timestamp': timezone.now().isoformat()}
            CallSignal.objects.bulk_create([
                CallSignal(
                    call=call,
                    sender_id=call.caller_id,
                    signal_type='feeds_unlocked',
                    payload=payload,
                ),
                CallSignal(
                    call=call,
                    sender_id=call.receiver_id,
                    signal_type='feeds_unlocked',
                    payload=payload,
                ),
            ])
            events.extend([
                {'recipient_id': call.caller_id, 'type': 'feeds_unlocked', 'payload': payload},
                {'recipient_id': call.receiver_id, 'type': 'feeds_unlocked', 'payload': payload},
            ])

        return signal, events
