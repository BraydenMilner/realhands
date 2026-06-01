// realhands popup — reads state from chrome.storage.local, subscribes to changes,
// and offers local bridge controls. No site knowledge here.

const els = {
  connection: document.getElementById("connection-pill"),
  url: document.getElementById("bridge-url"),
  token: document.getElementById("bridge-token"),
  browserId: document.getElementById("browser-id"),
  task: document.getElementById("current-task"),
  event: document.getElementById("last-event"),
  reconnect: document.getElementById("reconnect"),
  saveUrl: document.getElementById("save-url"),
  saveToken: document.getElementById("save-token"),
  saveId: document.getElementById("save-id"),
  version: document.getElementById("version"),
};

els.version.textContent = `v${chrome.runtime.getManifest().version}`;

function render(state) {
  if (!state) return;
  const conn = state.connection || "idle";
  els.connection.textContent = conn;
  els.connection.className = `pill pill-${conn}`;

  if (els.url && document.activeElement !== els.url) {
    els.url.value = state.bridge_url || "";
  }

  if (els.token && document.activeElement !== els.token) {
    els.token.value = state.bridge_token || "";
  }

  if (els.browserId && document.activeElement !== els.browserId) {
    els.browserId.value = state.browser_id || "";
  }

  if (state.current_task && typeof state.current_task === "object") {
    const summary =
      state.current_task.platform || state.current_task.task_type
        ? [state.current_task.platform, state.current_task.task_type]
            .filter(Boolean)
            .join(" / ")
        : JSON.stringify(state.current_task);
    els.task.textContent = summary;
  } else {
    els.task.textContent = "none";
  }

  if (state.last_event) {
    const ago = Math.max(0, Math.round((Date.now() - state.last_event.at) / 1000));
    const detail = state.last_event.value ? `:${state.last_event.value}` : "";
    els.event.textContent = `${state.last_event.kind}${detail} (${ago}s ago)`;
  } else {
    els.event.textContent = "—";
  }
}

chrome.runtime.sendMessage({ from: "popup", action: "get_state" }, (res) => {
  if (res && res.ok) render(res.state);
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes.realhands_state) return;
  render(changes.realhands_state.newValue);
});

els.reconnect.addEventListener("click", () => {
  els.reconnect.disabled = true;
  chrome.runtime.sendMessage({ from: "popup", action: "reconnect" }, () => {
    setTimeout(() => (els.reconnect.disabled = false), 400);
  });
});

els.saveUrl.addEventListener("click", () => {
  const url = els.url.value.trim();
  if (!url) return;
  els.saveUrl.disabled = true;
  chrome.runtime.sendMessage(
    { from: "popup", action: "set_bridge_url", bridge_url: url },
    (res) => {
      if (res && res.ok) render(res.state);
      setTimeout(() => (els.saveUrl.disabled = false), 400);
    },
  );
});

els.url.addEventListener("keydown", (e) => {
  if (e.key === "Enter") els.saveUrl.click();
});

els.saveToken.addEventListener("click", () => {
  const token = els.token.value.trim();
  els.saveToken.disabled = true;
  chrome.runtime.sendMessage(
    { from: "popup", action: "set_bridge_token", bridge_token: token },
    (res) => {
      if (res && res.ok) render(res.state);
      setTimeout(() => (els.saveToken.disabled = false), 400);
    },
  );
});

els.token.addEventListener("keydown", (e) => {
  if (e.key === "Enter") els.saveToken.click();
});

els.saveId.addEventListener("click", () => {
  // Empty id is allowed and resolves to "default" in the service worker.
  const browserId = els.browserId.value.trim();
  els.saveId.disabled = true;
  chrome.runtime.sendMessage(
    { from: "popup", action: "set_browser_id", browser_id: browserId },
    (res) => {
      if (res && res.ok) render(res.state);
      setTimeout(() => (els.saveId.disabled = false), 400);
    },
  );
});

els.browserId.addEventListener("keydown", (e) => {
  if (e.key === "Enter") els.saveId.click();
});
