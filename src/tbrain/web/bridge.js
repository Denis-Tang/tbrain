/* Desktop bridge: WebView2 postMessage channel (window.desktop Proxy). */
(() => {
  "use strict";

  const webview = window.chrome && window.chrome.webview ? window.chrome.webview : null;
  const requests = new Map();
  let sequence = 0;

  function newId() {
    try {
      return crypto.randomUUID();
    } catch (error) {
      sequence += 1;
      return `${Date.now().toString(36)}-${sequence.toString(36)}-${Math.random().toString(36).slice(2)}`;
    }
  }

  function dispatch(method, args) {
    return new Promise((resolve, reject) => {
      if (!webview) {
        const error = new Error("bridge-unavailable");
        error.bridge = true;
        reject(error);
        return;
      }
      const id = newId();
      requests.set(id, resolve);
      try {
        webview.postMessage({ id, method, args });
      } catch (error) {
        requests.delete(id);
        reject(error);
      }
    });
  }

  function send(command) {
    if (!webview) return;
    try {
      webview.postMessage(command);
    } catch (error) {
      /* The host may be closing; window commands are best effort. */
    }
  }

  window.desktopAvailable = !!webview;
  window.desktopBridge = { available: !!webview, send };
  window.desktop = new Proxy({}, {
    get: (_, method) => (typeof method === "string" ? (...args) => dispatch(method, args) : undefined),
  });

  if (webview) {
    webview.addEventListener("message", ({ data }) => {
      if (!data || typeof data !== "object") return;
      if (!("id" in data)) {
        window.dispatchEvent(new CustomEvent("desktop-message", { detail: data }));
        return;
      }
      const resolve = requests.get(data.id);
      if (!resolve) return;
      requests.delete(data.id);
      resolve(data.result);
    });
  }

  function wireWindow() {
    const titlebar = document.querySelector(".titlebar");
    if (titlebar) {
      titlebar.addEventListener("mousedown", (event) => {
        if (event.button === 0 && !event.target.closest("button")) send("drag");
      });
      titlebar.addEventListener("dblclick", (event) => {
        if (!event.target.closest("button")) send("maximize");
      });
    }
    document.querySelectorAll("[data-window]").forEach((button) => {
      button.addEventListener("click", () => send(button.dataset.window));
    });
  }

  function start() {
    wireWindow();
    window.dispatchEvent(new Event("desktopready"));
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
})();
