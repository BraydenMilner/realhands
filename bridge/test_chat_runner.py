"""Offline tests for the Chat (Ask) mode runner.

Mirrors the pattern in test_agent_runner.py: the vision package is faked in
sys.modules, litellm is monkeypatched, and the executor is faked. Covers:

1. A plain answer (no tool calls) streams a type:"chat" assistant event.
2. A tool-call to read_current_page is executed then the model answers.
3. web_search tool is NOT registered when REALHANDS_SEARCH_API_KEY is unset.
4. An exception publishes a role:"error" chat event.

Run from this directory:
    pytest test_chat_runner.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types

import httpx
import pytest
import pytest_asyncio


class _FakeActionDecision:
    def __init__(self, action="done", **_kw):
        self.action = action


class _FakeModelConfig:
    def __init__(self, model=None, api_key=None, base_url=None):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url


class _FakeVisionConfig:
    def __init__(self, models=None, **_kw):
        self.models = models or []


class _FakeStepHistoryItem:
    def __init__(self, **_kw):
        pass


def _install_fake_vision():
    existing = sys.modules.get("vision")
    if existing is not None and hasattr(existing, "ModelConfig"):
        return existing
    mod = types.ModuleType("vision")
    mod.ActionDecision = _FakeActionDecision
    mod.ModelConfig = _FakeModelConfig
    mod.VisionConfig = _FakeVisionConfig
    mod.StepHistoryItem = _FakeStepHistoryItem

    async def _default(**_kw):
        return _FakeActionDecision()

    mod.decide_action = _default
    sys.modules["vision"] = mod
    return mod


_FAKE_VISION = _install_fake_vision()

if "bridge" not in sys.modules:
    from bridge import app  # noqa: E402
else:
    app = sys.modules["bridge"].app


class _FakeExecutor:
    def __init__(self):
        self.calls = []
        self.ws = object()

    async def call(self, method, params=None, *, timeout=30.0):
        self.calls.append((method, params or {}))
        if method == "get_page_text":
            return {
                "text": "Hello world page content",
                "title": "Test Page",
                "url": "https://example.test/",
            }
        if method == "screenshot":
            return {
                "base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
                "url": "https://example.test/",
                "device_pixel_ratio": 1.0,
            }
        return {}


class _ChatEventCollector:
    def __init__(self):
        self.events = []
        self._task = None

    async def __aenter__(self):
        broker = app.state.broker
        started = asyncio.Event()

        async def _pump():
            agen = broker.subscribe(last_id=broker.last_seq)
            started.set()
            async for env in agen:
                if env.get("type") == "chat":
                    self.events.append(env)

        self._task = asyncio.create_task(_pump())
        await started.wait()
        return self

    async def __aexit__(self, *exc):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def wait_for_done(self, timeout=5.0):
        async def _spin():
            while not any(e.get("done") for e in self.events):
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(_spin(), timeout=timeout)
        except asyncio.TimeoutError:
            pass


@pytest.fixture(autouse=True)
def clean_state():
    app.state.executors.clear()
    app.state.agent_runs.clear()
    yield
    for rec in list(app.state.agent_runs.values()):
        for ev_name in ("stop_event", "approve_event", "reply_event"):
            ev = rec.get(ev_name)
            if ev is not None:
                try:
                    ev.set()
                except Exception:
                    pass
        t = rec.get("task")
        if t is not None and not t.done():
            try:
                t.cancel()
            except RuntimeError:
                pass
    app.state.agent_runs.clear()
    app.state.executors.clear()


@pytest.fixture(autouse=True)
def _clean_search_env(monkeypatch):
    monkeypatch.delenv("REALHANDS_SEARCH_API_KEY", raising=False)


@pytest_asyncio.fixture
async def http_client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class _FakeToolCall:
    def __init__(self, name, arguments="{}"):
        self.id = "tc_test"
        self.function = types.SimpleNamespace(name=name, arguments=arguments)


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self):
        d = {"role": "assistant"}
        if self.content:
            d["content"] = self.content
        if self.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in self.tool_calls
            ]
        return d


class _FakeChoice:
    def __init__(self, message, finish_reason="stop"):
        self.message = message
        self.finish_reason = finish_reason


class _FakeResponse:
    def __init__(self, choices):
        self.choices = choices


@pytest.mark.asyncio
async def test_plain_answer_streams_chat_event(http_client, monkeypatch):
    """A plain LLM answer (no tool calls) publishes a type:"chat" assistant
    event with the text and done:true."""
    import chat_runner
    import litellm

    captured_tools = []

    async def _fake_acompletion(*args, **kwargs):
        captured_tools.append(kwargs.get("tools", []))
        return _FakeResponse(
            [_FakeChoice(_FakeMessage(content="The sky is blue."), "stop")]
        )

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    async with _ChatEventCollector() as col:
        r = await http_client.post(
            "/agent/ask", json={"message": "What color is the sky?"}
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        await col.wait_for_done()

    done_events = [e for e in col.events if e.get("done")]
    assert len(done_events) >= 1
    assert done_events[0]["type"] == "chat"
    assert done_events[0]["role"] == "assistant"
    assert "sky" in done_events[0]["text"].lower()


@pytest.mark.asyncio
async def test_tool_call_read_page_then_answer(http_client, monkeypatch):
    """A tool-call to read_current_page is executed, then the model's final
    answer is published."""
    import chat_runner
    import litellm

    call_count = {"n": 0}

    async def _fake_acompletion(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _FakeResponse(
                [
                    _FakeChoice(
                        _FakeMessage(
                            tool_calls=[_FakeToolCall("read_current_page")]
                        ),
                        "tool_calls",
                    )
                ]
            )
        return _FakeResponse(
            [_FakeChoice(_FakeMessage(content="The page says Hello world."), "stop")]
        )

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    app.state.executors["default"] = _FakeExecutor()

    async with _ChatEventCollector() as col:
        r = await http_client.post(
            "/agent/ask", json={"message": "What does this page say?"}
        )
        assert r.status_code == 200
        await col.wait_for_done()

    tool_events = [e for e in col.events if e.get("role") == "tool"]
    assert tool_events, "expected a tool progress event"
    assert "read_current_page" in tool_events[0]["text"]

    done_events = [e for e in col.events if e.get("done")]
    assert done_events
    assert done_events[0]["role"] == "assistant"
    assert "hello" in done_events[0]["text"].lower()

    ex = app.state.executors["default"]
    assert any(m == "get_page_text" for (m, _p) in ex.calls)


@pytest.mark.asyncio
async def test_web_search_not_registered_without_api_key(http_client, monkeypatch):
    """When REALHANDS_SEARCH_API_KEY is unset, the web_search tool is NOT
    in the tools list."""
    import litellm

    monkeypatch.delenv("REALHANDS_SEARCH_API_KEY", raising=False)

    captured_tools = []

    async def _fake_acompletion(*args, **kwargs):
        captured_tools.append(kwargs.get("tools", []))
        return _FakeResponse(
            [_FakeChoice(_FakeMessage(content="No search needed."), "stop")]
        )

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    async with _ChatEventCollector() as col:
        r = await http_client.post("/agent/ask", json={"message": "hi"})
        assert r.status_code == 200
        await col.wait_for_done()

    assert captured_tools, "litellm.acompletion was never called"
    tools = captured_tools[0]
    tool_names = [
        t.get("function", {}).get("name") for t in tools if t.get("type") == "function"
    ]
    assert "web_search" not in tool_names
    assert "read_current_page" in tool_names
    assert "view_screenshot" in tool_names


@pytest.mark.asyncio
async def test_exception_publishes_error_event(http_client, monkeypatch):
    """An exception during the chat turn publishes a role:"error" chat event."""
    import litellm

    async def _fake_acompletion(*args, **kwargs):
        raise RuntimeError("model is broken")

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    async with _ChatEventCollector() as col:
        r = await http_client.post(
            "/agent/ask", json={"message": "break it"}
        )
        assert r.status_code == 200
        await col.wait_for_done()

    done_events = [e for e in col.events if e.get("done")]
    assert done_events
    assert done_events[0]["role"] == "error"
    assert "broken" in done_events[0]["text"].lower()
