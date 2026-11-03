"""Secret and personal-information redaction for log records.

Redaction only touches what is written to the log. Traffic forwarded to the
upstream API is never modified.

Each secret is replaced by a label that keeps a short hash of the original,
so you can tell "the same key was used in both requests" without storing it.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "cookie",
    "set-cookie",
}

# (kind, pattern). Order matters: more specific patterns first.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/]{16,}=*")),
]

# Chinese personal information. Python's \b does not fire between a Chinese
# character and a digit (both count as word characters), so digit runs are
# bounded with look-arounds instead. Number-like patterns are checked with a
# validator so that order numbers and similar long digit runs are left alone.
PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("cn-id", re.compile(r"(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])")),
    ("bank-card", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    (
        "cn-mobile",
        re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d(?:[- ]?\d{4}){2}(?!\d)"),
    ),
    ("cn-landline", re.compile(r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)")),
]

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"


def is_cn_id(value: str) -> bool:
    """18-digit resident ID: valid birth date and GB 11643 check digit."""
    if len(value) != 18 or not value[:17].isdigit():
        return False
    year, month, day = int(value[6:10]), int(value[10:12]), int(value[12:14])
    if not (1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return False
    total = sum(int(d) * w for d, w in zip(value[:17], _ID_WEIGHTS))
    return _ID_CHECK[total % 11] == value[17].upper()


def is_luhn(value: str) -> bool:
    digits = [int(d) for d in value]
    for i in range(len(digits) - 2, -1, -2):
        digits[i] = digits[i] * 2 - 9 if digits[i] > 4 else digits[i] * 2
    return sum(digits) % 10 == 0


_VALIDATORS = {"cn-id": is_cn_id, "bank-card": is_luhn}


def _label(kind: str, secret: str) -> str:
    digest = hashlib.sha256(secret.encode("utf-8", "replace")).hexdigest()[:8]
    return f"[REDACTED:{kind}:{digest}]"


def _sub(kind: str, match: re.Match[str]) -> str:
    found = match.group(0)
    check = _VALIDATORS.get(kind)
    if check is not None and not check(found):
        return found
    return _label(kind, found)


def redact_text(text: str) -> str:
    for kind, pattern in SECRET_PATTERNS + PII_PATTERNS:
        text = pattern.sub(lambda m, k=kind: _sub(k, m), text)
    return text


def redact_value(value: Any) -> Any:
    """Recursively redact every string inside a JSON-like value."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    return value


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in SENSITIVE_HEADERS:
            out[name] = _label("header", value)
        else:
            out[name] = redact_text(value)
    return out
