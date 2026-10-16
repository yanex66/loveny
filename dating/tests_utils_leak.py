"""
Quick sanity-test script for dating.utils detect_contact_leak.
Run with: python dating/tests_utils_leak.py
"""
import django
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'loveny_site.settings')
django.setup()

from unittest.mock import MagicMock
from dating.utils import _normalize, _check_phone_in_normalized, _check_social_in_raw, detect_contact_leak

# 1. Normalization: word-digits -> digits, strip non-alphanum
result = _normalize('zero eight zero one two three four')
assert result == '0801234', f'Normalization failed, got: {result}'
print('PASS test_normalize_word_digits')

# 2. Phone detection in normalized string (11-digit Nigerian)
is_phone, pat = _check_phone_in_normalized('08012345678')
assert is_phone, 'Should detect 11-digit Nigerian phone'
print(f'PASS test_phone_11digit: {pat}')

# 3. Phone detection with country code (+234...)
is_phone, pat = _check_phone_in_normalized(_normalize('+2348012345678'))
assert is_phone, 'Should detect +234 prefixed number'
print(f'PASS test_phone_country_code: {pat}')

# 4. Social keyword: whatsapp
is_social, pat = _check_social_in_raw('hit me up on whatsapp')
assert is_social, 'Should detect whatsapp'
print(f'PASS test_social_whatsapp: {pat}')

# 5. Social keyword: telegram
is_social, pat = _check_social_in_raw('find me on telegram')
assert is_social, 'Should detect telegram'
print(f'PASS test_social_telegram: {pat}')

# 6. Social handle @username
is_social, pat = _check_social_in_raw('dm me @lovegirl99')
assert is_social, 'Should detect @handle'
print(f'PASS test_social_at_handle: {pat}')

# 7. Clean message passes
is_social, _ = _check_social_in_raw('Hello, how are you doing today?')
assert not is_social, 'Should be clean'
is_phone, _ = _check_phone_in_normalized(_normalize('Hello, how are you doing today?'))
assert not is_phone, 'Should be clean'
print('PASS test_clean_message')

# 8. detect_contact_leak - whatsapp (no DB context)
is_leak, pat = detect_contact_leak('my whatsapp is active', sender=MagicMock())
assert is_leak, 'Should detect whatsapp'
print(f'PASS test_leak_whatsapp: {pat}')

# 9. detect_contact_leak - clean
is_leak, pat = detect_contact_leak('Nice to meet you!', sender=MagicMock())
assert not is_leak, f'Should be clean, got: {pat}'
print('PASS test_leak_clean')

# 10. Fragmented phone across sliding window simulation (normalized aggregation)
# Simulating: "0801" + "2345" + "678" -> aggregated = "080123456" ...needs 10+ digits
agg_norm = _normalize('0801') + _normalize('2345') + _normalize('67890')
is_phone, pat = _check_phone_in_normalized(agg_norm)
assert is_phone, f'Should detect fragmented phone in aggregated stream, got agg={agg_norm}'
print(f'PASS test_fragmented_phone_aggregated: {pat}')

print('\nAll sanity checks PASSED.')
