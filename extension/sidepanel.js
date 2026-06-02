// realhands side panel — a BYO-key chat UI that drives the local Agent Bridge.
//
// Contract (must match the bridge half exactly):
//   POST /agent/run     {task, browser_id?, max_steps?, mode?}  -> {run_id}
//   POST /agent/stop    {run_id?}                               -> {stopped:true}
//   POST /agent/approve {run_id, approved}                      -> {ok:true}
//   POST /agent/reply   {run_id, text}                          -> {ok:true}
//   GET  /events        SSE; agent events look like:
//     {type:"agent", run_id, step, phase, action?, reasoning?,
//      confidence?, model?, cost_usd?, message?}
//   The "awaiting_input" phase means the agent ran an ask_user action: it put a
//   question (in `message`) to the human and is waiting for POST /agent/reply.
//
// Config is read from chrome.storage.local.realhands_state (same place the popup
// writes it): bridge_url (a ws:// URL), bridge_token, browser_id. We translate
// the ws:// bridge URL into the http:// REST/SSE base. host_permissions in the
// manifest cover http://localhost:7878 and http://127.0.0.1:7878 so cross-origin
// fetch from this extension page is allowed with no CORS dance.

const DEFAULT_HTTP_BASE = "http://localhost:7878";

const els = {
  version: document.getElementById("version"),
  connection: document.getElementById("connection-pill"),
  messages: document.getElementById("messages"),
  statRun: document.getElementById("stat-run"),
  statStep: document.getElementById("stat-step"),
  statModel: document.getElementById("stat-model"),
  statCost: document.getElementById("stat-cost"),
  approvalBar: document.getElementById("approval-bar"),
  approvalDetail: document.getElementById("approval-detail"),
  approveBtn: document.getElementById("approve-btn"),
  rejectBtn: document.getElementById("reject-btn"),
  modeSelect: document.getElementById("mode-select"),
  autonomyToggle: document.getElementById("autonomy-toggle"),
  modeDoBtn: document.getElementById("mode-do-btn"),
  modeAskBtn: document.getElementById("mode-ask-btn"),
  stopBtn: document.getElementById("stop-btn"),
  micBtn: document.getElementById("mic-btn"),
  taskInput: document.getElementById("task-input"),
  sendBtn: document.getElementById("send-btn"),
};

els.version.textContent = `v${chrome.runtime.getManifest().version}`;

// ---------- runtime state ----------

const config = {
  httpBase: DEFAULT_HTTP_BASE,
  token: "",
  browserId: "",
};

let currentRunId = null; // the run we are actively streaming/filtering for
let runActive = false; // a run is in flight (Send disabled, Stop enabled)
let totalCost = 0;
let stepCount = 0;
let modelName = "";
let evtSource = null;
let interactionMode = "do"; // "do" or "ask"

// ---------- config loading ----------

// The popup stores the bridge URL as ws://host:port. The REST + SSE endpoints
// live on the same host:port over http. Translate ws->http / wss->https.
function wsToHttp(wsUrl) {
  if (!wsUrl || typeof wsUrl !== "string") return DEFAULT_HTTP_BASE;
  try {
    const u = new URL(wsUrl);
    const scheme = u.protocol === "wss:" ? "https:" : "http:";
    return `${scheme}//${u.host}`;
  } catch {
    return DEFAULT_HTTP_BASE;
  }
}

function applyState(realhandsState) {
  const s = realhandsState || {};
  config.httpBase = wsToHttp(s.bridge_url);
  config.token = typeof s.bridge_token === "string" ? s.bridge_token : "";
  config.browserId = s.browser_id || "";
}

chrome.storage.local.get("realhands_state").then(({ realhands_state }) => {
  applyState(realhands_state);
  openEventStream();
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes.realhands_state) return;
  applyState(changes.realhands_state.newValue);
  openEventStream();
});

// ---------- interaction mode (Do / Ask) ----------

function setInteractionMode(mode) {
  interactionMode = mode;
  if (mode === "ask") {
    els.modeDoBtn.classList.remove("toggle-active");
    els.modeAskBtn.classList.add("toggle-active");
    els.autonomyToggle.classList.add("hidden");
    els.stopBtn.classList.add("hidden");
    els.taskInput.placeholder = els.taskInput.dataset.placeholderAsk || "Ask a question about this page…";
  } else {
    els.modeDoBtn.classList.add("toggle-active");
    els.modeAskBtn.classList.remove("toggle-active");
    els.autonomyToggle.classList.remove("hidden");
    els.stopBtn.classList.remove("hidden");
    els.taskInput.placeholder = els.taskInput.dataset.placeholderDo || "Describe a task for the agent…";
  }
}

els.modeDoBtn.addEventListener("click", () => setInteractionMode("do"));
els.modeAskBtn.addEventListener("click", () => setInteractionMode("ask"));

// ---------- fetch helper ----------

function bridgeHeaders(extra) {
  const headers = { "Content-Type": "application/json", ...(extra || {}) };
  if (config.token) headers["X-RealHands-Token"] = config.token;
  return headers;
}

async function postJSON(path, body) {
  const res = await fetch(`${config.httpBase}${path}`, {
    method: "POST",
    headers: bridgeHeaders(),
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (data && (data.error || data.message)) detail = data.error || data.message;
    } catch {
      /* non-JSON body; keep status string */
    }
    throw new Error(detail);
  }
  return res.json();
}

async function getJSON(path) {
  const res = await fetch(`${config.httpBase}${path}`, {
    method: "GET",
    headers: bridgeHeaders(),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ---------- chat rendering ----------

function scrollToBottom() {
  els.messages.scrollTop = els.messages.scrollHeight;
}

function addBubble(kind, build) {
  const div = document.createElement("div");
  div.className = `bubble bubble-${kind}`;
  build(div);
  els.messages.appendChild(div);
  scrollToBottom();
  return div;
}

function addUserBubble(text) {
  addBubble("user", (el) => {
    el.textContent = text;
  });
}

function addSystemBubble(text) {
  addBubble("system", (el) => {
    el.textContent = text;
  });
}

function addErrorBubble(text) {
  addBubble("error", (el) => {
    el.textContent = text;
  });
}

// Render one agent event as a bubble. The visual style depends on the phase.
function addAgentBubble(evt) {
  const phase = evt.phase || "decision";
  let kind = "agent";
  if (phase === "done") kind = "done";
  else if (phase === "awaiting_approval" || phase === "awaiting_input") kind = "await";
  else if (phase === "error" || phase === "abort") kind = "error";

  addBubble(kind, (el) => {
    const phaseTag = document.createElement("span");
    phaseTag.className = "bubble-phase";
    const stepLabel = typeof evt.step === "number" ? `step ${evt.step} · ` : "";
    phaseTag.textContent = `${stepLabel}${phase}`;
    el.appendChild(phaseTag);

    if (evt.action) {
      const action = document.createElement("div");
      action.className = "bubble-action";
      action.textContent = evt.action;
      el.appendChild(action);
    }

    if (evt.reasoning) {
      const reasoning = document.createElement("div");
      reasoning.className = "bubble-reasoning";
      reasoning.textContent = evt.reasoning;
      el.appendChild(reasoning);
    }

    if (evt.message) {
      const msg = document.createElement("div");
      msg.className = "bubble-reasoning";
      msg.textContent = evt.message;
      el.appendChild(msg);
    }

    const metaBits = [];
    if (typeof evt.confidence === "number") {
      metaBits.push(`conf ${(evt.confidence * 100).toFixed(0)}%`);
    }
    if (evt.model) metaBits.push(evt.model);
    if (typeof evt.cost_usd === "number") metaBits.push(`$${evt.cost_usd.toFixed(4)}`);
    if (metaBits.length) {
      const meta = document.createElement("div");
      meta.className = "bubble-meta";
      meta.textContent = metaBits.join("  ·  ");
      el.appendChild(meta);
    }
  });
}

// ---------- readout ----------

function resetReadout(runId) {
  totalCost = 0;
  stepCount = 0;
  modelName = "";
  els.statRun.textContent = runId ? runId.slice(0, 12) : "—";
  els.statStep.textContent = "0";
  els.statModel.textContent = "—";
  els.statCost.textContent = "$0.0000";
}

function updateReadout(evt) {
  if (typeof evt.step === "number" && evt.step > stepCount) {
    stepCount = evt.step;
    els.statStep.textContent = String(stepCount);
  }
  if (evt.model) {
    modelName = evt.model;
    els.statModel.textContent = modelName;
  }
  if (typeof evt.cost_usd === "number") {
    totalCost += evt.cost_usd;
    els.statCost.textContent = `$${totalCost.toFixed(4)}`;
  }
}

// ---------- run lifecycle ----------

function setRunActive(active) {
  runActive = active;
  els.sendBtn.disabled = active;
  els.taskInput.disabled = active;
  els.stopBtn.disabled = !active;
  if (!active) {
    hideApproval();
    exitReplyMode();
  }
}

// ---------- ask_user (human-in-the-loop) ----------
//
// When the agent runs an ask_user action the bridge emits phase:awaiting_input
// with the question in `message`. We re-open the composer (which Send disables
// during a run) so the human can type/speak an answer; Send then posts it to
// /agent/reply instead of starting a new run, and the loop resumes.

let pendingReplyRunId = null;
let pendingReplyAwaitId = null;

function enterReplyMode(evt) {
  pendingReplyRunId = evt.run_id;
  pendingReplyAwaitId = evt.await_id || null;
  els.taskInput.disabled = false;
  els.sendBtn.disabled = false;
  els.sendBtn.textContent = "Answer";
  els.taskInput.placeholder = "Type your answer to the agent…";
  els.taskInput.focus();
}

function exitReplyMode() {
  if (!pendingReplyRunId) return;
  pendingReplyRunId = null;
  pendingReplyAwaitId = null;
  els.sendBtn.textContent = "Send";
  els.taskInput.placeholder = "Describe a task for the agent…";
  // If the run is still going, re-lock the composer until it finishes (or asks
  // again). When the run has ended, setRunActive(false) already re-enabled it.
  if (runActive) {
    els.taskInput.disabled = true;
    els.sendBtn.disabled = true;
  }
}

async function sendReply() {
  const text = els.taskInput.value.trim();
  if (!text || !pendingReplyRunId) return;
  const runId = pendingReplyRunId;
  const awaitId = pendingReplyAwaitId;
  addUserBubble(text);
  els.taskInput.value = "";
  autoSizeTextarea();
  exitReplyMode(); // run continues; re-lock the composer
  try {
    const body = { run_id: runId, text };
    if (awaitId) body.await_id = awaitId;
    const res = await postJSON("/agent/reply", body);
    // The bridge returns HTTP 200 {ok:false} when the run is gone or not
    // awaiting input — surface it instead of silently dropping the answer.
    if (!res || res.ok === false) {
      addErrorBubble("The agent didn't take that answer — the run may have ended.");
    }
  } catch (err) {
    addErrorBubble(`Could not send answer: ${describeError(err)}`);
  }
}

// Send button / Enter dispatch to the right action depending on whether the
// agent is waiting on an answer.
function onSend() {
  if (pendingReplyRunId) sendReply();
  else if (interactionMode === "ask") sendAsk();
  else sendTask();
}

async function sendAsk() {
  const message = els.taskInput.value.trim();
  if (!message) return;
  addUserBubble(message);
  els.taskInput.value = "";
  autoSizeTextarea();
  try {
    const body = { message };
    if (config.browserId) body.browser_id = config.browserId;
    const data = await postJSON("/agent/ask", body);
    if (!data || !data.ok) {
      addErrorBubble("Bridge rejected the question.");
    }
  } catch (err) {
    addErrorBubble(`Could not ask: ${describeError(err)}`);
  }
}

async function sendTask() {
  const task = els.taskInput.value.trim();
  if (!task || runActive) return;

  // Clear any stale reply mode left by a previous run (e.g. a lost terminal
  // event) so this fresh task never dispatches as an answer to an old run.
  exitReplyMode();

  const mode = els.modeSelect.value || "gated";

  addUserBubble(task);
  els.taskInput.value = "";
  autoSizeTextarea();
  setRunActive(true);

  try {
    const body = { task, mode };
    if (config.browserId) body.browser_id = config.browserId;
    const data = await postJSON("/agent/run", body);
    currentRunId = data && data.run_id ? data.run_id : null;
    if (!currentRunId) {
      addErrorBubble("Bridge accepted the run but returned no run_id.");
      setRunActive(false);
      return;
    }
    resetReadout(currentRunId);
    addSystemBubble(`Run started (${mode}) · ${currentRunId}`);
    // Ensure the event stream is live so we catch this run's events.
    openEventStream();
  } catch (err) {
    addErrorBubble(`Could not start run: ${describeError(err)}`);
    setRunActive(false);
  }
}

async function stopRun() {
  els.stopBtn.disabled = true;
  try {
    await postJSON("/agent/stop", currentRunId ? { run_id: currentRunId } : {});
    addSystemBubble("Stop requested.");
    // Free the composer NOW rather than waiting for the round-trip "stopped"
    // event — if that SSE frame is lost, Stop must still be a real escape
    // (otherwise a run paused at ask_user would strand the panel in answer mode).
    setRunActive(false);
  } catch (err) {
    addErrorBubble(`Stop failed: ${describeError(err)}`);
    els.stopBtn.disabled = false;
  }
}

// ---------- approval (ask mode) ----------

let pendingApprovalRunId = null;
let pendingApprovalAwaitId = null;

function showApproval(evt) {
  pendingApprovalRunId = evt.run_id;
  pendingApprovalAwaitId = evt.await_id || null;
  const bits = [evt.action, evt.reasoning].filter(Boolean).join(" — ");
  const label = evt.sensitive ? "⚠️ Approve sensitive action" : "Approve action?";
  els.approvalDetail.textContent = bits || label;
  els.approvalDetail.title = bits || "";
  els.approvalBar.classList.remove("hidden");
}

function hideApproval() {
  pendingApprovalRunId = null;
  pendingApprovalAwaitId = null;
  els.approvalBar.classList.add("hidden");
}

async function respondApproval(approved) {
  if (!pendingApprovalRunId) return;
  const runId = pendingApprovalRunId;
  const awaitId = pendingApprovalAwaitId;
  els.approveBtn.disabled = true;
  els.rejectBtn.disabled = true;
  try {
    const body = { run_id: runId, approved };
    if (awaitId) body.await_id = awaitId;
    const res = await postJSON("/agent/approve", body);
    if (!res || res.ok === false) {
      addErrorBubble("That decision didn't land — the run may have ended.");
    } else {
      addSystemBubble(approved ? "Approved." : "Rejected — stopping run.");
    }
  } catch (err) {
    addErrorBubble(`Approval failed: ${describeError(err)}`);
  } finally {
    els.approveBtn.disabled = false;
    els.rejectBtn.disabled = false;
    hideApproval();
  }
}

// ---------- SSE event stream ----------

function setConnection(stateName) {
  els.connection.textContent = stateName;
  els.connection.className = `pill pill-${stateName}`;
}

function openEventStream() {
  if (evtSource) {
    try {
      evtSource.close();
    } catch {
      /* ignore */
    }
    evtSource = null;
  }

  setConnection("connecting");
  const controller = new AbortController();
  evtSource = { close: () => controller.abort() };

  (async () => {
    try {
      try {
        await getJSON("/health");
      } catch {
        /* continue to stream attempt; errors there drive reconnect state */
      }
      const headers = {};
      if (config.token) headers["X-RealHands-Token"] = config.token;
      const res = await fetch(`${config.httpBase}/events`, {
        method: "GET",
        headers,
        signal: controller.signal,
      });
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
      setConnection("connected");

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let split;
        while ((split = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          handleSseFrame(frame);
        }
      }
      if (!controller.signal.aborted) throw new Error("event stream closed");
    } catch (err) {
      if (controller.signal.aborted) return;
      setConnection("disconnected");
      setTimeout(() => {
        if (evtSource && evtSource.close) openEventStream();
      }, 1500);
    }
  })();
}

function handleSseFrame(frame) {
  const dataLines = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("data: ")) dataLines.push(line.slice(6));
  }
  if (!dataLines.length) return;
  let evt;
  try {
    evt = JSON.parse(dataLines.join("\n"));
  } catch {
    return;
  }
  handleEvent(evt);
}

function handleChatEvent(evt) {
  const role = evt.role || "assistant";
  if (role === "error") {
    addErrorBubble(evt.text || "An error occurred.");
  } else if (role === "tool") {
    addBubble("system", (el) => {
      el.textContent = evt.text || "Used a tool.";
      el.style.opacity = "0.65";
    });
  } else if (evt.done) {
    addBubble("agent", (el) => {
      el.textContent = evt.text || "";
    });
  } else {
    addBubble("system", (el) => {
      el.textContent = evt.text || "";
      el.style.opacity = "0.65";
    });
  }
}

function handleEvent(evt) {
  if (!evt) return;
  if (evt.type === "chat") {
    handleChatEvent(evt);
    return;
  }
  if (evt.type !== "agent") return;
  // Only render events for the run we launched from this panel.
  // Drop any run-tagged event that isn't this panel's current run — INCLUDING
  // when we have no current run yet (currentRunId null). Otherwise a stray
  // awaiting_input from another run could arm reply mode against a run we never
  // launched, and a Send would post the human's answer to the wrong run.
  if (evt.run_id && evt.run_id !== currentRunId) return;

  updateReadout(evt);

  switch (evt.phase) {
    case "awaiting_approval":
      addAgentBubble(evt);
      showApproval(evt);
      break;
    case "awaiting_input":
      addAgentBubble(evt);
      addSystemBubble("The agent is asking you something — type your answer below.");
      enterReplyMode(evt);
      break;
    case "done":
      addAgentBubble(evt);
      addBubble("done", (el) => {
        el.textContent = evt.message
          ? `Done: ${evt.message}`
          : "Run complete.";
      });
      setRunActive(false);
      break;
    case "abort":
    case "error":
      addAgentBubble(evt);
      setRunActive(false);
      break;
    case "stopped":
      addSystemBubble(evt.message || "Run stopped.");
      setRunActive(false);
      break;
    default:
      // start / decision / acted and any other informational phases
      addAgentBubble(evt);
  }
}

// ---------- mic (Web Speech API) ----------

let recognition = null;
let recognizing = false;

function initSpeech() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    els.micBtn.disabled = true;
    els.micBtn.title = "Speech recognition not supported in this browser";
    return;
  }
  recognition = new SR();
  recognition.continuous = false;
  recognition.interimResults = true;
  recognition.lang = "en-US";

  let baseText = "";

  recognition.onstart = () => {
    recognizing = true;
    els.micBtn.classList.add("recording");
    baseText = els.taskInput.value ? els.taskInput.value.trimEnd() + " " : "";
  };

  recognition.onresult = (e) => {
    let transcript = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      transcript += e.results[i][0].transcript;
    }
    els.taskInput.value = baseText + transcript;
    autoSizeTextarea();
  };

  recognition.onerror = (e) => {
    if (e.error && e.error !== "no-speech" && e.error !== "aborted") {
      addSystemBubble(`Mic error: ${e.error}`);
    }
  };

  recognition.onend = () => {
    recognizing = false;
    els.micBtn.classList.remove("recording");
  };
}

function toggleMic() {
  if (!recognition) return;
  if (recognizing) {
    try {
      recognition.stop();
    } catch {
      /* ignore */
    }
  } else {
    try {
      recognition.start();
    } catch {
      /* start() throws if already started; ignore */
    }
  }
}

// ---------- misc helpers ----------

function describeError(err) {
  if (!err) return "unknown error";
  const msg = err.message || String(err);
  // A failed fetch to a down bridge surfaces as a TypeError "Failed to fetch".
  if (/failed to fetch|networkerror|load failed/i.test(msg)) {
    return `bridge not reachable at ${config.httpBase}`;
  }
  return msg;
}

function autoSizeTextarea() {
  els.taskInput.style.height = "auto";
  els.taskInput.style.height = `${Math.min(els.taskInput.scrollHeight, 120)}px`;
}

// ---------- wiring ----------

els.sendBtn.addEventListener("click", onSend);
els.stopBtn.addEventListener("click", stopRun);
els.approveBtn.addEventListener("click", () => respondApproval(true));
els.rejectBtn.addEventListener("click", () => respondApproval(false));
els.micBtn.addEventListener("click", toggleMic);

els.taskInput.addEventListener("input", autoSizeTextarea);
els.taskInput.addEventListener("keydown", (e) => {
  // Enter sends; Shift+Enter inserts a newline.
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    onSend();
  }
});

initSpeech();
