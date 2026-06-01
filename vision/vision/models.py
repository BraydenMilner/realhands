"""Pydantic models for the Vision Decision Service public API."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# Tier names — three rungs only. Keep this in sync with router.TIER_ORDER.
TierName = Literal["local", "cheap", "frontier"]

ActionType = Literal["click", "type", "navigate", "wait", "done", "abort"]


class ActionDecision(BaseModel):
    """The vision tier's single answer for what the executor should do next.

    Contract notes:
    - `coordinates` is (x, y) in screenshot pixels; only set for `click` / `type`.
    - `selector_hint` is a free-form description the executor can use to confirm
      the click target (e.g. "Sign in button, bottom-center of login form"). Not
      a CSS selector — extensions know nothing about specific sites.
    - `text` is set for `type` (what to type) and `navigate` (target URL).
    - `escalations` records prior tier attempts so the audit log can replay the
      whole decision chain.
    """

    model_config = ConfigDict(extra="forbid")

    action: ActionType
    coordinates: Optional[tuple[int, int]] = None
    selector_hint: Optional[str] = None
    text: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    model_used: str
    tier_used: TierName
    cost_usd: Optional[float] = None
    duration_ms: int
    escalations: list[dict] = Field(default_factory=list)


class StepHistoryItem(BaseModel):
    """One prior step the executor took on this task.

    Pass a list of these so the vision tier sees recent context. `decide_action`
    truncates to the last 5 before prompting the model — keeps token budget
    bounded and stops the model from anchoring on stale failures from 30 steps
    ago.
    """

    model_config = ConfigDict(extra="forbid")

    action: str
    target: Optional[str] = None
    outcome: str
    at: str


def _default_audit_path() -> str:
    """~/.local/share/realhands-vision/audit.jsonl — XDG-ish, no root needed."""
    return str(Path.home() / ".local" / "share" / "realhands-vision" / "audit.jsonl")


def _default_screenshot_dir() -> str:
    return str(Path.home() / ".local" / "share" / "realhands-vision" / "screenshots")


class VisionConfig(BaseModel):
    """Knobs for the vision service. Default values are safe local-first."""

    model_config = ConfigDict(extra="forbid")

    # Local tier — OpenAI-compatible chat completions endpoint.
    # NOTE: `local_model` is the exact string your local server expects.
    # llama.cpp / vLLM / Ollama installs name models differently, so this is
    # intentionally configurable.
    qwen_url: str = "http://localhost:9001/v1"
    qwen_model: str = "qwen2.5-vl-7b-instruct"

    # Cloud tiers — exact model IDs.
    cheap_model: str = "claude-haiku-4-5-20251001"
    frontier_model: str = "claude-opus-4-7"

    # Escalation rule: confidence < threshold -> bump to next tier.
    confidence_threshold: float = 0.7

    # Audit / screenshot persistence. None -> use XDG-ish defaults above.
    audit_path: Optional[str] = None
    screenshot_dir: Optional[str] = None

    # Money-action guardrail — if task_context or page_url contains any of these
    # tokens (case-insensitive), short-circuit to action=done. The runtime NEVER
    # clicks money-moving controls (redeem/deposit/withdraw/etc.); a human does.
    # CANONICAL MONEY TOKENS — keep this list in sync with the copies in
    # decide.py, prompts.py, and the extension's background.js.
    high_stakes_actions: set[str] = Field(
        default_factory=lambda: {
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

    # When False (default), a tier escalation that would ship a full screenshot
    # to a cloud provider (cheap/frontier) is blocked: the router returns
    # action="abort", reasoning="needs_review_cloud_disabled" instead of calling
    # out. Local-tier behavior is unaffected.
    allow_cloud_escalation: bool = False

    def resolved_audit_path(self) -> str:
        return self.audit_path or _default_audit_path()

    def resolved_screenshot_dir(self) -> str:
        return self.screenshot_dir or _default_screenshot_dir()
