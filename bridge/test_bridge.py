"""Smoke tests for the realhands Bridge (SWARM PROTOCOL v1).

Covers:
- Registry routing: two fake executors with distinct browser_id; /call routes
  to the right one; omitted browser_id resolves to "default"/sole.
- 404 unknown_browser for a browser_id that isn't registered.
- 503 when no executor is connected at all.
- /executors lists every connected browser.
- /call success and error paths (executor responds with {result} or {error}).
- /register returns a 200 HTML page.
- keepalive/heartbeat events are NOT published to /events.
- /credentials/read: 404 on missing key, success path with stub vault.
- /spawn maps to a (mocked) SwarmSpawner.

Run from this directory:
    pytest test_bridge.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time as _time

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bridge import app


# ---------- fixtures ----------


@pytest.fixture(autouse=True)
def clean_security_env():
    old_token = os.environ.pop("REALHANDS_BRIDGE_TOKEN", None)
    old_vault = os.environ.pop("REALHANDS_VAULT_API_ENABLED", None)
    yield
    if old_token is None:
        os.environ.pop("REALHANDS_BRIDGE_TOKEN", None)
    else:
        os.environ["REALHANDS_BRIDGE_TOKEN"] = old_token
    if old_vault is None:
        os.environ.pop("REALHANDS_VAULT_API_ENABLED", None)
    else:
        os.environ["REALHANDS_VAULT_API_ENABLED"] = old_vault


@pytest.fixture(autouse=True)
def reset_vault():
    """Each test starts with no vault unless it sets one."""
    app.state.vault = None
    yield
    app.state.vault = None


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure the executor registry is empty before/after each test so a
    leaked connection from one test can't bleed into the next."""
    app.state.executors.clear()
    app.state.spawn_nonces.clear()
    app.state.executor_tokens.clear()
    yield
    app.state.executors.clear()
    app.state.spawn_nonces.clear()
    app.state.executor_tokens.clear()


@pytest_asyncio.fixture
async def http_client():
    """An httpx.AsyncClient pointed at the in-process app."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class _FakeExecutor:
    """A tiny background thread acting as the Chrome extension over WS.

    Uses `fastapi.testclient.TestClient` to open a real WebSocket against the
    in-process app. On connect it sends an `executor_ready` carrying its
    `browser_id`, so the bridge registers it in app.state.executors under that
    id. The thread then blocks in `ws.receive_text()`, applies a per-method
    handler, and writes back `{id, result}` or `{id, error}`. Bridge keepalive
    pings (which have no "id"/"method") are ignored.
    """

    def __init__(self, handlers, browser_id=None, token=None, headers=None):
        self.handlers = handlers
        self.browser_id = browser_id  # None -> registers as "default"
        self.token = token
        self.headers = headers or {}
        self.ready = threading.Event()
        self.done = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: TestClient | None = None
        self._ws = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self.ready.wait(timeout=5), "fake executor failed to connect"

    def _run(self):
        client = TestClient(app)
        self._client = client
        try:
            path = "/"
            if self.token is not None:
                path = f"/?token={self.token}"
            with client.websocket_connect(path, headers=self.headers) as ws:
                self._ws = ws
                # Announce ourselves so the bridge registers us under browser_id.
                ready_msg = {"event": "executor_ready", "version": "test-1"}
                if self.browser_id is not None:
                    ready_msg["browser_id"] = self.browser_id
                ws.send_text(json.dumps(ready_msg))
                self.ready.set()
                while True:
                    try:
                        text = ws.receive_text()
                    except Exception:
                        break
                    try:
                        msg = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    # Ignore bridge keepalive pings and anything without a request.
                    if "id" not in msg or "method" not in msg:
                        continue
                    method = msg.get("method")
                    handler = self.handlers.get(method)
                    if handler is None:
                        ws.send_text(
                            json.dumps(
                                {
                                    "id": msg["id"],
                                    "error": {
                                        "code": "unsupported_method",
                                        "message": method or "",
                                    },
                                }
                            )
                        )
                        continue
                    resp = handler(msg.get("params") or {})
                    if "error" in resp:
                        ws.send_text(json.dumps({"id": msg["id"], **resp}))
                    else:
                        ws.send_text(
                            json.dumps({"id": msg["id"], "result": resp["result"]})
                        )
        except Exception:
            pass
        finally:
            self.done.set()
            self.ready.set()

    def close(self):
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)


def _resolved_id(browser_id):
    return browser_id if browser_id is not None else "default"


@pytest.fixture
def fake_executor():
    """Yields a callable (handlers, browser_id=None) -> _FakeExecutor that
    starts the fake executor and waits for it to appear in the registry under
    its resolved browser_id."""
    started: list[_FakeExecutor] = []

    def _start(handlers, browser_id=None, token=None, headers=None):
        fx = _FakeExecutor(handlers, browser_id=browser_id, token=token, headers=headers)
        fx.start()
        # The TestClient WS path runs in a thread; registration happens in the
        # FastAPI handler after executor_ready — wait for the registry slot.
        want = _resolved_id(browser_id)
        for _ in range(60):
            if want in app.state.executors:
                break
            _time.sleep(0.05)
        assert want in app.state.executors, f"bridge did not register {want}"
        started.append(fx)
        return fx

    yield _start

    for fx in started:
        fx.close()
    # Give the bridge a moment to deregister.
    for _ in range(40):
        if not app.state.executors:
            break
        _time.sleep(0.05)


# ---------- registry routing ----------


@pytest.mark.asyncio
async def test_rest_auth_unset_allows_back_compat(http_client):
    r = await http_client.get("/health")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_rest_auth_token_required_when_set(http_client):
    os.environ["REALHANDS_BRIDGE_TOKEN"] = "secret"

    missing = await http_client.get("/health")
    assert missing.status_code == 401

    wrong = await http_client.get("/health", headers={"X-RealHands-Token": "nope"})
    assert wrong.status_code == 401

    ok = await http_client.get("/health", headers={"X-RealHands-Token": "secret"})
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_rest_auth_protects_spawn_when_token_set(http_client, mock_spawner):
    os.environ["REALHANDS_BRIDGE_TOKEN"] = "secret"

    missing = await http_client.post("/spawn", json={"browser_id": "alpha"})
    assert missing.status_code == 401
    assert mock_spawner.spawned == []

    ok = await http_client.post(
        "/spawn",
        json={"browser_id": "alpha"},
        headers={"X-RealHands-Token": "secret"},
    )
    assert ok.status_code == 200
    assert mock_spawner.spawned[0]["browser_id"] == "alpha"


def test_ws_accepts_no_origin_when_unset():
    with TestClient(app) as client:
        with client.websocket_connect("/") as ws:
            ws.send_text(json.dumps({"event": "executor_ready", "browser_id": "alpha"}))


def test_ws_rejects_null_origin_when_unset():
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/", headers={"Origin": "null"}):
                pass


def test_ws_accepts_chrome_extension_origin():
    with TestClient(app) as client:
        with client.websocket_connect(
            "/",
            headers={"Origin": "chrome-extension://abcdefghijklmnop"},
        ) as ws:
            ws.send_text(json.dumps({"event": "executor_ready", "browser_id": "alpha"}))


def test_ws_rejects_http_origin():
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/",
                headers={"Origin": "https://evil.example"},
            ):
                pass


def test_ws_auth_token_required_when_set():
    os.environ["REALHANDS_BRIDGE_TOKEN"] = "secret"
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/") as ws:
                ws.send_text(json.dumps({"event": "executor_ready", "browser_id": "alpha"}))
                ws.receive_text()
                ws.receive_text()
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/") as ws:
                ws.send_text(
                    json.dumps(
                        {
                            "event": "executor_ready",
                            "browser_id": "alpha",
                            "bridge_token": "nope",
                        }
                    )
                )
                ws.receive_text()
                ws.receive_text()
        with client.websocket_connect("/") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "event": "executor_ready",
                        "browser_id": "alpha",
                        "bridge_token": "secret",
                    }
                )
            )


def test_ws_rejects_token_in_query_when_set():
    os.environ["REALHANDS_BRIDGE_TOKEN"] = "secret"
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/?token=secret") as ws:
                ws.send_text(json.dumps({"event": "executor_ready", "browser_id": "alpha"}))
                ws.receive_text()
                ws.receive_text()


def test_spawned_ws_can_reconnect_with_executor_token_when_bridge_token_set():
    os.environ["REALHANDS_BRIDGE_TOKEN"] = "secret"
    nonce = "spawn-nonce"
    app.state.spawn_nonces[nonce] = "alpha"
    with TestClient(app) as client:
        with client.websocket_connect("/") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "event": "executor_ready",
                        "browser_id": "alpha",
                        "registration_nonce": nonce,
                    }
                )
            )
            msg = json.loads(ws.receive_text())
            executor_token = msg["set_state"]["executor_token"]
            assert msg["set_state"]["registration_nonce"] == ""
            assert executor_token
            assert nonce not in app.state.spawn_nonces

    with TestClient(app) as client:
        with client.websocket_connect("/") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "event": "executor_ready",
                        "browser_id": "alpha",
                        "executor_token": executor_token,
                    }
                )
            )
            for _ in range(60):
                if "alpha" in app.state.executors:
                    break
                _time.sleep(0.05)
            assert "alpha" in app.state.executors


@pytest.mark.asyncio
async def test_call_routes_by_browser_id(http_client, fake_executor):
    """Two executors with different browser_id; /call routes to the right one."""
    fake_executor(
        {"who": lambda p: {"result": {"id": "alpha"}}}, browser_id="alpha"
    )
    fake_executor(
        {"who": lambda p: {"result": {"id": "beta"}}}, browser_id="beta"
    )

    r1 = await http_client.post("/call", json={"browser_id": "alpha", "method": "who"})
    assert r1.status_code == 200, r1.text
    assert r1.json()["result"] == {"id": "alpha"}

    r2 = await http_client.post("/call", json={"browser_id": "beta", "method": "who"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["result"] == {"id": "beta"}


@pytest.mark.asyncio
async def test_call_omitted_browser_id_uses_default(http_client, fake_executor):
    """browser_id omitted resolves to 'default' when it's registered, even with
    another browser also connected."""
    fake_executor(
        {"who": lambda p: {"result": {"id": "default"}}}, browser_id=None
    )
    fake_executor(
        {"who": lambda p: {"result": {"id": "other"}}}, browser_id="other"
    )
    r = await http_client.post("/call", json={"method": "who"})
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {"id": "default"}


@pytest.mark.asyncio
async def test_call_omitted_browser_id_uses_sole(http_client, fake_executor):
    """browser_id omitted resolves to the sole connected executor when there's
    exactly one and it isn't 'default'."""
    fake_executor(
        {"who": lambda p: {"result": {"id": "only"}}}, browser_id="only"
    )
    r = await http_client.post("/call", json={"method": "who"})
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {"id": "only"}


@pytest.mark.asyncio
async def test_call_unknown_browser_404(http_client, fake_executor):
    """A browser_id that isn't registered -> 404 {error:{code:unknown_browser}}."""
    fake_executor({"ping": lambda p: {"result": {"pong": True}}}, browser_id="alpha")
    r = await http_client.post(
        "/call", json={"browser_id": "nope", "method": "ping"}
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "unknown_browser"


@pytest.mark.asyncio
async def test_call_no_executor_503(http_client):
    """No executor connected at all -> 503."""
    r = await http_client.post("/call", json={"method": "ping"})
    assert r.status_code == 503


# ---------- /executors ----------


@pytest.mark.asyncio
async def test_executors_lists_all(http_client, fake_executor):
    fake_executor({"ping": lambda p: {"result": {}}}, browser_id="alpha")
    fake_executor({"ping": lambda p: {"result": {}}}, browser_id="beta")
    r = await http_client.get("/executors")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {b["browser_id"] for b in body["browsers"]}
    assert ids == {"alpha", "beta"}
    for b in body["browsers"]:
        assert b["connected"] is True
        assert b["version"] == "test-1"
        assert "uptime_s" in b
        assert "current_task" in b


# ---------- /call success/error ----------


@pytest.mark.asyncio
async def test_call_success(http_client, fake_executor):
    fake_executor(
        {"ping": lambda params: {"result": {"pong": True, "echoed": params}}},
        browser_id="alpha",
    )
    r = await http_client.post(
        "/call", json={"browser_id": "alpha", "method": "ping", "params": {"x": 1}}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["pong"] is True
    assert body["result"]["echoed"] == {"x": 1}


@pytest.mark.asyncio
async def test_call_error_path_returns_200(http_client, fake_executor):
    fake_executor(
        {"boom": lambda params: {"error": {"code": "boom_code", "message": "exploded"}}},
        browser_id="alpha",
    )
    r = await http_client.post("/call", json={"browser_id": "alpha", "method": "boom"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["error"]["code"] == "boom_code"
    assert body["error"]["message"] == "exploded"


# ---------- /register ----------


@pytest.mark.asyncio
async def test_register_returns_200_html(http_client):
    r = await http_client.get("/register", params={"browser_id": "alpha"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "alpha" in r.text
    assert "script-src" in r.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_register_no_id_defaults(http_client):
    r = await http_client.get("/register")
    assert r.status_code == 200
    assert "default" in r.text


@pytest.mark.asyncio
async def test_register_escapes_and_validates_browser_id(http_client):
    r = await http_client.get("/register", params={"browser_id": "<script>alert(1)</script>"})
    assert r.status_code == 400

    ok = await http_client.get("/register", params={"browser_id": "alpha-1"})
    assert ok.status_code == 200
    assert "alpha-1" in ok.text


# ---------- /events SSE ----------


@pytest.mark.asyncio
async def test_events_sse_delivers_published_event():
    """Subscribe to /events over a real port, publish, assert delivery.

    httpx's ASGITransport buffers the whole response body, so SSE — which never
    closes — can't be exercised through it. Spin up a real uvicorn on an
    ephemeral port for this test only.
    """
    import socket
    import uvicorn

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

    broker = app.state.broker
    received: dict = {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as ac:
            async with ac.stream("GET", f"http://127.0.0.1:{port}/events") as r:
                assert r.status_code == 200

                async def reader():
                    # Replayed/other events may precede ours (the broker replays
                    # its ring buffer on subscribe). Read until we see page_ready.
                    async for line in r.aiter_lines():
                        if line.startswith("data: "):
                            env = json.loads(line[6:])
                            if env.get("event") == "page_ready":
                                received.update(env)
                                return

                reader_task = asyncio.create_task(reader())
                await asyncio.sleep(0.2)
                await broker.publish(
                    {
                        "event": "page_ready",
                        "tab_id": 42,
                        "url": "https://example.com/",
                        "browser_id": "alpha",
                    }
                )
                try:
                    await asyncio.wait_for(reader_task, timeout=3.0)
                except asyncio.TimeoutError:
                    reader_task.cancel()
                    raise AssertionError("SSE event never arrived")
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=3.0)
        except asyncio.TimeoutError:
            server_task.cancel()

    assert received.get("event") == "page_ready"
    assert received.get("tab_id") == 42
    assert received.get("browser_id") == "alpha"


@pytest.mark.asyncio
async def test_event_tagged_with_browser_id():
    """An event arriving on an executor connection is tagged with that
    connection's browser_id before being published."""
    from event_broker import EventBroker
    from executor_client import ExecutorClient

    broker = EventBroker()
    executor = ExecutorClient(broker)
    # Register the connection's browser_id via executor_ready.
    await executor.on_message(
        json.dumps({"event": "executor_ready", "browser_id": "gamma", "version": "x"})
    )
    assert executor.browser_id == "gamma"

    seen: list[dict] = []

    async def collect():
        async for env in broker.subscribe():
            seen.append(env)
            if len(seen) >= 2:
                return

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.05)
    await executor.on_message(
        json.dumps({"event": "page_ready", "tab_id": 7, "url": "https://e.com/"})
    )
    await asyncio.wait_for(task, timeout=2.0)
    page = next(e for e in seen if e.get("event") == "page_ready")
    assert page["browser_id"] == "gamma"


@pytest.mark.asyncio
async def test_keepalive_event_not_published():
    """keepalive/heartbeat events must NOT reach the broker — otherwise they
    evict real events from the 1000-slot ring buffer. on_message should drop
    them before publish. (These are what the bridge keepalive sends; they must
    never surface on /events.)"""
    from event_broker import EventBroker
    from executor_client import ExecutorClient

    broker = EventBroker()
    executor = ExecutorClient(broker)
    seq_before = broker.last_seq

    await executor.on_message(json.dumps({"event": "keepalive", "ts": 123.0}))
    await executor.on_message(json.dumps({"event": "heartbeat"}))
    assert broker.last_seq == seq_before, "keepalive/heartbeat must not be published"
    assert executor.last_event is None, "keepalive must not become last_event"

    await executor.on_message(
        json.dumps({"event": "page_ready", "tab_id": 7, "url": "https://example.com/"})
    )
    assert broker.last_seq == seq_before + 1
    assert executor.last_event is not None
    assert executor.last_event["event"] == "page_ready"


# ---------- /credentials/read ----------


class _StubVault:
    def __init__(self, store):
        self._store = store

    def get(self, platform, field=None):
        rec = self._store.get(platform)
        if rec is None:
            return None
        if field is None:
            return rec
        return rec.get(field) if isinstance(rec, dict) else None


@pytest.mark.asyncio
async def test_credentials_read_disabled_by_default(http_client):
    app.state.vault = _StubVault({})
    r = await http_client.post(
        "/credentials/read", json={"platform": "example.com", "field": "totp"}
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "vault_api_disabled"


@pytest.mark.asyncio
async def test_credentials_read_404_when_missing(http_client):
    os.environ["REALHANDS_VAULT_API_ENABLED"] = "1"
    os.environ["REALHANDS_BRIDGE_TOKEN"] = "secret"
    app.state.vault = _StubVault({})
    r = await http_client.post(
        "/credentials/read",
        json={"platform": "example.com", "field": "totp"},
        headers={"X-RealHands-Token": "secret"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_credentials_read_success(http_client):
    os.environ["REALHANDS_VAULT_API_ENABLED"] = "1"
    os.environ["REALHANDS_BRIDGE_TOKEN"] = "secret"
    app.state.vault = _StubVault({"example.com": {"totp": "ABCDEF123456"}})
    r = await http_client.post(
        "/credentials/read",
        json={"platform": "example.com", "field": "totp"},
        headers={"X-RealHands-Token": "secret"},
    )
    assert r.status_code == 200
    assert r.json() == {"value": "ABCDEF123456"}


@pytest.mark.asyncio
async def test_credentials_read_enabled_requires_token_when_token_set(http_client):
    os.environ["REALHANDS_VAULT_API_ENABLED"] = "1"
    os.environ["REALHANDS_BRIDGE_TOKEN"] = "secret"
    app.state.vault = _StubVault({"example.com": {"totp": "ABCDEF123456"}})

    missing = await http_client.post(
        "/credentials/read", json={"platform": "example.com", "field": "totp"}
    )
    assert missing.status_code == 401

    ok = await http_client.post(
        "/credentials/read",
        json={"platform": "example.com", "field": "totp"},
        headers={"X-RealHands-Token": "secret"},
    )
    assert ok.status_code == 200
    assert ok.json() == {"value": "ABCDEF123456"}


@pytest.mark.asyncio
async def test_credentials_read_enabled_requires_token_even_if_bridge_auth_unset(http_client):
    os.environ["REALHANDS_VAULT_API_ENABLED"] = "1"
    app.state.vault = _StubVault({"example.com": {"totp": "ABCDEF123456"}})
    r = await http_client.post(
        "/credentials/read", json={"platform": "example.com", "field": "totp"}
    )
    assert r.status_code == 401


# ---------- /spawn (mocked spawner) ----------


class _MockSpawner:
    def __init__(self):
        self.spawned: list[dict] = []
        self.closed: list[str] = []
        self.spawn_exc: Exception | None = None

    async def spawn(
        self,
        browser_id=None,
        profile=None,
        persistent=False,
        start_url=None,
        registration_nonce=None,
    ):
        if self.spawn_exc is not None:
            raise self.spawn_exc
        rec = {"browser_id": browser_id, "pid": 4242}
        self.spawned.append(
            {
                "browser_id": browser_id,
                "profile": profile,
                "persistent": persistent,
                "start_url": start_url,
                "registration_nonce": registration_nonce,
            }
        )
        return rec

    async def close(self, browser_id):
        self.closed.append(browser_id)
        return True

    def list(self):
        return [
            {
                "browser_id": "alpha",
                "pid": 4242,
                "profile": "alpha",
                "persistent": False,
                "alive": True,
            }
        ]


@pytest.fixture
def mock_spawner():
    prior = app.state.spawner
    spawner = _MockSpawner()
    app.state.spawner = spawner
    yield spawner
    app.state.spawner = prior


@pytest.mark.asyncio
async def test_spawn_calls_spawner(http_client, mock_spawner):
    r = await http_client.post(
        "/spawn",
        json={
            "browser_id": "alpha_1.test-2",
            "profile": "acct_1.test-2",
            "persistent": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["browser_id"] == "alpha_1.test-2"
    assert body["pid"] == 4242
    assert mock_spawner.spawned[0]["browser_id"] == "alpha_1.test-2"
    assert mock_spawner.spawned[0]["profile"] == "acct_1.test-2"
    assert mock_spawner.spawned[0]["persistent"] is True
    assert mock_spawner.spawned[0]["registration_nonce"]


@pytest.mark.asyncio
async def test_spawn_generates_id_when_omitted(http_client, mock_spawner):
    r = await http_client.post("/spawn", json={})
    assert r.status_code == 200, r.text
    gen_id = mock_spawner.spawned[0]["browser_id"]
    assert gen_id is not None and gen_id != ""


@pytest.mark.asyncio
async def test_spawn_503_when_no_spawner(http_client):
    prior = app.state.spawner
    app.state.spawner = None
    try:
        r = await http_client.post("/spawn", json={"browser_id": "alpha"})
        assert r.status_code == 503
    finally:
        app.state.spawner = prior


@pytest.mark.asyncio
async def test_spawn_rejects_unsafe_browser_id(http_client, mock_spawner):
    for browser_id in ("../x", "has/slash", "..", "a" * 65):
        r = await http_client.post("/spawn", json={"browser_id": browser_id})
        assert r.status_code == 400, browser_id
    assert mock_spawner.spawned == []


@pytest.mark.asyncio
async def test_spawn_rejects_unsafe_profile(http_client, mock_spawner):
    for profile in ("../x", "has/slash", "..", "a" * 65):
        r = await http_client.post(
            "/spawn", json={"browser_id": "alpha", "profile": profile, "persistent": True}
        )
        assert r.status_code == 400, profile
    assert mock_spawner.spawned == []


@pytest.mark.asyncio
async def test_spawn_value_error_maps_to_400(http_client, mock_spawner):
    mock_spawner.spawn_exc = ValueError("bad profile")
    r = await http_client.post("/spawn", json={"browser_id": "alpha"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_close_browser(http_client, mock_spawner):
    r = await http_client.post("/browsers/alpha/close")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["closed"] is True
    assert body["browser_id"] == "alpha"
    assert mock_spawner.closed == ["alpha"]


@pytest.mark.asyncio
async def test_list_browsers(http_client, mock_spawner):
    r = await http_client.get("/browsers")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    assert body[0]["browser_id"] == "alpha"
    assert body[0]["alive"] is True


# ---------- health / executor status ----------


@pytest.mark.asyncio
async def test_health(http_client):
    r = await http_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["browsers_connected"] == 0
    assert "uptime_s" in body


@pytest.mark.asyncio
async def test_health_counts_browsers(http_client, fake_executor):
    fake_executor({"ping": lambda p: {"result": {}}}, browser_id="alpha")
    r = await http_client.get("/health")
    assert r.status_code == 200
    assert r.json()["browsers_connected"] == 1


@pytest.mark.asyncio
async def test_executor_status_shape(http_client, fake_executor):
    fake_executor({"ping": lambda p: {"result": {}}}, browser_id="alpha")
    r = await http_client.get("/executor", params={"browser_id": "alpha"})
    assert r.status_code == 200
    body = r.json()
    for key in ("connected", "version", "last_event", "current_task", "uptime_s"):
        assert key in body
    assert body["browser_id"] == "alpha"


@pytest.mark.asyncio
async def test_executor_status_503_when_none(http_client):
    r = await http_client.get("/executor")
    assert r.status_code == 503
