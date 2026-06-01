"""ExecutorClient — wraps the single WebSocket connection to the Chrome extension.

Owns the request/response futures map, last_event/current_task, and the
single-executor invariant. The FastAPI WS handler accepts a socket, hands it
to `connect()`, and returns when the executor disconnects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger("agent_bridge.executor")


class ExecutorError(Exception):
    """Raised when the executor returns {error: {code, message}} for a /call."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _resolve(fut: asyncio.Future, value) -> None:
    if not fut.done():
        fut.set_result(value)


def _resolve_exc(fut: asyncio.Future, exc: Exception) -> None:
    if not fut.done():
        fut.set_exception(exc)


class ExecutorClient:
    """Owns the single live WebSocket to the Chrome extension."""

    def __init__(self, broker) -> None:
        self.ws: Optional[WebSocket] = None
        # pending: id -> (future, future's home loop). The loop reference lets
        # us resolve safely if on_message runs in a different loop (test only).
        self.pending: dict[str, tuple[asyncio.Future, asyncio.AbstractEventLoop]] = {}
        self.connected_at: Optional[float] = None
        self.executor_started_at: Optional[float] = None
        self.last_event: Optional[dict] = None
        self.current_task: Optional[dict] = None
        self.version: Optional[str] = None
        # browser_id identifies which browser instance this connection is. It is
        # learned from the executor_ready registration message (carries
        # "browser_id"); a browser that never configures one registers as
        # "default" (BACKWARD COMPATIBLE). None until the first executor_ready.
        self.browser_id: Optional[str] = None
        self._broker = broker
        self._send_lock: Optional[asyncio.Lock] = None

    # ---------- introspection ----------

    @property
    def connected(self) -> bool:
        return self.ws is not None

    def status_payload(self) -> dict:
        now = time.time()
        return {
            "connected": self.connected,
            "browser_id": self.browser_id,
            "version": self.version,
            "last_event": self.last_event,
            "current_task": self.current_task,
            "uptime_s": round(now - self.connected_at, 3) if self.connected_at else None,
            "executor_uptime_s": (
                round(now - self.executor_started_at, 3) if self.executor_started_at else None
            ),
        }

    # ---------- lifecycle ----------

    async def connect(self, ws: WebSocket) -> None:
        """Take ownership of an accepted WebSocket and read until disconnect.

        The caller (FastAPI handler) is responsible for refusing a second
        connection BEFORE handing the socket here.
        """
        self.ws = ws
        self.connected_at = time.time()
        self.executor_started_at = self.connected_at
        log.info("executor connected from %s", ws.client)
        try:
            while True:
                text = await ws.receive_text()
                await self.on_message(text)
        except WebSocketDisconnect:
            log.info("executor disconnected (version=%s)", self.version)
        except Exception as exc:
            log.warning("executor receive loop crashed: %s", exc)
        finally:
            await self._cleanup()

    async def _cleanup(self) -> None:
        self.ws = None
        self.connected_at = None
        self.executor_started_at = None
        self.version = None
        self.browser_id = None
        self.current_task = None
        # Fail any in-flight requests so the REST callers don't hang.
        for fut, loop in list(self.pending.values()):
            loop.call_soon_threadsafe(
                _resolve_exc,
                fut,
                ExecutorError("executor_disconnected", "executor went away"),
            )
        self.pending.clear()

    # ---------- incoming messages ----------

    async def on_message(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            log.warning("non-JSON from executor: %r", text[:200])
            return
        if not isinstance(msg, dict):
            return

        # Response shape: {id, result} or {id, error}
        msg_id = msg.get("id")
        if msg_id and ("result" in msg or "error" in msg):
            entry = self.pending.pop(msg_id, None)
            if entry is None:
                return
            fut, loop = entry
            if fut.done():
                return
            if "error" in msg and isinstance(msg["error"], dict):
                err = msg["error"]
                exc = ExecutorError(err.get("code", "error"), err.get("message", ""))
                # Resolve on the future's home loop. In production this is the
                # same loop we're already on; in tests it may differ.
                loop.call_soon_threadsafe(_resolve_exc, fut, exc)
            else:
                loop.call_soon_threadsafe(_resolve, fut, msg.get("result"))
            return

        # Event shape: {event: "..."}
        event_kind = msg.get("event")
        if event_kind:
            # Keepalive/heartbeat traffic fires ~every 15s. If we published it,
            # it would evict real events from the 1000-slot SSE ring buffer
            # (event_broker.BUFFER_SIZE) within minutes. Drop it before publish.
            if event_kind in {"keepalive", "heartbeat"}:
                return
            self.last_event = msg
            if event_kind == "executor_ready":
                self.version = msg.get("version")
                # SWARM PROTOCOL v1: the registration message carries the
                # browser_id. A browser with none configured registers as
                # "default" (BACKWARD COMPATIBLE).
                self.browser_id = msg.get("browser_id") or "default"
                self.executor_started_at = time.time()
                log.info(
                    "executor_ready browser_id=%s version=%s",
                    self.browser_id,
                    self.version,
                )
            # Tag every emitted event with this connection's browser_id so /events
            # subscribers can demux across a swarm. Don't mutate the caller's dict.
            await self._broker.publish({**msg, "browser_id": self.browser_id})
            return

        # Anything else (echo/ack/etc) is ignored intentionally.

    # ---------- outgoing requests ----------

    async def call(
        self,
        method: str,
        params: Optional[dict] = None,
        *,
        timeout: float = 30.0,
    ) -> Any:
        if self.ws is None:
            raise ExecutorError("executor_disconnected", "no executor connected")
        params = params or {}
        req_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending[req_id] = (fut, loop)
        payload = {"id": req_id, "method": method, "params": params}
        # Log method and the KEYS of params only — values may include secrets.
        log.info("call method=%s params_keys=%s", method, sorted(params.keys()))
        try:
            await self._send_text(json.dumps(payload))
        except Exception as exc:
            self.pending.pop(req_id, None)
            raise ExecutorError("send_failed", str(exc)) from exc
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self.pending.pop(req_id, None)
            raise ExecutorError("timeout", f"{method} timed out after {timeout}s") from exc

    async def set_state(self, patch: dict) -> None:
        if self.ws is None:
            raise ExecutorError("executor_disconnected", "no executor connected")
        await self._send_text(json.dumps({"set_state": patch}))
        # Mirror locally so /executor reflects the change without a round trip.
        if "current_task" in patch:
            self.current_task = patch["current_task"]

    async def _send_text(self, text: str) -> None:
        """Serialize sends to the WS with a per-loop lock."""
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        async with self._send_lock:
            assert self.ws is not None
            await self.ws.send_text(text)
