"""Outbound redaction, applied to every request kind (chat, embed, rerank) before it leaves (DESIGN §7.1).

Pattern-based: emails, phone numbers, US SSNs and payment-card numbers (Luhn-checked). It does NOT catch
personal names. Name detection needs an NER model and is a Phase 5 item. Aviation identifiers (AD numbers
such as 2026-09-03 or 2025-12858, JASC codes, part numbers, registrations) must pass through unchanged,
and the tests pin that.
"""

import re

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# +1 (555) 123-4567, 555-123-4567, 555.123.4567, +44 20 7946 0958. Needs separators, so bare digit runs
# (part numbers, serials) are left alone.
PHONE = re.compile(r"(?<![\w-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)|\d{2,4})[ .-]\d{3,4}[ .-]\d{4}(?![\w-])")
SSN = re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])")
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group())
    return "[CARD]" if 13 <= len(digits) <= 19 and _luhn(digits) else match.group()


def redact(text: str) -> str:
    text = EMAIL.sub("[EMAIL]", text)
    text = CARD.sub(_card, text)
    text = SSN.sub("[SSN]", text)
    return PHONE.sub("[PHONE]", text)
