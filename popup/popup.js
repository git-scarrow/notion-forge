"use strict";

import { AGENTS, loadAgents, saveAgent, removeAgent, BUILTIN_AGENTS } from '../agent-manager/agents.js';
import { markdownToBlocks, blocksToMarkdown } from '../agent-manager/block-builder.js';

const $ = sel => document.querySelector(sel);

// ── Utilities ──────────────────────────────────────────────────────────────

async function send(msg) {
  return browser.runtime.sendMessage(msg);
}

function downloadBlob(content, filename, mime = "text/plain") {
  const blob = new Blob([content], { type: mime });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

async function getContext() {
  const tabs = await browser.tabs.query({ active: true, currentWindow: true });
  const urlStr = tabs[0]?.url ?? "";
  try {
    const url      = new URL(urlStr);
    const threadId = url.searchParams.get("t") || url.searchParams.get("at");
    const pageMatch = url.pathname.match(/([0-9a-f]{32,33})/i);
    return {
      pageId:   pageMatch ? pageMatch[1].toLowerCase() : null,
      threadId: threadId  ? threadId.toLowerCase()     : null,
    };
  } catch { return { pageId: null, threadId: null }; }
}

// ── Inline confirm ──────────────────────────────────────────────────────────

function inlineConfirm(message) {
  return new Promise(resolve => {
    const bar = $("#confirm-bar");
    $("#confirm-msg").textContent = message;
    bar.classList.remove("hidden");
    const cleanup = (result) => {
      bar.classList.add("hidden");
      $("#confirm-yes").removeEventListener("click", onYes);
      $("#confirm-no").removeEventListener("click", onNo);
      resolve(result);
    };
    const onYes = () => cleanup(true);
    const onNo = () => cleanup(false);
    $("#confirm-yes").addEventListener("click", onYes);
    $("#confirm-no").addEventListener("click", onNo);
  });
}

// ── Tab switching ─────────────────────────────────────────────────────────

document.querySelectorAll(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(t => {
      t.classList.toggle("active", t === tab);
      t.setAttribute("aria-selected", t === tab);
    });
    const target = tab.dataset.tab;
    document.querySelectorAll(".tab-panel").forEach(p => {
      p.classList.toggle("hidden", p.id !== `tab-${target}`);
    });
    if (target === "agents") { startConnPolling(); checkInFlightOp(); } else stopConnPolling();
  });
});

// ── Conversations tab ──────────────────────────────────────────────────────

async function render() {
  const { pageId, threadId } = await getContext();
  const convos = await send({ type: "GET_CONVERSATIONS", pageId, threadId });
  const list   = $("#conversation-list");
  const badge  = $("#status-badge");

  badge.textContent = `${convos.length} chat${convos.length !== 1 ? "s" : ""}`;
  list.replaceChildren();

  if (convos.length === 0) {
    const p = document.createElement("p");
    p.className = "empty-state";
    p.textContent = (pageId || threadId)
      ? "No conversation captured for this context yet."
      : "Open a Notion AI chat page to see conversations.";
    if (pageId || threadId) {
      p.appendChild(document.createElement("br"));
      p.appendChild(document.createTextNode("Chat with Notion AI to capture it."));
    }
    list.appendChild(p);
    return;
  }

  for (const c of convos) {
    const item = document.createElement("div");
    item.className = "convo-item";
    item.dataset.id = c.id;

    const meta   = document.createElement("div");
    meta.className = "convo-meta";

    const idEl = document.createElement("div");
    idEl.className = "convo-id";
    idEl.title = c.id;
    idEl.textContent = c.title ?? c.id;

    const turnsEl = document.createElement("div");
    turnsEl.className = "convo-turns";
    turnsEl.textContent = `${c.turnsCount ?? 0} turns · ${new Date(c.updatedAt ?? c.createdAt).toLocaleString()}`;

    meta.appendChild(idEl);
    meta.appendChild(turnsEl);

    const actions = document.createElement("div");
    actions.className = "convo-actions";

    for (const [action, label] of [["md", "MD"], ["json", "JSON"]]) {
      const btn = document.createElement("button");
      btn.className = "btn-icon";
      btn.dataset.action = action;
      btn.dataset.id = c.id;
      btn.textContent = label;
      actions.appendChild(btn);
    }

    item.appendChild(meta);
    item.appendChild(actions);
    list.appendChild(item);
  }
}

$("#btn-export-all-md").addEventListener("click", async () => {
  const { pageId, threadId } = await getContext();
  const res = await send({ type: "EXPORT_MD", pageId, threadId });
  if (res?.ok) downloadBlob(res.content, res.filename);
});

$("#btn-export-all-json").addEventListener("click", async () => {
  const { pageId, threadId } = await getContext();
  const res = await send({ type: "EXPORT_JSON", pageId, threadId });
  if (res?.ok) downloadBlob(res.content, res.filename, "application/json");
});

$("#btn-clear").addEventListener("click", async () => {
  if (!await inlineConfirm("Clear all captured conversations?")) return;
  await send({ type: "CLEAR_CONVERSATIONS" });
  render();
});

document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const { action, id } = btn.dataset;
  if (action === "md") {
    const res = await send({ type: "EXPORT_MD", conversationId: id });
    if (res?.ok) downloadBlob(res.content, res.filename);
  } else if (action === "json") {
    const res = await send({ type: "EXPORT_JSON", conversationId: id });
    if (res?.ok) downloadBlob(res.content, res.filename, "application/json");
  }
});

render();

// ── Agents tab ────────────────────────────────────────────────────────────

// Populate agent selector
const agentSelect = $("#agent-select");
let agentCache = {};

async function refreshAgentList() {
  agentCache = await loadAgents();
  // Sync the mutable AGENTS export for any code that reads it directly
  Object.keys(AGENTS).forEach(k => delete AGENTS[k]);
  Object.assign(AGENTS, agentCache);

  const prev = agentSelect.value;
  agentSelect.replaceChildren();
  for (const [key, cfg] of Object.entries(agentCache)) {
    const opt = document.createElement("option");
    opt.value = key;
    opt.textContent = (BUILTIN_AGENTS[key] ? "● " : "") + (cfg.label ?? key);
    agentSelect.appendChild(opt);
  }
  if (prev && agentCache[prev]) agentSelect.value = prev;
  updateRemoveBtn();
}

function updateRemoveBtn() {
  const key = agentSelect.value;
  $("#btn-rm-agent").disabled = !key || !!BUILTIN_AGENTS[key];
}

agentSelect.addEventListener("change", updateRemoveBtn);

function selectedAgent() {
  const key = agentSelect.value;
  const cfg = agentCache[key];
  if (!cfg) throw new Error(`Unknown agent: ${key}`);
  for (const field of ['workflow_id', 'space_id', 'block_id']) {
    if (!cfg[field] || cfg[field] === '...') throw new Error(`Agent "${key}" has placeholder ${field} — fill in real IDs first.`);
  }
  return { key, cfg };
}

refreshAgentList();

// ── Add agent from current page ─────────────────────────────────────────────

async function discoverAgentFromTab(tabId) {
  const results = await browser.scripting.executeScript({
    target: { tabId },
    func: () => {
      // Extract workflow ID from URL:
      //   /agent/<id>  (direct settings link)
      //   /chat?wfv=settings&p=<id>  (settings via conversation)
      const m = location.pathname.match(/\/agent\/([0-9a-f-]+)/i)
        || new URLSearchParams(location.search).get('p')?.match(/^([0-9a-f-]+)$/i);
      if (!m) return { error: "Not on an agent page. Navigate to an agent's settings page first." };
      // Notion URLs use dashless UUIDs; API needs 8-4-4-4-12 format
      let wid = m[1].replace(/-/g, '');
      const workflowId = wid.length === 32
        ? `${wid.slice(0,8)}-${wid.slice(8,12)}-${wid.slice(12,16)}-${wid.slice(16,20)}-${wid.slice(20)}`
        : m[1];
      // Fetch the workflow record to find the instruction block and name
      return fetch('/api/v3/getRecordValues', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ requests: [{ id: workflowId, table: 'workflow' }] }),
      }).then(r => r.json()).then(data => {
        const wf = data.results?.[0]?.value;
        if (!wf) return { error: `Could not load workflow ${workflowId}` };
        // The instruction block is the first content block of the workflow
        const instructions = wf.data?.instructions;
        const blockId = typeof instructions === 'string' ? instructions
          : instructions?.block_id || instructions?.id || instructions?.[0];
        if (!blockId) return { error: 'instructions field: ' + JSON.stringify(instructions).slice(0, 300) };
        return {
          label: wf.data?.name || wf.name || wf.title || 'Unknown Agent',
          workflow_id: workflowId,
          space_id: wf.space_id,
          block_id: blockId,
        };
      });
    },
  });
  const result = results[0];
  if (result.error) throw new Error(result.error.message ?? String(result.error));
  return result.result;
}

$("#btn-add-agent").addEventListener("click", async () => {
  const tab = await findNotionTab();
  if (!tab) { logClear(); logLine("No Notion tab open.", "err"); return; }
  try {
    logClear();
    logLine("Discovering agent from current page…", "muted");
    const info = await discoverAgentFromTab(tab.id);
    if (info.error) { logLine(info.error, "err"); return; }
    const key = info.label.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
    if (agentCache[key]) { logLine(`"${info.label}" already in registry.`, "muted"); agentSelect.value = key; return; }
    await saveAgent(key, info);
    await refreshAgentList();
    agentSelect.value = key;
    logLine(`Added "${info.label}" to registry.`, "ok");
  } catch (e) {
    logLine(`Error: ${e.message}`, "err");
  }
});

$("#btn-rm-agent").addEventListener("click", async () => {
  const key = agentSelect.value;
  if (!key || BUILTIN_AGENTS[key]) return;
  const label = agentCache[key]?.label ?? key;
  if (!await inlineConfirm(`Remove "${label}"?`)) return;
  await removeAgent(key);
  await refreshAgentList();
  logClear();
  logLine(`Removed "${label}".`, "ok");
});

// Connection status dot
async function findNotionTab() {
  const tabs = await browser.tabs.query({ url: "*://*.notion.so/*" });
  if (!tabs.length) return null;
  return tabs.find(t => t.active) ?? tabs[0];
}

async function refreshConnStatus() {
  const dot = $("#conn-dot");
  const tab = await findNotionTab();
  dot.className = "conn-dot " + (tab ? "connected" : "disconnected");
  dot.title = tab ? `Connected: ${tab.title?.slice(0, 50) ?? tab.url}` : "No Notion tab open";
  return tab;
}

// Log pane helpers
function logClear() {
  const pane = $("#agent-log");
  pane.replaceChildren();
}

function logLines(lines, cls = "") {
  const pane = $("#agent-log");
  const frag = document.createDocumentFragment();
  for (const text of lines) {
    const el = document.createElement("div");
    el.className = "log-line" + (cls ? " " + cls : "");
    el.textContent = text;
    if (text === "") el.innerHTML = "&nbsp;";
    frag.appendChild(el);
  }
  pane.appendChild(frag);
  pane.scrollTop = pane.scrollHeight;
}

function logLine(text, cls = "") {
  logLines([text], cls);
}

// Button lock
function setAgentButtons(disabled) {
  ["btn-dump", "btn-publish-only", "btn-dry-run", "btn-update"].forEach(id => {
    $(`#${id}`).disabled = disabled;
  });
}

// Connection dot polling — only while Agents tab is visible
let connInterval = null;
function startConnPolling() {
  if (connInterval) return;
  refreshConnStatus();
  connInterval = setInterval(refreshConnStatus, 5000);
}
function stopConnPolling() {
  if (connInterval) { clearInterval(connInterval); connInterval = null; }
}

// Shared wrapper — dispatches to background service worker, polls for completion
async function withAgentAction(label, payload, onDone) {
  logClear();
  logLine(label, "muted");
  logLine("working…", "loading");
  setAgentButtons(true);
  try {
    payload._label = label;
    const init = await send({ type: "AGENT_ACTION", payload });
    if (init?.error) throw new Error(init.error);
    // Poll for completion
    const result = await pollAgentStatus();
    $("#agent-log .loading")?.remove();
    if (result.state === "error") throw new Error(result.error);
    if (onDone) await onDone(result.result);
    logLines(result.log || []);
  } catch (e) {
    $("#agent-log .loading")?.remove();
    logLine(`Error: ${e.message}`, "err");
  } finally {
    setAgentButtons(false);
  }
}

async function pollAgentStatus() {
  while (true) {
    const status = await send({ type: "AGENT_STATUS" });
    if (status.state === "done" || status.state === "error") return status;
    await new Promise(r => setTimeout(r, 300));
  }
}

// Check for in-flight operation when agents tab opens
async function checkInFlightOp() {
  const status = await send({ type: "AGENT_STATUS" });
  if (status.state === "running") {
    logClear();
    logLine(status.label || "Operation in progress…", "muted");
    logLine("working…", "loading");
    setAgentButtons(true);
    const result = await pollAgentStatus();
    $("#agent-log .loading")?.remove();
    if (result.state === "error") logLine(`Error: ${result.error}`, "err");
    else { logLines(result.log || []); logLine("Done.", "ok"); }
    setAgentButtons(false);
  } else if (status.state === "done" && status.log?.length) {
    logClear();
    logLine(status.label || "Last operation", "muted");
    logLines(status.log);
    logLine("Done.", "ok");
  }
}

// ── Agent actions ──────────────────────────────────────────────────────────

$("#btn-dump").addEventListener("click", () => {
  const { key, cfg } = selectedAgent();
  withAgentAction(`Dumping "${key}"…`,
    { action: "dump", blockId: cfg.block_id, spaceId: cfg.space_id },
    (res) => {
      const md = blocksToMarkdown(res.recordMap.block, cfg.block_id);
      $("#instructions").value = md || "(empty)";
      markClean();
      logLine("Loaded into editor.", "ok");
    }
  );
});

async function doUpdate() {
  const { key, cfg } = selectedAgent();
  const md = $("#instructions").value.trim();
  if (!md) { logClear(); logLine("Editor is empty.", "err"); return; }
  if (!await inlineConfirm(`Replace "${key}" instructions and publish?`)) return;
  const newBlocks = markdownToBlocks(md);
  withAgentAction(`Updating "${key}" — ${newBlocks.length} blocks…`,
    { action: "update", blockId: cfg.block_id, spaceId: cfg.space_id, workflowId: cfg.workflow_id, newBlocks },
    () => { logLine("Done.", "ok"); markClean(); }
  );
}

$("#btn-update").addEventListener("click", doUpdate);

$("#btn-publish-only").addEventListener("click", async () => {
  const { key, cfg } = selectedAgent();
  if (!await inlineConfirm(`Publish "${key}" now?`)) return;
  withAgentAction(`Publishing "${key}"…`,
    { action: "publish", spaceId: cfg.space_id, workflowId: cfg.workflow_id },
    () => { logLine("Done.", "ok"); }
  );
});

$("#btn-dry-run").addEventListener("click", () => {
  const { key, cfg } = selectedAgent();
  const md = $("#instructions").value.trim();
  if (!md) { logClear(); logLine("Editor is empty.", "err"); return; }
  const newBlocks = markdownToBlocks(md);
  logClear();
  logLines([
    `[dry run] ${newBlocks.length} blocks → "${key}"`,
    `blockId  ${cfg.block_id}`,
    `workflow ${cfg.workflow_id}`,
    ``,
    ...newBlocks.slice(0, 12).map((b, i) => `[${i}] ${b.type}`),
    newBlocks.length > 12 ? `… +${newBlocks.length - 12} more` : "",
    ``,
    `[no api calls made]`,
  ], "muted");
  // Highlight the last line
  $("#agent-log").lastElementChild?.classList.replace("muted", "ok");
});

// ── Dirty state tracking ────────────────────────────────────────────────────

let isDirty = false;

function markDirty() {
  if (isDirty) return;
  isDirty = true;
  $("#btn-update").classList.add("dirty");
}

function markClean() {
  isDirty = false;
  $("#btn-update").classList.remove("dirty");
}

$("#instructions").addEventListener("input", markDirty);

// ── Keyboard shortcut: Ctrl+S / Cmd+S ──────────────────────────────────────

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "s") {
    e.preventDefault();
    e.stopPropagation();
    // Only fire when agents tab is active and buttons aren't locked
    if (!$("#tab-agents").classList.contains("hidden") && !$("#btn-update").disabled) doUpdate();
  }
}, true);
