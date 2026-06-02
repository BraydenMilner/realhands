# RealHands

**Give your AI agent a real, logged-in browser.**

[![tests](https://github.com/BraydenMilner/realhands/actions/workflows/ci.yml/badge.svg)](https://github.com/BraydenMilner/realhands/actions/workflows/ci.yml)
![license](https://img.shields.io/badge/license-Apache--2.0-blue)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![form](https://img.shields.io/badge/chrome-extension-brightgreen)
![status](https://img.shields.io/badge/status-alpha-orange)
![PRs](https://img.shields.io/badge/PRs-welcome-brightgreen)

> **Status: early / alpha.** The core is solid and tested (bridge + vision suites green), but the API may still shift. Issues and PRs are very welcome.

Most browser-automation tools spawn a *separate* headless Chromium. `realhands` is a
Chrome **extension + local bridge** that lets your agent drive the user's **real,
already-logged-in Chrome** with **trusted (`isTrusted=true`) input** via the Chrome
DevTools Protocol — no headless browser, no profile copying, no remote-debugging
flag (which modern Chrome blocks on the default profile anyway).

## Why

- **Co-resident.** Runs *inside* the user's real Chrome profile — live sessions, real
  cookies, real fingerprint. Nothing to log into twice.
- **Trusted input.** Clicks and keystrokes are dispatched over CDP as real
  (`isTrusted=true`) input, not synthetic `element.dispatchEvent(...)`.
- **Any agent.** Drive it from *any* agent — Claude, GPT, a plain script, your own
  orchestrator — over a tiny local REST/WebSocket API. Or use the bundled
  bring-your-own-key loop.
- **Swarm (advanced, optional).** Spawn, drive, and close many browsers on demand,
  each addressed by a `browser_id`. `POST /spawn` launches Chrome for Testing
  (auto-downloaded once, **no admin, no policy**) with the extension loaded; your
  agent controls them all through the same API. Most users never need this — see
  [docs/PLATFORMS.md](docs/PLATFORMS.md).
- **Safe by default.** A built-in guard **refuses** clicks/keys on money-moving
  controls (deposit / withdraw / transfer / cashout / cashier / …) by default;
  it fails closed when it can't verify a target. (Disabling it requires editing
  the source.)

## Two ways to use it

**A) Bring your own agent (composable).**
Install the extension, run the bridge, and `POST` to
`http://localhost:7878/call` with `{ "browser_id": ..., "method": ..., "params": ... }`
from anything that speaks HTTP. Executor methods include navigate, click, type,
screenshot, scroll, get page info, wait-for-url, and more. The full, authoritative
API is served at `GET /openapi.json` (FastAPI auto-docs at `/docs`).

**B) Bring your own key (turnkey).**
Run the bundled vision loop (`examples/byo_key_agent.py`) with your own LLM key
(OpenRouter / OpenAI / Anthropic / a local model). It screenshots, decides the next
action, executes it through the bridge, and repeats — honoring the money guard the
whole way.

## What a run looks like

The bring-your-own-key loop narrates each decision as it drives the page — action, confidence, which tier/model answered, latency, cost, and a one-line rationale:

```text
$ python3 examples/byo_key_agent.py "log in and open my profile"
task: 'log in and open my profile'
bridge: http://localhost:7878/call   model: qwen2.5-vl-7b-instruct

[01] type     conf=0.92  local/qwen2.5-vl-7b-instruct   840ms  $0  :: Email field is visible; enter the email first.
[02] type     conf=0.90  local/qwen2.5-vl-7b-instruct   610ms  $0  :: Password field below it; type the password.
[03] click    conf=0.94  local/qwen2.5-vl-7b-instruct   520ms  $0  :: "Sign in" is the clear next action.
[04] click    conf=0.88  local/qwen2.5-vl-7b-instruct   470ms  $0  :: Profile menu opened; click "My profile".
[05] done     conf=1.00  local/qwen2.5-vl-7b-instruct   300ms  $0  :: Profile page is open; task complete.
```

Point it at a money-moving control and the guard stops it cold — `done :: money_action_requires_human` — no click is ever dispatched.

> 📹 A screenshot/GIF walkthrough is coming. Until then, the Quickstart below gets you running in ~2 minutes.

## Quickstart

1. **Load the extension.** `chrome://extensions` → enable *Developer mode* → *Load
   unpacked* → select the `extension/` folder.
2. **Run the bridge.**
   ```bash
   cd bridge
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   .venv/bin/uvicorn bridge:app --host 127.0.0.1 --port 7878
    ```

   For the turnkey / vision mode, also install vision deps:
   ```bash
   pip install -r vision/requirements.txt
   ```
3. **Check the link.** The extension auto-connects. `curl localhost:7878/health`
   should return `{"ok": true, ...}`.
   On shared or multi-user hosts, set a bridge token and enter the same value in
   the extension popup:
   ```bash
   REALHANDS_BRIDGE_TOKEN='change-me' .venv/bin/uvicorn bridge:app --host 127.0.0.1 --port 7878
   curl -H 'X-RealHands-Token: change-me' localhost:7878/health
   ```
4. **Drive it.**
   ```bash
   curl -X POST localhost:7878/call \
     -H 'Content-Type: application/json' \
     -d '{"method":"navigate","params":{"url":"https://example.com"}}'
   curl -X POST localhost:7878/call \
     -H 'Content-Type: application/json' \
     -d '{"method":"screenshot","params":{}}'
   ```
5. **(Optional) Turnkey loop.** Set your LLM key and run `examples/byo_key_agent.py`
   to let the bundled vision tier decide and act on its own.

## Security Notes

- The bridge binds to loopback by default. If you expose it beyond localhost or
  run on a shared machine, set `REALHANDS_BRIDGE_TOKEN`; REST calls must then include
  `X-RealHands-Token`, and the extension sends the token in its WebSocket handshake.
- `/credentials/read` is sensitive and disabled by default. Enable it only when
  needed with `REALHANDS_VAULT_API_ENABLED=1`; enabled vault reads require
  `REALHANDS_BRIDGE_TOKEN` plus the matching token header.
- WebSocket executor connections reject normal web origins and accept only no
  origin, `null`, or `chrome-extension://...` origins.

## Permissions

The extension asks for only the Chrome permissions it uses:

- `storage`: saves the bridge URL, optional bridge token, browser ID, and popup
  status in Chrome local storage.
- `tabs`: creates, focuses, updates, closes, lists, and routes commands to tabs.
- `windows`: focuses the Chrome window that owns a tab before tab-level actions.
- `alarms`: wakes the MV3 service worker so it can keep the bridge connection
  alive.
- `debugger`: attaches to tabs for CDP input dispatch and screenshots.
- `host_permissions: https://*/*`: lets the content script run read-only DOM
  helpers on HTTPS pages.

## How it fits together

```
your agent ──HTTP/WS──▶  bridge  ──WebSocket──▶  extension (CDP)  ──▶  real Chrome
                          │                         │
                          │                         └─ trusted input + screenshots
                          └─ multiplexed by browser_id, spawn/close, optional vision tier
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design.

## Repository layout

| Path          | What it is                                                            |
|---------------|-----------------------------------------------------------------------|
| `extension/`  | MV3 Chrome extension — the executor (CDP trusted input, DOM reads, money guard, `browser_id`). |
| `bridge/`     | FastAPI bridge — multiplexed executor registry, swarm spawn/close, optional credential vault. |
| `vision/`     | Optional tiered LLM decision layer (`decide_action`) for the bring-your-own-key mode. |
| `examples/`   | Minimal reference scripts, including the BYO-key agent loop.          |

## License

[Apache-2.0](LICENSE).
