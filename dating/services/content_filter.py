"""
Automated Contact-Info Leak Filter & Off-Platform Bypass Detection Engine.
Detects phone numbers, worded numbers, social handles, external links,
leet-speak obfuscation, and Roman numeral encodings in user messages.
"""

import re
import unicodedata
from django.db.models import F


class ContentFilter:
    # Worded digits mapping (including homophones and casual spellings)
    WORDED_DIGITS = {
        'zero': '0', 'oh': '0', 'o': '0', 'nil': '0',
        'one': '1', 'won': '1',
        'two': '2', 'to': '2', 'too': '2',
        'three': '3', 'tree': '3',
        'four': '4', 'for': '4', 'fore': '4',
        'five': '5',
        'six': '6',
        'seven': '7',
        'eight': '8', 'ate': '8',
        'nine': '9',
        'ten': '10', 'eleven': '11', 'twelve': '12',
        'thirteen': '13', 'fourteen': '14', 'fifteen': '15',
        'sixteen': '16', 'seventeen': '17', 'eighteen': '18',
        'nineteen': '19', 'twenty': '20', 'thirty': '30',
        'forty': '40', 'fifty': '50', 'sixty': '60',
        'seventy': '70', 'eighty': '80', 'ninety': '90',
        'hundred': '00', 'thousand': '000',
    }

    ROMAN_NUMERALS = {
        'i': '1', 'ii': '2', 'iii': '3', 'iv': '4', 'v': '5',
        'vi': '6', 'vii': '7', 'viii': '8', 'ix': '9', 'x': '0',
    }

    # Leet-speak character substitutions
    LEET_MAP = {
        '@': 'a',
        '4': 'a',
        '8': 'b',
        '3': 'e',
        '1': 'i',
        '!': 'i',
        '|': 'i',
        '0': 'o',
        '$': 's',
        '5': 's',
        '7': 't',
        '+': 't',
        'v': 'u',
    }

    # Prohibited social and off-platform keywords
    BLOCKED_KEYWORDS = [
        r'whatsapp',
        r'whats\s*app',
        r'whatapp',
        r'watsapp',
        r'watsup',
        r'wa\.me',
        r'wa\.link',
        r'telegram',
        r'tele\s*gram',
        r't\.me',
        r'\btg\b',
        r'instagram',
        r'insta\b',
        r'\big\b',
        r'snapchat',
        r'snap\b',
        r'\bsc\b',
        r'tiktok',
        r'twitter',
        r'\bx\.com\b',
        r'facebook',
        r'\bfb\b',
        r'call\s+me\s+(?:on|at)',
        r'text\s+me\s+(?:on|at)',
        r'reach\s+me\s+(?:on|at)',
        r'dm\s+me\s+(?:on|at)',
        r'my\s+number\s+is',
        r'my\s+digits',
        r'drop\s+your\s+digits',
        r'drop\s+your\s+number',
    ]

    # External domain / link patterns
    EXTERNAL_URL_PATTERNS = [
        r'https?://[^\s]+',
        r'www\.[^\s]+',
        r'[a-zA-Z0-9_\-\.]+\.(?:com|org|net|me|io|ng|co|app|xyz|link|site|top|live)[^\s]*',
        r'wa\.me/\+?\d+',
        r't\.me/[a-zA-Z0-9_]+',
    ]

    @classmethod
    def normalize_text(cls, text: str) -> str:
        """Normalize unicode, lower-case, and strip accented characters."""
        if not text:
            return ""
        normalized = unicodedata.normalize('NFKD', text)
        return "".join(c for c in normalized if not unicodedata.combining(c)).lower()

    @classmethod
    def leet_decode(cls, text: str) -> str:
        """Convert common leet-speak obfuscations to standard alphabetical form."""
        decoded = []
        for char in text:
            decoded.append(cls.LEET_MAP.get(char, char))
        return "".join(decoded)

    @classmethod
    def check_digits_sequence(cls, text: str) -> tuple[bool, str]:
        """
        Check for 7 to 15 digit telephone sequences, even if separated
        by spaces, hyphens, periods, commas, slashes, or currency symbols.
        """
        # Currency/price prefix masking: e.g. ₦08012345678 or #08012345678 or $0801...
        cleaned = re.sub(r'[₦#\$Nn]\s*(?=\d)', '', text)

        # Remove allowable non-digit separators between numbers
        # Match any chunk of digits separated by single/multiple punctuation or spaces
        pattern = r'(?:\+?\d[\s\-\.\,\_\/\(\)\[\]]{0,3}){7,15}\d'
        match = re.search(pattern, cleaned)
        if match:
            # Verify extracted sequence actually contains 7-15 digits
            digits_only = re.sub(r'\D', '', match.group(0))
            if 7 <= len(digits_only) <= 15:
                return True, f"Detected direct phone number sequence ({match.group(0).strip()})"

        # Also check pure continuous digits
        continuous = re.findall(r'\b\d{7,15}\b', text)
        if continuous:
            return True, f"Detected continuous number ({continuous[0]})"

        return False, ""

    @classmethod
    def check_worded_numbers(cls, text: str) -> tuple[bool, str]:
        """
        Detect sequences of 7+ consecutive spoken number words
        e.g. 'zero eight zero one two three four five six seven eight'
        """
        # Tokenize by punctuation and spaces
        tokens = re.findall(r'[a-zA-Z]+', text.lower())
        streak_words = []
        for token in tokens:
            if token in cls.WORDED_DIGITS:
                streak_words.append(token)
                digit_str = "".join(cls.WORDED_DIGITS[w] for w in streak_words)
                if len(digit_str) >= 7:
                    return True, f"Detected worded number sequence ('{' '.join(streak_words)}')"
            else:
                streak_words = []

        return False, ""

    @classmethod
    def check_roman_numerals(cls, text: str) -> tuple[bool, str]:
        """
        Detect sequences of 7+ consecutive Roman numerals used to obfuscate digits.
        """
        tokens = re.findall(r'[a-zA-Z]+', text.lower())
        streak = []
        for token in tokens:
            if token in cls.ROMAN_NUMERALS:
                streak.append(token)
                if len(streak) >= 7:
                    return True, f"Detected Roman numeral sequence ('{' '.join(streak)}')"
            else:
                streak = []

        return False, ""

    @classmethod
    def check_social_keywords(cls, text: str) -> tuple[bool, str]:
        """
        Detect direct or obfuscated mentions of WhatsApp, Telegram, Instagram, etc.
        """
        norm = cls.normalize_text(text)
        leet = cls.leet_decode(norm)

        # Check raw normalized text
        for kw_pattern in cls.BLOCKED_KEYWORDS:
            if re.search(kw_pattern, norm, re.IGNORECASE):
                return True, f"Detected off-platform communication keyword ({kw_pattern})"

        # Check leet-decoded text
        for kw_pattern in cls.BLOCKED_KEYWORDS:
            if re.search(kw_pattern, leet, re.IGNORECASE):
                return True, f"Detected obfuscated off-platform keyword ({kw_pattern})"

        # Check intra-word punctuation: e.g. w.h.a.t.s.a.p.p or w h a t s a p p
        collapsed = re.sub(r'[\s\.\-\_\,\*]+', '', norm)
        collapsed_leet = cls.leet_decode(collapsed)
        for basic in ['whatsapp', 'telegram', 'instagram', 'snapchat']:
            if basic in collapsed or basic in collapsed_leet:
                return True, f"Detected spaced/punctuated keyword ({basic})"

        # Check social handles pattern: e.g. @username or ig: username
        if re.search(r'(?:ig|insta|sc|snap|tg)\s*[:@\-_]\s*[a-zA-Z0-9_\.]{3,}', norm):
            return True, "Detected social media handle"

        # Check external URLs
        for url_pattern in cls.EXTERNAL_URL_PATTERNS:
            if re.search(url_pattern, text, re.IGNORECASE):
                return True, "Detected external link or off-platform URL"

        return False, ""

    @classmethod
    def inspect(cls, text: str) -> tuple[bool, str]:
        """
        Inspect message for all prohibited content.
        Returns:
            (is_blocked: bool, reason: str)
        """
        if not text or not text.strip():
            return False, ""

        # 1. Digits sequence check
        blocked, reason = cls.check_digits_sequence(text)
        if blocked:
            return True, reason

        # 2. Worded numbers check
        blocked, reason = cls.check_worded_numbers(text)
        if blocked:
            return True, reason

        # 3. Roman numerals check
        blocked, reason = cls.check_roman_numerals(text)
        if blocked:
            return True, reason

        # 4. Social handles & keywords check
        blocked, reason = cls.check_social_keywords(text)
        if blocked:
            return True, reason

        # 5. AI Monitoring (Fallback for obfuscated hints)
        blocked, reason = cls.check_ai_monitoring(text)
        if blocked:
            return True, reason

        return False, ""

    @classmethod
    def check_ai_monitoring(cls, text: str) -> tuple[bool, str]:
        """
        Fallback AI check for tricky messages. Only runs if GEMINI_API_KEY is configured
        and the text contains suspicious trigger words.
        """
        from django.conf import settings
        
        api_key = getattr(settings, 'GEMINI_API_KEY', None)
        if not api_key:
            return False, ""

        trigger_words = [
            'spell', 'digit', 'code', 'number', 'add me', 'contact', 'snap', 'ig',
            'whatsapp', 'call', 'text', 'reach', 'birth', 'year', 'month', 'age'
        ]
        
        # Only invoke AI if the text hints at sharing info
        if not any(word in text.lower() for word in trigger_words):
            return False, ""

        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            
            prompt = (
                "You are a strict content filter for a dating app. "
                "Analyze this chat message (which may be part of a conversation history). "
                "Does this text contain a hidden or obfuscated attempt to share contact information "
                "(like phone numbers, social media handles, email addresses) or bypass safety filters? "
                f"Message to analyze: '{text}'\n\n"
                "Reply strictly with YES if it is an attempt to share contact info, or NO if it is innocent conversation."
            )
            
            response = client.models.generate_content(
                model='gemini-1.5-flash',
                contents=prompt,
            )
            
            if response.text and "YES" in response.text.strip().upper():
                return True, "AI detected obfuscated contact sharing attempt"
                
        except Exception as e:
            # Silently fail and allow message if AI service is down or rate limited
            pass
            
        return False, ""

    @classmethod
    def inspect_and_record(cls, user, text: str) -> tuple[bool, str | None]:
        """
        Inspect message text. If prohibited:
        - Increments user's profile.off_platform_attempts
        - Returns (False, error_message)
        If clean:
        - Returns (True, None)
        """
        is_blocked, reason = cls.inspect(text)
        if is_blocked:
            if user and hasattr(user, 'profile') and user.profile:
                # Increment off_platform_attempts counter
                user.profile.off_platform_attempts = F('off_platform_attempts') + 1
                user.profile.save(update_fields=['off_platform_attempts'])
                
                # Deactivate user account immediately
                user.is_active = False
                user.save(update_fields=['is_active'])

            warning_message = (
                "⚠️ Sharing external contact information, phone numbers, or social handles "
                "is strictly prohibited. Your account has been flagged and deactivated for violating our safety policies."
            )
            return False, warning_message

        return True, None

