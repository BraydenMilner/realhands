"""Tests for the Vision Decision Service.

We mock litellm.acompletion at the boundary so no real LLM calls happen and the
suite runs offline. Each tier's response is faked by inspecting the model
string the router asks for.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Make the `vision` package importable without installing — tests run from
# vision-service/ root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vision import (  # noqa: E402
    ActionDecision,
    StepHistoryItem,
    VisionConfig,
    decide_action,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _png_bytes() -> bytes:
    """Tiny but valid PNG; we don't care about pixels here since LLM is mocked."""
    fixture = Path(__file__).parent / "fixtures" / "login_page.png"
    if fixture.exists():
        return fixture.read_bytes()
    # Smallest legal PNG: 1x1 transparent. Generated once and inlined.
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c6300010000000500010d0a2db40000000049454e44"
        "ae426082"
    )


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


def _fake_response(payload: dict[str, Any]) -> _FakeResponse:
    return _FakeResponse(json.dumps(payload))


def _make_config(tmp_path: Path) -> VisionConfig:
    return VisionConfig(
        audit_path=str(tmp_path / "audit.jsonl"),
        screenshot_dir=str(tmp_path / "screens"),
    )


def _make_cloud_config(tmp_path: Path) -> VisionConfig:
    """Config that permits cloud-tier escalation (cheap/frontier calls).

    Cloud escalation is OFF by default (full screenshots would ship to
    Anthropic). Tests that exercise the cheap/frontier tiers must opt in.
    """
    return VisionConfig(
        audit_path=str(tmp_path / "audit.jsonl"),
        screenshot_dir=str(tmp_path / "screens"),
        allow_cloud_escalation=True,
    )


# A "high confidence" payload the router will accept.
_HAPPY_PAYLOAD = {
    "action": "type",
    "coordinates": [402, 280],
    "selector_hint": "Email field",
    "text": "user@example.com",
    "confidence": 0.92,
    "reasoning": "Email field is visible and labeled.",
}

# A "low confidence" payload that should trigger escalation.
_LOW_PAYLOAD = {
    "action": "click",
    "coordinates": [100, 100],
    "selector_hint": "something",
    "text": None,
    "confidence": 0.35,
    "reasoning": "I cannot tell what this is.",
}


# ---------------------------------------------------------------------------
# 1. Happy path — local returns high confidence, no escalation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_high_confidence_no_escalation(tmp_path):
    config = _make_config(tmp_path)

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
    assert decision.tier_used == "local"
    assert decision.confidence >= 0.7
    assert decision.escalations == []
    # Only the local tier should have been called.
    assert len(calls) == 1
    assert "qwen" in calls[0].lower() or calls[0].startswith("openai/")

    # Audit log should have exactly one row.
    audit_text = (tmp_path / "audit.jsonl").read_text()
    rows = [json.loads(line) for line in audit_text.splitlines()]
    assert len(rows) == 1
    assert rows[0]["decision"]["action"] == "type"
    assert rows[0]["guardrail_triggered"] is None


# ---------------------------------------------------------------------------
# 2. Escalation chain — local low -> Haiku low -> Opus high.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_local_to_cheap_to_frontier(tmp_path):
    config = _make_cloud_config(tmp_path)
    calls: list[str] = []

    async def fake_acompletion(model: str, **kwargs):
        calls.append(model)
        if "qwen" in model or "openai/" in model:
            return _fake_response(_LOW_PAYLOAD)
        if "haiku" in model:
            return _fake_response({**_LOW_PAYLOAD, "confidence": 0.55})
        if "opus" in model:
            return _fake_response(_HAPPY_PAYLOAD)
        raise AssertionError(f"unexpected model: {model}")

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
    assert decision.tier_used == "frontier"
    assert len(decision.escalations) == 2
    assert decision.escalations[0]["tier"] == "local"
    assert decision.escalations[1]["tier"] == "cheap"
    # All three tiers got called once.
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# 3. Final abort — every tier under threshold.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_tiers_low_confidence_aborts(tmp_path):
    config = _make_cloud_config(tmp_path)

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
    config = _make_config(tmp_path)
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
    # Zero LLM calls.
    assert calls == []
    # Audit row still written with guardrail_triggered set.
    rows = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["guardrail_triggered"] == "redeem"


@pytest.mark.asyncio
async def test_money_action_guardrail_matches_each_token(tmp_path):
    """Sanity-check every canonical high-stakes token triggers the guardrail."""
    canonical = [
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
    ]
    for token in canonical:
        config = _make_config(tmp_path / token.replace(" ", "_"))
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
    deterministically by the router guard, even when the task looks innocuous."""
    config = _make_config(tmp_path)
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

    # Router guard forces the human-required stop.
    assert decision.action == "done"
    assert decision.reasoning == "money_action_requires_human"
    assert decision.confidence == 1.0
    assert decision.coordinates is None
    assert decision.selector_hint is None
    # The model WAS called (this is a response-content guard, not a task guard).
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cloud_escalation_disabled_aborts_instead_of_calling(tmp_path):
    """With allow_cloud_escalation=False (default), an escalation that would
    ship the screenshot to a cloud tier returns an abort instead of calling out."""
    config = _make_config(tmp_path)  # allow_cloud_escalation defaults to False
    calls: list[str] = []

    async def fake_acompletion(model: str, **kwargs):
        calls.append(model)
        # Local returns low confidence -> would normally escalate to cheap.
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
    assert decision.reasoning == "needs_review_cloud_disabled"
    assert decision.confidence == 0.0
    # Only the local tier was ever called — no cloud call happened.
    assert len(calls) == 1
    assert "qwen" in calls[0].lower() or calls[0].startswith("openai/")


# ---------------------------------------------------------------------------
# 5. Step history truncation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_truncated_to_last_five(tmp_path):
    config = _make_config(tmp_path)
    captured_messages: list = []

    async def fake_acompletion(model: str, messages, **kwargs):
        captured_messages.append(messages)
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

    # Last user message holds the rendered history. We sent 10; only the last
    # 5 should appear.
    user_text = ""
    for part in captured_messages[0][-1]["content"]:
        if isinstance(part, dict) and part.get("type") == "text":
            user_text = part["text"]
            break
    assert "click_5" in user_text
    assert "click_9" in user_text
    assert "click_0" not in user_text  # truncated
    assert "click_4" not in user_text  # truncated


# ---------------------------------------------------------------------------
# 6. Screenshot dedupe — same bytes -> one file on disk.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screenshot_dedup(tmp_path):
    config = _make_config(tmp_path)

    async def fake_acompletion(model: str, **kwargs):
        return _fake_response(_HAPPY_PAYLOAD)

    png = _png_bytes()
    with patch("vision.router.litellm.acompletion", side_effect=fake_acompletion), \
         patch("vision.router.litellm.completion_cost", return_value=0.0):
        for _ in range(3):
            await decide_action(
                screenshot=png,
                task_context="log in",
                step_history=[],
                page_url="https://example.com/login",
                config=config,
            )

    # Three audit rows.
    rows = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(rows) == 3
    # All point at the same screenshot hash.
    hashes = {json.loads(r)["screenshot_sha256"] for r in rows}
    assert len(hashes) == 1
    # And only one file on disk.
    files = list((tmp_path / "screens").iterdir())
    assert len(files) == 1


# ---------------------------------------------------------------------------
# 7. Password-leak masking.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_password_masking_in_response(tmp_path):
    """Model misbehaves and echoes a password in reasoning — we must scrub it."""
    config = _make_config(tmp_path)

    bad_payload = {
        **_HAPPY_PAYLOAD,
        "reasoning": "Entered email and saw password=supersecret123 in field.",
    }

    async def fake_acompletion(model: str, **kwargs):
        return _fake_response(bad_payload)

    with patch("vision.router.litellm.acompletion", side_effect=fake_acompletion), \
         patch("vision.router.litellm.completion_cost", return_value=0.0):
        decision = await decide_action(
            screenshot=_png_bytes(),
            task_context="log in",
            step_history=[],
            page_url="https://example.com/login",
            config=config,
        )

    assert "supersecret123" not in decision.reasoning
    assert "[REDACTED]" in decision.reasoning


# ---------------------------------------------------------------------------
# 8. Audit row schema sanity.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_row_has_required_fields(tmp_path):
    config = _make_config(tmp_path)

    async def fake_acompletion(model: str, **kwargs):
        return _fake_response(_HAPPY_PAYLOAD)

    with patch("vision.router.litellm.acompletion", side_effect=fake_acompletion), \
         patch("vision.router.litellm.completion_cost", return_value=0.0):
        await decide_action(
            screenshot=_png_bytes(),
            task_context="log in",
            step_history=[],
            page_url="https://example.com/login",
            config=config,
        )

    row = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    for key in (
        "at",
        "screenshot_sha256",
        "task_context",
        "page_url",
        "history_len",
        "entry_tier",
        "guardrail_triggered",
        "decision",
    ):
        assert key in row
    # Decision fields preserve tier/model/duration/cost.
    for key in ("action", "tier_used", "model_used", "duration_ms"):
        assert key in row["decision"]


# ---------------------------------------------------------------------------
# 9. Entry tier honored — pass "cheap", never touch local.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entry_tier_cheap_skips_local(tmp_path):
    config = _make_cloud_config(tmp_path)
    calls: list[str] = []

    async def fake_acompletion(model: str, **kwargs):
        calls.append(model)
        return _fake_response(_HAPPY_PAYLOAD)

    with patch("vision.router.litellm.acompletion", side_effect=fake_acompletion), \
         patch("vision.router.litellm.completion_cost", return_value=0.0):
        decision = await decide_action(
            screenshot=_png_bytes(),
            task_context="log in",
            step_history=[],
            page_url="https://example.com/login",
            model_tier="cheap",
            config=config,
        )

    assert decision.tier_used == "cheap"
    # Exactly one call, and it went to haiku, never qwen.
    assert len(calls) == 1
    assert "haiku" in calls[0]


# ---------------------------------------------------------------------------
# 10. Resilience: first tier raises, router escalates rather than crashing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_exception_escalates(tmp_path):
    config = _make_cloud_config(tmp_path)
    calls: list[str] = []

    async def fake_acompletion(model: str, **kwargs):
        calls.append(model)
        if "qwen" in model or "openai/" in model:
            raise ConnectionError("local server is down")
        return _fake_response(_HAPPY_PAYLOAD)

    with patch("vision.router.litellm.acompletion", side_effect=fake_acompletion), \
         patch("vision.router.litellm.completion_cost", return_value=0.0):
        decision = await decide_action(
            screenshot=_png_bytes(),
            task_context="log in",
            step_history=[],
            page_url="https://example.com/login",
            config=config,
        )

    assert decision.tier_used == "cheap"
    assert len(decision.escalations) == 1
    assert "ConnectionError" in decision.escalations[0]["error"]
