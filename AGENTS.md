# RealHands — for your agent

You can control a **real, logged-in Chrome** through a tiny local HTTP API. The
RealHands extension runs inside the user's Chrome and connects to a local bridge
at `http://localhost:7878`. You drive the browser by POSTing JSON. No cloud, no
keys — everything is local.

## Setup — getting a browser to drive

**A human already loaded the extension?** (the consumer path) You're done — the bridge is at
`http://localhost:7878` and you're driving their real, logged-in Chrome. Skip to the API below.

**Setting up entirely yourself, no human (headless-friendly):**
```bash
git clone https://github.com/BraydenMilner/realhands && cd realhands/bridge
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn bridge:app --host 127.0.0.1 --port 7878 &
```
Then spawn a fresh browser that already has the extension (auto-downloads Chrome for Testing
once — no admin, no GUI):
```
POST http://localhost:7878/spawn   { "browser_id": "b1" }   →   { "browser_id": "b1", "pid": … }
```
Pass `"browser_id": "b1"` in every call below to drive it; close it with
`POST http://localhost:7878/browsers/b1/close`. (You can't auto-load the extension into a
human's *existing* Chrome — that's a one-time GUI step for them — but spawned browsers are
entirely yours.)

## The one call you need

```
POST http://localhost:7878/call
{ "method": "<method>", "params": { ... } }
```
Returns `{ "result": ... }` on success, or `{ "error": { "code", "message" } }`.

(If the user set a token, also send header `X-RealHands-Token: <token>`.)

## Methods you'll use most

| method | params | does |
|---|---|---|
| `navigate` | `{ "url" }` | load a URL |
| `screenshot` | `{}` | returns `{ base64, url, device_pixel_ratio }` |
| `click_at` | `{ "x", "y" }` | click at a point (CSS pixels) |
| `click_selector` | `{ "selector" }` | click a CSS selector |
| `type` | `{ "x", "y", "text" }` (or `{ "selector", "text" }`) | type into a field |
| `key_press` | `{ "key" }` | e.g. `"Enter"` |
| `scroll` | `{ "x", "y" }` | scroll the page |
| `get_page_info` | `{}` | url, title, viewport |
| `wait_for_url` | `{ "url" }` | wait for a navigation |

The **full, authoritative method list + schemas** is always at
`GET http://localhost:7878/openapi.json` (human docs at `/docs`).

> **Coordinates:** `screenshot` returns pixels at `device_pixel_ratio`. Divide
> screenshot-pixel coordinates by `device_pixel_ratio` before passing them to
> `click_at` / `type` (which expect CSS pixels).

## How to drive it (the loop)

1. `screenshot` → look at the page.
2. Decide the single next action.
3. Call the matching method.
4. Repeat until the task is done.

A minimal reference loop is in `examples/byo_key_agent.py`.

## Autonomy levels — ask your user

Before starting a browser-automation task, **ask your user how autonomous they
want the agent to be**. Then pass the matching `mode` when starting a run via
`POST /agent/run`:

| mode | behavior |
|---|---|
| `ask` | Confirm every actuating action before executing (most cautious). |
| `gated` | Runs on its own but **pauses for explicit approval on high-risk actions** (payments, sending/publishing, deleting data, entering credentials). Once approved, subsequent sensitive actions run without a second prompt. **Recommended default.** |
| `auto` | Full autonomy — never asks. Use only when the user is comfortable. |

Frame it as: the human decides their own risk tolerance; the tool does not
restrict use.

## Safety — read this

Never read, echo, or store password-field contents.

## Health check

```
GET http://localhost:7878/health   →   { "ok": true, ... }
```
If `/call` returns `503`, the extension isn't connected — tell the user to make
sure Chrome is open with the RealHands extension loaded and the bridge running.

## Multiple browsers (advanced — most tasks don't need this)

Every call takes an optional `"browser_id"`. With one browser, omit it. To run
several browsers at once, `POST /spawn { "browser_id": "b1" }` launches another
one; then address it with `"browser_id": "b1"` in `/call`. Close with
`POST /browsers/b1/close`. See `docs/PLATFORMS.md`.

## Chat / Ask mode (read-only Q&A)

RealHands has two interaction modes:

- **Do** — drive the browser (the default; uses `/agent/run`).
- **Ask** — answer questions about the current page or the web, **read-only**
  (no clicks, typing, navigation, or scrolling). The agent reads the page text,
  views a screenshot, and optionally searches the web, then answers in the chat.

```
POST http://localhost:7878/agent/ask
{ "message": "What does this page say?", "browser_id": "b1" }
→ { "ok": true }
```

The answer streams over the existing `GET /events` SSE as `{type:"chat", ...}` events:
- `{type:"chat", role:"tool", text, done:false}` — tool-use progress (read page, search).
- `{type:"chat", role:"assistant", text, done:true}` — the final answer.
- `{type:"chat", role:"error", text, done:true}` — on failure.

**Web search** (optional): set `REALHANDS_SEARCH_API_KEY` to a [Tavily](https://tavily.com) API key.
When unset, the agent answers from page content + its own knowledge only.
