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

from dating.models import (
    ConnectionFeeSetting,
    HookupSettings,
    SiteConfiguration,
    SupportKnowledgeBase,
)

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
    "pay the configured connection fee through Paystack or Flutterwave to unlock access; "
    "mutual WhatsApp contact matches reveal each phone number only after both participants "
    "have paid or used a match credit; "
    "never claim a payment "
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
    if any(word in normalized for word in ('verification', 'profile verification')):
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
    if any(term in normalized for term in ('whatsapp', 'contact number')) and any(
        term in normalized for term in ('unlock', 'reveal', 'share', 'pay')
    ):
        fees = ConnectionFeeSetting.get_solo()
        return (
            'After a mutual match, both participants must each pay the fee for that connection mode '
            'or use one match credit. Once both payments are complete, their WhatsApp contacts '
            f'are revealed on the Matches page. Current fees are Dating ₦{fees.dating_fee}, '
            f'Hookup ₦{fees.hookup_fee}, and Sex Call ₦{fees.sexcall_fee}.'
        )
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
        if any(term in normalized for term in ('coin', 'wallet')):
            return (
                'To buy coins, open the Coins shop from your coin balance, choose a package, '
                'and complete checkout. Coins are added only after the payment provider verifies the payment.'
            )
        if any(term in normalized for term in ('premium', 'subscription', 'pass', 'dating access')):
            return (
                'Open the Premium or Upgrade page, choose a pass for your active connection mode, and '
                'complete checkout. Premium access starts only after the payment provider verifies payment.'
            )
        return (
            'I can guide you through payment for mode switching (Settings), a Hookup request (open the '
            'pending request), coins (Coins shop), or a Premium pass (Upgrade). Which '
            'one are you trying to pay for? LOVENY activates purchases only after the payment provider '
            'confirms them.'
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
            'I can help with account settings, changing modes, referrals, contact matches, payments, '
            'and common app questions. '
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
            'Contact matches are unlocked when both participants pay the mode fee or use a match credit. '
            'You can review pending matches and their payment status on the Matches page.'
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
