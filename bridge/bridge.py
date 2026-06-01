"""realhands Bridge — the local control plane.

A FastAPI app that mediates between the Chrome extension (WebSocket) and the
controlling agent (REST). SWARM PROTOCOL v1: MANY concurrent executor connections,
one per distinct browser_id, multiplexed through a registry. A single browser
with no browser_id configured still works as browser_id="default". Single bind
on 127.0.0.1:7878.

Run with:
    uvicorn bridge:app --host 127.0.0.1 --port 7878
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, status
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from event_broker import EventBroker
from executor_client import ExecutorClient, ExecutorError

# Default browser_id for a browser that never configures one (back-compat).
DEFAULT_BROWSER_ID = "default"

# ---------- logging ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("agent_bridge")

# ---------- auth / input validation ----------

_AUTH_WARNING_EMITTED = False
_SAFE_SPAWN_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _bridge_token() -> Optional[str]:
    token = os.environ.get("REALHANDS_BRIDGE_TOKEN")
    return token if token else None


def _vault_api_enabled() -> bool:
    return os.environ.get("REALHANDS_VAULT_API_ENABLED") == "1"


def _warn_if_running_without_auth() -> None:
    global _AUTH_WARNING_EMITTED
    if _bridge_token() is not None or _AUTH_WARNING_EMITTED:
        return
    _AUTH_WARNING_EMITTED = True
    log.warning(
        "bridge running without auth — set REALHANDS_BRIDGE_TOKEN on shared/multi-user hosts"
    )


def _ws_origin_allowed(origin: Optional[str]) -> bool:
    if origin is None or origin == "" or origin == "null":
        return True
    return origin.startswith("chrome-extension://")


def _validate_spawn_name(value: str, field_name: str) -> str:
    if not _SAFE_SPAWN_NAME_RE.fullmatch(value) or ".." in value:
        raise ValueError(
            f"invalid {field_name}: must match {_SAFE_SPAWN_NAME_RE.pattern} "
            "and not contain '..'"
        )
    return value

# ---------- money-action guard (defense in depth at the bridge) ----------
# Canonical money-token list — IDENTICAL to the set pinned across the runtime
# (models.py, decide.py, prompts.py,
# background.js). realhands never clicks redeem/deposit/transfer; upstream layers
# may refuse these too, but the bridge enforces it again so a buggy/compromised caller
# can't drive a money action through the executor.
MONEY_TOKENS = [
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

# Methods that actuate the page (could trigger a money action). Read-only
# methods (screenshot, get_page_info, wait_for_element, tabs_list, ...) are NOT
# guarded — they only observe state.
_MONEY_GUARDED_METHODS = frozenset(
    {"click_at", "click_selector", "type", "key_press"}
)

# Param fields that can carry a target string a token would appear in.
_MONEY_PARAM_FIELDS = ("selector", "target_selector", "text", "url")


def _is_money_action(method: str, params: dict) -> bool:
    """True if `method` actuates the page AND any money token appears in a
    relevant param field. Mirrors the upstream money-action check
    (lowercase substring match), scoped to the param fields the bridge sees.
    """
    if method not in _MONEY_GUARDED_METHODS:
        return False
    params = params or {}
    for field in _MONEY_PARAM_FIELDS:
        value = params.get(field)
        if not isinstance(value, str):
            continue
        lowered = value.lower()
        if any(token in lowered for token in MONEY_TOKENS):
            return True
    return False


# ---------- vault import (sibling file, may not exist yet) ----------

try:
    from vault import VaultManager  # type: ignore
except ImportError:  # vault.py not written yet — keep bridge importable.
    VaultManager = None  # type: ignore[assignment]
    log.warning("vault.py not importable — /credentials/read will 503 until vault lands")


# ---------- spawn manager import (sibling file, may not exist yet) ----------
# SwarmSpawner manages Chrome processes on the machine where the bridge runs
# (a host with a graphical display / X server). If spawn_manager.py isn't present, keep the
# bridge importable and make /spawn return 503.

try:
    from spawn_manager import SwarmSpawner  # type: ignore
except ImportError:  # spawn_manager.py not written yet — keep bridge importable.
    SwarmSpawner = None  # type: ignore[assignment]
    log.warning(
        "spawn_manager.py not importable — /spawn will 503 until spawn_manager lands"
    )


# ---------- pydantic request bodies ----------


class CallBody(BaseModel):
    browser_id: Optional[str] = None
    method: str
    params: dict = Field(default_factory=dict)
    timeout: float = 30.0


class SequenceStep(BaseModel):
    method: str
    params: dict = Field(default_factory=dict)


class SequenceBody(BaseModel):
    browser_id: Optional[str] = None
    steps: list[SequenceStep]
    continue_on_error: bool = False
    timeout: float = 30.0


class TaskBody(BaseModel):
    browser_id: Optional[str] = None
    task: Optional[dict] = None


class CredentialBody(BaseModel):
    platform: str
    field: str


class SpawnBody(BaseModel):
    browser_id: Optional[str] = None
    profile: Optional[str] = None
    persistent: bool = False
    start_url: Optional[str] = None


# ---------- app factory ----------


def _init_state(app: FastAPI) -> None:
    """Initialize app.state. Called at import time so tests that bypass
    lifespan still see populated state.

    SWARM PROTOCOL v1: instead of a single executor, we keep a registry
    `executors: dict[browser_id -> ExecutorClient]`. Each live WS connection
    registers itself under its browser_id on the first executor_ready.
    """
    app.state.broker = EventBroker()
    app.state.executors = {}  # browser_id -> ExecutorClient
    app.state.started_at = time.time()
    app.state.vault = None
    if VaultManager is not None:
        try:
            app.state.vault = VaultManager()
            log.info("vault initialized: %s", type(app.state.vault).__name__)
        except Exception as exc:
            log.warning("vault init failed: %s", exc)

    # SwarmSpawner: construct from env. Kept on app.state.spawner; None if the
    # module isn't importable (then /spawn returns 503).
    app.state.spawner = None
    if SwarmSpawner is not None:
        try:
            profiles_dir = os.environ.get(
                "REALHANDS_PROFILES_DIR", str(Path.home() / ".config" / "realhands-swarm")
            )
            xauthority = os.environ.get(
                "XAUTHORITY", str(Path.home() / ".Xauthority")
            )
            app.state.spawner = SwarmSpawner(
                chrome_bin=os.environ.get("CHROME_BIN", "google-chrome"),
                profiles_dir=profiles_dir,
                bridge_port=int(os.environ.get("BRIDGE_PORT", "7878")),
                display=os.environ.get("DISPLAY", ":10"),
                xauthority=xauthority,
            )
            log.info("spawner initialized: %s", type(app.state.spawner).__name__)
        except Exception as exc:
            log.warning("spawner init failed: %s", exc)


class _ResolveError(Exception):
    """Carries the exact HTTP status + JSON body for a browser_id that can't be
    resolved, so endpoints can surface the PINS-pinned response bodies verbatim
    (e.g. 404 {error:{code:"unknown_browser"}}) rather than FastAPI's
    {detail:...} wrapper.
    """

    def __init__(self, status_code: int, content: dict):
        super().__init__(content.get("error", {}).get("code", str(status_code)))
        self.status_code = status_code
        self.content = content

    def response(self) -> JSONResponse:
        return JSONResponse(status_code=self.status_code, content=self.content)


def _resolve_executor(browser_id: Optional[str]) -> ExecutorClient:
    """Resolve a browser_id to a connected ExecutorClient per the PINS.

    Rules:
      - none connected at all          -> HTTP 503
      - browser_id omitted             -> "default" if registered, else the
                                          sole connected executor
      - browser_id given but unknown   -> HTTP 404 {error:{code:"unknown_browser"}}

    Raises `_ResolveError` (carrying the exact status + body) on failure.
    """
    executors: dict[str, ExecutorClient] = app.state.executors
    if not executors:
        raise _ResolveError(503, {"error": {"code": "no_executor"}})

    if browser_id is None:
        if DEFAULT_BROWSER_ID in executors:
            return executors[DEFAULT_BROWSER_ID]
        if len(executors) == 1:
            return next(iter(executors.values()))
        # Ambiguous: multiple browsers, none is "default", caller didn't pick.
        raise _ResolveError(
            400,
            {"error": {"code": "browser_id_required"}},
        )

    executor = executors.get(browser_id)
    if executor is None:
        raise _ResolveError(404, {"error": {"code": "unknown_browser"}})
    return executor


def _state_lifespan_started() -> bool:
    return bool(getattr(app.state, "lifespan_started", False))


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("agent-bridge starting on 127.0.0.1:7878")
    app.state.lifespan_started = True
    _warn_if_running_without_auth()
    try:
        yield
    finally:
        app.state.lifespan_started = False
        log.info("agent-bridge shutting down")


app = FastAPI(title="realhands Bridge", lifespan=lifespan)
_init_state(app)


@app.middleware("http")
async def require_bridge_token(request: Request, call_next):
    token = _bridge_token()
    if token is None:
        if not _state_lifespan_started():
            _warn_if_running_without_auth()
        return await call_next(request)
    # /register only carries a spawned browser_id in a tab URL; it has no control
    # power and cannot send custom headers from Chrome's navigation request.
    if request.url.path == "/register":
        return await call_next(request)
    if request.headers.get("X-RealHands-Token") != token:
        return JSONResponse(
            status_code=401,
            content={"error": {"code": "unauthorized"}},
        )
    return await call_next(request)


# ---------- WS endpoint ----------


def _register_executor(browser_id: str, executor: ExecutorClient) -> None:
    """Register `executor` under `browser_id`, cleanly closing any prior
    connection holding the same id. Called on the FIRST executor_ready of a
    connection (and again if the browser later re-announces a changed id).
    """
    executors: dict[str, ExecutorClient] = app.state.executors
    prior = executors.get(browser_id)
    if prior is not None and prior is not executor:
        log.warning("replacing existing executor for browser_id=%s", browser_id)
        prior_ws = prior.ws
        if prior_ws is not None:
            # Close the old socket out-of-band; its read loop will then run its
            # own finally/_cleanup and deregister itself. Schedule, don't await.
            async def _close_prior(w):
                try:
                    await w.close(code=status.WS_1008_POLICY_VIOLATION)
                except Exception:
                    pass

            asyncio.create_task(_close_prior(prior_ws))
    executors[browser_id] = executor
    log.info("registered executor browser_id=%s", browser_id)


@app.websocket("/")
async def ws_executor(ws: WebSocket) -> None:
    """Chrome extension connects here. SWARM PROTOCOL v1: MANY concurrent
    connections allowed, one per distinct browser_id. The connection registers
    itself in app.state.executors on its first executor_ready (which carries
    browser_id); a duplicate id cleanly replaces the prior connection.
    """
    origin = ws.headers.get("origin")
    if not _ws_origin_allowed(origin):
        log.warning("rejecting executor WS with origin=%s", origin)
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    token = _bridge_token()
    if token is not None and ws.query_params.get("token") != token:
        log.warning("rejecting executor WS with missing/wrong token")
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await ws.accept()

    # Each connection owns its own ExecutorClient. It joins the registry only
    # once it announces a browser_id via executor_ready.
    executor = ExecutorClient(app.state.broker)
    executor.ws = ws
    executor.connected_at = time.time()
    executor.executor_started_at = executor.connected_at
    log.info("executor connected from %s (awaiting browser_id)", ws.client)

    registered_id: Optional[str] = None

    # MV3 keepalive: send a small message every 15s on THIS connection so the
    # extension's onmessage handler fires, which resets Chrome's service worker
    # ~30s idle timer. INCOMING WS messages count as activity for the SW;
    # outgoing don't (the docs are misleading). Without this the SW suspends,
    # the WS drops, and the connection flaps until the heartbeat alarm
    # reconnects ~1min later. One keepalive task per connection.
    async def _bridge_keepalive() -> None:
        try:
            while True:
                await asyncio.sleep(15)
                if executor.ws is not ws:
                    return
                try:
                    await executor._send_text(
                        json.dumps({"keepalive": True, "ts": time.time()})
                    )
                except Exception:
                    return
        except asyncio.CancelledError:
            return

    keepalive_task = asyncio.create_task(_bridge_keepalive())

    try:
        while True:
            text = await ws.receive_text()
            await executor.on_message(text)
            # After on_message processes an executor_ready, this connection's
            # browser_id is known — (re)register under it.
            current_id = executor.browser_id
            if current_id is not None and current_id != registered_id:
                # If we previously registered under a different id and still own
                # that slot, vacate it before taking over the new one.
                if registered_id is not None:
                    if app.state.executors.get(registered_id) is executor:
                        app.state.executors.pop(registered_id, None)
                _register_executor(current_id, executor)
                registered_id = current_id
    except Exception as exc:
        # WebSocketDisconnect or anything else terminating the receive loop.
        log.info("executor receive loop ended: %s", type(exc).__name__)
    finally:
        keepalive_task.cancel()
        # Await the cancelled task so it finishes unwinding before we clear
        # executor state out from under it. Swallow the CancelledError.
        try:
            await keepalive_task
        except asyncio.CancelledError:
            pass
        # Remove our registry slot — but only if it's still ours (a replacement
        # connection may already own it).
        if registered_id is not None and app.state.executors.get(registered_id) is executor:
            app.state.executors.pop(registered_id, None)
            log.info("deregistered executor browser_id=%s", registered_id)
        await executor._cleanup()


# ---------- REST endpoints ----------


@app.get("/health")
async def health() -> dict:
    executors: dict[str, ExecutorClient] = app.state.executors
    return {
        "ok": True,
        "browsers_connected": len(executors),
        "uptime_s": round(time.time() - app.state.started_at, 3),
    }


@app.get("/executor")
async def executor_status(browser_id: Optional[str] = None) -> JSONResponse:
    """Single-browser status. ?browser_id=X selects; no arg = default/sole.
    Back-compat shape unchanged (ExecutorClient.status_payload)."""
    try:
        executor = _resolve_executor(browser_id)
    except _ResolveError as exc:
        return exc.response()
    return JSONResponse(executor.status_payload())


@app.get("/executors")
async def executors_list() -> dict:
    """List every connected browser in the swarm."""
    executors: dict[str, ExecutorClient] = app.state.executors
    now = time.time()
    browsers = []
    for browser_id, ex in executors.items():
        browsers.append(
            {
                "browser_id": browser_id,
                "connected": ex.connected,
                "version": ex.version,
                "uptime_s": round(now - ex.connected_at, 3) if ex.connected_at else None,
                "current_task": ex.current_task,
            }
        )
    return {"browsers": browsers}


@app.post("/call")
async def call_method(body: CallBody) -> JSONResponse:
    # Money-guard runs FIRST, before any executor resolution — a money action
    # is refused regardless of browser_id (or whether one is connected).
    if _is_money_action(body.method, body.params):
        log.warning("blocked money action method=%s", body.method)
        return JSONResponse(
            status_code=403,
            content={"error": {"code": "money_action_blocked"}},
        )
    try:
        executor = _resolve_executor(body.browser_id)
    except _ResolveError as exc:
        return exc.response()
    try:
        result = await executor.call(body.method, body.params, timeout=body.timeout)
        return JSONResponse({"result": result})
    except ExecutorError as exc:
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}})


@app.post("/sequence")
async def call_sequence(body: SequenceBody) -> JSONResponse:
    # Reject the whole sequence if ANY step is a money action — never run a
    # partial sequence up to the blocked step. Guard runs before resolution.
    for step in body.steps:
        if _is_money_action(step.method, step.params):
            log.warning("blocked money action in sequence method=%s", step.method)
            return JSONResponse(
                status_code=403,
                content={"error": {"code": "money_action_blocked"}},
            )
    try:
        executor = _resolve_executor(body.browser_id)
    except _ResolveError as exc:
        return exc.response()
    out: list[dict] = []
    for step in body.steps:
        started = time.time()
        entry: dict[str, Any] = {"method": step.method}
        try:
            result = await executor.call(step.method, step.params, timeout=body.timeout)
            entry["result"] = result
        except ExecutorError as exc:
            entry["error"] = {"code": exc.code, "message": exc.message}
        entry["duration_ms"] = round((time.time() - started) * 1000, 2)
        out.append(entry)
        if "error" in entry and not body.continue_on_error:
            break
    return JSONResponse({"steps": out})


@app.post("/executor/task")
async def set_task(body: TaskBody) -> JSONResponse:
    try:
        executor = _resolve_executor(body.browser_id)
    except _ResolveError as exc:
        return exc.response()
    await executor.set_state({"current_task": body.task})
    return JSONResponse({"ok": True, "current_task": body.task})


@app.get("/events")
async def events(request: Request, last_id: Optional[int] = None) -> StreamingResponse:
    broker: EventBroker = app.state.broker

    async def gen():
        try:
            async for env in broker.subscribe(last_id=last_id):
                if await request.is_disconnected():
                    return
                # SSE: id line lets curl/clients track progress; data is JSON.
                seq = env.get("_seq")
                yield f"id: {seq}\ndata: {json.dumps(env)}\n\n"
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/credentials/read")
async def credentials_read(body: CredentialBody, request: Request) -> dict:
    if not _vault_api_enabled():
        raise HTTPException(status_code=403, detail="vault_api_disabled")
    token = _bridge_token()
    if token is None or request.headers.get("X-RealHands-Token") != token:
        raise HTTPException(status_code=401, detail="bridge token required")
    vault = app.state.vault
    if vault is None:
        raise HTTPException(status_code=503, detail="vault not available")
    # Log platform + field only. NEVER the value.
    log.info("credentials_read platform=%s field=%s", body.platform, body.field)
    try:
        value = vault.get(body.platform, field=body.field)
    except ValueError as exc:
        # Unknown field name — surface as 400.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TypeError:
        # Fallback if vault.get doesn't accept a field kwarg.
        record = vault.get(body.platform)
        if isinstance(record, dict):
            value = record.get(body.field)
        else:
            value = record
    except Exception as exc:
        log.warning("vault.get failed for %s.%s: %s", body.platform, body.field, exc)
        raise HTTPException(status_code=500, detail="vault read failed") from exc
    if value is None:
        raise HTTPException(status_code=404, detail="credential not found")
    return {"value": value}


# ---------- /register (id carrier for spawned browsers) ----------


@app.get("/register")
async def register_page(browser_id: Optional[str] = None) -> HTMLResponse:
    """A tiny HTML page whose ONLY purpose is to carry browser_id in the URL so
    a spawned Chrome's service worker can read it off the tab URL and register
    under it. No auth, no logic — just a 200 page.
    """
    bid = browser_id or DEFAULT_BROWSER_ID
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8>"
        f"<title>realhands</title><body>realhands browser {bid} registered</body>",
        status_code=200,
    )


# ---------- spawn / process management ----------


@app.post("/spawn")
async def spawn_browser(body: SpawnBody) -> JSONResponse:
    spawner = app.state.spawner
    if spawner is None:
        raise HTTPException(status_code=503, detail="spawner not available")
    # If browser_id omitted, generate a short uuid-based id in real Python.
    browser_id = body.browser_id or f"b-{uuid.uuid4().hex[:8]}"
    try:
        browser_id = _validate_spawn_name(browser_id, "browser_id")
        profile = (
            _validate_spawn_name(body.profile, "profile")
            if body.profile is not None
            else None
        )
        result = await spawner.spawn(
            browser_id,
            profile=profile,
            persistent=body.persistent,
            start_url=body.start_url,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.warning("spawn failed for %s: %s", browser_id, exc)
        raise HTTPException(status_code=500, detail=f"spawn failed: {exc}") from exc
    return JSONResponse(result)


class CloseBody(BaseModel):
    browser_id: Optional[str] = None


async def _do_close(browser_id: Optional[str]) -> JSONResponse:
    spawner = app.state.spawner
    if spawner is None:
        raise HTTPException(status_code=503, detail="spawner not available")
    if not browser_id:
        raise HTTPException(status_code=400, detail="browser_id required")
    closed = await spawner.close(browser_id)
    return JSONResponse({"closed": bool(closed), "browser_id": browser_id})


# Close a spawned browser. The canonical route is POST /browsers/{id}/close, but
# agents reliably guess other shapes — so accept every common variant rather than
# 404. (realhands tried /close, /close/{id}, /close?browser_id=, DELETE ... — all of
# these now work.)
@app.post("/browsers/{browser_id}/close")
@app.delete("/browsers/{browser_id}/close")
@app.delete("/browsers/{browser_id}")
@app.post("/close/{browser_id}")
@app.delete("/close/{browser_id}")
async def close_browser_path(browser_id: str) -> JSONResponse:
    return await _do_close(browser_id)


@app.post("/close")
@app.delete("/close")
async def close_browser_body(
    body: Optional[CloseBody] = None, browser_id: Optional[str] = None
) -> JSONResponse:
    bid = browser_id or (body.browser_id if body else None)
    return await _do_close(bid)


@app.get("/browsers")
async def list_browsers() -> JSONResponse:
    spawner = app.state.spawner
    if spawner is None:
        raise HTTPException(status_code=503, detail="spawner not available")
    return JSONResponse(spawner.list())


# ---------- entry point glue ----------


def main() -> None:
    """`python -m agent-bridge` or `python bridge.py` runs the server."""
    import uvicorn

    uvicorn.run(
        "bridge:app",
        host="127.0.0.1",
        port=7878,
        log_level="info",
    )


if __name__ == "__main__":
    main()
