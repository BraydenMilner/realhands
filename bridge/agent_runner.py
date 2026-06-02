"""AgentRunner — the server-side BYO-key agent loop.

This is the BRIDGE half of the RealHands chat panel. The extension's chat panel
POSTs a task to /agent/run; this module runs the loop in the background:

    screenshot  ->  decide_action (vision tier)  ->  (ask-gate)  ->  act  ->  repeat

Each step publishes a structured {type:"agent", ...} event to the EventBroker so
the panel can render progress live off the existing GET /events SSE stream.

The vision tier lives in the sibling `vision/` package and needs litellm. We
import it LAZILY (in `_load_vision`) so the bridge stays importable on a host
that hasn't `pip install -r vision/requirements.txt`'d yet — /agent/run returns
503 in that case rather than crashing the whole bridge at import time.

Money-moving actions are guarded upstream (vision's high_stakes_actions short-
circuits to done) and again at the bridge's /call money-guard; this loop never
clicks redeem/deposit/transfer. We do NOT touch those token lists here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("agent_bridge.agent")

# Actions that actuate the page. In "ask" mode each of these waits for an
# explicit POST /agent/approve before executing. screenshot / get_page_info are
# observation-only and never gated. (The vision ActionType can only emit these;
# ask_user/wait/done/abort are handled separately and are not gated here.)
_ACTUATING = frozenset({"click", "type", "navigate", "scroll"})

# Vision-tier ActionType values that END the run (no execution).
_TERMINAL = frozenset({"done", "abort"})

# Default per-step timeout for executor round-trips (CSS-pixel clicks etc.).
_EXEC_TIMEOUT = 30.0

# How long a "wait" action sleeps (matches examples/byo_key_agent.py).
_WAIT_SECONDS = 1.5

_ZOOM_FRACTION = 0.34
_MIN_CROP = 80
_MAX_ZOOM_DEPTH = 3


class VisionUnavailable(Exception):
    """Raised when the sibling vision package (or litellm) can't be imported.

    The /agent/run endpoint catches this and returns 503 with the canonical
    install hint, so the loop never starts half-wired.
    """


def _load_vision():
    """Lazily import the sibling vision package. Returns a small namespace with
    decide_action / VisionConfig / ModelConfig / StepHistoryItem.

    Adds the parent of `tether/vision/vision` (i.e. `tether/vision`) to sys.path
    so `from vision import ...` resolves the package, mirroring how
    examples/byo_key_agent.py bootstraps it. Raises VisionUnavailable on any
    import failure (most commonly: litellm not installed).
    """
    # bridge/agent_runner.py -> tether/ -> tether/vision (parent of the package).
    vision_parent = Path(__file__).resolve().parent.parent / "vision"
    p = str(vision_parent)
    if p not in sys.path:
        sys.path.insert(0, p)
    try:
        from vision import (  # type: ignore  # noqa: E402
            ModelConfig,
            StepHistoryItem,
            VisionConfig,
            decide_action,
        )
    except Exception as exc:  # noqa: BLE001 — ImportError or transitive failure.
        raise VisionUnavailable(str(exc)) from exc
    return decide_action, VisionConfig, ModelConfig, StepHistoryItem


def _build_vision_config(VisionConfig, ModelConfig):
    """Construct a VisionConfig from env (one-shot model).

    REALHANDS_VISION_MODEL    default "openai/qwen2.5-vl-7b-instruct"
    REALHANDS_VISION_API_KEY  your key (LiteLLM falls back to provider env var)
    REALHANDS_VISION_BASE_URL default "http://localhost:9001/v1"
    """
    model = os.environ.get("REALHANDS_VISION_MODEL", "openai/qwen2.5-vl-7b-instruct")
    api_key = os.environ.get("REALHANDS_VISION_API_KEY")
    base_url = os.environ.get("REALHANDS_VISION_BASE_URL", "http://localhost:9001/v1")
    return VisionConfig(
        models=[ModelConfig(model=model, api_key=api_key, base_url=base_url)]
    )


class AgentRunner:
    """Owns the set of in-flight agent runs.

    State lives on app.state.agent_runs (a dict run_id -> record) so the bridge
    endpoints and this runner share one source of truth and tests can inspect
    it. Each record:
        {
          "task": asyncio.Task,          # the background loop
          "stop_event": asyncio.Event,   # set by /agent/stop
          "approve_event": asyncio.Event,# set by /agent/approve
          "approved": bool|None,         # the latest approval verdict
          "reply_event": asyncio.Event,  # set by /agent/reply (ask_user answer)
          "reply": str|None,             # the latest human answer text
          "awaiting": None|"approval"|"input",  # what the loop is paused for
        }
    """

    def __init__(self, app) -> None:
        self._app = app

    # ---------- helpers bound to app.state ----------

    @property
    def _runs(self) -> dict[str, dict]:
        return self._app.state.agent_runs

    @property
    def _broker(self):
        return self._app.state.broker

    async def _publish(self, **event) -> None:
        await self._broker.publish({"type": "agent", **event})

    # ---------- public API (wired to the endpoints) ----------

    def start(
        self,
        task: str,
        browser_id: Optional[str] = None,
        max_steps: int = 25,
        mode: str = "ask",
    ) -> str:
        """Create a run and kick off its background loop. Returns the run_id.

        Validates the vision import EAGERLY so /agent/run can return 503 before
        creating a run when the deps are missing (raises VisionUnavailable).
        """
        decide_action, VisionConfig, ModelConfig, StepHistoryItem = _load_vision()
        config = _build_vision_config(VisionConfig, ModelConfig)

        run_id = uuid.uuid4().hex
        record: dict[str, Any] = {
            "task": None,
            "stop_event": asyncio.Event(),
            "approve_event": asyncio.Event(),
            "approved": None,
            "reply_event": asyncio.Event(),
            "reply": None,
            "awaiting": None,
        }
        self._runs[run_id] = record

        loop_task = asyncio.create_task(
            self._run_loop(
                run_id=run_id,
                task_text=task,
                browser_id=browser_id,
                max_steps=max_steps,
                mode=mode,
                decide_action=decide_action,
                config=config,
                StepHistoryItem=StepHistoryItem,
            )
        )
        record["task"] = loop_task
        return run_id

    def stop(self, run_id: Optional[str] = None) -> dict:
        """Signal one run (or all runs) to stop at the next checkpoint."""
        if run_id is None:
            for rec in self._runs.values():
                rec["stop_event"].set()
                # Unblock anything waiting on approval or a reply so it can
                # observe the stop.
                rec["approve_event"].set()
                rec["reply_event"].set()
            return {"stopped": True}
        rec = self._runs.get(run_id)
        if rec is not None:
            rec["stop_event"].set()
            rec["approve_event"].set()
            rec["reply_event"].set()
        return {"stopped": True}

    def approve(self, run_id: str, approved: bool) -> dict:
        """Record the verdict for a run's pending awaiting_approval step and
        wake the loop. approved=False causes the loop to stop the run.

        Ignored (ok:false) unless the run is actually paused for approval, so a
        duplicate/retried POST can't pre-arm the gate for a later step."""
        rec = self._runs.get(run_id)
        if rec is None:
            return {"ok": False, "reason": "unknown_run"}
        if rec.get("awaiting") != "approval":
            return {"ok": False, "reason": "not_awaiting"}
        rec["approved"] = bool(approved)
        rec["approve_event"].set()
        return {"ok": True}

    def reply(self, run_id: str, text: str) -> dict:
        """Record the human's answer to a run's pending awaiting_input step
        (an ask_user action) and wake the loop so it can resume.

        Ignored (ok:false) unless the run is actually paused on an ask_user, so a
        duplicate/retried POST can't pre-arm the wait and auto-answer the NEXT
        ask_user with a stale value."""
        rec = self._runs.get(run_id)
        if rec is None:
            return {"ok": False, "reason": "unknown_run"}
        if rec.get("awaiting") != "input":
            return {"ok": False, "reason": "not_awaiting"}
        rec["reply"] = text
        rec["reply_event"].set()
        return {"ok": True}

    # ---------- the loop ----------

    async def _run_loop(
        self,
        *,
        run_id: str,
        task_text: str,
        browser_id: Optional[str],
        max_steps: int,
        mode: str,
        decide_action,
        config,
        StepHistoryItem,
    ) -> None:
        rec = self._runs.get(run_id, {})
        stop_event: asyncio.Event = rec["stop_event"]
        model_name = config.models[0].model if config.models else None

        # Import bridge helpers lazily to avoid an import cycle (bridge imports
        # this module).
        from bridge import _ResolveError, _resolve_executor  # type: ignore
        from executor_client import ExecutorError  # type: ignore

        await self._publish(
            run_id=run_id, step=0, phase="start", message=task_text, model=model_name
        )

        try:
            executor = _resolve_executor(browser_id)
        except _ResolveError as exc:
            await self._publish(
                run_id=run_id,
                step=0,
                phase="error",
                message=exc.content.get("error", {}).get("code", "no_executor"),
            )
            self._finish(run_id)
            return

        history: list = []
        view = None

        for step in range(1, max_steps + 1):
            if stop_event.is_set():
                await self._publish(run_id=run_id, step=step, phase="stopped")
                break

            # ---- observe ----
            try:
                shot = await executor.call("screenshot", {}, timeout=_EXEC_TIMEOUT)
            except ExecutorError as exc:
                await self._publish(
                    run_id=run_id, step=step, phase="error", message=str(exc)
                )
                break

            png, url, dpr = _parse_screenshot(shot)
            if png is None:
                await self._publish(
                    run_id=run_id,
                    step=step,
                    phase="error",
                    message="bad screenshot response (no base64)",
                )
                break

            display_png = _apply_view(png, view)

            # ---- decide ----
            try:
                effective_task = (
                    task_text + "\n\n[The current screenshot is a ZOOMED-IN close-up of part of the page. Give coordinates within THIS image; they are mapped back to the full page.]"
                ) if view else task_text
                decision = await decide_action(
                    screenshot=display_png,
                    task_context=effective_task,
                    step_history=history,
                    page_url=url,
                    config=config,
                )
            except Exception as exc:  # noqa: BLE001 — surface decide failures.
                await self._publish(
                    run_id=run_id, step=step, phase="error", message=str(exc)
                )
                break

            action = decision.action
            await self._publish(
                run_id=run_id,
                step=step,
                phase="decision",
                action=action,
                reasoning=decision.reasoning,
                confidence=decision.confidence,
                model=decision.model_used,
                cost_usd=decision.cost_usd,
            )

            # ---- terminal actions: stop the run, no execution ----
            if action in _TERMINAL:
                await self._publish(
                    run_id=run_id,
                    step=step,
                    phase="abort" if action == "abort" else "done",
                    action=action,
                    reasoning=decision.reasoning,
                )
                break

            # ---- ask_user: pause, ask the human, resume with their answer ----
            # This is NOT a page-actuating action and has its own gate, so it is
            # handled before (and independently of) the ask-mode approval gate.
            if action == "ask_user":
                question = (
                    decision.text or decision.reasoning or "The agent needs your input."
                )
                rec["awaiting"] = "input"  # set BEFORE publishing so a reply is accepted
                await self._publish(
                    run_id=run_id,
                    step=step,
                    phase="awaiting_input",
                    action="ask_user",
                    message=question,
                    reasoning=decision.reasoning,
                )
                answer = await self._await_reply(run_id, stop_event)
                if stop_event.is_set():
                    await self._publish(run_id=run_id, step=step, phase="stopped")
                    break
                outcome = f"human answered: {answer}" if answer else "no answer given"
                await self._publish(
                    run_id=run_id,
                    step=step,
                    phase="acted",
                    action="ask_user",
                    message=outcome,
                )
                history.append(
                    StepHistoryItem(
                        action="ask_user",
                        target=question,
                        outcome=outcome,
                        at=time.strftime("%H:%M:%S"),
                    )
                )
                continue

            if action == "zoom":
                new_view = None
                if decision.coordinates is not None:
                    new_view = _zoom_view(png, decision.coordinates[0], decision.coordinates[1], view)
                if new_view is None:
                    view = None
                    await self._publish(run_id=run_id, step=step, phase="acted",
                                        action="zoom", message="zoom unavailable; showing full page")
                    history.append(StepHistoryItem(action="zoom", target=decision.selector_hint,
                                                   outcome="zoom unavailable; full page", at=time.strftime("%H:%M:%S")))
                    continue
                view = new_view
                await self._publish(run_id=run_id, step=step, phase="acted", action="zoom",
                                    message=f"zoomed into region {new_view['box']}")
                history.append(StepHistoryItem(action="zoom", target=decision.selector_hint,
                                               outcome="zoomed in; next screenshot is a close-up of that region",
                                               at=time.strftime("%H:%M:%S")))
                continue

            # ---- ask-gate before any actuating action ----
            if mode == "ask" and action in _ACTUATING:
                rec["awaiting"] = "approval"  # set BEFORE publishing
                await self._publish(
                    run_id=run_id,
                    step=step,
                    phase="awaiting_approval",
                    action=action,
                    reasoning=decision.reasoning,
                    confidence=decision.confidence,
                )
                approved = await self._await_approval(run_id, stop_event)
                if stop_event.is_set() or not approved:
                    await self._publish(run_id=run_id, step=step, phase="stopped")
                    break

            # ---- act ----
            if view and action in ("click", "type", "scroll"):
                decision.coordinates = _remap_from_view(action, decision.coordinates, view)
            if action in ("navigate", "click", "type", "scroll"):
                view = None
            try:
                outcome = await self._execute(executor, decision, dpr, stop_event)
            except ExecutorError as exc:
                await self._publish(
                    run_id=run_id, step=step, phase="error", message=str(exc)
                )
                break

            await self._publish(
                run_id=run_id,
                step=step,
                phase="acted",
                action=action,
                message=outcome,
            )

            history.append(
                StepHistoryItem(
                    action=action,
                    target=decision.selector_hint,
                    outcome=outcome,
                    at=time.strftime("%H:%M:%S"),
                )
            )
        else:
            # for-loop ran to completion without break: hit max_steps.
            await self._publish(
                run_id=run_id,
                step=max_steps,
                phase="done",
                message=f"reached max_steps={max_steps}",
            )

        self._finish(run_id)

    # ---------- step mechanics ----------

    async def _await_approval(self, run_id: str, stop_event: asyncio.Event) -> bool:
        """Block until /agent/approve (or /agent/stop) fires for this run.
        Returns the approval verdict; a stop returns False."""
        rec = self._runs.get(run_id)
        if rec is None:
            return False
        approve_event: asyncio.Event = rec["approve_event"]
        await approve_event.wait()
        # Atomic cleanup (no await before return): clearing awaiting here means a
        # duplicate /agent/approve that lands afterward is rejected as not_awaiting.
        approve_event.clear()
        rec["awaiting"] = None
        if stop_event.is_set():
            return False
        return bool(rec.get("approved"))

    async def _await_reply(self, run_id: str, stop_event: asyncio.Event) -> str:
        """Block until /agent/reply (or /agent/stop) fires for this run.
        Returns the human's answer text; a stop returns ""."""
        rec = self._runs.get(run_id)
        if rec is None:
            return ""
        reply_event: asyncio.Event = rec["reply_event"]
        await reply_event.wait()
        # Atomic cleanup (no await before return): consume the answer and clear
        # awaiting so a duplicate/stale reply can't pre-arm the NEXT ask_user.
        reply_event.clear()
        answer = rec.get("reply") or ""
        rec["reply"] = None
        rec["awaiting"] = None
        if stop_event.is_set():
            return ""
        return answer

    async def _execute(
        self, executor, decision, dpr: float, stop_event: asyncio.Event
    ) -> str:
        """Run one decision through the executor. Mirrors the reference loop:
        navigate / click_at{x,y} / type{x,y,text} / scroll{x,y} / wait=sleep.
        Coordinates are SCREENSHOT pixels — divide by the screenshot's
        device_pixel_ratio to get CSS pixels for the executor.

        click/type/scroll require coordinates; if a (weak) model omits them we
        return a recoverable no-op string instead of raising, so one malformed
        decision is recorded in history and re-prompted rather than killing the
        loop task."""
        action = decision.action
        if action in ("click", "type", "scroll") and decision.coordinates is None:
            return f"{action} skipped: no coordinates"
        if action == "navigate":
            await executor.call("navigate", {"url": decision.text}, timeout=_EXEC_TIMEOUT)
        elif action == "click":
            x, y = _css_coords(decision.coordinates, dpr)
            await executor.call("click_at", {"x": x, "y": y}, timeout=_EXEC_TIMEOUT)
        elif action == "type":
            x, y = _css_coords(decision.coordinates, dpr)
            await executor.call(
                "type", {"x": x, "y": y, "text": decision.text}, timeout=_EXEC_TIMEOUT
            )
        elif action == "scroll":
            x, y = _css_coords(decision.coordinates, dpr)
            await executor.call("scroll", {"x": x, "y": y}, timeout=_EXEC_TIMEOUT)
        elif action == "wait":
            # Sleep, but wake early if the run is stopped mid-wait so the loop
            # can exit promptly instead of holding the event loop for 1.5s.
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_WAIT_SECONDS)
            except asyncio.TimeoutError:
                pass
            return "waited"
        else:
            return action
        return "ok"

    def _finish(self, run_id: str) -> None:
        """Drop a finished run from the registry."""
        self._runs.pop(run_id, None)


# ---------- module-level pure helpers ----------


def _parse_screenshot(result: Any) -> tuple[Optional[bytes], str, float]:
    """Pull (png_bytes, url, device_pixel_ratio) out of a screenshot result.

    The executor returns {base64, url, device_pixel_ratio}. Returns
    (None, "", 1.0) if base64 is missing so the loop can emit a clean error.
    """
    import base64

    if not isinstance(result, dict) or "base64" not in result:
        return None, "", 1.0
    try:
        png = base64.b64decode(result["base64"])
    except Exception:  # noqa: BLE001 — malformed base64.
        return None, "", 1.0
    url = result.get("url", "") or ""
    dpr = result.get("device_pixel_ratio", 1.0) or 1.0
    return png, url, float(dpr)


def _css_coords(coordinates, dpr: float) -> tuple[float, float]:
    """Convert (x, y) SCREENSHOT pixels to CSS pixels by dividing by dpr."""
    x, y = coordinates
    d = dpr or 1.0
    return x / d, y / d


def _png_size(png: bytes):
    """(width, height) of a PNG via Pillow; (0,0) if Pillow missing/bad bytes."""
    try:
        from PIL import Image
        from io import BytesIO
        with Image.open(BytesIO(png)) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return (0, 0)


def _apply_view(png: bytes, view) -> bytes:
    """Crop `png` to view['box'] and enlarge by view['scale']. Returns the original
    bytes unchanged if view is None or Pillow is unavailable."""
    if not view:
        return png
    try:
        from PIL import Image
        from io import BytesIO
        x0, y0, x1, y1 = view["box"]
        s = view["scale"]
        with Image.open(BytesIO(png)) as im:
            crop = im.convert("RGB").crop((x0, y0, x1, y1))
            w = max(1, round((x1 - x0) * s))
            h = max(1, round((y1 - y0) * s))
            disp = crop.resize((w, h))
            out = BytesIO()
            disp.save(out, format="PNG")
            return out.getvalue()
    except Exception:  # noqa: BLE001
        return png


def _zoom_view(png: bytes, cx: float, cy: float, cur_view):
    """Compute the NEW view dict for a zoom centered at display point (cx, cy).
    Returns None if Pillow can't read the image or the depth cap is reached."""
    W, H = _png_size(png)
    if W == 0 or H == 0:
        return None
    depth = (cur_view or {}).get("depth", 0) + 1
    if depth > _MAX_ZOOM_DEPTH:
        return None
    if cur_view:
        bx0, by0, bx1, by1 = cur_view["box"]
        s = cur_view["scale"]
        scx = bx0 + cx / s
        scy = by0 + cy / s
        cur_w, cur_h = (bx1 - bx0), (by1 - by0)
    else:
        scx, scy = cx, cy
        cur_w, cur_h = W, H
    nw = max(_MIN_CROP, round(cur_w * _ZOOM_FRACTION))
    nh = max(_MIN_CROP, round(cur_h * _ZOOM_FRACTION))
    nw = min(nw, W); nh = min(nh, H)
    nx0 = min(max(round(scx - nw / 2), 0), W - nw)
    ny0 = min(max(round(scy - nh / 2), 0), H - nh)
    return {"box": (nx0, ny0, nx0 + nw, ny0 + nh), "scale": W / nw, "depth": depth}


def _remap_from_view(action: str, coordinates, view):
    """Map display-space coordinates back to SCREENSHOT space. For point actions
    (click/type) apply offset+scale; for scroll (a delta) apply scale only."""
    if not view or coordinates is None:
        return coordinates
    x0, y0, x1, y1 = view["box"]
    s = view["scale"]
    dx, dy = coordinates
    if action == "scroll":
        return (dx / s, dy / s)
    return (x0 + dx / s, y0 + dy / s)
