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

from dating.models import SupportKnowledgeBase

logger = logging.getLogger(__name__)

FALLBACK_RESPONSE = (
    "Thanks for reaching out! We have received your message. Our human support team "
    "is reviewing it and will get back to you within 1 hour."
)

SUPPORT_INSTRUCTIONS = (
    "You are LOVENY's in-app support assistant. Answer concisely in no more than "
    "2-3 sentences. Platform rules: Hookup Mode costs ₦1,500 per participant, is "
    "coin-free, and unlocks a 24-hour direct chat only after both participants pay; "
    "phone numbers, social handles, and off-platform contact links are prohibited; "
    "video calls require a local 3-second face verification from both participants; "
    "feeds are blocked while native screen capture is active. Never claim a payment "
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
        r'\b(refund(?:ed|s)?|ban(?:ned)?|suspend(?:ed|sion)?|suspension|bugs?|error|crash|broken)\b',
        question,
        re.IGNORECASE,
    ))


def _fallback(ticket, reason):
    logger.warning('Support bot falling back for ticket %s: %s', ticket.pk, reason)
    if ticket.status != 'needs_admin':
        ticket.status = 'needs_admin'
        ticket.save(update_fields=['status', 'updated_at'])
    return FALLBACK_RESPONSE


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
        return cached.answer

    try:
        answer = _generate_answer(question)
    except HTTPError as exc:
        return _fallback(ticket, f'Gemini HTTP {exc.code}')
    except (URLError, TimeoutError, OSError) as exc:
        return _fallback(ticket, f'Gemini connection error ({type(exc).__name__})')
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        RuntimeError,
        KeyError,
        IndexError,
        TypeError,
    ) as exc:
        return _fallback(ticket, f'Gemini response error ({type(exc).__name__})')

    if not answer:
        return _fallback(ticket, 'Gemini returned an empty response')
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
    return answer
