"""Small privacy helpers for bridge-visible text and event payloads."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "token",
        "auth",
        "authorization",
        "access_token",
        "refresh_token",
        "id_token",
        "key",
        "api_key",
        "apikey",
        "secret",
        "sig",
        "signature",
        "code",
        "magic",
        "session",
        "jwt",
    }
)

_URL_IN_TEXT = re.compile(r"https?://\S+", re.IGNORECASE)
_BEARER_TOKEN = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_SECRET_LABEL_VALUE = re.compile(
    r"\b("
    r"password|passwd|pwd|passcode|pin|otp|totp|mfa|2fa|cvv|"
    r"api[_-]?key|secret|token|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|session|jwt|authorization"
    r")\b\s*[:=]\s*(?!//)([^\s,;]+)",
    re.IGNORECASE,
)
_PASSWORD_WORD_VALUE = re.compile(
    r"\b(password|passwd|pwd|passcode|pin|otp|totp|mfa|2fa|cvv)\b\s+(\S+)",
    re.IGNORECASE,
)
_OPENAI_STYLE_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b")
_SENSITIVE_FIELD_MARKERS = (
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "authorization",
    "api_key",
    "apikey",
    "nonce",
)


def _scrub_single_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except (TypeError, ValueError):
        return url

    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        if any(k.lower() in _SENSITIVE_QUERY_KEYS for k, _ in pairs):
            query = urlencode(
                [
                    (k, "[REDACTED]" if k.lower() in _SENSITIVE_QUERY_KEYS else v)
                    for k, v in pairs
                ]
            )

    fragment = ""
    if parts.fragment:
        fragment_pairs = parse_qsl(parts.fragment, keep_blank_values=True)
        if fragment_pairs:
            if any(k.lower() in _SENSITIVE_QUERY_KEYS for k, _ in fragment_pairs):
                fragment = urlencode(
                    [
                        (k, "[REDACTED]" if k.lower() in _SENSITIVE_QUERY_KEYS else v)
                        for k, v in fragment_pairs
                    ]
                )
            else:
                fragment = parts.fragment
        else:
            fragment = "[REDACTED]"

    return urlunsplit(parts._replace(query=query, fragment=fragment))


def redact_text(value: str) -> str:
    """Redact common secret-shaped values from bridge events/status text."""
    if not value:
        return value
    out = _URL_IN_TEXT.sub(lambda m: _scrub_single_url(m.group(0)), value)
    out = _BEARER_TOKEN.sub("Bearer [REDACTED]", out)
    out = _OPENAI_STYLE_KEY.sub("[REDACTED]", out)
    out = _SECRET_LABEL_VALUE.sub(lambda m: f"{m.group(1)}=[REDACTED]", out)
    out = _PASSWORD_WORD_VALUE.sub(lambda m: f"{m.group(1)} [REDACTED]", out)
    return out


def redact_payload(value: Any) -> Any:
    """Recursively redact textual values while preserving payload structure."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_payload(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_payload(v) for v in value)
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"base64", "screenshot", "image"}:
                redacted[key] = "[REDACTED]"
            elif any(marker in lowered for marker in _SENSITIVE_FIELD_MARKERS) and lowered not in {
                "await_id"
            }:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_payload(item)
        return redacted
    return value
