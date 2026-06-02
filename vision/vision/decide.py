"""Top-level entry point: decide_action().

Wraps the tier router with:
  1. Money-action guardrail (short-circuits BEFORE any LLM call).
  2. Step-history truncation to last 5.
  3. Audit logging + screenshot persistence.
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


# CANONICAL MONEY TOKENS — keep verbatim in sync with VisionConfig.high_stakes_actions
# (models.py), prompts.py, background.js, bridge.py.
MONEY_TOKENS = frozenset(
    {
        "redeem",
        "redemption",
        "deposit",
        "withdraw",
        "withdrawal",
        "transfer",
        "cashout",
        "cash out",
        "cashier",
        "payout",
    }
)


# URL query-param names whose VALUES may carry secrets — scrubbed before audit.
_SENSITIVE_QUERY_KEYS = frozenset(
    {"token", "auth", "access_token", "key", "sig", "code", "magic"}
)

# Match http(s) URLs embedded anywhere in a string so we can scrub each one
# without disturbing the surrounding free text. Stops at whitespace.
_URL_IN_TEXT = re.compile(r"https?://\S+", re.IGNORECASE)


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
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not any(k.lower() in _SENSITIVE_QUERY_KEYS for k, _ in pairs):
        return url
    scrubbed = [
        (k, "[REDACTED]" if k.lower() in _SENSITIVE_QUERY_KEYS else v)
        for k, v in pairs
    ]
    return urlunsplit(parts._replace(query=urlencode(scrubbed)))


def _detect_money_action(
    task_context: str, page_url: str, high_stakes: set[str]
) -> Optional[str]:
    """Token match against task + url. Returns the matched token or None.

    Substring match on lowercased haystack. The token set is small enough that
    false positives are cheap (we'd just decline a non-money action that
    happens to contain the word "deposit") and false negatives are the
    expensive case (we'd auto-click a money button).
    """
    haystack = f"{task_context} {page_url}".lower()
    # `high_stakes` is a set (unordered). Return the token that appears EARLIEST
    # in the haystack so the result is deterministic regardless of set ordering;
    # ties broken by token length (longer/more-specific first).
    best_token: Optional[str] = None
    best_pos = len(haystack) + 1
    for token in high_stakes:
        pos = haystack.find(token.lower())
        if pos == -1:
            continue
        if pos < best_pos or (pos == best_pos and len(token) > len(best_token or "")):
            best_pos = pos
            best_token = token
    return best_token


def _redacted_decision_dump(decision: ActionDecision) -> dict:
    """model_dump with the typed `text` redacted for type-actions.

    A `type` action's `text` is literally what the executor will key into a
    field — possibly a password/passcode. We must never persist it to the
    JSONL audit log. Other action types leave `text` (URL for navigate, etc.).
    """
    dumped = decision.model_dump()
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
      1. Applies the money-action guardrail (no LLM call if triggered).
      2. Truncates step history to the most recent 5 items.
      3. Persists the screenshot to the content-addressed store.
      4. Runs the tier router (escalating as needed).
      5. Appends one audit row to the JSONL log.

    Returns the final ActionDecision. The decision is also written to the audit
    log; callers don't need to log it again.
    """
    if config is None:
        config = VisionConfig()

    start_wall = time.monotonic()

    # ---- step 1: money-action guardrail ------------------------------------
    money_hit = _detect_money_action(
        task_context, page_url, config.high_stakes_actions
    )
    if money_hit:
        decision = ActionDecision(
            action="done",
            coordinates=None,
            selector_hint=None,
            text=None,
            confidence=1.0,
            reasoning="money_action_requires_human",
            model_used="guardrail",
            cost_usd=0.0,
            duration_ms=int((time.monotonic() - start_wall) * 1000),
            escalations=[],
        )
        # Still record the screenshot + audit row — we want a full trail of
        # what would have happened.
        try:
            digest, _ = save_screenshot(screenshot, config.resolved_screenshot_dir())
        except Exception:  # noqa: BLE001
            digest = "unhashed"
        append_audit(
            config.resolved_audit_path(),
            {
                "at": now_iso(),
                "screenshot_sha256": digest,
                "task_context": _scrub_url(task_context),
                "page_url": _scrub_url(page_url),
                "history_len": len(step_history),
                "models": [m.model for m in config.models],
                "guardrail_triggered": money_hit,
                "decision": _redacted_decision_dump(decision),
            },
        )
        return decision

    # ---- step 2: truncate history ------------------------------------------
    history = list(step_history[-HISTORY_MAX:])

    # ---- step 3: persist screenshot ----------------------------------------
    digest, _ = save_screenshot(screenshot, config.resolved_screenshot_dir())

    # ---- step 4: model router (one-shot, or optional fallback chain) --------
    decision = await route(
        screenshot=screenshot,
        task_context=task_context,
        page_url=page_url,
        step_history=history,
        config=config,
    )

    # ---- step 5: audit -----------------------------------------------------
    append_audit(
        config.resolved_audit_path(),
        {
            "at": now_iso(),
            "screenshot_sha256": digest,
            "task_context": _scrub_url(task_context),
            "page_url": _scrub_url(page_url),
            "history_len": len(history),
            "models": [m.model for m in config.models],
            "guardrail_triggered": None,
            "decision": _redacted_decision_dump(decision),
        },
    )
    return decision
