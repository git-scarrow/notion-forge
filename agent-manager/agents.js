// agents.js — Notion AI Agent registry.
// Built-in defaults + user-added agents stored in browser.storage.local.

export const BUILTIN_AGENTS = {
  librarian: {
    label: 'Librarian',
    notion_internal_id: '316e7cc7-01d5-81f4-a1d0-0092ce682e4a',
    space_id:    'f04bc8a1-18df-42d1-ba9f-961c491cdc1b',
    notion_public_id:    '316e7cc7-01d5-812e-be04-f06d86525bc2',
  },
};

const AGENTS_STORAGE_KEY = 'notion_ai_agents';

export async function loadAgents() {
  const res = await browser.storage.local.get(AGENTS_STORAGE_KEY);
  const custom = res[AGENTS_STORAGE_KEY] || {};
  return { ...BUILTIN_AGENTS, ...custom };
}

export async function saveAgent(key, cfg) {
  const res = await browser.storage.local.get(AGENTS_STORAGE_KEY);
  const custom = res[AGENTS_STORAGE_KEY] || {};
  custom[key] = cfg;
  await browser.storage.local.set({ [AGENTS_STORAGE_KEY]: custom });
}

export async function removeAgent(key) {
  if (BUILTIN_AGENTS[key]) return; // can't remove builtins
  const res = await browser.storage.local.get(AGENTS_STORAGE_KEY);
  const custom = res[AGENTS_STORAGE_KEY] || {};
  delete custom[key];
  await browser.storage.local.set({ [AGENTS_STORAGE_KEY]: custom });
}

// Keep a synchronous reference for backward compat — populated by caller
export let AGENTS = { ...BUILTIN_AGENTS };
