// agent-manager.js — Notion Agent Manager extension page.
//
// Runs as a privileged extension page opened in a new tab.
// Uses browser.scripting.executeScript to inject self-contained API calls
// into an open Notion tab — which automatically carries the session cookie.

import { AGENTS } from './agents.js';
import { markdownToBlocks, blocksToMarkdown } from './block-builder.js';


// ── Injected function (runs inside the Notion tab) ────────────────────────────
// MUST be self-contained: no closures, all data via `payload` argument.

async function notionApiAction(payload) {
  const { action, blockId, spaceId, workflowId, newBlocks } = payload;
  const log = [];

  function uuid() { return crypto.randomUUID(); }

  async function post(endpoint, body) {
    const r = await fetch(`/api/v3/${endpoint}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const text = await r.text().catch(() => '');
      throw new Error(`${endpoint} → HTTP ${r.status}: ${text.slice(0, 300)}`);
    }
    return r.json();
  }

  function tx(operations) {
    return {
      requestId: uuid(),
      transactions: [{
        id: uuid(),
        spaceId,
        debug: { userAction: 'extension.agent_manager' },
        operations,
      }],
    };
  }

  function ptr(id) { return { table: 'block', id, spaceId }; }

  async function getBlockChildren(pid) {
    const data = await post('loadPageChunk', {
      pageId: pid, limit: 500, cursor: { stack: [] }, chunkNumber: 0, verticalColumns: false,
    });
    const blocks = data?.recordMap?.block ?? {};
    return {
      children: blocks[pid]?.value?.content ?? [],
      recordMap: data.recordMap,
    };
  }

  async function deleteBlock(bid, parentId) {
    await post('saveTransactionsFanout', tx([
      { pointer: ptr(bid),      path: [],          command: 'update',     args: { alive: false } },
      { pointer: ptr(parentId), path: ['content'], command: 'listRemove', args: { id: bid } },
    ]));
  }

  async function insertBlock(block, parentId, afterId) {
    const bid = uuid();
    const now = Date.now();
    const value = {
      id: bid, parent_id: parentId, parent_table: 'block', alive: true,
      created_time: now, last_edited_time: now, space_id: spaceId,
      ...block,
    };
    const listArgs = afterId ? { id: bid, after: afterId } : { id: bid };
    await post('saveTransactionsFanout', tx([
      { pointer: ptr(bid),      path: [],          command: 'set',       args: value },
      { pointer: ptr(parentId), path: ['content'], command: 'listAfter', args: listArgs },
    ]));
    return bid;
  }

  if (action === 'dump') {
    const { recordMap } = await getBlockChildren(blockId);
    log.push(`Fetched block tree for ${blockId}`);
    return { success: true, recordMap, log };
  }

  if (action === 'update') {
    const { children } = await getBlockChildren(blockId);
    log.push(`Deleting ${children.length} existing block(s)...`);
    for (const cid of children) await deleteBlock(cid, blockId);
    log.push(`Inserting ${newBlocks.length} new block(s)...`);
    let afterId = null;
    for (const block of newBlocks) {
      afterId = await insertBlock(block, blockId, afterId);
    }
    log.push('Content updated.');
    if (workflowId) {
      log.push(`Publishing workflow ${workflowId}...`);
      const result = await post('publishCustomAgentVersion', { workflowId, spaceId });
      log.push(`Published: artifact=${result.workflowArtifactId}  v${result.version}`);
      return { success: true, publishResult: result, log };
    }
    return { success: true, log };
  }

  if (action === 'publish') {
    log.push(`Publishing workflow ${workflowId}...`);
    const result = await post('publishCustomAgentVersion', { workflowId, spaceId });
    log.push(`Published: artifact=${result.workflowArtifactId}  v${result.version}`);
    return { success: true, publishResult: result, log };
  }

  throw new Error(`Unknown action: ${action}`);
}


// ── Extension page logic ───────────────────────────────────────────────────────

const $ = id => document.getElementById(id);

function logLine(text, type = 'info') {
  const el = document.createElement('div');
  el.className = `log-line log-${type}`;
  el.textContent = text;
  $('log').appendChild(el);
  $('log').scrollTop = $('log').scrollHeight;
}

function logLines(lines, type = 'info') {
  const fragment = document.createDocumentFragment();
  for (const text of lines) {
    const el = document.createElement('div');
    el.className = `log-line log-${type}`;
    el.textContent = text;
    fragment.appendChild(el);
  }
  $('log').appendChild(fragment);
  $('log').scrollTop = $('log').scrollHeight;
}

function logClear() {
  $('log').replaceChildren();
}

function setStatus(text, type) {
  const badge = $('tab-status');
  badge.textContent = text;
  badge.className = `status-badge status-${type}`;
}

async function findNotionTab() {
  const tabs = await browser.tabs.query({ url: '*://*.notion.so/*' });
  if (tabs.length === 0) return null;
  return tabs.find(t => t.active) ?? tabs[0];
}

async function refreshTabStatus() {
  const tab = await findNotionTab();
  if (tab) {
    setStatus(`Connected: ${tab.title?.slice(0, 40) ?? tab.url}`, 'ok');
  } else {
    setStatus('No Notion tab open', 'error');
  }
  return !!tab;
}

async function runInNotionTab(payload) {
  // Always do a fresh tab lookup — avoids stale tab IDs if tab was closed
  const tab = await findNotionTab();
  if (!tab) throw new Error('Open Notion in a tab first, then try again.');
  const results = await browser.scripting.executeScript({
    target: { tabId: tab.id },
    func: notionApiAction,
    args: [payload],
  });
  const result = results[0];
  if (result.error) throw new Error(result.error.message ?? String(result.error));
  return result.result;
}

function selectedAgent() {
  const key = $('agent-select').value;
  return { key, cfg: AGENTS[key] };
}

function setButtons(disabled) {
  ['btn-dump', 'btn-publish-only', 'btn-dry-run', 'btn-update'].forEach(id => {
    $(id).disabled = disabled;
  });
}

// Shared wrapper: clears log, locks buttons, runs handler, shows Done/Error
async function withUIBlock(label, handler) {
  logClear();
  logLine(label);
  setButtons(true);
  try {
    await handler();
  } catch (e) {
    logLine(`Error: ${e.message}`, 'error');
  } finally {
    setButtons(false);
  }
}

async function handleDump() {
  const { key, cfg } = selectedAgent();
  await withUIBlock(`Dumping current instructions for "${key}"...`, async () => {
    const res = await runInNotionTab({
      action: 'dump',
      blockId: cfg.notion_public_id,
      spaceId: cfg.space_id,
    });
    logLines(res.log);
    const md = blocksToMarkdown(res.recordMap.block, cfg.notion_public_id);
    $('instructions').value = md || '(empty)';
    logLine('Done. Instructions loaded into editor.', 'ok');
  });
}

async function handleUpdate(publish) {
  const { key, cfg } = selectedAgent();
  const md = $('instructions').value.trim();
  if (!md) { logClear(); logLine('Textarea is empty — nothing to write.', 'error'); return; }

  const newBlocks = markdownToBlocks(md);
  await withUIBlock(`Updating "${key}" instructions (${newBlocks.length} block(s))...`, async () => {
    const res = await runInNotionTab({
      action: 'update',
      blockId:    cfg.notion_public_id,
      spaceId:    cfg.space_id,
      workflowId: publish ? cfg.notion_internal_id : null,
      newBlocks,
    });
    logLines(res.log);
    logLine('Done.', 'ok');
  });
}

async function handleDryRun() {
  const { key, cfg } = selectedAgent();
  const md = $('instructions').value.trim();
  if (!md) { logClear(); logLine('Textarea is empty.', 'error'); return; }

  const newBlocks = markdownToBlocks(md);
  logClear();
  const lines = [
    `[DRY RUN] Would replace ${newBlocks.length} block(s) in "${key}" then publish.`,
    '',
    'Parsed blocks:',
    ...newBlocks.map((b, i) => `  [${i}] type=${b.type}  text=${JSON.stringify(b.properties?.title?.[0]?.[0] ?? '').slice(0, 80)}`),
    '',
    'Payload that would be POSTed:',
    JSON.stringify({ blockId: cfg.notion_public_id, spaceId: cfg.space_id, workflowId: cfg.notion_internal_id, newBlockCount: newBlocks.length }, null, 2),
    '[No API calls made]',
  ];
  logLines(lines);
  // Mark last line as 'ok'
  $('log').lastElementChild?.classList.replace('log-info', 'log-ok');
}

async function handlePublishOnly() {
  const { key, cfg } = selectedAgent();
  await withUIBlock(`Publishing "${key}" without changing content...`, async () => {
    const res = await runInNotionTab({
      action: 'publish',
      spaceId:    cfg.space_id,
      workflowId: cfg.notion_internal_id,
    });
    logLines(res.log);
    logLine('Done.', 'ok');
  });
}


// ── Init ──────────────────────────────────────────────────────────────────────

function init() {
  const select = $('agent-select');
  for (const [key, cfg] of Object.entries(AGENTS)) {
    const opt = document.createElement('option');
    opt.value = key;
    opt.textContent = cfg.label ?? key;
    select.appendChild(opt);
  }

  $('btn-refresh').addEventListener('click', refreshTabStatus);
  $('btn-dump').addEventListener('click', handleDump);
  $('btn-dry-run').addEventListener('click', handleDryRun);
  $('btn-update').addEventListener('click', () => handleUpdate(true));
  $('btn-publish-only').addEventListener('click', handlePublishOnly);

  refreshTabStatus();
}

document.addEventListener('DOMContentLoaded', init);
