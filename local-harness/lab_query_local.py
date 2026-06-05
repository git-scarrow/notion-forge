"""Local Lab Query — a zero-credit clone of Notion's Lab Query agent.

Runs the local-harness interleaved-thinking loop (default Ollama `qwen3:32b`)
over notion-forge's credit-free Notion read tools, with the live Lab Query
system prompt (`cli/agent_instructions/lab_query.md`) as the transcript config.

Cloud Lab Query burns Notion credits per run; this path costs **zero Notion
credits** — reads go through the plain Notion public API, inference runs locally.
Flip --backend fireworks to run the exact model Notion uses (MiniMax M2.5).

Usage:
    NOTION_TOKEN=... cli/.venv/bin/python local-harness/lab_query_local.py \\
        --prompt "In Work Items, how many total rows, and how many Dispatch Ready? Exact counts."

    # the real model Notion serves:
    ... lab_query_local.py --backend fireworks --api-key "$FIREWORKS_API_KEY" --prompt "..."
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from harness import Step, from_notion_transcript, make_backend, run_agent
import notion_tools

_LAB_QUERY_MD = os.path.join(
    os.path.dirname(_HERE), "cli", "agent_instructions", "lab_query.md"
)


def _load_instructions() -> str:
    with open(_LAB_QUERY_MD, "r", encoding="utf-8") as f:
        return f.read()


def main() -> int:
    ap = argparse.ArgumentParser(description="Local zero-credit Lab Query clone.")
    ap.add_argument("--prompt", required=True, help="Natural-language Lab question.")
    ap.add_argument("--backend", default="ollama",
                    choices=["ollama", "fireworks", "openai"])
    ap.add_argument("--model", default=None, help="Override model name.")
    ap.add_argument("--base", default=None, help="OpenAI-compatible base URL.")
    ap.add_argument("--api-key", default=os.environ.get("FIREWORKS_API_KEY", ""))
    ap.add_argument("--max-steps", type=int, default=8)
    ap.add_argument("--quiet", action="store_true",
                    help="Suppress the step trace (stderr); print only the answer.")
    args = ap.parse_args()

    backend_kw: dict = {}
    if args.model:
        backend_kw["model"] = args.model
    if args.base:
        backend_kw["base"] = args.base
    if args.backend in ("fireworks", "openai") and args.api_key:
        backend_kw["api_key"] = args.api_key
    backend = make_backend(args.backend, **backend_kw)

    # Notion-shaped transcript: [config, context, user].
    transcript = [
        {"role": "config", "instructions": _load_instructions()},
        {"role": "context",
         "content": "user=Sam Scarrow; space=Lab; surface=local-harness (read-only)"},
        {"role": "user", "content": args.prompt},
    ]
    messages = from_notion_transcript(transcript)
    registry = notion_tools.build_registry()

    def emit(step: Step) -> None:
        if args.quiet:
            return
        if step.type == "agent-inference":
            if step.thinking:
                print(f"\n[think] {step.thinking.strip()[:600]}", file=sys.stderr)
            if step.text:
                print(f"[say] {step.text.strip()[:600]}", file=sys.stderr)
        elif step.type == "agent-tool-result":
            print(f"[tool] {step.tool_name}({step.tool_args}) -> "
                  f"{step.tool_result.strip()[:300]}", file=sys.stderr)

    result = run_agent(messages, registry, backend,
                       max_steps=args.max_steps, emit=emit)

    print("\n=== ANSWER ===")
    print(result.final_text.strip())
    print(f"\n[stopped={result.stopped_reason} steps={len(result.steps)} "
          f"backend={backend.name}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
