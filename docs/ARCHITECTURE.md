# Architecture

`realhands` is three small layers between your agent and a real Chrome window.

```
┌─────────────┐   HTTP / WebSocket   ┌──────────┐   WebSocket   ┌──────────────────┐
│  your agent │ ───────────────────▶ │  bridge  │ ────────────▶ │ extension (MV3)  │
│  (any LLM/  │ ◀─────────────────── │ (FastAPI)│ ◀──────────── │  service worker  │
│   script)   │     results/events   └────┬─────┘   ws frames    └────────┬─────────┘
└─────────────┘                           │                               │
                                          │                          chrome.debugger
                              optional vision tier                       (CDP)
                              (decide next action)                         │
                                                                           ▼
                                                                    real Chrome tab
                                                              (trusted input + capture)
```

## 1. Extension (`extension/`) — the executor

A Manifest V3 extension whose service worker attaches to the active tab via
`chrome.debugger` and drives it over the **Chrome DevTools Protocol**:

- **Trusted input.** `Input.dispatchMouseEvent`, `Input.insertText`, and key events
  produce `isTrusted=true` events — i.e. real browser-level input dispatched through
  Chrome's own input pipeline via CDP, not synthetic `element.dispatchEvent(...)`.
  This is the core reason to run *inside* the real browser instead of a separate
  headless one.
- **Screenshots.** `Page.captureScreenshot` returns the current frame for the agent
  (or the vision tier) to look at.
- **DOM reads.** A lightweight content script exposes read-only helpers
  (`describe_point`, `describe_element`, `wait_for_element`, page info) used both by
  the agent and by the money guard.
- **`browser_id`.** Each extension instance carries an id so one bridge can address
  many browsers. The id can be set in the popup or learned automatically from a
  `/register?browser_id=...` URL the bridge opens when spawning a browser.

### Money guard (safe by default)

Before any click or keystroke, the executor checks the *target* (its visible label /
nearby text / the page intent) against a canonical set of money-moving tokens —
`redeem`, `redemption`, `deposit`, `withdraw`, `withdrawal`, `transfer`, `cashout`,
`cash out`, `cashier`, `payout`. If a match is found, the action is **refused**
(fail-closed): blind coordinate clicks and blind Enter presses are blocked too, so
the agent can't sidestep it by not naming the target. Moving money is a human's job;
the agent surfaces state and stops. This guard is enforced redundantly in the
extension, the bridge, and the vision tier.

## 2. Bridge (`bridge/`) — multiplexed control plane

A small FastAPI app the agent talks to over REST, holding the WebSocket(s) to the
extension(s):

- **Executor registry keyed by `browser_id`.** REST calls take an optional
  `browser_id`; the bridge routes to the matching executor. If omitted it resolves to
  the sole/`default` browser; an unknown id is a `404`; no browsers connected is a
  `503`.
- **Endpoints.** `POST /call` (one method), `POST /sequence` (a list), `GET
  /executors` / `GET /browsers` (what's connected), `GET /events` (stream), `GET
  /health`. The full schema is always at `GET /openapi.json`.
- **Swarm lifecycle.** `POST /spawn` launches a fresh Chrome profile that loads the
  extension and self-registers under its `browser_id`; `POST /call` drives it; closing
  is accepted in many forms (`POST`/`DELETE /browsers/{id}/close`, `DELETE
  /browsers/{id}`, `/close/{id}`, or `/close` with the id in the body/query) because
  agents guess endpoints — actuation is intentionally forgiving.
- **Keepalive.** MV3 service workers idle out, so the bridge pings every ~15s
  (server-initiated) to keep the executor connection warm.
- **Credential vault (optional).** An encrypted local store (`vault.py`) exists for
  secrets. The `/credentials/read` API is disabled unless
  `REALHANDS_VAULT_API_ENABLED=1`, and enabled vault reads require
  `REALHANDS_BRIDGE_TOKEN` plus the matching token header. It is not wired to
  autofill by default — the design preference is that the user logs in once in
  their own profile and the session persists.

## 3. Vision tier (`vision/`) — optional decision layer

For the bring-your-own-key mode, `decide_action()` takes a screenshot + task context
+ recent step history and returns a single structured `ActionDecision`
(`click`/`type`/`navigate`/`wait`/`done`/`abort` with coordinates, a confidence, and
short reasoning). It is built to route across tiers — a fast local
(OpenAI-compatible) model first, escalating to a stronger hosted model only when
confidence is low — and it applies the same money guard before returning. If you
bring your own agent (mode A), you don't need this layer at all.

## Design constraints

- **No site knowledge in the executor.** The extension knows how to click, type, read,
  and screenshot — never *what* site it's on. Site-specific logic, if any, lives above
  the bridge in your agent.
- **Local-first.** Everything binds to `127.0.0.1`; nothing phones home. Your LLM key
  (if you use the vision tier) is the only outbound dependency, and it's yours.
- **Optional local auth.** `REALHANDS_BRIDGE_TOKEN` adds a REST header check and a
  WebSocket handshake token for shared or remote environments. The default remains
  localhost-only without auth for compatibility, with a startup warning.
- **Fail loud.** Connection loss, repeated failures, and guard trips are surfaced, not
  swallowed.
