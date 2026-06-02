"""Tests for the Vision Decision Service (bring-your-own-model).

We mock litellm.acompletion at the boundary so no real LLM calls happen and the
suite runs offline. Each configured model's response is faked by inspecting the
model string the router asks for. The mock returns a complete (non-streaming)
response object; the router's stream path only engages for real async streams,
so the mock exercises the same parsing without a stream.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vision import (  # noqa: E402
    ActionDecision,
    ModelConfig,
    StepHistoryItem,
    VisionConfig,
    decide_action,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _png_bytes() -> bytes:
    fixture = Path(__file__).parent / "fixtures" / "login_page.png"
    if fixture.exists():
        return fixture.read_bytes()
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c6300010000000500010d0a2db40000000049454e44"
        "ae426082"
    )


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = None


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


def _fake_response(payload: dict[str, Any]) -> _FakeResponse:
    return _FakeResponse(json.dumps(payload))


def _config(tmp_path: Path, *models: str) -> VisionConfig:
    """Build a config with one or more BYO models (default a single 'openai/m1')."""
    ids = models or ("openai/m1",)
    return VisionConfig(
        models=[ModelConfig(model=m, api_key="test") for m in ids],
        audit_path=str(tmp_path / "audit.jsonl"),
        screenshot_dir=str(tmp_path / "screens"),
    )


_HAPPY_PAYLOAD = {
    "action": "type",
    "coordinates": [402, 280],
    "selector_hint": "Email field",
    "text": "user@example.com",
    "confidence": 0.92,
    "reasoning": "Email field is visible and labeled.",
}

_LOW_PAYLOAD = {
    "action": "click",
    "coordinates": [100, 100],
    "selector_hint": "something",
    "text": None,
    "confidence": 0.35,
    "reasoning": "I cannot tell what this is.",
}


# ---------------------------------------------------------------------------
# 1. One-shot: single model, high confidence, no fallback.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_shot_high_confidence(tmp_path):
    config = _config(tmp_path)  # one model
    calls: list[str] = []

    async def fake_acompletion(model: str, **kwargs):
        calls.append(model)
        return _fake_response(_HAPPY_PAYLOAD)

    with patch("vision.router.litellm.acompletion", side_effect=fake_acompletion), \
         patch("vision.router.litellm.completion_cost", return_value=0.0):
        decision = await decide_action(
            screenshot=_png_bytes(),
            task_context="log in as test@example.com",
            step_history=[],
            page_url="https://example.com/login",
            config=config,
        )

    assert decision.action == "type"
    assert decision.model_index == 0
    assert decision.model_used == "openai/m1"
    assert decision.confidence >= 0.7
    assert decision.escalations == []
    assert calls == ["openai/m1"]

    rows = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["decision"]["action"] == "type"
    assert rows[0]["guardrail_triggered"] is None


# ---------------------------------------------------------------------------
# 2. Fallback chain — m1 low -> m2 low -> m3 high.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_chain_uses_third_model(tmp_path):
    config = _config(tmp_path, "openai/m1", "openai/m2", "openai/m3")
    calls: list[str] = []

    async def fake_acompletion(model: str, **kwargs):
        calls.append(model)
        if model == "openai/m1":
            return _fake_response(_LOW_PAYLOAD)
        if model == "openai/m2":
            return _fake_response({**_LOW_PAYLOAD, "confidence": 0.55})
        return _fake_response(_HAPPY_PAYLOAD)

    with patch("vision.router.litellm.acompletion", side_effect=fake_acompletion), \
         patch("vision.router.litellm.completion_cost", return_value=0.001):
        decision = await decide_action(
            screenshot=_png_bytes(),
            task_context="log in as test@example.com",
            step_history=[],
            page_url="https://example.com/login",
            config=config,
        )

    assert decision.action == "type"
    assert decision.model_index == 2
    assert decision.model_used == "openai/m3"
    assert len(decision.escalations) == 2
    assert decision.escalations[0]["model"] == "openai/m1"
    assert decision.escalations[1]["model"] == "openai/m2"
    assert calls == ["openai/m1", "openai/m2", "openai/m3"]


# ---------------------------------------------------------------------------
# 3. Every model under threshold -> abort needs_review.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_models_low_confidence_aborts(tmp_path):
    config = _config(tmp_path, "openai/m1", "openai/m2", "openai/m3")

    async def fake_acompletion(model: str, **kwargs):
        return _fake_response(_LOW_PAYLOAD)

    with patch("vision.router.litellm.acompletion", side_effect=fake_acompletion), \
         patch("vision.router.litellm.completion_cost", return_value=0.0):
        decision = await decide_action(
            screenshot=_png_bytes(),
            task_context="log in",
            step_history=[],
            page_url="https://example.com/login",
            config=config,
        )

    assert decision.action == "abort"
    assert decision.reasoning == "needs_review"
    assert len(decision.escalations) == 3


# ---------------------------------------------------------------------------
# 4. Money-action guardrail — short-circuits BEFORE any LLM call.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_money_action_guardrail_no_llm_calls(tmp_path):
    config = _config(tmp_path)
    calls: list[str] = []

    async def fake_acompletion(model: str, **kwargs):
        calls.append(model)
        return _fake_response(_HAPPY_PAYLOAD)

    with patch("vision.router.litellm.acompletion", side_effect=fake_acompletion):
        decision = await decide_action(
            screenshot=_png_bytes(),
            task_context="redeem $50 from example.com",
            step_history=[],
            page_url="https://example.com/cashier",
            config=config,
        )

    assert decision.action == "done"
    assert decision.reasoning == "money_action_requires_human"
    assert decision.confidence == 1.0
    assert decision.model_used == "guardrail"
    assert calls == []
    rows = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["guardrail_triggered"] == "redeem"


@pytest.mark.asyncio
async def test_money_action_guardrail_matches_each_token(tmp_path):
    canonical = [
        "redeem", "redemption", "deposit", "withdraw", "withdrawal",
        "transfer", "cashout", "cash out", "cashier", "payout",
    ]
    for token in canonical:
        config = _config(tmp_path / token.replace(" ", "_"))
        decision = await decide_action(
            screenshot=_png_bytes(),
            task_context=f"please {token} my balance",
            step_history=[],
            page_url="https://example.com/wallet",
            config=config,
        )
        assert decision.action == "done", f"token {token!r} failed to trigger"
        assert decision.reasoning == "money_action_requires_human"


@pytest.mark.asyncio
async def test_money_guard_fires_on_selector_hint_cashout(tmp_path):
    """A click the model recommends via a money-laden selector_hint is blocked
    deterministically, even when the task looks innocuous."""
    config = _config(tmp_path)
    calls: list[str] = []
    money_click = {
        "action": "click",
        "coordinates": [320, 480],
        "selector_hint": "Cashout button in the wallet panel",
        "text": None,
        "confidence": 0.95,
        "reasoning": "The wallet panel is open and this is the primary CTA.",
    }

    async def fake_acompletion(model: str, **kwargs):
        calls.append(model)
        return _fake_response(money_click)

    with patch("vision.router.litellm.acompletion", side_effect=fake_acompletion), \
         patch("vision.router.litellm.completion_cost", return_value=0.0):
        decision = await decide_action(
            screenshot=_png_bytes(),
            task_context="check my wallet balance",
            step_history=[],
            page_url="https://example.com/wallet",
            config=config,
        )

    assert decision.action == "done"
    assert decision.reasoning == "money_action_requires_human"
    assert decision.confidence == 1.0
    assert decision.coordinates is None
    assert decision.selector_hint is None
    assert len(calls) == 1  # response-content guard fires after the model is called


# ---------------------------------------------------------------------------
# 5. Step history truncation to last 5.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_truncated_to_last_five(tmp_path):
    config = _config(tmp_path)
    captured: list = []

    async def fake_acompletion(model: str, messages, **kwargs):
        captured.append(messages)
        return _fake_response(_HAPPY_PAYLOAD)

    history = [
        StepHistoryItem(action=f"click_{i}", target=f"t{i}", outcome="ok", at="2026-05-26T00:00:00+00:00")
        for i in range(10)
    ]
    with patch("vision.router.litellm.acompletion", side_effect=fake_acompletion), \
         patch("vision.router.litellm.completion_cost", return_value=0.0):
        await decide_action(
            screenshot=_png_bytes(),
            task_context="navigate the form",
            step_history=history,
            page_url="https://example.com/form",
            config=config,
        )

    user_text = ""
    for part in captured[0][-1]["content"]:
        if isinstance(part, dict) and part.get("type") == "text":
            user_text = part["text"]
            break
    assert "click_5" in user_text and "click_9" in user_text
    assert "click_0" not in user_text and "click_4" not in user_text


# ---------------------------------------------------------------------------
# 6. Screenshot dedupe.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screenshot_dedup(tmp_path):
    config = _config(tmp_path)

    async def fake_acompletion(model: str, **kwargs):
        return _fake_response(_HAPPY_PAYLOAD)

    png = _png_bytes()
    with patch("vision.router.litellm.acompletion", side_effect=fake_acompletion), \
         patch("vision.router.litellm.completion_cost", return_value=0.0):
        for _ in range(3):
            await decide_action(
                screenshot=png, task_context="log in", step_history=[],
                page_url="https://example.com/login", config=config,
            )

    rows = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(rows) == 3
    assert len({json.loads(r)["screenshot_sha256"] for r in rows}) == 1
    assert len(list((tmp_path / "screens").iterdir())) == 1


# ---------------------------------------------------------------------------
# 7. Password-leak masking.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_password_masking_in_response(tmp_path):
    config = _config(tmp_path)
    bad = {**_HAPPY_PAYLOAD, "reasoning": "Saw password=supersecret123 in field."}

    async def fake_acompletion(model: str, **kwargs):
        return _fake_response(bad)

    with patch("vision.router.litellm.acompletion", side_effect=fake_acompletion), \
         patch("vision.router.litellm.completion_cost", return_value=0.0):
        decision = await decide_action(
            screenshot=_png_bytes(), task_context="log in", step_history=[],
            page_url="https://example.com/login", config=config,
        )

    assert "supersecret123" not in decision.reasoning
    assert "[REDACTED]" in decision.reasoning


# ---------------------------------------------------------------------------
# 8. Audit row schema.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_row_has_required_fields(tmp_path):
    config = _config(tmp_path)

    async def fake_acompletion(model: str, **kwargs):
        return _fake_response(_HAPPY_PAYLOAD)

    with patch("vision.router.litellm.acompletion", side_effect=fake_acompletion), \
         patch("vision.router.litellm.completion_cost", return_value=0.0):
        await decide_action(
            screenshot=_png_bytes(), task_context="log in", step_history=[],
            page_url="https://example.com/login", config=config,
        )

    row = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    for key in ("at", "screenshot_sha256", "task_context", "page_url",
                "history_len", "models", "guardrail_triggered", "decision"):
        assert key in row
    for key in ("action", "model_index", "model_used", "duration_ms"):
        assert key in row["decision"]


# ---------------------------------------------------------------------------
# 9. Resilience: first model raises, router falls through.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_exception_falls_through(tmp_path):
    config = _config(tmp_path, "openai/m1", "openai/m2")
    calls: list[str] = []

    async def fake_acompletion(model: str, **kwargs):
        calls.append(model)
        if model == "openai/m1":
            raise ConnectionError("local server is down")
        return _fake_response(_HAPPY_PAYLOAD)

    with patch("vision.router.litellm.acompletion", side_effect=fake_acompletion), \
         patch("vision.router.litellm.completion_cost", return_value=0.0):
        decision = await decide_action(
            screenshot=_png_bytes(), task_context="log in", step_history=[],
            page_url="https://example.com/login", config=config,
        )

    assert decision.action == "type"
    assert decision.model_index == 1
    assert len(decision.escalations) == 1
    assert "ConnectionError" in decision.escalations[0]["error"]
    assert calls == ["openai/m1", "openai/m2"]


# ---------------------------------------------------------------------------
# 10. Tool-call answer (native function calling) is parsed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_arguments_parsed(tmp_path):
    """A model that returns the action as native tool-call arguments (no text
    content) is still parsed."""
    config = _config(tmp_path)

    class _ToolFn:
        arguments = json.dumps(_HAPPY_PAYLOAD)

    class _ToolCall:
        function = _ToolFn()

    async def fake_acompletion(model: str, **kwargs):
        resp = _FakeResponse("")  # empty text content
        resp.choices[0].message.tool_calls = [_ToolCall()]
        return resp

    with patch("vision.router.litellm.acompletion", side_effect=fake_acompletion), \
         patch("vision.router.litellm.completion_cost", return_value=0.0):
        decision = await decide_action(
            screenshot=_png_bytes(), task_context="log in", step_history=[],
            page_url="https://example.com/login", config=config,
        )

    assert decision.action == "type"
    assert decision.model_index == 0
