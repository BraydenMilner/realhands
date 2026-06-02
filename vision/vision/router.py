"""Model routing for the vision tier — bring your own model(s).

`config.models` is tried in order. ONE entry = one-shot (any LiteLLM-supported
provider + your key). More entries = an optional cheap→fallback chain: the next
model is only called if the previous returns confidence below threshold.

LiteLLM is the universal client: one async call (`litellm.acompletion`) that
speaks Gemini / OpenRouter / OpenAI / Anthropic / Vertex and any OpenAI-compatible
local server (vLLM / llama.cpp / Ollama). We request `stream=True` and reassemble
the SSE chunks with `litellm.stream_chunk_builder`, so forced-streaming backends
and *thinking* models work: a reasoning model's `reasoning_content` is kept
separate from its `content`, and native tool-call answers are picked up too.
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Optional

import litellm

from vision.models import (
    ActionDecision,
    ModelConfig,
    StepHistoryItem,
    VisionConfig,
)
from vision.prompts import FEW_SHOT_EXAMPLES, SYSTEM_PROMPT, build_user_prompt


# CANONICAL MONEY TOKENS — keep verbatim in sync with VisionConfig.high_stakes_actions
# (models.py), decide.py, prompts.py, and the extension's background.js.
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
    """Raised when every configured model fails to return a parseable response."""


def _build_messages(
    screenshot_b64: str,
    task_context: str,
    page_url: str,
    step_history: list[StepHistoryItem],
) -> list[dict[str, Any]]:
    """OpenAI-style chat messages with system + few-shot + screenshot.

    LiteLLM normalizes this to each provider's content-block shape, so the same
    payload works for a local vision model, Gemini, Anthropic, etc.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
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
                    "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
                },
            ],
        }
    )
    return messages


# Match a JSON object even if the model wrapped it in ```json fences.
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(text: str) -> Optional[dict[str, Any]]:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


_PASSWORD_LIKE = re.compile(r"\b(?:password|passwd|pwd)\s*[:=]\s*\S+", re.IGNORECASE)
_SECRET_LABEL_VALUE = re.compile(
    r"\b(password|passcode|pin|otp|cvv)\b\s*[:=]?\s*(\S+)", re.IGNORECASE
)


def _mask_passwords(payload: dict[str, Any]) -> dict[str, Any]:
    """Final-line-of-defense scrub of password-like patterns in textual fields."""
    for key in ("reasoning", "selector_hint"):
        value = payload.get(key)
        if isinstance(value, str):
            value = _PASSWORD_LIKE.sub("password=[REDACTED]", value)
            value = _SECRET_LABEL_VALUE.sub(r"\1 [REDACTED]", value)
            payload[key] = value
    return payload


def finalize_decision(decision: ActionDecision) -> ActionDecision:
    """Single funnel every returned decision passes through (password scrub)."""
    masked = _mask_passwords(
        {"reasoning": decision.reasoning, "selector_hint": decision.selector_hint}
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
    model_index: int,
    cost_usd: Optional[float],
    duration_ms: int,
    escalations: list[dict],
) -> ActionDecision:
    parsed = _mask_passwords(dict(parsed))
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
    # stop, regardless of upstream task-context matching.
    if action in ("click", "type") and _contains_money_token(
        selector_hint, text, reasoning
    ):
        action = "done"
        parsed["coordinates"] = None
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
        model_index=model_index,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        escalations=escalations,
    )
    return finalize_decision(decision)


async def _call_model(
    model_cfg: ModelConfig, messages: list[dict[str, Any]]
) -> tuple[Optional[dict[str, Any]], str, Optional[float], int, Optional[str]]:
    """One model call. Returns (parsed_json, model_id, cost_usd, duration_ms, error).

    Streams and reassembles so reasoning models and forced-streaming backends
    work; the model's answer (`content`, or a native tool-call's arguments) is
    parsed as the action JSON, with `reasoning_content` ignored.
    """
    kwargs: dict[str, Any] = {}
    if model_cfg.api_key:
        kwargs["api_key"] = model_cfg.api_key
    if model_cfg.base_url:
        kwargs["api_base"] = model_cfg.base_url

    start = time.monotonic()
    try:
        resp = await litellm.acompletion(
            model=model_cfg.model,
            messages=messages,
            temperature=0.0,
            max_tokens=512,
            stream=True,
            **kwargs,
        )
        # Real backends return an async stream; reassemble into one response.
        # (Test mocks return a complete response object — not async-iterable.)
        if hasattr(resp, "__aiter__"):
            chunks = [chunk async for chunk in resp]
            resp = litellm.stream_chunk_builder(chunks, messages=messages)
    except Exception as exc:  # noqa: BLE001 — fallback needs to catch anything
        duration_ms = int((time.monotonic() - start) * 1000)
        return None, model_cfg.model, None, duration_ms, f"{type(exc).__name__}: {exc}"
    duration_ms = int((time.monotonic() - start) * 1000)

    try:
        cost: Optional[float] = float(
            litellm.completion_cost(completion_response=resp) or 0.0
        )
    except Exception:  # noqa: BLE001
        cost = None

    msg = resp.choices[0].message if getattr(resp, "choices", None) else None
    content = (getattr(msg, "content", None) or "") if msg is not None else ""
    if not content.strip() and msg is not None:
        # Model answered via native function/tool calling instead of text content.
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            try:
                content = tool_calls[0].function.arguments or ""
            except Exception:  # noqa: BLE001
                content = ""

    parsed = _parse_json(content)
    if parsed is None:
        return None, model_cfg.model, cost, duration_ms, "unparseable_json"
    return parsed, model_cfg.model, cost, duration_ms, None


def screenshot_to_b64(screenshot: bytes) -> str:
    return base64.b64encode(screenshot).decode("ascii")


async def route(
    screenshot: bytes,
    task_context: str,
    page_url: str,
    step_history: list[StepHistoryItem],
    config: VisionConfig,
) -> ActionDecision:
    """Try each configured model in order; return the first whose confidence
    meets the threshold. If none do, return an abort with reasoning="needs_review".
    """
    if not config.models:
        raise VisionRouterError("no models configured")

    screenshot_b64 = screenshot_to_b64(screenshot)
    messages = _build_messages(screenshot_b64, task_context, page_url, step_history)
    escalations: list[dict] = []
    last: Optional[tuple[dict[str, Any], int, str, Optional[float], int]] = None

    for idx, model_cfg in enumerate(config.models):
        parsed, model_id, cost, duration_ms, error = await _call_model(
            model_cfg, messages
        )
        if error or parsed is None:
            escalations.append(
                {
                    "model": model_id,
                    "index": idx,
                    "duration_ms": duration_ms,
                    "cost_usd": cost,
                    "error": error or "no_parsed_output",
                }
            )
            continue

        confidence = float(parsed.get("confidence", 0.0))
        last = (parsed, idx, model_id, cost, duration_ms)

        if confidence >= config.confidence_threshold:
            return _to_decision(
                parsed,
                model_used=model_id,
                model_index=idx,
                cost_usd=cost,
                duration_ms=duration_ms,
                escalations=escalations,
            )

        escalations.append(
            {
                "model": model_id,
                "index": idx,
                "confidence": confidence,
                "duration_ms": duration_ms,
                "cost_usd": cost,
                "reason": "below_threshold",
            }
        )

    if last is not None:
        parsed, idx, model_id, cost, duration_ms = last
        return finalize_decision(
            ActionDecision(
                action="abort",
                coordinates=None,
                selector_hint=None,
                text=None,
                confidence=float(parsed.get("confidence", 0.0)),
                reasoning="needs_review",
                model_used=model_id,
                model_index=idx,
                cost_usd=cost,
                duration_ms=duration_ms,
                escalations=escalations,
            )
        )

    raise VisionRouterError(f"all models failed: {escalations}")
