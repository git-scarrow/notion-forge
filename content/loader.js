/**
 * Content Script Loader (runs in isolated world)
 *
 * 1. Injects interceptor.js into the page's MAIN world via a <script> tag
 *    so it can monkey-patch the page's own fetch before Notion scripts load.
 * 2. Listens for window.postMessage from the page script and forwards
 *    payloads to the background service worker via browser.runtime.sendMessage.
 */

"use strict";

// ── Inject the interceptor into the page (MAIN world) ─────────────────────

const script = document.createElement("script");
script.src = browser.runtime.getURL("content/interceptor.js");
script.onload = () => script.remove();
(document.documentElement || document.head || document.body).appendChild(script);

// ── Relay messages from page world → background ───────────────────────────

const MSG_TAG = "__notion_ai_scraper__";

window.addEventListener("message", (event) => {
  if (event.source !== window) return;
  if (event.data?.tag !== MSG_TAG) return;

  const payload = event.data.payload;
  if (!payload?.type) return;

  browser.runtime.sendMessage(payload).catch((err) => {
    console.warn("[notion-ai-scraper] failed to relay to background:", err);
  });
});

console.debug("[notion-ai-scraper] loader active, interceptor injected");
