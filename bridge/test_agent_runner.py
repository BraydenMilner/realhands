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

    async def call(self, method, params=None, *, timeout=30.0):
        self.calls.append((method, params or {}))
        if method == "screenshot":
            return {"base64": _PNG_1x1, "url": "https://example.test/", "device_pixel_ratio": 2.0}
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
    _FAKE_VISION.decide_action = _script(_FakeActionDecision(action="wait"))

    ids = []
    for _ in range(2):
        r = await http_client.post(
            "/agent/run", json={"task": "spin", "mode": "auto", "max_steps": 100}
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
        assert any(e["phase"] == "awaiting_approval" for e in events)
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
