#!/usr/bin/env python3
"""Bring-your-own-key agent loop — the turnkey mode of realhands.

screenshot  ->  decide (vision tier)  ->  act (via the bridge)  ->  repeat

This is the minimal reference for driving a real browser end-to-end with your
own model. It:
  * pulls a screenshot + current URL from the bridge,
  * asks the vision tier (`decide_action`) for the single next action,
  * executes that action back through the bridge,
  * stops on `done` / `abort` / the money-action guardrail.

Prereqs: the extension is loaded, the bridge is running on localhost:7878, and
your model endpoint is reachable. Configure the model via env (see CONFIG below).

    python3 examples/byo_key_agent.py "log in and open my profile"

Cost/usage is printed per step so you can watch your spend live.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import urllib.request

# --- make the sibling `vision/` package importable -------------------------
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "vision"))

from vision import (  # noqa: E402
    ActionDecision,
    ModelConfig,
    StepHistoryItem,
    VisionConfig,
    decide_action,
)

# --- CONFIG (override via env) ---------------------------------------------
BRIDGE = os.environ.get("BRIDGE_URL", "http://localhost:7878") + "/call"
BRIDGE_TOKEN = os.environ.get("REALHANDS_BRIDGE_TOKEN")
BROWSER_ID = os.environ.get("BROWSER_ID")  # None -> the sole / default browser
MAX_STEPS = int(os.environ.get("MAX_STEPS", "25"))

# Bring your own model. `VISION_MODEL` is any LiteLLM id:
#   gemini/gemini-2.5-flash · openrouter/google/gemini-2.5-flash ·
#   anthropic/claude-opus-4-... · openai/<name> (+ VISION_BASE_URL for local)
# `VISION_API_KEY` is your key (or leave it and set the provider's env var).
MODEL = os.environ.get("VISION_MODEL", "openai/qwen2.5-vl-7b-instruct")
CONFIG = VisionConfig(
    models=[
        ModelConfig(
            model=MODEL,
            api_key=os.environ.get("VISION_API_KEY"),
            base_url=os.environ.get("VISION_BASE_URL", "http://localhost:9001/v1"),
        )
    ]
)


def call(method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
    """One bridge /call round-trip. Returns the JSON body."""
    body = {"method": method, "params": params or {}}
    if BROWSER_ID:
        body["browser_id"] = BROWSER_ID
    headers = {"Content-Type": "application/json"}
    if BRIDGE_TOKEN:
        headers["X-RealHands-Token"] = BRIDGE_TOKEN
    req = urllib.request.Request(
        BRIDGE, data=json.dumps(body).encode(), headers=headers
    )
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def screenshot() -> tuple[bytes, str, float]:
    """Grab the current frame. Returns (png_bytes, current_url, device_pixel_ratio)."""
    r = call("screenshot").get("result") or {}
    if "base64" not in r:
        raise RuntimeError(f"bad screenshot response: {r}")
    return base64.b64decode(r["base64"]), r.get("url", ""), r.get("device_pixel_ratio", 1.0)


def act(decision: ActionDecision, dpr: float = 1.0) -> str:
    """Execute a decision through the bridge. Returns an outcome string."""
    a = decision.action
    # click/type/scroll need coordinates; degrade a malformed (weak-model) decision
    # to a recoverable no-op instead of crashing the loop on a None unpack.
    if a in ("click", "type", "scroll") and decision.coordinates is None:
        return f"{a} skipped: no coordinates"
    if a == "navigate":
        call("navigate", {"url": decision.text})
    elif a == "click":
        x, y = decision.coordinates
        call("click_at", {"x": x / dpr, "y": y / dpr})
    elif a == "type":
        x, y = decision.coordinates
        call("type", {"x": x / dpr, "y": y / dpr, "text": decision.text})
    elif a == "scroll":
        x, y = decision.coordinates
        call("scroll", {"x": x / dpr, "y": y / dpr})
    elif a == "ask_user":
        # Human-in-the-loop: surface the agent's question and feed the answer
        # back as this step's outcome so the model can use it next turn.
        question = decision.text or decision.reasoning or "The agent needs your input."
        print(f"\n  \N{BUST IN SILHOUETTE} {question}")
        answer = input("  your answer > ").strip()
        return f"human answered: {answer}" if answer else "no answer given"
    elif a == "wait":
        time.sleep(1.5)
    else:
        return a
    return "ok"


async def run(task: str) -> None:
    history: list[StepHistoryItem] = []
    print(f"task: {task!r}\nbridge: {BRIDGE}  model: {MODEL}\n")

    for step in range(1, MAX_STEPS + 1):
        png, url, dpr = screenshot()
        decision = await decide_action(
            screenshot=png,
            task_context=task,
            step_history=history,
            page_url=url,
            config=CONFIG,
        )
        cost = f"${decision.cost_usd:.4f}" if decision.cost_usd else "$0"
        print(
            f"[{step:02d}] {decision.action:8s} conf={decision.confidence:.2f} "
            f"{decision.model_used} "
            f"{decision.duration_ms}ms {cost} :: {decision.reasoning}"
        )

        if decision.action in ("done", "abort"):
            print(f"\nstopped: {decision.action} — {decision.reasoning}")
            return

        outcome = act(decision, dpr)
        history.append(
            StepHistoryItem(
                action=decision.action,
                target=decision.selector_hint,
                outcome=outcome,
                at=time.strftime("%H:%M:%S"),
            )
        )

    print(f"\nstopped: reached MAX_STEPS={MAX_STEPS}")


if __name__ == "__main__":
    task = " ".join(sys.argv[1:]) or "describe what is on the screen"
    asyncio.run(run(task))
