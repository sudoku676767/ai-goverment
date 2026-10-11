"""Secret redaction for log records.

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


def _label(kind: str, secret: str) -> str:
    digest = hashlib.sha256(secret.encode("utf-8", "replace")).hexdigest()[:8]
    return f"[REDACTED:{kind}:{digest}]"


def redact_text(text: str) -> str:
    for kind, pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda m, k=kind: _label(k, m.group(0)), text)
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
