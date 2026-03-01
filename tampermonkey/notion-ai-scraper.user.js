// ==UserScript==
// @name         Notion AI Chat Scraper
// @namespace    https://notion.so
// @version      0.3.1
// @description  Captures Notion AI chat conversations (live + historical) and exports as Markdown or JSON
// @author       notion-ai-scraper
// @match        https://www.notion.so/*
// @match        https://notion.so/*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_registerMenuCommand
// @run-at       document-start
// ==/UserScript==

/**
 * Architecture:
 *   GM_* grants cause Tampermonkey to run in a sandbox — window.fetch patches
 *   there don't affect the real page. We inject a <script> tag into the page's
 *   MAIN world that patches fetch and posts captured data via window.postMessage.
 *   This script listens for those messages and stores them via GM_setValue.
 */
(function () {
  "use strict";

  const MSG_TAG = "__notion_ai_scraper__";
  const STORAGE_KEY = "notion_ai_conversations";

  // ── Inject page-world interceptor ─────────────────────────────────────────

  const pageScript = `(function () {
  "use strict";
  const MSG_TAG = "__notion_ai_scraper__";
  const LIVE_PATH = "/api/v3/runInferenceTranscript";
  const SYNC_PATH = "/api/v3/syncRecordValuesSpaceInitial";

  function emit(payload) {
    window.postMessage({ tag: MSG_TAG, payload }, "*");
  }

  function cleanText(text) {
    return text
      .replace(/<lang[^>]*\\/>/g, "")
      .replace(/<edit_reference[^>]*>[\\s\\S]*?<\\/edit_reference>/g, "")
      .trim();
  }

  function extractRichText(value) {
    if (!Array.isArray(value)) return typeof value === "string" ? value.trim() : null;
    return value.map((chunk) => {
      if (!Array.isArray(chunk)) return typeof chunk === "string" ? chunk : "";
      const text = chunk[0] ?? "";
      const annotations = chunk[1];
      if (text === "\\u2023" && Array.isArray(annotations)) {
        for (const ann of annotations) {
          if (Array.isArray(ann) && ann.length >= 2) {
            if (ann[0] === "p") return "[page:" + ann[1] + "]";
            if (ann[0] === "u") return "[user:" + ann[1] + "]";
            if (ann[0] === "a") return "[agent:" + ann[1] + "]";
          }
        }
      }
      return text;
    }).join("").trim() || null;
  }

  function extractUserMessage(reqBody) {
    if (!reqBody?.transcript) return null;
    const userEntries = reqBody.transcript.filter((e) => e.type === "user");
    const last = userEntries.at(-1);
    if (!last?.value) return null;
    return extractRichText(last.value);
  }

  function handleSyncResponse(data) {
    const rm = data?.recordMap ?? {};
    const threads = {};
    for (const [id, rec] of Object.entries(rm.thread ?? {})) {
      const val = rec?.value?.value ?? rec?.value ?? {};
      if (val.type === "workflow" && val.messages?.length) threads[id] = val;
    }
    const messages = {};
    for (const [id, rec] of Object.entries(rm.thread_message ?? {})) {
      const val = rec?.value?.value ?? rec?.value ?? {};
      if (val.step || val.role) messages[id] = val;
    }
    if (!Object.keys(threads).length && !Object.keys(messages).length) return;
    emit({ type: "SYNC_RECORDS", threads, messages });
  }

  async function handleNDJSONStream(response, meta) {
    const reader = response.body?.getReader();
    if (!reader) return;
    const decoder = new TextDecoder();
    let buffer = "";
    const lines = [];
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\\n");
        buffer = parts.pop() ?? "";
        for (const p of parts) {
          const t = p.trim();
          if (!t) continue;
          try { lines.push(JSON.parse(t)); } catch {}
        }
      }
      if (buffer.trim()) { try { lines.push(JSON.parse(buffer.trim())); } catch {} }
    } catch {}
    if (lines.length) emit({ type: "TRANSCRIPT", lines, meta });
  }

  const _fetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : input?.url ?? "";
    let path = "";
    try { path = new URL(url, location.origin).pathname; } catch {}

    if (path === LIVE_PATH) {
      let reqBody = null;
      try {
        const raw = init?.body ?? (input instanceof Request ? await input.clone().text() : null);
        if (raw) reqBody = JSON.parse(raw);
      } catch {}
      const response = await _fetch(input, init);
      handleNDJSONStream(response.clone(), {
        userMessage: extractUserMessage(reqBody),
        traceId: reqBody?.traceId ?? null,
        spaceId: reqBody?.spaceId ?? null,
      });
      return response;
    }

    if (path === SYNC_PATH) {
      const response = await _fetch(input, init);
      response.clone().json().then((data) => {
        try { handleSyncResponse(data); } catch {}
      }).catch(() => {});
      return response;
    }

    return _fetch(input, init);
  };

  console.debug("[notion-ai-scraper] v0.3.1 page-world interceptor active");
})();`;

  const script = document.createElement("script");
  script.textContent = pageScript;
  (document.head ?? document.documentElement).appendChild(script);
  script.remove();

  // ── Receive messages from page world ──────────────────────────────────────

  window.addEventListener("message", (event) => {
    if (event.source !== window || event.data?.tag !== MSG_TAG) return;
    const { payload } = event.data;
    if (!payload?.type) return;

    if (payload.type === "TRANSCRIPT") {
      handleTranscript(payload.lines, payload.meta);
    } else if (payload.type === "SYNC_RECORDS") {
      handleSyncRecords(payload.threads, payload.messages);
    }
  });

  // ── Storage ───────────────────────────────────────────────────────────────

  function loadStore() {
    try { return JSON.parse(GM_getValue(STORAGE_KEY, "{}")); } catch { return {}; }
  }

  function saveStore(store) {
    GM_setValue(STORAGE_KEY, JSON.stringify(store));
  }

  // ── Shared helpers ────────────────────────────────────────────────────────

  function cleanText(text) {
    return text
      .replace(/<lang[^>]*\/>/g, "")
      .replace(/<edit_reference[^>]*>[\s\S]*?<\/edit_reference>/g, "")
      .trim();
  }

  function extractRichText(value) {
    if (!Array.isArray(value)) return typeof value === "string" ? value.trim() : null;
    return value.map((chunk) => {
      if (!Array.isArray(chunk)) return typeof chunk === "string" ? chunk : "";
      const text = chunk[0] ?? "";
      const annotations = chunk[1];
      if (text === "\u2023" && Array.isArray(annotations)) {
        for (const ann of annotations) {
          if (Array.isArray(ann) && ann.length >= 2) {
            if (ann[0] === "p") return `[page:${ann[1]}]`;
            if (ann[0] === "u") return `[user:${ann[1]}]`;
            if (ann[0] === "a") return `[agent:${ann[1]}]`;
          }
        }
      }
      return text;
    }).join("").trim() || null;
  }

  // ── TRANSCRIPT handler (live chat) ────────────────────────────────────────

  function handleTranscript(ndjsonLines, meta) {
    const contentByPath = {};
    const steps = [];
    const toolResults = [];

    for (const obj of ndjsonLines) {
      if (obj.type !== "patch") continue;
      for (const v of obj.v ?? []) {
        const op = v.o, path = v.p ?? "", val = v.v;
        if (op === "a" && path.endsWith("/-") && val !== null && typeof val === "object") {
          if (val.type === "agent-inference") steps.push({ id: val.id, model: null });
          else if (val.type === "agent-tool-result") toolResults.push({ toolName: val.toolName, state: val.state, input: val.input });
        }
        if ((op === "x" || op === "p") && path.includes("/content") && typeof val === "string") {
          contentByPath[path] = op === "x" ? (contentByPath[path] ?? "") + val : val;
        }
        if (op === "a" && path.includes("/model") && typeof val === "string") {
          const last = steps.at(-1);
          if (last) last.model = val;
        }
      }
    }

    const inferenceTexts = [];
    for (const [path, text] of Object.entries(contentByPath)) {
      const match = path.match(/^\/s\/(\d+)\/value\/(\d+)\/content$/);
      if (!match) continue;
      const trimmed = text.trim();
      if (trimmed.startsWith("{") && (trimmed.includes('"urls"') || trimmed.includes('"pageUrl"') || trimmed.includes('"command"'))) continue;
      if (trimmed.length > 0) inferenceTexts.push({ stepIdx: +match[1], valueIdx: +match[2], content: trimmed });
    }
    inferenceTexts.sort((a, b) => a.stepIdx - b.stepIdx || a.valueIdx - b.valueIdx);

    const stepGroups = new Map();
    for (const t of inferenceTexts) {
      if (!stepGroups.has(t.stepIdx)) stepGroups.set(t.stepIdx, []);
      stepGroups.get(t.stepIdx).push(t);
    }
    const groupKeys = [...stepGroups.keys()].sort((a, b) => a - b);
    const lastGroup = groupKeys.at(-1);
    const responseParts = [], thinkingParts = [];
    for (const key of groupKeys) {
      const texts = stepGroups.get(key).map((t) => t.content);
      if (key === lastGroup) responseParts.push(...texts);
      else for (const text of texts) { if (text.length > 200) thinkingParts.push(text); else responseParts.push(text); }
    }

    const assistantContent = cleanText(responseParts.join("\n"));
    const thinkingContent = thinkingParts.join("\n").trim() || null;
    if (!assistantContent && !meta.userMessage) return;

    const key = meta.traceId ?? `unknown-${Date.now()}`;
    const store = loadStore();
    if (!store[key]) {
      store[key] = { id: key, spaceId: meta.spaceId, model: null, turns: [], toolCalls: [], createdAt: Date.now() };
    }
    const entry = store[key];
    if (meta.userMessage) {
      const lastUser = entry.turns.findLast((t) => t.role === "user");
      if (!(lastUser && lastUser.content === meta.userMessage && Math.abs((lastUser.timestamp ?? 0) - Date.now()) < 2000)) {
        entry.turns.push({ role: "user", content: meta.userMessage, timestamp: Date.now() });
      }
    }
    if (assistantContent) {
      const turn = { role: "assistant", content: assistantContent, timestamp: Date.now() };
      if (thinkingContent) turn.thinking = thinkingContent;
      entry.turns.push(turn);
    }
    entry.model = steps.find((s) => s.model)?.model ?? entry.model;
    const fc = toolResults.filter((t) => t.toolName && t.state !== "pending").map((t) => ({ tool: t.toolName, input: t.input }));
    if (fc.length) entry.toolCalls.push(...fc);
    entry.updatedAt = Date.now();
    saveStore(store);
    console.debug(`[notion-ai-scraper] live: trace ${key}, turns=${entry.turns.length}`);
  }

  // ── SYNC_RECORDS handler (historical chat) ────────────────────────────────

  function handleSyncRecords(threads, messages) {
    if (!threads && !messages) return;
    const store = loadStore();

    for (const [threadId, thread] of Object.entries(threads ?? {})) {
      const key = `thread-${threadId}`;
      if (!store[key]) {
        store[key] = {
          id: key, threadId, title: thread.data?.title ?? null, spaceId: thread.space_id,
          model: null, turns: [], toolCalls: [], createdAt: thread.created_time ?? Date.now(),
          messageOrder: thread.messages,
        };
      } else {
        store[key].messageOrder = thread.messages;
        if (thread.data?.title) store[key].title = thread.data.title;
      }
      store[key].updatedAt = Date.now();
    }

    for (const [msgId, msg] of Object.entries(messages ?? {})) {
      const step = msg.step ?? {};
      const key = `thread-${msg.parent_id}`;
      if (!store[key]) {
        store[key] = {
          id: key, threadId: msg.parent_id, title: null, spaceId: msg.space_id,
          model: null, turns: [], toolCalls: [], createdAt: msg.created_time ?? Date.now(), messageOrder: [],
        };
      }
      const entry = store[key];
      if (!entry._processedMsgIds) entry._processedMsgIds = [];
      if (entry._processedMsgIds.includes(msgId)) continue;

      if (!step.type && msg.role === "editor") {
        // Cached stub — no content
        entry._processedMsgIds.push(msgId);
      } else if (step.type === "agent-inference") {
        const values = step.value ?? [];
        const responseParts = [], thinkingParts = [];
        let model = step.model ?? null;
        for (const item of (Array.isArray(values) ? values : [])) {
          if (item.type === "text") { const c = cleanText(item.content ?? ""); if (c) responseParts.push(c); }
          else if (item.type === "thinking") { const c = (item.content ?? "").trim(); if (c) thinkingParts.push(c); }
        }
        const content = responseParts.join("\n").trim();
        if (content) {
          const turn = { role: "assistant", content, msgId, timestamp: msg.created_time ?? Date.now() };
          if (thinkingParts.length) turn.thinking = thinkingParts.join("\n");
          if (model) { turn.model = model; entry.model = model; }
          entry.turns.push(turn);
        }
        entry._processedMsgIds.push(msgId);
      } else if (step.type === "user" || step.type === "human") {
        const content = extractRichText(step.value);
        if (content) {
          entry.turns.push({ role: "user", content, msgId, timestamp: msg.created_time ?? Date.now() });
          entry._processedMsgIds.push(msgId);
        }
      } else {
        entry._processedMsgIds.push(msgId);
      }
      entry.updatedAt = Date.now();
    }

    for (const entry of Object.values(store)) {
      if (!entry.messageOrder?.length || entry.turns.length < 2) continue;
      const order = entry.messageOrder;
      entry.turns.sort((a, b) => {
        const ai = order.indexOf(a.msgId), bi = order.indexOf(b.msgId);
        if (ai === -1 && bi === -1) return (a.timestamp ?? 0) - (b.timestamp ?? 0);
        if (ai === -1) return 1; if (bi === -1) return -1;
        return ai - bi;
      });
    }

    saveStore(store);
    console.debug(`[notion-ai-scraper] sync: ${Object.keys(threads ?? {}).length} thread(s), ${Object.keys(messages ?? {}).length} msg(s)`);
  }

  // ── Menu commands ─────────────────────────────────────────────────────────

  function toMarkdown(store) {
    return Object.values(store)
      .filter((c) => c.turns?.length > 0)
      .map((c) => {
        const title = c.title ? ` — ${c.title}` : "";
        const model = c.model ? ` (${c.model})` : "";
        const header = `# Notion AI Chat${title}${model}\n_ID: ${c.id}_\n_Captured: ${new Date(c.createdAt).toISOString()}_\n\n`;
        const body = (c.turns ?? [])
          .map((t) => `**${t.role === "assistant" ? "Notion AI" : "You"}**\n\n${t.content}`)
          .join("\n\n---\n\n");
        let toolSection = "";
        if (c.toolCalls?.length) {
          toolSection = "\n\n---\n\n<details><summary>Tool calls</summary>\n\n" +
            c.toolCalls.map((tc) => `- **${tc.tool}**: \`${JSON.stringify(tc.input).slice(0, 200)}\``).join("\n") +
            "\n</details>";
        }
        return header + body + toolSection;
      }).join("\n\n===\n\n");
  }

  function downloadText(content, filename, mime) {
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename; a.click();
    URL.revokeObjectURL(url);
  }

  GM_registerMenuCommand("Export All → Markdown", () => {
    const store = loadStore();
    const withTurns = Object.values(store).filter((c) => c.turns?.length);
    if (!withTurns.length) { alert("No conversations captured yet."); return; }
    downloadText(toMarkdown(store), `notion-ai-${Date.now()}.md`, "text/markdown");
  });

  GM_registerMenuCommand("Export All → JSON", () => {
    const store = loadStore();
    const withTurns = Object.values(store).filter((c) => c.turns?.length);
    if (!withTurns.length) { alert("No conversations captured yet."); return; }
    const data = withTurns.map(({ _processedMsgIds, messageOrder, ...clean }) => clean);
    downloadText(JSON.stringify(data, null, 2), `notion-ai-${Date.now()}.json`, "application/json");
  });

  GM_registerMenuCommand("Clear captured conversations", () => {
    if (confirm("Clear all captured Notion AI conversations?")) { saveStore({}); alert("Cleared."); }
  });

  GM_registerMenuCommand("Show capture stats", () => {
    const store = loadStore();
    const convos = Object.values(store).filter((c) => c.turns?.length);
    const turns = convos.reduce((n, c) => n + (c.turns?.length ?? 0), 0);
    const models = [...new Set(convos.map((c) => c.model).filter(Boolean))];
    const titles = convos.map((c) => c.title).filter(Boolean).slice(0, 5);
    alert(
      `${convos.length} conversation(s), ${turns} total turn(s)\n` +
      `Models: ${models.join(", ") || "unknown"}\n` +
      (titles.length ? `Recent: ${titles.join(", ")}` : "")
    );
  });

  console.log("[notion-ai-scraper] v0.3.1 active — watching live + historical chat");
})();
