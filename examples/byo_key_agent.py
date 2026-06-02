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
from io import BytesIO

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


# --- zoom helpers (Tier 1) -------------------------------------------------
# Crop+enlarge a region of the captured screenshot so a weak model can read
# small text / pinpoint small targets. Pillow-based; degrade to a no-op if it
# is missing. Mirrors bridge/agent_runner.py (the chat-panel loop).
_ZOOM_FRACTION = 0.34
_MIN_CROP = 80
_MAX_ZOOM_DEPTH = 3


def _png_size(png: bytes):
    """(width, height) of a PNG via Pillow; (0,0) if Pillow missing/bad bytes."""
    try:
        from PIL import Image

        with Image.open(BytesIO(png)) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return (0, 0)


def _apply_view(png: bytes, view) -> bytes:
    """Crop `png` to view['box'] and enlarge by view['scale']. Returns the
    original bytes if view is None or Pillow is unavailable."""
    if not view:
        return png
    try:
        from PIL import Image

        x0, y0, x1, y1 = view["box"]
        s = view["scale"]
        with Image.open(BytesIO(png)) as im:
            crop = im.convert("RGB").crop((x0, y0, x1, y1))
            w = max(1, round((x1 - x0) * s))
            h = max(1, round((y1 - y0) * s))
            out = BytesIO()
            crop.resize((w, h)).save(out, format="PNG")
            return out.getvalue()
    except Exception:  # noqa: BLE001
        return png


def _zoom_view(png: bytes, cx: float, cy: float, cur_view):
    """New view dict for a zoom centered at display point (cx, cy). Returns None
    if Pillow can't read the image or the depth cap is reached."""
    W, H = _png_size(png)
    if W == 0 or H == 0:
        return None
    depth = (cur_view or {}).get("depth", 0) + 1
    if depth > _MAX_ZOOM_DEPTH:
        return None
    if cur_view:
        bx0, by0, bx1, by1 = cur_view["box"]
        s = cur_view["scale"]
        scx, scy = bx0 + cx / s, by0 + cy / s
        cur_w, cur_h = (bx1 - bx0), (by1 - by0)
    else:
        scx, scy = cx, cy
        cur_w, cur_h = W, H
    nw = min(max(_MIN_CROP, round(cur_w * _ZOOM_FRACTION)), W)
    nh = min(max(_MIN_CROP, round(cur_h * _ZOOM_FRACTION)), H)
    nx0 = min(max(round(scx - nw / 2), 0), W - nw)
    ny0 = min(max(round(scy - nh / 2), 0), H - nh)
    return {"box": (nx0, ny0, nx0 + nw, ny0 + nh), "scale": W / nw, "depth": depth}


def _remap_from_view(action: str, coordinates, view):
    """Map display-space coordinates back to SCREENSHOT space (offset+scale for
    point actions; scale-only for scroll deltas)."""
    if not view or coordinates is None:
        return coordinates
    x0, y0, x1, y1 = view["box"]
    s = view["scale"]
    dx, dy = coordinates
    if action == "scroll":
        return (dx / s, dy / s)
    return (x0 + dx / s, y0 + dy / s)


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
    view = None
    print(f"task: {task!r}\nbridge: {BRIDGE}  model: {MODEL}\n")

    for step in range(1, MAX_STEPS + 1):
        png, url, dpr = screenshot()
        display_png = _apply_view(png, view)
        effective_task = (
            task + "\n\n[The current screenshot is a ZOOMED-IN close-up of part of the page. Give coordinates within THIS image; they are mapped back to the full page.]"
        ) if view else task
        decision = await decide_action(
            screenshot=display_png,
            task_context=effective_task,
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

        if decision.action == "zoom":
            new_view = None
            if decision.coordinates is not None:
                new_view = _zoom_view(png, decision.coordinates[0], decision.coordinates[1], view)
            if new_view is None:
                view = None
                history.append(
                    StepHistoryItem(
                        action="zoom", target=decision.selector_hint,
                        outcome="zoom unavailable; full page", at=time.strftime("%H:%M:%S"),
                    )
                )
                print("  -> zoom unavailable; showing full page")
                continue
            view = new_view
            history.append(
                StepHistoryItem(
                    action="zoom", target=decision.selector_hint,
                    outcome="zoomed in; next screenshot is a close-up of that region",
                    at=time.strftime("%H:%M:%S"),
                )
            )
            print(f"  -> zoomed into region {new_view['box']}")
            continue

        if view and decision.action in ("click", "type", "scroll"):
            decision.coordinates = _remap_from_view(decision.action, decision.coordinates, view)
        if decision.action in ("navigate", "click", "type", "scroll"):
            view = None

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
