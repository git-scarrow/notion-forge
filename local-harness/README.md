# local-harness

A **locally hosted clone of the harness Notion runs around MiniMax M2.5**
(`fireworks-minimax-m2.5`). It reproduces the two things that make that harness
what it is, and swaps only the weights.

## What "the harness Notion uses" actually is

`fireworks-minimax-m2.5` is the codename in `cli/notion_agent_config.py`. Read
it like the rest of the table (`anthropic-haiku-4.5`): `<provider>-<model>`.
So:

| Layer | Notion's choice | Evidence |
|---|---|---|
| **Inference** | MiniMax M2.5 served by **Fireworks AI** (OpenAI-compatible API). Notion does **not** self-host the ~230B MoE. | codename prefix `fireworks-` |
| **Agent loop** | Notion's **NDJSON transcript protocol**: `{traceId, spaceId, transcript:[config, context, user, …]}`; assistant reply built from patch ops `o:"a"` (add step) / `o:"x"` (append) / `o:"p"` (replace); step types `agent-inference` and `agent-tool-result`. | notion-forge protocol notes |
| **Model contract** | **Interleaved thinking** — `<think>…</think>` retained across the tool-call rounds of the *current* user turn (dropped from older turns); `minimax_m2` reasoning/tool parsers; sampling **temp 1.0 / top_p 0.95 / top_k 40**. | [MiniMax-M2](https://github.com/MiniMax-AI/MiniMax-M2), [vLLM interleaved thinking](https://docs.vllm.ai/en/latest/features/interleaved_thinking/), [MiniMax tool guide](https://platform.minimax.io/docs/guides/text-m2-function-call) |

The defining feature is the **interleaved-thinking retention contract**: M2
reasons *between* every tool call, and if the harness strips that `<think>`
content from history, agentic quality drops. A faithful clone must keep it.

## What this clone does

- `harness.py` — the engine. Notion-shaped transcript model, the
  `retain_interleaved_thinking()` contract, a backend adapter that normalizes
  any provider to `(thinking, content, tool_calls)`, a tool registry, and the
  `run_agent()` loop (think → tool → observe → think → … → answer).
- `tools.py` — demo tools (`calc`, `now`, `wordcount`, and `lab_count`, a mock
  of `count_database` so the demo mirrors the Lab Query agent's job).
- `server.py` — the **locally hosted instance**: a `POST /agent/run` endpoint
  that accepts a Notion transcript and streams back the exact `o:a/o:x/o:p`
  patch-op vocabulary.
- `demo.py` — `--selftest` (deterministic, no LLM) and a live trace runner.

**The real M2.5 doesn't fit a 16 GB GPU**, so the default backend is local
Ollama **`qwen3:32b`** — *also* a `<think>`-tag interleaved-thinking model with
`tools`+`thinking` capability. Same harness contract, different weights. The
backend is a one-line switch to the real Fireworks-hosted M2.5 or a vLLM box.

## Run it

Requires only Python 3 stdlib. Local default needs Ollama serving an
interleaved-thinking model (`ollama pull qwen3:32b`).

```bash
cd local-harness

# 1. Deterministic contract test — no LLM, proves the loop + thinking retention
python3 demo.py --selftest

# 2. Live trace against local qwen3:32b
python3 demo.py                       # default Lab-flavored multi-tool prompt
python3 demo.py --prompt "what is 19*23 then word-count the result"

# 3. The locally hosted instance (Notion-shaped NDJSON server)
python3 server.py --port 8088
curl -N http://127.0.0.1:8088/agent/run -H 'Content-Type: application/json' -d '{
  "traceId":"t1","spaceId":"s1","transcript":[
    {"role":"config","instructions":"You are a terse Lab assistant. Use a tool for any count."},
    {"role":"context","content":"user=sam space=Lab"},
    {"role":"user","content":"How many lab projects in total?"}]}'
```

## Point it at the real model Notion uses

```bash
# Fireworks-hosted MiniMax M2.5 — identical to Notion's inference layer
python3 demo.py  --backend fireworks --api-key "$FIREWORKS_API_KEY"
python3 server.py --backend fireworks --api-key "$FIREWORKS_API_KEY"

# Any OpenAI-compatible base (self-hosted vLLM/SGLang with minimax_m2 parsers)
python3 server.py --backend openai \
  --base http://your-vllm-host:8000/v1 \
  --model MiniMaxAI/MiniMax-M2.5
```

Verify the model name against Fireworks' current catalog; `make_backend()`
presets `accounts/fireworks/models/minimax-m2p5` but Fireworks slugs change.

For a real vLLM host, serve with the documented flags:
`vllm serve MiniMaxAI/MiniMax-M2.5 --tool-call-parser minimax_m2
--reasoning-parser minimax_m2 --enable-auto-tool-choice` (temp 1.0, top_p 0.95,
top_k 40).

## Local Lab Query (Pattern A — live tool-calls) — IMPLEMENTED

`tools.py`'s `lab_count` is a mock. The real bridge lives in two files:

- `notion_tools.py` — registers `describe_database` / `count_database` /
  `query_database` as thin wrappers over `cli/database_tools.py` (the exact
  functions the notion-forge MCP server wraps). These hit the plain Notion
  public API with `NOTION_TOKEN`. **Notion meters Custom Agent runs, not API
  reads — so this path costs zero Notion AI credits.**
- `lab_query_local.py` — the entry point: loads `cli/agent_instructions/lab_query.md`
  as the transcript `config`, runs the harness loop over `notion_tools`, and
  prints the answer. The harness loop itself is unchanged.

`NOTION_TOKEN` is provisioned via 1Password, same as the MCP server launcher, so
run it through `op run`:

```bash
op run --env-file "$HOME/.env" --no-masking -- \
  cli/.venv/bin/python local-harness/lab_query_local.py \
  --prompt "In Work Items, how many total rows, and how many Dispatch Ready? Exact counts. One sentence."

# the real model Notion serves (MiniMax M2.5 on Fireworks):
op run --env-file "$HOME/.env" --no-masking -- \
  cli/.venv/bin/python local-harness/lab_query_local.py \
  --backend fireworks --api-key "$FIREWORKS_API_KEY" --prompt "..."
```

Verified live against local `qwen3:32b` (zero Notion credits):

```
Work Items: 610 exact total; Dispatch Ready: 29 matched count.
[stopped=final steps=4 backend=ollama]
```

The local model emitted the `exact total` / `matched count` scope labels from
the Lab Query canonicality contract and read live counts (not memorized) — the
loop, the tool bridge, and the instruction contract all hold end to end.

### Next: Pattern B (precompute + local RAG)

For speed and full offline operation, periodically dump Notion into compact
local artifacts (sqlite mirror, `graph_export` JSON, per-DB aggregate digests,
embeddings via the local `bge-m3` / `nomic-embed-text`) and let the model query
those instead of live tool-calling. Best for repeated legibility/browse queries;
keep Pattern A for freshness-critical asks.

## Design notes

- **Interleaved-thinking retention** is applied at *every* backend call via
  `retain_interleaved_thinking()`: assistant thinking is kept for messages
  at/after the last user message, dropped before it — the documented MiniMax
  nuance.
- **Backends normalize, not translate.** Ollama's native `/api/chat` returns
  `message.thinking` separately; the OpenAI/Fireworks path re-embeds retained
  thinking as `<think>…</think>` in assistant content (MiniMax's "pass history
  back in original format" rule). Both collapse to the same internal shape.
- **Tool errors are data.** A failed tool returns a JSON `{"error": …}` string
  into the transcript; the model reflects on it in the next `<think>` block and
  self-corrects (observed live: a bad `calc` expression was fixed on the next
  round).

## Sources

- MiniMax-M2 — https://github.com/MiniMax-AI/MiniMax-M2
- vLLM interleaved thinking — https://docs.vllm.ai/en/latest/features/interleaved_thinking/
- vLLM MiniMax-M2 recipe — https://docs.vllm.ai/projects/recipes/en/latest/MiniMax/MiniMax-M2.html
- MiniMax tool use & interleaved thinking — https://platform.minimax.io/docs/guides/text-m2-function-call
