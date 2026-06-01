"""Tier escalation logic.

Design note:

We evaluated LiteLLM's built-in Router with fallback chains. LiteLLM gives us
two genuinely useful things:
  1. ONE async client (`litellm.acompletion`) that speaks to Anthropic, OpenAI,
     and any OpenAI-compatible local endpoint (qwen36 on llama.cpp / vLLM /
     Ollama). No three separate SDK wrappers needed.
  2. Built-in cost tracking via `litellm.completion_cost(response)`.
  3. Image/vision input across all three providers in the OpenAI-compatible
     `image_url` content-part shape.

What LiteLLM's Router does NOT do natively: fall back based on RESPONSE
CONTENT. Its `fallbacks=[...]` config triggers on exceptions/HTTP errors. Our
trigger is "the model returned a parseable JSON but its `confidence` field is
below the threshold" — that's a content predicate, not an error. We'd end up
wrapping every call with our own confidence check anyway and pretending it's
an exception so the router catches it. That extra layer adds debug surface for
no gain.

Decision: use LiteLLM as the unified client, write the ~50-line escalation
loop here. This trade-off can be revisited if LiteLLM's router grows
content-predicate hooks.
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Optional

import litellm

from vision.models import ActionDecision, StepHistoryItem, TierName, VisionConfig
from vision.prompts import FEW_SHOT_EXAMPLES, SYSTEM_PROMPT, build_user_prompt


# Tier order — entry tier may be any of these; we escalate to later entries.
TIER_ORDER: list[TierName] = ["local", "cheap", "frontier"]

# Tiers whose calls ship the full screenshot to a cloud provider. Gated behind
# config.allow_cloud_escalation.
_CLOUD_TIERS: frozenset[TierName] = frozenset({"cheap", "frontier"})


# CANONICAL MONEY TOKENS — keep verbatim in sync with VisionConfig.high_stakes_actions
# (models.py), decide.py, prompts.py,
# background.js, bridge.py.
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


def _contains_money_token(*values: Optional[str]) -> bool:
    """True if any canonical money token appears (case-insensitive substring)
    in any of the supplied strings."""
    for value in values:
        if not value:
            continue
        low = value.lower()
        if any(token in low for token in MONEY_TOKENS):
            return True
    return False


# Suppress LiteLLM's default chatter — it pollutes test output otherwise.
litellm.suppress_debug_info = True


class VisionRouterError(RuntimeError):
    """Raised when every tier fails to return a parseable response."""


def _model_for_tier(tier: TierName, config: VisionConfig) -> str:
    if tier == "local":
        # LiteLLM's "openai/<model>" prefix routes to any OpenAI-compatible
        # endpoint; `api_base` directs it to the local qwen36 server.
        return f"openai/{config.qwen_model}"
    if tier == "cheap":
        return f"anthropic/{config.cheap_model}"
    if tier == "frontier":
        return f"anthropic/{config.frontier_model}"
    raise ValueError(f"unknown tier: {tier}")


def _completion_kwargs(tier: TierName, config: VisionConfig) -> dict[str, Any]:
    """Per-tier extras for litellm.acompletion."""
    if tier == "local":
        return {
            "api_base": config.qwen_url,
            # Local server doesn't need a real key; LiteLLM still requires one.
            "api_key": "local-not-used",
        }
    return {}


def _build_messages(
    screenshot_b64: str,
    task_context: str,
    page_url: str,
    step_history: list[StepHistoryItem],
) -> list[dict[str, Any]]:
    """OpenAI-style chat messages with system + few-shot + screenshot.

    LiteLLM normalizes this shape to Anthropic's content blocks under the hood,
    so the same payload works for qwen36, Haiku, and Opus.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT}
    ]
    for example in FEW_SHOT_EXAMPLES:
        messages.append({"role": "user", "content": example["user"]})
        messages.append({"role": "assistant", "content": example["assistant"]})

    user_text = build_user_prompt(task_context, page_url, step_history)
    messages.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{screenshot_b64}"
                    },
                },
            ],
        }
    )
    return messages


# Match a JSON object even if the model wrapped it in ```json fences. We try
# strict parse first; this regex is the fallback.
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(text: str) -> Optional[dict[str, Any]]:
    text = text.strip()
    # Try strict first — well-behaved models return raw JSON.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: scan for the first {...} blob.
    m = _JSON_BLOCK.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


_PASSWORD_LIKE = re.compile(
    r"\b(?:password|passwd|pwd)\s*[:=]\s*\S+", re.IGNORECASE
)

# Broader catch: a secret-bearing label followed by its value even WITHOUT an
# `=`/`:` separator, e.g. "password supersecret123" or "otp 481922". The label
# is kept; the following token is redacted.
_SECRET_LABEL_VALUE = re.compile(
    r"\b(password|passcode|pin|otp|cvv)\b\s*[:=]?\s*(\S+)",
    re.IGNORECASE,
)


def _mask_passwords(payload: dict[str, Any]) -> dict[str, Any]:
    """Final-line-of-defense scrub.

    The prompt tells the model not to read passwords, but a buggy or compromised
    model could still leak one. We sub out anything that looks like a
    password=... pattern (or an unseparated `password <value>` / `otp <value>`
    pattern) in the textual fields. We do NOT clear `text` for type-actions here
    because that's how legitimate non-password typing works; the agent layer is
    responsible for never asking the vision tier to type into password fields in
    the first place, and decide.py redacts typed text before the audit log.
    """
    for key in ("reasoning", "selector_hint"):
        value = payload.get(key)
        if isinstance(value, str):
            value = _PASSWORD_LIKE.sub("password=[REDACTED]", value)
            value = _SECRET_LABEL_VALUE.sub(r"\1 [REDACTED]", value)
            payload[key] = value
    return payload


def finalize_decision(decision: ActionDecision) -> ActionDecision:
    """Single funnel every returned decision passes through.

    Applies the password/secret masker to the decision's textual fields so the
    guardrail / needs_review / abort paths get the same scrub the happy path
    does. Returns a new ActionDecision; never mutates the input.
    """
    masked = _mask_passwords(
        {
            "reasoning": decision.reasoning,
            "selector_hint": decision.selector_hint,
        }
    )
    return decision.model_copy(
        update={
            "reasoning": masked["reasoning"],
            "selector_hint": masked["selector_hint"],
        }
    )


def _to_decision(
    parsed: dict[str, Any],
    *,
    model_used: str,
    tier_used: TierName,
    cost_usd: Optional[float],
    duration_ms: int,
    escalations: list[dict],
) -> ActionDecision:
    parsed = _mask_passwords(dict(parsed))
    # Coerce list -> tuple for coordinates so pydantic accepts it.
    coords = parsed.get("coordinates")
    if isinstance(coords, list) and len(coords) == 2:
        parsed["coordinates"] = (int(coords[0]), int(coords[1]))

    action = parsed.get("action", "abort")
    selector_hint = parsed.get("selector_hint")
    text = parsed.get("text")
    reasoning = parsed.get("reasoning", "")
    confidence = float(parsed.get("confidence", 0.0))

    # Deterministic money guard: a click/type whose selector_hint, text, or
    # reasoning carries any canonical money token is forced to a human-required
    # stop, regardless of task-context substring matching upstream. This blocks a
    # model that recommends a money click via the hint/text even when the task
    # itself looks innocuous.
    if action in ("click", "type") and _contains_money_token(
        selector_hint, text, reasoning
    ):
        action = "done"
        coords = parsed["coordinates"] = None
        selector_hint = None
        text = None
        reasoning = "money_action_requires_human"
        confidence = 1.0

    decision = ActionDecision(
        action=action,
        coordinates=parsed.get("coordinates"),
        selector_hint=selector_hint,
        text=text,
        confidence=confidence,
        reasoning=reasoning,
        model_used=model_used,
        tier_used=tier_used,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        escalations=escalations,
    )
    return finalize_decision(decision)


async def _call_tier(
    tier: TierName,
    screenshot_b64: str,
    task_context: str,
    page_url: str,
    step_history: list[StepHistoryItem],
    config: VisionConfig,
) -> tuple[Optional[dict[str, Any]], str, Optional[float], int, Optional[str]]:
    """Single-tier call. Returns (parsed_json, model_id, cost_usd, duration_ms, error)."""
    model = _model_for_tier(tier, config)
    kwargs = _completion_kwargs(tier, config)
    messages = _build_messages(screenshot_b64, task_context, page_url, step_history)

    start = time.monotonic()
    try:
        response = await litellm.acompletion(
            model=model,
            messages=messages,
            temperature=0.0,
            max_tokens=512,
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — escalation needs to catch anything
        duration_ms = int((time.monotonic() - start) * 1000)
        return None, model, None, duration_ms, f"{type(exc).__name__}: {exc}"
    duration_ms = int((time.monotonic() - start) * 1000)

    # Best-effort cost; litellm returns 0.0 / None for some local providers.
    cost: Optional[float]
    try:
        cost = float(litellm.completion_cost(completion_response=response) or 0.0)
    except Exception:  # noqa: BLE001
        cost = None

    content = response.choices[0].message.content if response.choices else ""
    parsed = _parse_json(content or "")
    if parsed is None:
        return None, model, cost, duration_ms, "unparseable_json"
    return parsed, model, cost, duration_ms, None


def screenshot_to_b64(screenshot: bytes) -> str:
    return base64.b64encode(screenshot).decode("ascii")


async def route(
    screenshot: bytes,
    task_context: str,
    page_url: str,
    step_history: list[StepHistoryItem],
    entry_tier: TierName,
    config: VisionConfig,
) -> ActionDecision:
    """Try entry_tier; escalate while confidence < threshold or call fails.

    If frontier still under threshold, returns abort with reasoning="needs_review".
    """
    screenshot_b64 = screenshot_to_b64(screenshot)
    escalations: list[dict] = []

    # Slice the tier order so we never go backwards.
    try:
        start_idx = TIER_ORDER.index(entry_tier)
    except ValueError:
        start_idx = 0
    tiers_to_try = TIER_ORDER[start_idx:]

    last_parsed: Optional[dict[str, Any]] = None
    last_tier: TierName = tiers_to_try[-1]
    last_model = ""
    last_cost: Optional[float] = None
    last_duration = 0

    for tier in tiers_to_try:
        # Cloud redaction gate: a cheap/frontier call ships the full screenshot
        # to Anthropic. If escalation to a cloud tier is disabled, stop here and
        # return a human-review abort instead of making the call. Local tier is
        # always allowed.
        if tier in _CLOUD_TIERS and not config.allow_cloud_escalation:
            escalations.append(
                {
                    "tier": tier,
                    "model": _model_for_tier(tier, config),
                    "reason": "cloud_escalation_disabled",
                }
            )
            return finalize_decision(
                ActionDecision(
                    action="abort",
                    coordinates=None,
                    selector_hint=None,
                    text=None,
                    confidence=0.0,
                    reasoning="needs_review_cloud_disabled",
                    model_used=last_model or "router",
                    tier_used=last_tier if last_parsed is not None else tier,
                    cost_usd=last_cost,
                    duration_ms=last_duration,
                    escalations=escalations,
                )
            )

        parsed, model_id, cost, duration_ms, error = await _call_tier(
            tier,
            screenshot_b64,
            task_context,
            page_url,
            step_history,
            config,
        )
        if error or parsed is None:
            escalations.append(
                {
                    "tier": tier,
                    "model": model_id,
                    "duration_ms": duration_ms,
                    "cost_usd": cost,
                    "error": error or "no_parsed_output",
                }
            )
            continue

        confidence = float(parsed.get("confidence", 0.0))
        last_parsed = parsed
        last_tier = tier
        last_model = model_id
        last_cost = cost
        last_duration = duration_ms

        if confidence >= config.confidence_threshold:
            return _to_decision(
                parsed,
                model_used=model_id,
                tier_used=tier,
                cost_usd=cost,
                duration_ms=duration_ms,
                escalations=escalations,
            )

        # Below threshold — note the attempt and try the next tier.
        escalations.append(
            {
                "tier": tier,
                "model": model_id,
                "confidence": confidence,
                "duration_ms": duration_ms,
                "cost_usd": cost,
                "reason": "below_threshold",
            }
        )

    # Exhausted all tiers.
    if last_parsed is not None:
        # Frontier returned something parseable but still under threshold.
        return finalize_decision(
            ActionDecision(
                action="abort",
                coordinates=None,
                selector_hint=None,
                text=None,
                confidence=float(last_parsed.get("confidence", 0.0)),
                reasoning="needs_review",
                model_used=last_model,
                tier_used=last_tier,
                cost_usd=last_cost,
                duration_ms=last_duration,
                escalations=escalations,
            )
        )
    # No tier returned parseable output at all.
    raise VisionRouterError(f"all tiers failed: {escalations}")
