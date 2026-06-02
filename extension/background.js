// realhands — Browser Executor service worker.
// the executor of the realhands architecture. Maintains a persistent WebSocket to the
// Agent Bridge (default ws://localhost:7878), routes commands to background
// handlers or per-tab content scripts, and forwards tab/page events back.
// Knows nothing about any specific site.
//
// Input (clicks/typing/keys) and screenshots go through the chrome.debugger
// API (Chrome DevTools Protocol) so events are isTrusted=true — trusted input dispatched via the Chrome DevTools Protocol. DOM introspection (element boxes,
// waits, page info, scroll) stays in content.js.

const BRIDGE_URL_DEFAULT = "ws://localhost:7878";
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30_000;
const HEARTBEAT_ALARM = "realhands_heartbeat";
const CDP_VERSION = "1.3";
// Ping the bridge well within Chrome's ~30s MV3 idle window so WS activity keeps
// the service worker (and therefore the WebSocket) alive instead of being suspended.
const KEEPALIVE_MS = 20_000;

let ws = null;
let reconnectAttempts = 0;
let reconnectTimer = null;
let keepaliveTimer = null;

const attachedTabs = new Set();

// Default browser_id for the swarm. A single, unconfigured browser still works
// as "default" (backward compatible). A SPAWNED browser learns its real id by
// being launched at the bridge's /register?browser_id=<ID> URL (see the
// chrome.tabs.onUpdated handler below).
const BROWSER_ID_DEFAULT = "default";

const state = {
  connection: "idle",
  bridge_url: BRIDGE_URL_DEFAULT,
  bridge_token: "",
  browser_id: BROWSER_ID_DEFAULT,
  last_event: null,
  current_task: null,
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function makeError(code, message) {
  const err = new Error(message);
  err.code = code;
  return err;
}

// ---------- money-action guard (last-resort, defense in depth) ----------
//
// This is the FINAL line of defense. Upstream layers (the bridge and
// the optional vision tier's money-action checks, and
// the bridge) are all supposed to refuse money-moving clicks before they ever
// reach the executor. This guard exists so that even if every one of those
// upstream guards fails or is bypassed, the executor itself still refuses to
// click/type/submit anything that looks like a redeem/deposit/withdraw/etc.
//
// MONEY_TOKENS is the canonical set, identical to the list shared across
// models.py, decide.py, prompts.py,
// bridge.py. Matching is conservative: a lowercase substring match of any token
// against the selector string, any provided text, the page URL, and (best
// effort) the target element's innerText / aria-label / nearby label text.
const MONEY_TOKENS = [
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
];

// True if any canonical money token appears as a substring of any provided
// string. Inputs may be undefined/null; those are skipped.
function textIsMoneyAction(...candidates) {
  for (const c of candidates) {
    if (c == null) continue;
    const s = String(c).toLowerCase();
    if (!s) continue;
    for (const tok of MONEY_TOKENS) {
      if (s.includes(tok)) return true;
    }
  }
  return false;
}

// Ask the content script to describe a target element (by point, by selector,
// or the active element). Returns { ok, text }:
//   ok=true  -> the content script responded; `text` is the joined visible
//               text / aria-label / label (possibly empty if the element
//               legitimately has none).
//   ok=false -> the content script could NOT be reached, so we could not verify
//               the target at all. Callers whose ONLY money signal is the
//               element text (coordinate clicks, blind Enter) must FAIL CLOSED
//               on ok=false rather than trust an empty string.
async function describeTarget(tabId, method, params) {
  try {
    const inner = await chrome.tabs.sendMessage(tabId, { method, params });
    if (inner && inner.result) {
      const r = inner.result;
      return { ok: true, text: [r.text, r.aria_label, r.label].filter(Boolean).join(" ") };
    }
    return { ok: false, text: "" };
  } catch {
    // content script may be absent (e.g. just after a navigation)
    return { ok: false, text: "" };
  }
}

// Describe with one retry — the content script may be mid-injection right after
// a navigation. Used by the coordinate / blind actuation paths that fail closed
// when the target cannot be read.
async function describeTargetVerified(tabId, method, params) {
  const first = await describeTarget(tabId, method, params);
  if (first.ok) return first;
  await sleep(300);
  return describeTarget(tabId, method, params);
}

// ---------- chrome.debugger (CDP) infrastructure ----------

async function ensureAttached(tabId) {
  if (attachedTabs.has(tabId)) return;
  try {
    await chrome.debugger.attach({ tabId }, CDP_VERSION);
    attachedTabs.add(tabId);
    await chrome.debugger.sendCommand({ tabId }, "Page.enable").catch(() => {});
    await chrome.debugger.sendCommand({ tabId }, "DOM.enable").catch(() => {});
  } catch (err) {
    const msg = String(err?.message || err);
    if (/already attached|Another debugger/i.test(msg)) {
      // A debugger is already on this tab (often DevTools). Track it anyway so
      // we can try to issue commands; if those fail we surface a clear error.
      attachedTabs.add(tabId);
      return;
    }
    throw makeError("debugger_attach_failed", msg);
  }
}

async function cdp(tabId, method, params = {}) {
  await ensureAttached(tabId);
  try {
    return await chrome.debugger.sendCommand({ tabId }, method, params);
  } catch (err) {
    throw makeError("cdp_command_failed", `${method}: ${err?.message || err}`);
  }
}

function detachTab(tabId) {
  if (!attachedTabs.has(tabId)) return;
  attachedTabs.delete(tabId);
  chrome.debugger.detach({ tabId }).catch(() => {});
}

chrome.debugger.onDetach.addListener((source, reason) => {
  if (source.tabId != null) {
    attachedTabs.delete(source.tabId);
    sendToBridge({ event: "debugger_detached", tab_id: source.tabId, reason });
  }
});

// CDP input helpers — all produce isTrusted=true events.

async function cdpClick(tabId, x, y, { clickCount = 1, button = "left" } = {}) {
  await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none", buttons: 0 });
  await cdp(tabId, "Input.dispatchMouseEvent", { type: "mousePressed", x, y, button, clickCount, buttons: 1 });
  await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button, clickCount, buttons: 0 });
}

const CDP_KEYS = {
  Enter: { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, text: "\r" },
  Tab: { key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 },
  Escape: { key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 },
  Backspace: { key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8 },
  Delete: { key: "Delete", code: "Delete", windowsVirtualKeyCode: 46 },
  ArrowUp: { key: "ArrowUp", code: "ArrowUp", windowsVirtualKeyCode: 38 },
  ArrowDown: { key: "ArrowDown", code: "ArrowDown", windowsVirtualKeyCode: 40 },
  ArrowLeft: { key: "ArrowLeft", code: "ArrowLeft", windowsVirtualKeyCode: 37 },
  ArrowRight: { key: "ArrowRight", code: "ArrowRight", windowsVirtualKeyCode: 39 },
  Home: { key: "Home", code: "Home", windowsVirtualKeyCode: 36 },
  End: { key: "End", code: "End", windowsVirtualKeyCode: 35 },
};

function parseModifiers(mods) {
  if (typeof mods === "number") return mods;
  if (!mods || typeof mods !== "string") return 0;
  const m = mods.toLowerCase();
  let bits = 0;
  if (m.includes("alt")) bits |= 1;
  if (m.includes("ctrl") || m.includes("control")) bits |= 2;
  if (m.includes("cmd") || m.includes("meta")) bits |= 4;
  if (m.includes("shift")) bits |= 8;
  return bits;
}

async function cdpKey(tabId, key, { modifiers = 0 } = {}) {
  const def = CDP_KEYS[key];
  if (def) {
    await cdp(tabId, "Input.dispatchKeyEvent", { type: "keyDown", modifiers, ...def });
    await cdp(tabId, "Input.dispatchKeyEvent", { type: "keyUp", modifiers, ...def });
  } else if (key.length === 1) {
    await cdp(tabId, "Input.dispatchKeyEvent", { type: "keyDown", modifiers, key, text: key });
    await cdp(tabId, "Input.dispatchKeyEvent", { type: "keyUp", modifiers, key });
  } else {
    await cdp(tabId, "Input.dispatchKeyEvent", { type: "keyDown", modifiers, key });
    await cdp(tabId, "Input.dispatchKeyEvent", { type: "keyUp", modifiers, key });
  }
}

// Ask the content script for an element's viewport box (CSS pixels).
async function contentBox(tabId, selector) {
  const inner = await chrome.tabs.sendMessage(tabId, {
    method: "get_element_box",
    params: { selector },
  });
  if (!inner) throw makeError("content_script_unavailable", `no content script in tab ${tabId}`);
  if (inner.error) throw makeError(inner.error.code || "error", inner.error.message);
  const r = inner.result || {};
  if (!r.box) throw makeError("element_not_found", selector);
  return { ...r.box, visible: r.visible };
}

// ---------- background-level primitive handlers ----------

const BG_HANDLERS = {
  async navigate({ url, tab_id }) {
    if (!url) throw makeError("invalid_params", "navigate requires url");
    const tab = await resolveTab({ tab_id });
    await chrome.tabs.update(tab.id, { url });
    return { tab_id: tab.id, url };
  },

  async new_tab({ url, active = true }) {
    const t = await chrome.tabs.create({ url, active });
    return { tab_id: t.id, url: t.url, window_id: t.windowId };
  },

  async close_tab({ tab_id }) {
    if (!tab_id) throw makeError("invalid_params", "close_tab requires tab_id");
    detachTab(tab_id);
    await chrome.tabs.remove(tab_id);
    return { tab_id };
  },

  // CDP screenshot — works on background tabs, can capture full page. Reports
  // device_pixel_ratio + viewport so callers can map screenshot-pixel coords
  // (what vision returns) to CSS-pixel coords (what click_at expects).
  async screenshot({ tab_id, format = "png", full_page = false } = {}) {
    const tab = await resolveTab({ tab_id });
    let dpr = 1;
    let viewport = null;
    try {
      const info = await chrome.tabs.sendMessage(tab.id, { method: "get_page_info", params: {} });
      const vp = info?.result?.viewport;
      if (vp) {
        dpr = vp.dpr || 1;
        viewport = { w: vp.w, h: vp.h };
      }
    } catch {
      // content script may not be present (e.g. just after navigation); non-fatal
    }
    const params = { format };
    if (full_page) params.captureBeyondViewport = true;
    const res = await cdp(tab.id, "Page.captureScreenshot", params);
    return {
      tab_id: tab.id,
      format,
      base64: res.data,
      device_pixel_ratio: dpr,
      viewport,
      url: tab.url,
    };
  },

  async click_at({ x, y, tab_id, button = "left", clickCount = 1 }) {
    if (typeof x !== "number" || typeof y !== "number") {
      throw makeError("invalid_params", "click_at requires numeric x, y");
    }
    const tab = await resolveTab({ tab_id });
    // Last-resort money guard. A coordinate click has NO selector — the element
    // under the point is the only money signal besides the URL, so we MUST be
    // able to read it. If the content script can't describe the target, FAIL
    // CLOSED rather than fall back to a URL-only check (a redeem button on a
    // tokenless SPA route would otherwise slip through).
    const desc = await describeTargetVerified(tab.id, "describe_point", { x, y });
    if (!desc.ok) {
      throw makeError(
        "money_action_unverifiable",
        `click_at refused: cannot read the element at (${x},${y}) to verify it is not a money action`,
      );
    }
    if (textIsMoneyAction(desc.text, tab.url)) {
      throw makeError("money_action_blocked", `click_at refused (money action): ${desc.text || tab.url}`);
    }
    await cdpClick(tab.id, x, y, { button, clickCount });
    return { x, y, tab_id: tab.id };
  },

  async click_selector({ selector, tab_id }) {
    if (!selector) throw makeError("invalid_params", "click_selector requires selector");
    const tab = await resolveTab({ tab_id });
    // Last-resort money guard: match against the selector string, the element's
    // visible text / aria-label, and the page URL. The selector string is always
    // available as a money signal (and contentBox below also needs the content
    // script), so a best-effort describe is sufficient here — no fail-closed.
    const desc = await describeTarget(tab.id, "describe_element", { selector });
    if (textIsMoneyAction(selector, desc.text, tab.url)) {
      throw makeError("money_action_blocked", `click_selector refused (money action): ${selector}`);
    }
    const box = await contentBox(tab.id, selector);
    if (box.visible === false) {
      throw makeError("element_not_interactable", `selector found but not visible: ${selector}`);
    }
    const cx = box.x + box.w / 2;
    const cy = box.y + box.h / 2;
    await cdpClick(tab.id, cx, cy);
    return { selector, x: cx, y: cy, tab_id: tab.id };
  },

  // Accepts EITHER {selector, text} OR {x, y, text}. With a selector we resolve
  // the box via the content script and click its center; with x,y we focus by
  // clicking the point directly. options.submit sends Enter afterward.
  async type({ selector, x, y, text, options = {}, tab_id }) {
    if (text == null) throw makeError("invalid_params", "type requires text");
    const hasSelector = !!selector;
    const hasPoint = typeof x === "number" && typeof y === "number";
    if (!hasSelector && !hasPoint) {
      throw makeError("invalid_params", "type requires either selector or numeric x,y");
    }
    const tab = await resolveTab({ tab_id });
    const clear = options.clear !== false;

    // Last-resort money guard: refuse to type into / submit on a money action.
    // With a selector the selector string is always a money signal (and the
    // contentBox below needs the content script anyway). With coordinates the
    // element text is the only signal, so FAIL CLOSED if it can't be read.
    let targetText = "";
    if (hasSelector) {
      const desc = await describeTarget(tab.id, "describe_element", { selector });
      targetText = desc.text;
    } else {
      const desc = await describeTargetVerified(tab.id, "describe_point", { x, y });
      if (!desc.ok) {
        throw makeError(
          "money_action_unverifiable",
          `type refused: cannot read the element at (${x},${y}) to verify it is not a money action`,
        );
      }
      targetText = desc.text;
    }
    if (textIsMoneyAction(selector, text, targetText, tab.url)) {
      throw makeError("money_action_blocked", `type refused (money action): ${selector || `${x},${y}`}`);
    }

    if (hasSelector) {
      const box = await contentBox(tab.id, selector);
      if (box.visible === false) {
        throw makeError("element_not_interactable", `selector found but not visible: ${selector}`);
      }
      const cx = box.x + box.w / 2;
      const cy = box.y + box.h / 2;
      // Triple-click selects existing text so insertText replaces it; single-click just focuses.
      await cdpClick(tab.id, cx, cy, { clickCount: clear ? 3 : 1 });
    } else {
      await cdpClick(tab.id, x, y, { clickCount: clear ? 3 : 1 });
    }
    await cdp(tab.id, "Input.insertText", { text: String(text) });
    if (options.submit) await cdpKey(tab.id, "Enter");
    return { selector, length: String(text).length, submitted: !!options.submit, tab_id: tab.id };
  },

  async key_press({ key, target_selector, modifiers, tab_id }) {
    if (!key) throw makeError("invalid_params", "key_press requires key");
    const tab = await resolveTab({ tab_id });
    const isActuating = key === "Enter" || key === " " || key === "Spacebar" || key === "Space" || key === "NumpadEnter";
    // Space and NumpadEnter also activate a focused button, so they require
    // money-target verification and must fail closed when the focused element
    // can't be read, just like Enter.
    if (target_selector) {
      const desc = await describeTarget(tab.id, "describe_element", { selector: target_selector });
      if (textIsMoneyAction(target_selector, desc.text, isActuating ? tab.url : null)) {
        throw makeError("money_action_blocked", `key_press refused (money action): ${target_selector}`);
      }
    } else if (isActuating) {
      const desc = await describeTargetVerified(tab.id, "describe_active", {});
      if (!desc.ok) {
        throw makeError(
          "money_action_unverifiable",
          "key_press refused: cannot read the focused element to verify it is not a money submit",
        );
      }
      if (textIsMoneyAction(desc.text, tab.url)) {
        throw makeError("money_action_blocked", `key_press refused (money action): ${key}`);
      }
    }
    if (target_selector) {
      const box = await contentBox(tab.id, target_selector);
      await cdpClick(tab.id, box.x + box.w / 2, box.y + box.h / 2);
    }
    await cdpKey(tab.id, key, { modifiers: parseModifiers(modifiers) });
    return { key, tab_id: tab.id };
  },

  async wait({ ms = 1000 }) {
    const clamped = Math.min(60_000, Math.max(0, ms | 0));
    await sleep(clamped);
    return { ms: clamped };
  },

  async wait_for_url({ pattern, tab_id, timeout = 10_000, poll = 200 }) {
    if (!pattern) throw makeError("invalid_params", "wait_for_url requires pattern");
    const tab = await resolveTab({ tab_id });
    const regex = new RegExp(pattern);
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const cur = await chrome.tabs.get(tab.id);
      if (cur.url && regex.test(cur.url)) return { tab_id: tab.id, url: cur.url };
      await sleep(poll);
    }
    throw makeError("timeout", `url did not match ${pattern} within ${timeout}ms`);
  },

  async tabs_list() {
    const tabs = await chrome.tabs.query({});
    return {
      tabs: tabs.map((t) => ({
        tab_id: t.id,
        url: t.url,
        title: t.title,
        active: t.active,
        window_id: t.windowId,
      })),
    };
  },

  async focus_tab({ tab_id }) {
    if (!tab_id) throw makeError("invalid_params", "focus_tab requires tab_id");
    const t = await chrome.tabs.get(tab_id);
    await chrome.windows.update(t.windowId, { focused: true });
    await chrome.tabs.update(tab_id, { active: true });
    return { tab_id };
  },

  async ping() {
    return { pong: true, ts: Date.now(), version: chrome.runtime.getManifest().version };
  },
};

// Methods routed to the active (or specified) tab's content script — DOM
// introspection only. Input + screenshots are handled above via CDP.
const CONTENT_METHODS = new Set([
  "wait_for_element",
  "get_page_info",
  "get_element_box",
  "scroll",
  "scroll_to",
]);

// ---------- WebSocket lifecycle ----------

function bridgeWsUrlWithToken(rawUrl, token) {
  if (!token) return rawUrl;
  const u = new URL(rawUrl);
  u.searchParams.set("token", token);
  return u.toString();
}

function isLoopbackAutoBridgeUrl(rawUrl) {
  try {
    const u = new URL(rawUrl);
    return u.protocol === "ws:" && (u.hostname === "localhost" || u.hostname === "127.0.0.1");
  } catch {
    return false;
  }
}

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  clearTimeout(reconnectTimer);
  setStatus("connecting");
  try {
    ws = new WebSocket(bridgeWsUrlWithToken(state.bridge_url, state.bridge_token));
  } catch (err) {
    console.warn("[realhands] ws constructor threw", err);
    scheduleReconnect();
    return;
  }
  ws.addEventListener("open", onWsOpen);
  ws.addEventListener("message", onWsMessage);
  ws.addEventListener("close", onWsClose);
  ws.addEventListener("error", onWsError);
}

// Send the registration handshake (executor_ready) under the current
// browser_id. Called on every connect AND whenever browser_id is learned or
// changed while the WS is already open, so the bridge re-registers this
// connection under the new id.
function sendExecutorReady() {
  return sendToBridge({
    event: "executor_ready",
    browser_id: state.browser_id,
    version: chrome.runtime.getManifest().version,
    user_agent: navigator.userAgent,
    bridge_url: state.bridge_url,
  });
}

function onWsOpen() {
  reconnectAttempts = 0;
  setStatus("connected");
  sendExecutorReady();
  // Keepalive: a WS message every 20s resets the MV3 idle timer so the worker
  // (and this connection) stays alive instead of being suspended after ~30s.
  clearInterval(keepaliveTimer);
  keepaliveTimer = setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      sendToBridge({ event: "keepalive", ts: Date.now() });
    }
  }, KEEPALIVE_MS);
}

function onWsMessage(evt) {
  let msg;
  try {
    msg = JSON.parse(evt.data);
  } catch {
    sendToBridge({ error: { code: "invalid_json", message: "bridge sent non-JSON" } });
    return;
  }
  if (!msg || typeof msg !== "object") return;
  if (msg.method) {
    handleCommand(msg);
    return;
  }
  if (msg.set_state) {
    applySetState(msg.set_state, { source: "ws" });
    return;
  }
  // Other shapes (e.g. echo, ack) are ignored intentionally.
}

function onWsClose() {
  clearInterval(keepaliveTimer);
  keepaliveTimer = null;
  setStatus("disconnected");
  scheduleReconnect();
}

function onWsError(evt) {
  console.warn("[realhands] ws error", evt);
}

function scheduleReconnect() {
  const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** Math.min(reconnectAttempts, 10));
  reconnectAttempts++;
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, delay);
}

function sendToBridge(obj) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  try {
    ws.send(JSON.stringify(obj));
    return true;
  } catch (err) {
    console.warn("[realhands] ws.send failed", err);
    return false;
  }
}

// ---------- command dispatcher ----------

async function handleCommand(msg) {
  const { id, method, params = {} } = msg;
  if (params && typeof params.current_task === "object") {
    state.current_task = params.current_task;
    persistState();
  }
  try {
    let result;
    if (BG_HANDLERS[method]) {
      result = await BG_HANDLERS[method](params);
    } else if (CONTENT_METHODS.has(method)) {
      const tab = await resolveTab(params);
      const inner = await chrome.tabs.sendMessage(tab.id, { method, params });
      if (!inner) {
        throw makeError("content_script_unavailable", `no content script in tab ${tab.id}`);
      }
      if (inner.error) throw makeError(inner.error.code || "error", inner.error.message);
      result = { ...(inner.result || {}), tab_id: tab.id };
    } else {
      throw makeError("unsupported_method", `unknown method: ${method}`);
    }
    sendToBridge({ id, result });
  } catch (err) {
    sendToBridge({
      id,
      error: {
        code: err.code || "error",
        message: err.message || String(err),
      },
    });
  }
}

async function resolveTab({ tab_id } = {}) {
  if (tab_id) {
    try {
      return await chrome.tabs.get(tab_id);
    } catch {
      throw makeError("tab_unavailable", `tab ${tab_id} not found`);
    }
  }
  const [active] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (active) return active;
  const [anyActive] = await chrome.tabs.query({ active: true });
  if (anyActive) return anyActive;
  throw makeError("tab_unavailable", "no active tab");
}

// ---------- state + popup integration ----------

function setStatus(connection) {
  state.connection = connection;
  state.last_event = { kind: "status", value: connection, at: Date.now() };
  persistState();
}

function persistState() {
  chrome.storage.local.set({ realhands_state: state }).catch(() => {});
}

function applySetState(patch, { source = "internal" } = {}) {
  let needReconnect = false;
  if (patch && typeof patch.bridge_url === "string") {
    const nextUrl = patch.bridge_url.trim() || BRIDGE_URL_DEFAULT;
    if (source === "ws" && !isLoopbackAutoBridgeUrl(nextUrl)) {
      console.warn("[realhands] ignored non-loopback bridge_url from WS message", nextUrl);
    } else if (nextUrl !== state.bridge_url) {
      state.bridge_url = nextUrl;
      needReconnect = true;
    }
  }
  if (patch && typeof patch.bridge_token === "string") {
    if (source === "ws") {
      console.warn("[realhands] ignored bridge_token from WS message");
    } else if (patch.bridge_token !== state.bridge_token) {
      state.bridge_token = patch.bridge_token;
      needReconnect = true;
    }
  }
  persistState();
  if (needReconnect && ws) {
    try { ws.close(); } catch {}
  }
}

// Persist a (new) browser_id and re-register. If the WS is open we send a fresh
// executor_ready so the bridge moves this connection under the new id; otherwise
// the next connect()/onWsOpen registers it. Used both by the popup (set_browser_id)
// and by the /register?browser_id= URL-learning path below.
function applyBrowserId(rawId) {
  const id = (rawId || "").trim() || BROWSER_ID_DEFAULT;
  const changed = id !== state.browser_id;
  state.browser_id = id;
  persistState();
  if (ws && ws.readyState === WebSocket.OPEN) {
    sendExecutorReady();
  } else {
    connect();
  }
  return changed;
}

// If a localhost URL is a /register?browser_id=<ID> page, return <ID>; else null.
// This is how a SPAWNED Chrome self-identifies: it is launched at the bridge's
// /register URL, and the service worker reads the id off the tab URL here.
// (A content script can't do this — content_scripts only match https://*/*, so
// they never run on http://localhost; the SW tab-URL approach is required.)
function browserIdFromRegisterUrl(rawUrl) {
  if (!rawUrl) return null;
  let u;
  try {
    u = new URL(rawUrl);
  } catch {
    return null;
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") return null;
  if (u.hostname !== "localhost" && u.hostname !== "127.0.0.1") return null;
  if (u.pathname !== "/register") return null;
  const id = u.searchParams.get("browser_id");
  return id ? id.trim() : null;
}

// ---------- message routing (popup + content events) ----------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return;

  if (msg.from === "popup") {
    handlePopupCommand(msg, sendResponse);
    return true;
  }

  // Content-script-originated event.
  if (msg.event && sender && sender.tab) {
    const payload = {
      event: msg.event,
      tab_id: sender.tab.id,
      url: msg.url,
      title: msg.title,
      ...(msg.payload || {}),
    };
    sendToBridge(payload);
    state.last_event = { kind: msg.event, at: Date.now(), tab_id: sender.tab.id };
    persistState();
  }
});

function handlePopupCommand(msg, sendResponse) {
  switch (msg.action) {
    case "get_state":
      sendResponse({ ok: true, state });
      return;
    case "reconnect":
      reconnectAttempts = 0;
      if (ws) try { ws.close(); } catch {}
      connect();
      sendResponse({ ok: true });
      return;
    case "set_bridge_url":
      applySetState({ bridge_url: (msg.bridge_url || "").trim() || BRIDGE_URL_DEFAULT }, { source: "popup" });
      sendResponse({ ok: true, state });
      return;
    case "set_bridge_token":
      applySetState({ bridge_token: (msg.bridge_token || "").trim() }, { source: "popup" });
      sendResponse({ ok: true, state });
      return;
    case "set_browser_id":
      applyBrowserId(msg.browser_id);
      sendResponse({ ok: true, state });
      return;
    default:
      sendResponse({ ok: false, error: "unknown_popup_action" });
  }
}

// ---------- tab-level events forwarded to bridge ----------

chrome.tabs.onCreated.addListener((tab) => {
  sendToBridge({ event: "tab_created", tab_id: tab.id, url: tab.url, window_id: tab.windowId });
});

chrome.tabs.onRemoved.addListener((tabId, info) => {
  detachTab(tabId);
  sendToBridge({ event: "tab_closed", tab_id: tabId, window_id: info.windowId });
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (!changeInfo.status && !changeInfo.url) return;

  // Swarm self-identification: a spawned Chrome is launched at the bridge's
  // http://localhost:<port>/register?browser_id=<ID> page. When we see that URL,
  // adopt <ID> as our browser_id and (re)register with the bridge under it.
  const learnedId = browserIdFromRegisterUrl(changeInfo.url || tab.url);
  if (learnedId) {
    applyBrowserId(learnedId);
  }

  sendToBridge({
    event: "tab_updated",
    tab_id: tabId,
    url: tab.url,
    title: tab.title,
    status: tab.status,
    change: changeInfo,
  });
});

// ---------- side panel ----------

// Make the chat side panel openable. A default_popup is configured on the
// action, so action-click opens the popup (which carries an "Open chat" button
// that calls chrome.sidePanel.open()). We still set the behavior defensively so
// the panel is usable even if the popup is removed later.
if (chrome.sidePanel && chrome.sidePanel.setPanelBehavior) {
  chrome.sidePanel
    .setPanelBehavior({ openPanelOnActionClick: false })
    .catch(() => {});
}

// ---------- bootstrap ----------

chrome.alarms.create(HEARTBEAT_ALARM, { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== HEARTBEAT_ALARM) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    connect();
  } else {
    sendToBridge({ event: "heartbeat", ts: Date.now() });
  }
});

chrome.storage.local.get("realhands_state").then(async ({ realhands_state }) => {
  if (realhands_state?.bridge_url) state.bridge_url = realhands_state.bridge_url;
  if (typeof realhands_state?.bridge_token === "string") state.bridge_token = realhands_state.bridge_token;
  if (realhands_state?.browser_id) state.browser_id = realhands_state.browser_id;
  // Swarm self-identification on startup: a spawned Chrome is launched at the
  // bridge's /register?browser_id=<ID> URL, which may have finished loading
  // BEFORE this service worker (and its onUpdated listener below) started — so
  // the onUpdated path alone races and misses it. Scan existing tabs now.
  try {
    const tabs = await chrome.tabs.query({});
    for (const t of tabs) {
      const learnedId = browserIdFromRegisterUrl(t.url) || browserIdFromRegisterUrl(t.pendingUrl);
      if (learnedId) {
        state.browser_id = learnedId;
        persistState();
        break;
      }
    }
  } catch {
    // tabs may be unavailable mid-startup; the onUpdated listener still covers
    // any register tab that finishes loading after this point.
  }
  connect();
});
