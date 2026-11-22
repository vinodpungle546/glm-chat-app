#!/usr/bin/env python3
"""Detect content that looks confidential (used only in Auto model mode).

detect() returns human-readable reasons; an empty list means nothing sensitive
was found. It is deliberately conservative and transparent: the user always
sees why the app suggests the local model, and decides.
"""

from __future__ import annotations

import re

KEYWORDS = [
    "confidential", "internal only", "internal use only", "strictly private", "do not share",
    "do not distribute", "not for distribution", "nda", "non-disclosure", "proprietary",
    "trade secret", "salary", "salaries", "compensation", "payroll", "bonus", "appraisal",
    "performance review", "layoff", "lay off", "termination", "resignation", "disciplinary",
    "merger", "acquisition", "m&a", "due diligence", "term sheet", "pre-announcement",
    "password", "passcode", "credentials", "medical", "diagnosis", "health record",
    "bank account", "privileged", "attorney-client", "legal hold", "pii", "personal data",
]
_KEYWORD_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in KEYWORDS) + r")\b", re.IGNORECASE)

PATTERNS = [
    ("email address", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\b")),
    ("PAN number", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("Aadhaar number", re.compile(r"(?<![\d][ -])(?<!\d)\d{4}\s\d{4}\s\d{4}(?![ -]?\d)")),
    ("US SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("API key or token", re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|xox[abpr]-[A-Za-z0-9-]{10,}"
        r"|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
]
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_PHONE_CANDIDATE = re.compile(r"(?<![\w+])\+?\d[\d\s().-]{7,17}\d(?!\w)")
_GROUPED_4S = re.compile(r"\d{4}([ -])\d{4}\1\d{4}(?:\1\d{4})*")


def _has_phone(text: str) -> bool:
    """10-13 digit numbers in phone-like groupings (not card/ID-style 4-4-4 blocks)."""
    for match in _PHONE_CANDIDATE.finditer(text):
        candidate = match.group(0).strip()
        digits = re.sub(r"\D", "", candidate)
        if not 10 <= len(digits) <= 13 or _GROUPED_4S.fullmatch(candidate):
            continue
        return True
    return False


def _luhn_ok(number: str) -> bool:
    """The Luhn checksum used by payment card numbers; filters out random digit runs that only look like a card
    number.
    """
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            # Doubling a digit can give 10-18; subtracting 9 adds its two digits together (Luhn's rule).
            d -= 9 if d > 9 else 0
        total += d
    return total % 10 == 0


def detect(text: str, attachments: list[dict] | None = None, extra_terms: list[str] | None = None) -> list[str]:
    """Reasons this content looks confidential (empty list = nothing found)."""
    reasons: list[str] = []
    text = text or ""

    for att in attachments or []:
        if att.get("kind") == "transcript":
            reasons.append(f"meeting transcript attached ({att.get('name', 'transcript')})")
    attachment_text = "\n".join(a.get("text", "") for a in attachments or [] if a.get("kind") != "transcript")
    haystack = text + "\n" + attachment_text

    # Each check below adds a reason the user will see in the 'send to the cloud?' question.
    found = sorted({m.group(1).lower() for m in _KEYWORD_RE.finditer(haystack)})
    if found:
        reasons.append("keywords: " + ", ".join(f"“{k}”" for k in found[:5]))

    for term in extra_terms or []:
        term = term.strip()
        if term and re.search(r"\b" + re.escape(term) + r"\b", haystack, re.IGNORECASE):
            reasons.append(f"confidential term for this project: “{term}”")

    if _has_phone(haystack):
        reasons.append("phone number")
    for label, pattern in PATTERNS:
        if pattern.search(haystack):
            reasons.append(label)

    # A long digit run only counts as a card number when its Luhn checksum is valid.
    if any(_luhn_ok(m.group(0)) for m in _CARD_RE.finditer(haystack)):
        reasons.append("payment card number")

    seen, unique = set(), []
    for r in reasons:
        if r not in seen:
            unique.append(r)
            seen.add(r)
    return unique
