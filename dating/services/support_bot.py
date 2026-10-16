import json
import logging
import re
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from dating.models import HookupSettings, SiteConfiguration, SupportKnowledgeBase

logger = logging.getLogger(__name__)

FALLBACK_RESPONSE = (
    "I’m LOVENY’s automated support assistant. I couldn’t find an instant answer to "
    "that message. Please share what you need help with, and I’ll do my best to guide you."
)
ESCALATION_RESPONSE = (
    "I couldn’t resolve this automatically, so I’ve flagged your request for a support "
    "agent. Please add any details that could help them."
)
GREETING_RESPONSE = (
    "Hi! I’m LOVENY’s automated support assistant. What can I help you with?"
)

SUPPORT_INSTRUCTIONS = (
    "You are LOVENY's in-app support assistant. Answer concisely in no more than "
    "2-3 sentences. Platform rules: after a Hookup request is accepted, both users must each "
    "pay the configured cash connection fee through Paystack or Flutterwave to unlock the chat; "
    "Dating matches are free, but sending messages requires paid Dating Premium; "
    "new Sex Call accounts receive one free three-minute call, after which calls require coins or premium; "
    "phone numbers, social handles, and off-platform contact links are prohibited; "
    "video calls require a local 3-second face verification from both participants; "
    "both participants must also have live speech monitoring active before feeds unlock. "
    "The browser speech-recognition provider may process audio; LOVENY does not record calls "
    "or retain full transcripts. Suspected contact sharing ends the call and suspends the "
    "account pending admin review. Feeds are blocked while native screen capture is active. "
    "Never claim a payment "
    "is complete unless Paystack confirms it. For refund requests, account bans or "
    "suspensions, or software bugs, append [ESCALATE_ADMIN]."
)


def normalize_query(question):
    normalized = unicodedata.normalize('NFKC', question or '').lower().strip()
    return re.sub(r'\s+', ' ', normalized)[:255]


def _category_for(question):
    normalized = question.lower()
    if any(word in normalized for word in ('pay', 'payment', 'billing', 'refund', 'paystack')):
        return 'billing'
    if any(word in normalized for word in ('hookup', 'connection fee', '24-hour', '24 hour')):
        return 'hookup'
    if any(word in normalized for word in ('face', 'verification', 'screen capture', 'recording', 'privacy')):
        return 'verification'
    return 'general'


def _needs_admin(question):
    return bool(re.search(
        r'\b(refund(?:ed|s)?|ban(?:ned)?|suspend(?:ed|sion)?|suspension|bugs?|error|crash|broken|human|agent|person)\b|speak to support',
        question,
        re.IGNORECASE,
    ))


def _small_talk_response(question):
    normalized = re.sub(r'[^\w\s]', '', normalize_query(question))
    if re.fullmatch(
        r'(hi|hello|hey|good morning|good afternoon|good evening|'
        r'(hi|hello|hey)[\w\s]*(are you there|anyone there|you there))',
        normalized,
    ):
        return GREETING_RESPONSE
    if re.fullmatch(r'(ok|okay|thanks|thank you|cool|got it)', normalized):
        return ''
    return None


def _known_platform_answer(question):
    normalized = normalize_query(question)
    payment_intent = any(phrase in normalized for phrase in (
        'want to pay',
        'need to pay',
        'trying to pay',
        'how do i pay',
        'how can i pay',
        'make a payment',
        'payment options',
        'payment methods',
        'pay now',
    ))
    if payment_intent:
        if any(term in normalized for term in ('mode switch', 'switch mode', 'change mode', 'switching')):
            return (
                'To unlock mode switching, open Settings and choose Change Connection Mode, then select '
                'Unlock Mode Switching. The one-time fee is charged through Paystack; switching unlocks '
                'only after the payment is verified.'
            )
        if any(term in normalized for term in ('hookup', 'connection request', 'connection fee')):
            config = HookupSettings.get_solo()
            return (
                f'After an accepted Hookup request, both users must each pay ₦{config.connection_fee} '
                f'through Paystack or Flutterwave before the 24-hour chat unlocks.'
            )
        if any(term in normalized for term in ('coin', 'call minute', 'minutes', 'wallet')):
            return (
                'To buy coins or call minutes, open the Coins shop from your coin balance, choose a package, '
                'and complete checkout. Coins are added only after the payment provider verifies the payment.'
            )
        if any(term in normalized for term in ('premium', 'subscription', 'pass', 'dating access')):
            return (
                'Open the Premium or Upgrade page, choose a pass for your active connection mode, and '
                'complete checkout. Premium access starts only after the payment provider verifies payment.'
            )
        return (
            'I can guide you through payment for mode switching (Settings), a Hookup request (open the '
            'pending request), coins or call minutes (Coins shop), or a Premium pass (Upgrade). Which '
            'one are you trying to pay for? LOVENY activates purchases only after the payment provider '
            'confirms them.'
        )
    if any(term in normalized for term in ('live call', 'video call', 'call audio', 'call speech')) and any(
        term in normalized for term in ('monitor', 'record', 'contact', 'whatsapp', 'number', 'exchange', 'listen')
    ):
        return (
            'LOVENY uses automated speech recognition during live calls to detect suspected '
            'attempts to exchange phone numbers, social handles, or other contact details. '
            'Your browser’s speech-recognition provider may process audio; LOVENY does not '
            'record calls or retain full transcripts. A likely match ends the call and '
            'immediately suspends the account pending admin review.'
        )
    if any(phrase in normalized for phrase in (
        'what can you do',
        'what can you do for me',
        'what can you help with',
        'what can you help me with',
        'what do you do',
        'how can you help',
        'how can you help me',
    )):
        return (
            'I can help with account settings, changing modes, referrals, premium messaging, free Sex Calls, '
            'Hookup requests and payments, video calls, and common app questions. '
            'Tell me what you are trying to do, and I’ll point you to the right place.'
        )
    if any(word in normalized for word in ('mode', 'connection mode')) and any(
        word in normalized for word in ('change', 'switch', 'edit', 'dating', 'hookup', 'sex call')
    ):
        fee = SiteConfiguration.get_solo().mode_switch_fee
        return (
            f'You can change your connection mode from Settings. The ₦{fee} Paystack fee is paid once '
            f'to unlock lifetime switching between Dating, Hookup, and Sex Call. Your saved details for each mode are kept.'
        )
    if any(word in normalized for word in ('refer', 'referral', 'invite friends', 'invite a friend')):
        config = SiteConfiguration.get_solo()
        return (
            f'Open My Profile and copy your referral link. When a new user signs up through it, '
            f'you receive {config.referral_reward_coins} coins. Referral rewards are coins and '
            f'do not unlock Hookup connections; after acceptance, both participants pay the '
            f'configured connection fee.'
        )
    if any(word in normalized for word in ('free trial', 'free access', 'trial')):
        return (
            'New accounts do not receive a 24-hour premium trial. New Sex Call accounts get one '
            'three-minute free call. Dating users can match for free, but need Dating Premium to message.'
        )
    if ('request' in normalized or 'meet today' in normalized) and any(
        word in normalized for word in ('sent', 'receive', 'where', 'inbox', 'get')
    ):
        return (
            'Hookup requests go to the recipient’s Hookup inbox, where they can accept or decline. '
            'They also receive an in-app notification. If you already sent a request, it will show as pending rather than sending a duplicate.'
        )
    return None


def _fallback(ticket, reason, *, escalate):
    logger.warning('Support bot falling back for ticket %s: %s', ticket.pk, reason)
    if escalate and ticket.status != 'needs_admin':
        ticket.status = 'needs_admin'
        ticket.save(update_fields=['status', 'updated_at'])
    return ESCALATION_RESPONSE if escalate else FALLBACK_RESPONSE


def _generate_answer(question):
    api_key = getattr(settings, 'GEMINI_API_KEY', '')
    if not api_key or api_key == 'your_gemini_api_key_here':
        raise RuntimeError('GEMINI_API_KEY is not configured')

    url = (
        'https://generativelanguage.googleapis.com/v1beta/models/'
        f'gemini-2.5-flash:generateContent?{urlencode({"key": api_key})}'
    )
    body = json.dumps({
        'systemInstruction': {'parts': [{'text': SUPPORT_INSTRUCTIONS}]},
        'contents': [{'role': 'user', 'parts': [{'text': question}]}],
        'generationConfig': {'maxOutputTokens': 180, 'temperature': 0.2},
    }).encode('utf-8')
    request = Request(
        url,
        data=body,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urlopen(request, timeout=12) as response:
        result = json.loads(response.read().decode('utf-8'))
    return ' '.join(
        part['text'].strip()
        for part in result['candidates'][0]['content']['parts']
        if isinstance(part, dict) and isinstance(part.get('text'), str)
    ).strip()


def process_support_inquiry(ticket, question):
    small_talk_response = _small_talk_response(question)
    if small_talk_response is not None:
        return small_talk_response
    known_answer = _known_platform_answer(question)
    if known_answer is not None:
        return known_answer

    normalized_query = normalize_query(question)
    cached = SupportKnowledgeBase.objects.filter(
        normalized_query=normalized_query,
    ).order_by('-is_verified_by_admin', '-updated_at').first()
    if cached:
        SupportKnowledgeBase.objects.filter(pk=cached.pk).update(
            hit_count=F('hit_count') + 1,
            updated_at=timezone.now(),
        )
        if '[ESCALATE_ADMIN]' in cached.answer and ticket.status != 'needs_admin':
            ticket.status = 'needs_admin'
            ticket.save(update_fields=['status', 'updated_at'])
        return cached.answer.replace('[ESCALATE_ADMIN]', '').strip()

    try:
        answer = _generate_answer(question)
    except HTTPError as exc:
        return _fallback(
            ticket,
            f'Gemini HTTP {exc.code}',
            escalate=_needs_admin(question),
        )
    except (URLError, TimeoutError, OSError) as exc:
        return _fallback(
            ticket,
            f'Gemini connection error ({type(exc).__name__})',
            escalate=_needs_admin(question),
        )
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        RuntimeError,
        KeyError,
        IndexError,
        TypeError,
    ) as exc:
        return _fallback(
            ticket,
            f'Gemini response error ({type(exc).__name__})',
            escalate=_needs_admin(question),
        )

    if not answer:
        return _fallback(
            ticket,
            'Gemini returned an empty response',
            escalate=_needs_admin(question),
        )
    if _needs_admin(question) and '[ESCALATE_ADMIN]' not in answer:
        answer = f'{answer.rstrip()} [ESCALATE_ADMIN]'

    SupportKnowledgeBase.objects.create(
        normalized_query=normalized_query,
        question_sample=question,
        answer=answer,
        category=_category_for(question),
        is_verified_by_admin=False,
    )
    if '[ESCALATE_ADMIN]' in answer and ticket.status != 'needs_admin':
        ticket.status = 'needs_admin'
        ticket.save(update_fields=['status', 'updated_at'])
    return answer.replace('[ESCALATE_ADMIN]', '').strip()
