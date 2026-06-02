"""Pydantic models for the Vision Decision Service public API."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


ActionType = Literal[
    "click", "type", "navigate", "scroll", "wait", "ask_user", "zoom", "done", "abort"
]


class ActionDecision(BaseModel):
    """The vision tier's single answer for what the executor should do next.

    Contract notes:
    - `coordinates` is (x, y) in screenshot pixels. Set for `click` / `type`
      (the target point) and for `scroll` (the [dx, dy] delta to scroll, where
      positive dy scrolls DOWN). For `zoom`, `coordinates` is the [cx, cy]
      point (in the shown image) to inspect closer.
    - `selector_hint` is a free-form description the executor can use to confirm
      the target (NOT a CSS selector).
    - `text` is set for `type` (what to type), `navigate` (target URL), and
      `ask_user` (the question to put to the human).
    - `model_index` is which configured model produced this (0 = the first/only).
    - `escalations` records prior model attempts so the audit log can replay the
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
    model_index: int = 0
    cost_usd: Optional[float] = None
    duration_ms: int
    escalations: list[dict] = Field(default_factory=list)


class StepHistoryItem(BaseModel):
    """One prior step the executor took on this task. `decide_action` truncates
    to the last 5 before prompting, keeping the token budget bounded."""

    model_config = ConfigDict(extra="forbid")

    action: str
    target: Optional[str] = None
    outcome: str
    at: str


class ModelConfig(BaseModel):
    """One LLM endpoint — bring your own.

    `model` is any LiteLLM model id, e.g.:
      - "gemini/gemini-2.5-flash"                  (Gemini, key via GEMINI_API_KEY or api_key)
      - "openrouter/google/gemini-2.5-flash"       (OpenRouter, key via OPENROUTER_API_KEY or api_key)
      - "anthropic/claude-opus-4-..."              (Anthropic)
      - "openai/<name>" + base_url                 (any OpenAI-compatible / local server, e.g. vLLM/Ollama)

    LiteLLM routes each provider natively, so "connect a key" = set `model` +
    `api_key`. If `api_key` is omitted, LiteLLM reads the provider's env var.
    """

    model_config = ConfigDict(extra="forbid")

    model: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None


def _default_models() -> list["ModelConfig"]:
    """One-shot default: a local OpenAI-compatible vision server."""
    return [
        ModelConfig(
            model="openai/qwen2.5-vl-7b-instruct",
            base_url="http://localhost:9001/v1",
            api_key="local",
        )
    ]


def _default_audit_path() -> str:
    return str(Path.home() / ".local" / "share" / "realhands-vision" / "audit.jsonl")


def _default_screenshot_dir() -> str:
    return str(Path.home() / ".local" / "share" / "realhands-vision" / "screenshots")


class VisionConfig(BaseModel):
    """Knobs for the vision service. Bring your own model(s).

    `models` are tried in order: ONE entry = one-shot (any model, your key). Add
    more for an optional cheap→fallback chain — the next model is only called if
    the previous returns `confidence` below `confidence_threshold`.
    """

    model_config = ConfigDict(extra="forbid")

    models: list[ModelConfig] = Field(default_factory=_default_models)
    confidence_threshold: float = 0.7

    audit_path: Optional[str] = None
    screenshot_dir: Optional[str] = None

    # Money-action guardrail — if task_context or page_url contains any of these
    # tokens (case-insensitive), short-circuit to action=done. The runtime NEVER
    # clicks money-moving controls; a human does.
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

    def resolved_audit_path(self) -> str:
        return self.audit_path or _default_audit_path()

    def resolved_screenshot_dir(self) -> str:
        return self.screenshot_dir or _default_screenshot_dir()
