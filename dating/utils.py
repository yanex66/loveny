"""
Off-Platform Contact-Leak Detection Utilities
==============================================
Provides text normalization and a sliding-window aggregation check to detect
fragmented phone numbers and off-platform contact handles in user chat messages.

Designed to catch obfuscation techniques such as:
  - Digit-by-digit splitting across successive messages
  - Mixed word-digit sequences ("zero eight zero one two …")
  - Standard social-media handles and external links
"""

import re
from datetime import timedelta

from django.utils import timezone


# ---------------------------------------------------------------------------
# Word → Digit mapping (including homophones and casual spellings)
# ---------------------------------------------------------------------------
WORD_TO_DIGIT = {
    'zero': '0',
    'oh':   '0',
    'one':  '1',
    'two':  '2',
    'three': '3',
    'four':  '4',
    'five':  '5',
    'six':   '6',
    'seven': '7',
    'eight': '8',
    'nine':  '9',
}

# ---------------------------------------------------------------------------
# Off-platform pattern regexes
# ---------------------------------------------------------------------------

# Matches 10–14 consecutive digits (covers Nigerian mobile: 080…/090…/+234…)
_PHONE_DIGITS_RE = re.compile(r'\d{10,14}')

# Matches common off-platform social keywords and handle patterns
_SOCIAL_PATTERNS = [
    re.compile(r'whatsapp',                       re.IGNORECASE),
    re.compile(r'whats\s*app',                    re.IGNORECASE),
    re.compile(r'whatapp|watsapp|watsup',         re.IGNORECASE),
    re.compile(r'wa\.me',                         re.IGNORECASE),
    re.compile(r'wa\.link',                       re.IGNORECASE),
    re.compile(r'telegram',                       re.IGNORECASE),
    re.compile(r'tele\s*gram',                    re.IGNORECASE),
    re.compile(r't\.me',                          re.IGNORECASE),
    re.compile(r'\btg\b',                         re.IGNORECASE),
    re.compile(r'snapchat',                       re.IGNORECASE),
    re.compile(r'\bsnap\b',                       re.IGNORECASE),
    re.compile(r'instagram',                      re.IGNORECASE),
    re.compile(r'\binsta\b',                      re.IGNORECASE),
    re.compile(r'\big\b',                         re.IGNORECASE),
    re.compile(r'tiktok',                         re.IGNORECASE),
    re.compile(r'twitter',                        re.IGNORECASE),
    re.compile(r'\bx\.com\b',                     re.IGNORECASE),
    re.compile(r'facebook',                       re.IGNORECASE),
    re.compile(r'\bfb\b',                         re.IGNORECASE),
    # Contextual "call me / text me / reach me" phrases
    re.compile(r'call\s+me\s+(?:on|at)',          re.IGNORECASE),
    re.compile(r'text\s+me\s+(?:on|at)',          re.IGNORECASE),
    re.compile(r'reach\s+me\s+(?:on|at)',         re.IGNORECASE),
    re.compile(r'my\s+number\s+is',              re.IGNORECASE),
    re.compile(r'my\s+digits',                   re.IGNORECASE),
    re.compile(r'drop\s+your\s+(?:digits|number)', re.IGNORECASE),
    # Social handle patterns:  @username  or  ig: handle  or  sc: handle
    re.compile(r'(?:ig|insta|sc|snap|tg)\s*[:@\-_]\s*[a-zA-Z0-9_.]{3,}', re.IGNORECASE),
    re.compile(r'(?<!\w)@[a-zA-Z0-9_.]{3,}',    re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """
    Normalise *text* for digit-aggregation checks:

    1. Lower-case.
    2. Replace number words with their digit equivalents.
    3. Strip all non-alphanumeric characters (whitespace, punctuation,
       hyphens, brackets, dots, separators).

    Returns a string of lowercase alphanumeric characters only.
    """
    lowered = text.lower()
    # Replace each recognised word-digit with its numeric character.
    # Use word-boundary anchors to avoid partial replacements.
    for word, digit in WORD_TO_DIGIT.items():
        lowered = re.sub(r'\b' + re.escape(word) + r'\b', digit, lowered)
    # Remove everything that is not a letter or digit.
    return re.sub(r'[^a-z0-9]', '', lowered)


def _check_phone_in_normalized(normalized: str) -> tuple[bool, str | None]:
    """Check *normalized* (no spaces/punct) for a 10-14-digit sequence."""
    match = _PHONE_DIGITS_RE.search(normalized)
    if match:
        return True, f"phone_number_sequence({match.group()})"
    return False, None

def _check_social_in_raw(text: str) -> tuple[bool, str | None]:
    """Check *text* (raw, un-stripped) for social keywords / handles."""
    for pattern in _SOCIAL_PATTERNS:
        m = pattern.search(text)
        if m:
            return True, f"off_platform_keyword({m.group().strip()})"
    return False, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_contact_leak(
    new_text: str,
    sender,
    recipient=None,
    conversation=None,
) -> tuple[bool, str | None]:
    """
    Detect off-platform contact information in *new_text*, taking into account
    the sender's recent message history (sliding-window aggregation).

    Parameters
    ----------
    new_text : str
        The raw incoming message text about to be saved.
    sender : User
        The Django User instance sending the message.
    recipient : User or None
        The recipient User (used to filter history when *conversation* is None).
    conversation : Conversation or None
        If provided, history is scoped to this ``Conversation`` object.

    Returns
    -------
    (is_leak, detected_pattern)
        ``is_leak`` is ``True`` when prohibited content is found.
        ``detected_pattern`` is a short description of what triggered the
        detection, or ``None`` when clean.
    """
    # ------------------------------------------------------------------
    # 1. Check the raw single message first (social keywords & handles).
    # ------------------------------------------------------------------
    is_social, social_pattern = _check_social_in_raw(new_text)
    if is_social:
        return True, social_pattern

    # ------------------------------------------------------------------
    # 2. Build the sliding-window text.
    # ------------------------------------------------------------------
    window_start = timezone.now() - timedelta(minutes=10)

    try:
        if conversation is not None:
            # Import here to avoid circular imports at module load time.
            from .models import ChatMessage  # noqa: PLC0415
            recent_qs = (
                ChatMessage.objects
                .filter(
                    conversation=conversation,
                    sender=sender,
                    created_at__gte=window_start,
                )
                .order_by('-created_at')[:10]
            )
            history_texts = [m.text for m in reversed(list(recent_qs)) if m.text]
        elif recipient is not None:
            from .models import ChatMessage, Conversation  # noqa: PLC0415
            conv = (
                Conversation.objects
                .filter(participants=sender)
                .filter(participants=recipient)
                .first()
            )
            if conv:
                recent_qs = (
                    ChatMessage.objects
                    .filter(
                        conversation=conv,
                        sender=sender,
                        created_at__gte=window_start,
                    )
                    .order_by('-created_at')[:10]
                )
                history_texts = [m.text for m in reversed(list(recent_qs)) if m.text]
            else:
                history_texts = []
        else:
            history_texts = []
    except Exception:
        # Defensive: never let a DB error suppress message sending entirely.
        history_texts = []

    # ------------------------------------------------------------------
    # 3. Normalize and concatenate historical texts + new text.
    # ------------------------------------------------------------------
    normalized_parts = [_normalize(t) for t in history_texts]
    normalized_parts.append(_normalize(new_text))
    aggregated = "".join(normalized_parts)

    # ------------------------------------------------------------------
    # 4. Run digit-sequence check on the aggregated normalized stream.
    # ------------------------------------------------------------------
    is_phone, phone_pattern = _check_phone_in_normalized(aggregated)
    if is_phone:
        return True, phone_pattern

    # Also run against the normalized single message alone (catches a
    # complete number delivered in one shot after normalization).
    is_phone_single, phone_pattern_single = _check_phone_in_normalized(
        _normalize(new_text)
    )
    if is_phone_single:
        return True, phone_pattern_single

    # ------------------------------------------------------------------
    # 5. Run social-keyword check on the aggregated raw text as well.
    # ------------------------------------------------------------------
    aggregated_raw = " ".join(history_texts + [new_text])
    is_social_agg, social_pattern_agg = _check_social_in_raw(aggregated_raw)
    if is_social_agg:
        return True, social_pattern_agg

    return False, None
