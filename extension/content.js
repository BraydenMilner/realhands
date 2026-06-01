// realhands — content script.
// Runs in every https page and implements the DOM-introspection primitives
// (element boxes, waits, page info, scroll). Input (clicks/typing/keys) and
// screenshots are handled in background.js via the chrome.debugger API so they
// are isTrusted=true. Emits page_ready events on navigation. No site knowledge.

(function () {
  if (window.__realhandsContentLoaded) return;
  window.__realhandsContentLoaded = true;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function makeError(code, message) {
    const err = new Error(message);
    err.code = code;
    return err;
  }

  function rectToBox(rect) {
    return {
      x: rect.left,
      y: rect.top,
      w: rect.width,
      h: rect.height,
      right: rect.right,
      bottom: rect.bottom,
    };
  }

  function isVisible(el) {
    if (!el || !(el instanceof Element)) return false;
    if (typeof el.checkVisibility === "function") return el.checkVisibility();
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = window.getComputedStyle(el);
    return style.visibility !== "hidden" && style.display !== "none" && style.opacity !== "0";
  }

  // Best-effort visible label for an element: its own innerText, falling back
  // to aria-label / aria-labelledby / title / value / alt. Used by the
  // executor's last-resort money guard, so it errs toward returning *something*.
  function describeElement(el) {
    if (!el || !(el instanceof Element)) return { text: "", aria_label: "", label: "" };
    const text = (el.innerText || el.textContent || "").trim().slice(0, 200);
    let aria = el.getAttribute("aria-label") || "";
    const labelledby = el.getAttribute("aria-labelledby");
    if (!aria && labelledby) {
      aria = labelledby
        .split(/\s+/)
        .map((id) => document.getElementById(id)?.innerText || "")
        .join(" ")
        .trim();
    }
    const label =
      el.getAttribute("title") ||
      el.getAttribute("alt") ||
      el.getAttribute("name") ||
      el.getAttribute("value") ||
      "";
    return { text, aria_label: aria.trim().slice(0, 200), label: String(label).trim().slice(0, 200) };
  }

  const HANDLERS = {
    async wait_for_element({ selector, timeout = 5000, poll = 100, require_visible = true }) {
      if (!selector) throw makeError("invalid_params", "wait_for_element requires selector");
      const deadline = Date.now() + timeout;
      while (Date.now() < deadline) {
        const el = document.querySelector(selector);
        if (el && (!require_visible || isVisible(el))) {
          return {
            selector,
            found: true,
            visible: isVisible(el),
            box: rectToBox(el.getBoundingClientRect()),
            tag: el.tagName.toLowerCase(),
          };
        }
        await sleep(poll);
      }
      // On timeout, return a clean negative instead of throwing so the agent's
      // element_present verify gets {found:false} rather than an exception.
      return { selector, found: false, visible: false };
    },

    // Describe the element at a viewport point (for the executor's money guard
    // on click_at, where there is no selector).
    async describe_point({ x, y }) {
      const el = document.elementFromPoint(x, y);
      return { ...describeElement(el), tag: el?.tagName?.toLowerCase() || null };
    },

    // Describe an element matched by selector (for the money guard on
    // click_selector / type / key_press).
    async describe_element({ selector }) {
      if (!selector) throw makeError("invalid_params", "describe_element requires selector");
      const el = document.querySelector(selector);
      return { ...describeElement(el), tag: el?.tagName?.toLowerCase() || null };
    },

    // Describe the currently focused element (for the money guard on a blind
    // Enter / key_press with no target selector — a focused redemption form).
    async describe_active() {
      const el = document.activeElement;
      return { ...describeElement(el), tag: el?.tagName?.toLowerCase() || null };
    },

    async get_page_info() {
      return {
        url: location.href,
        origin: location.origin,
        title: document.title,
        ready_state: document.readyState,
        viewport: { w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio },
        doc_size: {
          w: document.documentElement.scrollWidth,
          h: document.documentElement.scrollHeight,
        },
        scroll: { x: window.scrollX, y: window.scrollY },
        active_tag: document.activeElement?.tagName?.toLowerCase() || null,
      };
    },

    async get_element_box({ selector }) {
      if (!selector) throw makeError("invalid_params", "get_element_box requires selector");
      const el = document.querySelector(selector);
      if (!el) throw makeError("element_not_found", selector);
      return {
        selector,
        box: rectToBox(el.getBoundingClientRect()),
        visible: isVisible(el),
        tag: el.tagName.toLowerCase(),
      };
    },

    async scroll({ x = 0, y = 0, behavior = "auto" }) {
      window.scrollBy({ left: x, top: y, behavior });
      await sleep(50);
      return { x: window.scrollX, y: window.scrollY };
    },

    async scroll_to({ selector, behavior = "auto", block = "center", inline = "nearest" }) {
      if (!selector) throw makeError("invalid_params", "scroll_to requires selector");
      const el = document.querySelector(selector);
      if (!el) throw makeError("element_not_found", selector);
      el.scrollIntoView({ behavior, block, inline });
      await sleep(50);
      return {
        selector,
        scroll: { x: window.scrollX, y: window.scrollY },
        box: rectToBox(el.getBoundingClientRect()),
      };
    },
  };

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || !msg.method) return;
    const handler = HANDLERS[msg.method];
    if (!handler) {
      sendResponse({ error: { code: "unsupported_method", message: msg.method } });
      return;
    }
    Promise.resolve()
      .then(() => handler(msg.params || {}))
      .then((result) => sendResponse({ result }))
      .catch((err) =>
        sendResponse({
          error: { code: err.code || "error", message: err.message || String(err) },
        }),
      );
    return true;
  });

  function emitPageReady() {
    try {
      chrome.runtime.sendMessage({
        event: "page_ready",
        url: location.href,
        title: document.title,
        payload: { ready_state: document.readyState },
      });
    } catch {
      // service worker may be cycling; non-fatal
    }
  }

  emitPageReady();
  window.addEventListener("popstate", emitPageReady, { passive: true });
  window.addEventListener("hashchange", emitPageReady, { passive: true });
})();
