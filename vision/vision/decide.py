"""Top-level entry point: decide_action().

Wraps the tier router with:
   1. Step-history truncation to last 5.
   2. Audit logging + screenshot persistence.
"""

from __future__ import annotations

import re
import time
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from vision.audit import append_audit, now_iso, save_screenshot
from vision.models import ActionDecision, StepHistoryItem, VisionConfig
from vision.router import route


HISTORY_MAX = 5


# URL query-param names whose VALUES may carry secrets — scrubbed before audit.
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

# Match http(s) URLs embedded anywhere in a string so we can scrub each one
# without disturbing the surrounding free text. Stops at whitespace.
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


def _scrub_url(value: str) -> str:
    """Redact sensitive query-param VALUES in any URL found inside `value`.

    Works on both bare URLs (page_url) and free text that embeds a URL
    (task_context). Non-URL substrings are left byte-for-byte unchanged, so a
    plain task string containing a stray "?" is never mangled.
    """
    if not value:
        return value
    return _URL_IN_TEXT.sub(lambda m: _scrub_single_url(m.group(0)), value)


def _scrub_single_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except (ValueError, TypeError):
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


def _scrub_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    out = _scrub_url(value)
    out = _BEARER_TOKEN.sub("Bearer [REDACTED]", out)
    out = _OPENAI_STYLE_KEY.sub("[REDACTED]", out)
    out = _SECRET_LABEL_VALUE.sub(lambda m: f"{m.group(1)}=[REDACTED]", out)
    out = _PASSWORD_WORD_VALUE.sub(lambda m: f"{m.group(1)} [REDACTED]", out)
    return out


def _scrub_decision_value(value):
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, list):
        return [_scrub_decision_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_decision_value(v) for k, v in value.items()}
    return value


def _redacted_decision_dump(decision: ActionDecision) -> dict:
    """model_dump with the typed `text` redacted for type-actions.

    A `type` action's `text` is literally what the executor will key into a
    field — possibly a password/passcode. Other text fields are scrubbed for
    URL tokens and common secret label/value patterns before JSONL persistence.
    """
    dumped = _scrub_decision_value(decision.model_dump())
    if decision.action == "type" and dumped.get("text") is not None:
        dumped["text"] = "[REDACTED]"
    return dumped


async def decide_action(
    screenshot: bytes,
    task_context: str,
    step_history: list[StepHistoryItem],
    page_url: str,
    config: Optional[VisionConfig] = None,
) -> ActionDecision:
    """Single entry point. See vision-service spec for the full contract.

    The function:
       1. Truncates step history to the most recent 5 items.
       2. Persists the screenshot to the content-addressed store.
       3. Runs the tier router (escalating as needed).
       4. Appends one audit row to the JSONL log.

    Returns the final ActionDecision. The decision is also written to the audit
    log; callers don't need to log it again.
    """
    if config is None:
        config = VisionConfig()

    start_wall = time.monotonic()

    # ---- step 1: truncate history ------------------------------------------
    history = list(step_history[-HISTORY_MAX:])

    # ---- step 2: persist screenshot ----------------------------------------
    digest, _ = save_screenshot(screenshot, config.resolved_screenshot_dir())

    # ---- step 3: model router (one-shot, or optional fallback chain) --------
    decision = await route(
        screenshot=screenshot,
        task_context=task_context,
        page_url=page_url,
        step_history=history,
        config=config,
    )

    # ---- step 4: audit -----------------------------------------------------
    append_audit(
        config.resolved_audit_path(),
        {
            "at": now_iso(),
            "screenshot_sha256": digest,
            "task_context": _scrub_text(task_context),
            "page_url": _scrub_text(page_url),
            "history_len": len(history),
            "models": [m.model for m in config.models],
            "guardrail_triggered": None,
            "decision": _redacted_decision_dump(decision),
        },
    )
    return decision
