"""Offline tests for the BYO-key agent loop (bridge half).

These never touch a real LLM, a real browser, or the network:

  * The sibling `vision` package is faked in sys.modules so `_load_vision`
    succeeds even though litellm isn't installed in the bridge venv. The fake
    exposes ModelConfig / VisionConfig / StepHistoryItem and a `decide_action`
    we monkeypatch per-test with a SCRIPTED sequence of ActionDecisions.
  * The executor is faked (an object with an async `call()` returning a tiny
    1x1 PNG screenshot result) and dropped straight into app.state.executors,
    so `_resolve_executor` finds it.

Covered:
  - /agent/run streams agent events (start -> decision -> acted -> done) and the
    run completes (registry drains).
  - /agent/stop halts a run mid-flight.
  - "ask" mode emits awaiting_approval before an actuating action and waits for
    /agent/approve; approve:true executes it, approve:false stops the run.
  - 503 when the vision deps can't be imported.

Run from this directory:
    pytest test_agent_runner.py -v
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import types

import httpx
import pytest
import pytest_asyncio


# A real 1x1 transparent PNG, base64-encoded — what the fake screenshot returns.
_PNG_1x1 = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )
).decode()


def _make_png_b64(w: int, h: int) -> str:
    """Return a base64-encoded minimal (w x h) white PNG using Pillow."""
    from PIL import Image
    from io import BytesIO
    im = Image.new("RGB", (w, h), (255, 255, 255))
    buf = BytesIO()
    im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


_PNG_100x100 = _make_png_b64(100, 100)


# ---------- a fake `vision` package, installed into sys.modules ----------
#
# We build it ONCE at import time so `from vision import ...` (inside
# agent_runner._load_vision) resolves to these objects. Tests then monkeypatch
# `vision.decide_action` to script the decision sequence.


class _FakeActionDecision:
    """Mimics vision.ActionDecision's attribute surface used by the loop."""

    def __init__(
        self,
        action,
        coordinates=None,
        text=None,
        selector_hint=None,
        confidence=0.9,
        reasoning="because",
        model_used="fake-model",
        cost_usd=0.0001,
    ):
        self.action = action
        self.coordinates = coordinates
        self.text = text
        self.selector_hint = selector_hint
        self.confidence = confidence
        self.reasoning = reasoning
        self.model_used = model_used
        self.cost_usd = cost_usd


class _FakeModelConfig:
    def __init__(self, model=None, api_key=None, base_url=None):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url


class _FakeVisionConfig:
    def __init__(self, models=None, **_kw):
        self.models = models or []


class _FakeStepHistoryItem:
    def __init__(self, action=None, target=None, outcome=None, at=None):
        self.action = action
        self.target = target
        self.outcome = outcome
        self.at = at


def _install_fake_vision():
    """Insert a fake `vision` module so agent_runner._load_vision imports it."""
    mod = types.ModuleType("vision")
    mod.ActionDecision = _FakeActionDecision
    mod.ModelConfig = _FakeModelConfig
    mod.VisionConfig = _FakeVisionConfig
    mod.StepHistoryItem = _FakeStepHistoryItem

    async def _default_decide_action(**_kw):
        return _FakeActionDecision(action="done", reasoning="nothing to do")

    mod.decide_action = _default_decide_action
    sys.modules["vision"] = mod
    return mod


_FAKE_VISION = _install_fake_vision()

# Import the app AFTER faking vision (agent_runner imports vision lazily, so
# order doesn't strictly matter, but this keeps the intent obvious).
from bridge import app  # noqa: E402


# ---------- fixtures ----------


@pytest.fixture(autouse=True)
def clean_state():
    """Each test starts with an empty executor registry and no live runs."""
    app.state.executors.clear()
    app.state.agent_runs.clear()
    yield
    # Cancel any leaked background loops so they can't bleed across tests. A run
    # started on a now-closed loop (e.g. the ephemeral-uvicorn SSE test) can't be
    # cancelled — its loop is gone — so swallow that; clearing the dict is enough.
    for rec in list(app.state.agent_runs.values()):
        t = rec.get("task")
        if t is not None and not t.done():
            try:
                t.cancel()
            except RuntimeError:
                pass
    app.state.agent_runs.clear()
    app.state.executors.clear()


@pytest.fixture(autouse=True)
def restore_decide():
    """Reset the scripted decide_action after each test."""
    saved = _FAKE_VISION.decide_action
    yield
    _FAKE_VISION.decide_action = saved


@pytest_asyncio.fixture
async def http_client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class _FakeExecutor:
    """Stands in for ExecutorClient. Records every call; returns a tiny
    screenshot for `screenshot`, and {} for everything else."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.ws = object()  # truthy; _resolve_executor only checks registry presence
        self._png_b64 = _PNG_1x1

    async def call(self, method, params=None, *, timeout=30.0):
        self.calls.append((method, params or {}))
        if method == "screenshot":
            return {"base64": self._png_b64, "url": "https://example.test/", "device_pixel_ratio": 2.0}
        return {}


def _script(*decisions):
    """Return an async decide_action that yields the given decisions in order,
    repeating the last one forever (so a max_steps overrun still terminates)."""
    seq = list(decisions)
    idx = {"i": 0}

    async def _decide(**_kw):
        i = idx["i"]
        idx["i"] = min(i + 1, len(seq) - 1)
        return seq[i]

    return _decide


class _EventCollector:
    """Subscribes to the broker (the same fan-out /events reads) BEFORE the run
    starts, so no early event is missed to a late-subscribe race. Collects agent
    events for a run_id and can wait until a set of phases has been observed."""

    def __init__(self):
        self.events: list[dict] = []
        self._seen: set[tuple] = set()  # (run_id, phase)
        self._task: asyncio.Task | None = None

    async def __aenter__(self):
        broker = app.state.broker
        started = asyncio.Event()

        async def _pump():
            agen = broker.subscribe(last_id=broker.last_seq)
            started.set()
            async for env in agen:
                if env.get("type") != "agent":
                    continue
                self.events.append(env)
                self._seen.add((env.get("run_id"), env.get("phase")))

        self._task = asyncio.create_task(_pump())
        await started.wait()  # subscription registered before we return
        return self

    async def __aexit__(self, *exc):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def wait_for(self, run_id, want_phases, timeout=5.0):
        """Block until every phase in want_phases is seen for run_id."""
        async def _spin():
            while not all((run_id, p) in self._seen for p in want_phases):
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(_spin(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return [e for e in self.events if e.get("run_id") == run_id]


# ---------- tests ----------


@pytest.mark.asyncio
async def test_run_streams_events_and_completes(http_client):
    """auto mode: one navigate then done. Stream shows start/decision/acted/done
    and the run drains from the registry."""
    app.state.executors["default"] = _FakeExecutor()
    _FAKE_VISION.decide_action = _script(
        _FakeActionDecision(action="navigate", text="https://example.test/login"),
        _FakeActionDecision(action="done", reasoning="finished"),
    )

    async with _EventCollector() as col:
        r = await http_client.post(
            "/agent/run", json={"task": "open login", "mode": "auto", "max_steps": 5}
        )
        assert r.status_code == 200
        run_id = r.json()["run_id"]
        assert run_id

        events = await col.wait_for(
            run_id, {"start", "decision", "acted", "done"}
        )

    phases = [e["phase"] for e in events]
    assert "start" in phases
    assert "decision" in phases
    assert "acted" in phases
    assert "done" in phases
    # The acted event names the executed action; cost/model surface on decision.
    dec = next(e for e in events if e["phase"] == "decision")
    assert dec["model"] == "fake-model"
    assert dec["cost_usd"] == 0.0001

    # The navigate actually went to the executor with the right URL.
    ex = app.state.executors["default"]
    navs = [p for (m, p) in ex.calls if m == "navigate"]
    assert navs and navs[0]["url"] == "https://example.test/login"

    # Run drains from the registry once finished.
    for _ in range(50):
        if run_id not in app.state.agent_runs:
            break
        await asyncio.sleep(0.02)
    assert run_id not in app.state.agent_runs


@pytest.mark.asyncio
async def test_coordinates_divided_by_dpr(http_client):
    """click coordinates (screenshot px) are divided by device_pixel_ratio
    before reaching click_at (CSS px). dpr=2.0 -> (200,100) becomes (100,50)."""
    app.state.executors["default"] = _FakeExecutor()
    _FAKE_VISION.decide_action = _script(
        _FakeActionDecision(action="click", coordinates=(200, 100)),
        _FakeActionDecision(action="done"),
    )

    async with _EventCollector() as col:
        r = await http_client.post(
            "/agent/run", json={"task": "click it", "mode": "auto", "max_steps": 5}
        )
        run_id = r.json()["run_id"]
        await col.wait_for(run_id, {"done"})

    ex = app.state.executors["default"]
    clicks = [p for (m, p) in ex.calls if m == "click_at"]
    assert clicks and clicks[0] == {"x": 100.0, "y": 50.0}


@pytest.mark.asyncio
async def test_stop_halts_run(http_client):
    """/agent/stop signals a run to stop. A run scripted to wait forever stops
    after we POST /agent/stop, emitting phase:stopped, and drains."""
    app.state.executors["default"] = _FakeExecutor()
    # 'wait' loops without ever finishing on its own.
    _FAKE_VISION.decide_action = _script(_FakeActionDecision(action="wait"))

    async with _EventCollector() as col:
        r = await http_client.post(
            "/agent/run", json={"task": "spin", "mode": "auto", "max_steps": 100}
        )
        run_id = r.json()["run_id"]

        # Let it take at least one step, then stop it.
        await col.wait_for(run_id, {"start"})
        s = await http_client.post("/agent/stop", json={"run_id": run_id})
        assert s.status_code == 200 and s.json()["stopped"] is True

        events = await col.wait_for(run_id, {"stopped"})

    assert any(e["phase"] == "stopped" for e in events)

    for _ in range(100):
        if run_id not in app.state.agent_runs:
            break
        await asyncio.sleep(0.02)
    assert run_id not in app.state.agent_runs


@pytest.mark.asyncio
async def test_stop_all(http_client):
    """/agent/stop with no run_id stops every live run."""
    app.state.executors["default"] = _FakeExecutor()
    app.state.executors["other"] = _FakeExecutor()
    _FAKE_VISION.decide_action = _script(_FakeActionDecision(action="wait"))

    ids = []
    for browser_id in (None, "other"):
        body = {"task": "spin", "mode": "auto", "max_steps": 100}
        if browser_id:
            body["browser_id"] = browser_id
        r = await http_client.post(
            "/agent/run", json=body
        )
        ids.append(r.json()["run_id"])

    await asyncio.sleep(0.05)
    s = await http_client.post("/agent/stop", json={})
    assert s.json()["stopped"] is True

    for _ in range(100):
        if not any(i in app.state.agent_runs for i in ids):
            break
        await asyncio.sleep(0.02)
    assert all(i not in app.state.agent_runs for i in ids)


@pytest.mark.asyncio
async def test_ask_mode_awaiting_approval_then_approve(http_client):
    """ask mode: an actuating action emits awaiting_approval and WAITS. After
    /agent/approve {approved:true} the action executes and the run finishes."""
    app.state.executors["default"] = _FakeExecutor()
    _FAKE_VISION.decide_action = _script(
        _FakeActionDecision(action="navigate", text="https://example.test/x"),
        _FakeActionDecision(action="done"),
    )

    async with _EventCollector() as col:
        r = await http_client.post(
            "/agent/run", json={"task": "go", "mode": "ask", "max_steps": 5}
        )
        run_id = r.json()["run_id"]

        # It should pause at awaiting_approval and NOT have navigated yet.
        events = await col.wait_for(run_id, {"awaiting_approval"})
        approval_evt = next(e for e in events if e["phase"] == "awaiting_approval")
        assert approval_evt.get("await_id")
        ex = app.state.executors["default"]
        assert not any(m == "navigate" for (m, _p) in ex.calls)

        # Approve -> it executes the navigate and finishes.
        a = await http_client.post(
            "/agent/approve", json={"run_id": run_id, "approved": True}
        )
        assert a.status_code == 200 and a.json()["ok"] is True

        await col.wait_for(run_id, {"acted", "done"})

    assert any(m == "navigate" for (m, _p) in ex.calls)


@pytest.mark.asyncio
async def test_ask_mode_reject_stops_run(http_client):
    """ask mode: /agent/approve {approved:false} stops the run without acting."""
    app.state.executors["default"] = _FakeExecutor()
    _FAKE_VISION.decide_action = _script(
        _FakeActionDecision(action="click", coordinates=(10, 10)),
        _FakeActionDecision(action="done"),
    )

    async with _EventCollector() as col:
        r = await http_client.post(
            "/agent/run", json={"task": "maybe click", "mode": "ask", "max_steps": 5}
        )
        run_id = r.json()["run_id"]

        await col.wait_for(run_id, {"awaiting_approval"})
        a = await http_client.post(
            "/agent/approve", json={"run_id": run_id, "approved": False}
        )
        assert a.json()["ok"] is True

        events = await col.wait_for(run_id, {"stopped"})

    assert any(e["phase"] == "stopped" for e in events)

    ex = app.state.executors["default"]
    assert not any(m == "click_at" for (m, _p) in ex.calls)


@pytest.mark.asyncio
async def test_approval_stale_await_id_rejected(http_client):
    app.state.executors["default"] = _FakeExecutor()
    _FAKE_VISION.decide_action = _script(
        _FakeActionDecision(action="click", coordinates=(10, 10)),
        _FakeActionDecision(action="done"),
    )

    async with _EventCollector() as col:
        r = await http_client.post(
            "/agent/run", json={"task": "click", "mode": "ask", "max_steps": 5}
        )
        run_id = r.json()["run_id"]
        await col.wait_for(run_id, {"awaiting_approval"})
        bad = await http_client.post(
            "/agent/approve", json={"run_id": run_id, "approved": True, "await_id": "stale"}
        )
        assert bad.json() == {"ok": False, "reason": "stale_await_id"}
        await http_client.post("/agent/stop", json={"run_id": run_id})
        await col.wait_for(run_id, {"stopped"})


@pytest.mark.asyncio
async def test_invalid_mode_and_max_steps_rejected(http_client):
    app.state.executors["default"] = _FakeExecutor()
    bad_mode = await http_client.post(
        "/agent/run", json={"task": "x", "mode": "oops", "max_steps": 5}
    )
    assert bad_mode.status_code == 422

    bad_steps = await http_client.post(
        "/agent/run", json={"task": "x", "mode": "ask", "max_steps": 101}
    )
    assert bad_steps.status_code == 422


@pytest.mark.asyncio
async def test_concurrent_same_browser_run_conflict(http_client):
    app.state.executors["default"] = _FakeExecutor()
    _FAKE_VISION.decide_action = _script(_FakeActionDecision(action="wait"))

    async with _EventCollector() as col:
        first = await http_client.post(
            "/agent/run", json={"task": "spin", "mode": "auto", "max_steps": 100}
        )
        assert first.status_code == 200
        run_id = first.json()["run_id"]
        await col.wait_for(run_id, {"start"})

        second = await http_client.post(
            "/agent/run", json={"task": "spin 2", "mode": "auto", "max_steps": 100}
        )
        assert second.status_code == 409
        await http_client.post("/agent/stop", json={"run_id": run_id})
        await col.wait_for(run_id, {"stopped"})


@pytest.mark.asyncio
async def test_ask_user_secret_question_aborts(http_client):
    app.state.executors["default"] = _FakeExecutor()
    _FAKE_VISION.decide_action = _script(
        _FakeActionDecision(action="ask_user", text="What is your password?", confidence=0.95),
    )

    async with _EventCollector() as col:
        r = await http_client.post(
            "/agent/run", json={"task": "log in", "mode": "auto", "max_steps": 5}
        )
        run_id = r.json()["run_id"]
        events = await col.wait_for(run_id, {"abort"})

    assert any(e.get("reasoning") == "ask_user_secret_request_blocked" for e in events)


@pytest.mark.asyncio
async def test_ask_user_pauses_for_reply_then_resumes(http_client):
    """ask_user emits awaiting_input (with the question) and WAITS; /agent/reply
    feeds the answer and the run resumes to done. No page actuation happens for
    the ask — only the screenshot observations."""
    ex = _FakeExecutor()
    app.state.executors["default"] = ex
    _FAKE_VISION.decide_action = _script(
        _FakeActionDecision(action="ask_user", text="Which account?", confidence=0.95),
        _FakeActionDecision(action="done", reasoning="finished"),
    )

    async with _EventCollector() as col:
        r = await http_client.post(
            "/agent/run", json={"task": "log in", "mode": "auto", "max_steps": 5}
        )
        run_id = r.json()["run_id"]

        first = await col.wait_for(run_id, {"awaiting_input"})
        ai = next(e for e in first if e["phase"] == "awaiting_input")
        assert ai.get("message") == "Which account?"
        assert ai.get("action") == "ask_user"
        # It must WAIT — only screenshots so far, no click/type/navigate/scroll.
        assert all(m == "screenshot" for (m, _p) in ex.calls)

        rp = await http_client.post(
            "/agent/reply", json={"run_id": run_id, "text": "the gold one"}
        )
        assert rp.status_code == 200 and rp.json()["ok"] is True

        final = await col.wait_for(run_id, {"acted", "done"})

    acted = [
        e for e in final if e["phase"] == "acted" and e.get("action") == "ask_user"
    ]
    assert acted and "the gold one" not in (acted[0].get("message") or "")
    assert "[REDACTED]" in (acted[0].get("message") or "")
    assert any(e["phase"] == "done" for e in final)


@pytest.mark.asyncio
async def test_scroll_executes_with_dpr(http_client):
    """A scroll decision reaches the executor's `scroll` method with its delta
    divided by device_pixel_ratio (dpr=2.0 -> (0,600) becomes (0,300))."""
    ex = _FakeExecutor()
    app.state.executors["default"] = ex
    _FAKE_VISION.decide_action = _script(
        _FakeActionDecision(action="scroll", coordinates=(0, 600)),
        _FakeActionDecision(action="done"),
    )

    async with _EventCollector() as col:
        r = await http_client.post(
            "/agent/run", json={"task": "scroll down", "mode": "auto", "max_steps": 5}
        )
        run_id = r.json()["run_id"]
        await col.wait_for(run_id, {"done"})

    scrolls = [p for (m, p) in ex.calls if m == "scroll"]
    assert scrolls and scrolls[0] == {"x": 0.0, "y": 300.0}


@pytest.mark.asyncio
async def test_reply_unknown_run_is_noop(http_client):
    """/agent/reply for a run that doesn't exist returns ok:false, not a crash."""
    rp = await http_client.post(
        "/agent/reply", json={"run_id": "does-not-exist", "text": "hi"}
    )
    assert rp.status_code == 200 and rp.json()["ok"] is False


@pytest.mark.asyncio
async def test_stop_during_awaiting_input(http_client):
    """Stop while paused at ask_user: stop() unblocks _await_reply, the loop
    emits phase:stopped and drains, and no page action is dispatched."""
    ex = _FakeExecutor()
    app.state.executors["default"] = ex
    _FAKE_VISION.decide_action = _script(
        _FakeActionDecision(action="ask_user", text="Which account?", confidence=0.95),
        _FakeActionDecision(action="done"),
    )

    async with _EventCollector() as col:
        r = await http_client.post(
            "/agent/run", json={"task": "x", "mode": "auto", "max_steps": 5}
        )
        run_id = r.json()["run_id"]
        await col.wait_for(run_id, {"awaiting_input"})
        s = await http_client.post("/agent/stop", json={"run_id": run_id})
        assert s.json()["stopped"] is True
        events = await col.wait_for(run_id, {"stopped"})

    assert any(e["phase"] == "stopped" for e in events)
    assert all(m == "screenshot" for (m, _p) in ex.calls)  # no actuation
    for _ in range(100):
        if run_id not in app.state.agent_runs:
            break
        await asyncio.sleep(0.02)
    assert run_id not in app.state.agent_runs


@pytest.mark.asyncio
async def test_ask_mode_ask_user_uses_input_gate_not_approval(http_client):
    """In mode='ask', ask_user emits awaiting_INPUT (resumed by /agent/reply),
    NOT awaiting_approval — it has its own gate, independent of the ask gate."""
    app.state.executors["default"] = _FakeExecutor()
    _FAKE_VISION.decide_action = _script(
        _FakeActionDecision(action="ask_user", text="Which one?", confidence=0.9),
        _FakeActionDecision(action="done"),
    )

    async with _EventCollector() as col:
        r = await http_client.post(
            "/agent/run", json={"task": "x", "mode": "ask", "max_steps": 5}
        )
        run_id = r.json()["run_id"]
        first = await col.wait_for(run_id, {"awaiting_input"})
        phases = {e["phase"] for e in first}
        assert "awaiting_input" in phases
        assert "awaiting_approval" not in phases

        rp = await http_client.post(
            "/agent/reply", json={"run_id": run_id, "text": "first"}
        )
        assert rp.json()["ok"] is True
        final = await col.wait_for(run_id, {"done"})

    assert any(e["phase"] == "done" for e in final)


@pytest.mark.asyncio
async def test_reply_when_not_awaiting_is_rejected(http_client):
    """A /agent/reply for a run that isn't paused on an ask_user is rejected
    (ok:false, not_awaiting) — so a stray/duplicate reply can't pre-arm a later
    ask_user with a stale answer."""
    app.state.executors["default"] = _FakeExecutor()
    _FAKE_VISION.decide_action = _script(_FakeActionDecision(action="wait"))

    async with _EventCollector() as col:
        r = await http_client.post(
            "/agent/run", json={"task": "spin", "mode": "auto", "max_steps": 100}
        )
        run_id = r.json()["run_id"]
        await col.wait_for(run_id, {"start"})
        await asyncio.sleep(0.05)  # let it settle into the wait loop (awaiting is None)

        rp = await http_client.post(
            "/agent/reply", json={"run_id": run_id, "text": "stale"}
        )
        assert rp.status_code == 200
        body = rp.json()
        assert body["ok"] is False and body.get("reason") == "not_awaiting"

        await http_client.post("/agent/stop", json={"run_id": run_id})
        await col.wait_for(run_id, {"stopped"})


@pytest.mark.asyncio
async def test_ask_user_answer_reaches_model_history_redacted(http_client):
    """The human's answer reaches next-step history only after redaction."""
    app.state.executors["default"] = _FakeExecutor()
    captured: list = []
    seq = [
        _FakeActionDecision(action="ask_user", text="Which account?", confidence=0.95),
        _FakeActionDecision(action="done", reasoning="finished"),
    ]
    idx = {"i": 0}

    async def _decide(**kw):
        captured.append(kw.get("step_history"))
        i = idx["i"]
        idx["i"] = min(i + 1, len(seq) - 1)
        return seq[i]

    _FAKE_VISION.decide_action = _decide

    async with _EventCollector() as col:
        r = await http_client.post(
            "/agent/run", json={"task": "log in", "mode": "auto", "max_steps": 5}
        )
        run_id = r.json()["run_id"]
        await col.wait_for(run_id, {"awaiting_input"})
        await http_client.post(
            "/agent/reply", json={"run_id": run_id, "text": "password hunter2"}
        )
        await col.wait_for(run_id, {"done"})

    assert len(captured) >= 2
    hist = captured[1] or []
    assert any(
        getattr(h, "action", None) == "ask_user"
        and "hunter2" not in (getattr(h, "outcome", "") or "")
        and "[REDACTED]" in (getattr(h, "outcome", "") or "")
        for h in hist
    ), "the redacted reply did not reach the model's next-step history"


@pytest.mark.asyncio
async def test_two_sequential_ask_users_each_wait(http_client):
    """Two ask_user steps in one run each pause independently — the second does
    NOT auto-resume off the first answer (reply_event/awaiting reset between
    them). This is the regression guard for the stale-reply race."""
    app.state.executors["default"] = _FakeExecutor()
    _FAKE_VISION.decide_action = _script(
        _FakeActionDecision(action="ask_user", text="Q1", confidence=0.95),
        _FakeActionDecision(action="ask_user", text="Q2", confidence=0.95),
        _FakeActionDecision(action="done"),
    )

    async def _seen_question(col, run_id, q, timeout=5.0):
        for _ in range(int(timeout / 0.02)):
            if any(
                e.get("phase") == "awaiting_input"
                and e.get("message") == q
                and e.get("run_id") == run_id
                for e in col.events
            ):
                return True
            await asyncio.sleep(0.02)
        return False

    async with _EventCollector() as col:
        r = await http_client.post(
            "/agent/run", json={"task": "x", "mode": "auto", "max_steps": 6}
        )
        run_id = r.json()["run_id"]
        assert await _seen_question(col, run_id, "Q1")

        await http_client.post("/agent/reply", json={"run_id": run_id, "text": "A1"})
        assert await _seen_question(col, run_id, "Q2")
        # Q2 must be genuinely waiting — the run is NOT done yet.
        assert not any(e.get("phase") == "done" for e in col.events)

        await http_client.post("/agent/reply", json={"run_id": run_id, "text": "A2"})
        final = await col.wait_for(run_id, {"done"})

    assert any(e["phase"] == "done" for e in final)


@pytest.mark.asyncio
async def test_no_executor_emits_error(http_client):
    """No browser connected -> the loop publishes phase:error and drains."""
    # registry intentionally empty
    async with _EventCollector() as col:
        r = await http_client.post(
            "/agent/run", json={"task": "x", "mode": "auto", "max_steps": 3}
        )
        assert r.status_code == 200
        run_id = r.json()["run_id"]

        events = await col.wait_for(run_id, {"error"})

    assert any(e["phase"] == "error" for e in events)


@pytest.mark.asyncio
async def test_run_503_when_vision_unavailable(http_client, monkeypatch):
    """If the vision import fails, /agent/run returns 503 with the install hint
    and starts no run."""
    import agent_runner

    def _boom():
        raise agent_runner.VisionUnavailable("No module named 'litellm'")

    monkeypatch.setattr(agent_runner, "_load_vision", _boom)

    before = len(app.state.agent_runs)
    r = await http_client.post(
        "/agent/run", json={"task": "x", "mode": "auto"}
    )
    assert r.status_code == 503
    assert "vision deps not installed" in r.json()["error"]
    assert len(app.state.agent_runs) == before


@pytest.mark.asyncio
async def test_events_flow_over_real_sse():
    """End-to-end over the ACTUAL GET /events SSE wire (the channel the chat
    panel reads): an auto run's agent events arrive as `data: {type:"agent"...}`
    lines.

    httpx's ASGITransport buffers the whole response body, so SSE — which never
    closes — can't be exercised through it. Spin up a real uvicorn on an
    ephemeral port for this test only (mirrors test_bridge.py's SSE test)."""
    import socket
    import uvicorn

    app.state.executors["default"] = _FakeExecutor()
    _FAKE_VISION.decide_action = _script(
        _FakeActionDecision(action="wait"),
        _FakeActionDecision(action="done"),
    )

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "uvicorn did not start"

    seen: set[str] = set()
    try:
        async with httpx.AsyncClient(timeout=8.0) as ac:
            async with ac.stream("GET", f"http://127.0.0.1:{port}/events") as r:
                assert r.status_code == 200

                async def reader():
                    async for line in r.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        env = json.loads(line[6:])
                        if env.get("type") == "agent":
                            seen.add(env.get("phase"))
                            if {"start", "decision"} <= seen:
                                return

                reader_task = asyncio.create_task(reader())
                await asyncio.sleep(0.2)
                await ac.post(
                    f"http://127.0.0.1:{port}/agent/run",
                    json={"task": "stream", "mode": "auto", "max_steps": 3},
                )
                try:
                    await asyncio.wait_for(reader_task, timeout=5.0)
                except asyncio.TimeoutError:
                    reader_task.cancel()
                    raise AssertionError("agent SSE events never arrived")
            # Stop the run on the server's still-live loop so its background task
            # drains here, not on a closed loop at teardown.
            await ac.post(f"http://127.0.0.1:{port}/agent/stop", json={})
            await asyncio.sleep(0.1)
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=3.0)
        except asyncio.TimeoutError:
            server_task.cancel()

    assert "start" in seen
    assert "decision" in seen


@pytest.mark.asyncio
async def test_zoom_emits_acted_and_no_executor_actuation(http_client):
    """A zoom decision publishes phase:acted action:zoom with 'zoomed into region'
    and does NOT trigger any executor actuation (only screenshot calls)."""
    ex = _FakeExecutor()
    ex._png_b64 = _PNG_100x100
    app.state.executors["default"] = ex
    _FAKE_VISION.decide_action = _script(
        _FakeActionDecision(action="zoom", coordinates=(50, 50)),
        _FakeActionDecision(action="done"),
    )

    async with _EventCollector() as col:
        r = await http_client.post(
            "/agent/run", json={"task": "zoom test", "mode": "auto", "max_steps": 5}
        )
        run_id = r.json()["run_id"]
        events = await col.wait_for(run_id, {"acted", "done"})

    zoom_events = [e for e in events if e.get("phase") == "acted" and e.get("action") == "zoom"]
    assert zoom_events
    assert zoom_events[0]["message"].startswith("zoomed into region")
    assert any(e["phase"] == "done" for e in events)
    assert all(m == "screenshot" for (m, _p) in ex.calls)


@pytest.mark.asyncio
async def test_zoom_with_none_coordinates_degrades_gracefully(http_client):
    """A zoom with coordinates=None degrades to 'zoom unavailable; showing full page'
    and does not crash."""
    ex = _FakeExecutor()
    ex._png_b64 = _PNG_100x100
    app.state.executors["default"] = ex
    _FAKE_VISION.decide_action = _script(
        _FakeActionDecision(action="zoom", coordinates=None),
        _FakeActionDecision(action="done"),
    )

    async with _EventCollector() as col:
        r = await http_client.post(
            "/agent/run", json={"task": "zoom none test", "mode": "auto", "max_steps": 5}
        )
        run_id = r.json()["run_id"]
        events = await col.wait_for(run_id, {"acted", "done"})

    zoom_events = [e for e in events if e.get("phase") == "acted" and e.get("action") == "zoom"]
    assert zoom_events
    assert "zoom unavailable" in zoom_events[0]["message"]
    assert any(e["phase"] == "done" for e in events)


@pytest.mark.asyncio
async def test_zoom_then_click_remaps_coordinates(http_client):
    """After a zoom, a subsequent click's coordinates are remapped through the view
    back to screenshot space (larger than the display-space coords)."""
    ex = _FakeExecutor()
    ex._png_b64 = _PNG_100x100
    app.state.executors["default"] = ex
    _FAKE_VISION.decide_action = _script(
        _FakeActionDecision(action="zoom", coordinates=(50, 50)),
        _FakeActionDecision(action="click", coordinates=(10, 10)),
        _FakeActionDecision(action="done"),
    )

    async with _EventCollector() as col:
        r = await http_client.post(
            "/agent/run", json={"task": "zoom then click", "mode": "auto", "max_steps": 5}
        )
        run_id = r.json()["run_id"]
        events = await col.wait_for(run_id, {"done"})

    clicks = [p for (m, p) in ex.calls if m == "click_at"]
    assert clicks
    cx, cy = clicks[0]["x"], clicks[0]["y"]
    # Derivation (100x100 png, dpr=2.0): zoom at display (50,50) with no prior view
    # -> crop box (10,10,90,90), scale = 100/80 = 1.25. Click at display (10,10) ->
    # screenshot (10 + 10/1.25, 10 + 10/1.25) = (18,18) -> CSS /dpr = (9.0, 9.0).
    # (Un-remapped would be display/dpr = (5,5), so this pins the offset+scale.)
    assert (cx, cy) == (9.0, 9.0), f"remapped click coords were ({cx}, {cy}), expected (9.0, 9.0)"
