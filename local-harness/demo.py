"""
Demo / self-test driver for the local harness.

  python local-harness/demo.py --selftest
        Deterministic MockBackend run. Proves the interleaved-thinking
        retention contract and the multi-tool loop WITHOUT an LLM. Exits
        non-zero on contract violation.

  python local-harness/demo.py [--backend ollama] [--model qwen3:32b]
                               [--prompt "..."] [--max-steps 6]
        Live end-to-end run, prints the think -> tool -> think -> answer trace.
"""

from __future__ import annotations

import argparse
import sys

from harness import (BackendResponse, Message, MockBackend, ToolCall,
                     make_backend, retain_interleaved_thinking, run_agent)
from tools import build_registry

SYSTEM = ("You are a fast, read-only Lab query agent. Use tools for any number "
          "you report. Think briefly before each tool call. Give a one-sentence "
          "final answer.")

DEFAULT_PROMPT = ("How many Work Items are Dispatch Ready, and what percentage "
                  "is that of the 581 total? Use tools for both the count and "
                  "the percentage.")


def _trace(step):
    if step.type == "agent-inference":
        if step.thinking:
            print(f"  🧠 think: {step.thinking.strip()[:300]}")
        if step.text:
            print(f"  💬 text : {step.text.strip()[:400]}")
    else:
        print(f"  🔧 tool : {step.tool_name}({step.tool_args}) -> {step.tool_result}")


# --------------------------------------------------------------------------- #
# Self-test: scripted two-round tool loop + retention assertions.
# --------------------------------------------------------------------------- #

def selftest() -> int:
    reg = build_registry()
    script = [
        # round 1: think, call lab_count
        BackendResponse(
            thinking="Need the Dispatch Ready count first.",
            content="",
            tool_calls=[ToolCall("c1", "lab_count",
                                 {"metric": "work_items_dispatch_ready"})]),
        # round 2: reflect on tool output, call calc
        BackendResponse(
            thinking="Got 22. Now compute 22/581*100.",
            content="",
            tool_calls=[ToolCall("c2", "calc", {"expression": "22/581*100"})]),
        # round 3: final answer, no tools
        BackendResponse(
            thinking="3.79%. Done.",
            content="22 of 581 Work Items are Dispatch Ready (~3.79%).",
            tool_calls=[]),
    ]
    backend = MockBackend(script)
    messages = [Message(role="system", content=SYSTEM),
                Message(role="user", content=DEFAULT_PROMPT)]
    result = run_agent(messages, reg, backend, max_steps=6, emit=_trace)

    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and cond

    print("\n-- assertions --")
    check("stopped on final answer", result.stopped_reason == "final")
    check("three inference + two tool steps == 5",
          len(result.steps) == 5)
    check("lab_count executed with real result in transcript",
          any(m.role == "tool" and "22" in m.content for m in result.messages))
    check("calc executed (percentage)",
          any(m.role == "tool" and m.name == "calc" and "3.7" in m.content
              for m in result.messages))

    # Interleaved-thinking contract: at the LAST backend call (round 3), the
    # earlier assistant tool-round thinking must still be present, because all
    # rounds are after the single user message.
    last_seen = backend.seen[-1]
    assistant_thinks = [m.thinking for m in last_seen if m.role == "assistant"]
    check("current-turn thinking retained across rounds",
          assistant_thinks and all(t for t in assistant_thinks))

    # And retention DROPS thinking from assistant turns before a *new* user msg.
    convo = [
        Message(role="user", content="first"),
        Message(role="assistant", thinking="old reasoning", content="answer 1"),
        Message(role="user", content="second"),
        Message(role="assistant", thinking="new reasoning", content=""),
    ]
    retained = retain_interleaved_thinking(convo)
    check("pre-latest-user thinking dropped",
          retained[1].thinking == "" and retained[3].thinking == "new reasoning")

    print(f"\nself-test: {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# Live run
# --------------------------------------------------------------------------- #

def live(args) -> int:
    kw = {"model": args.model}
    if args.base:
        kw["base"] = args.base
    if args.api_key:
        kw["api_key"] = args.api_key
    if args.backend == "ollama":
        kw.pop("api_key", None)
    backend = make_backend(args.backend, **kw)

    print(f"backend={backend.name} model={getattr(backend,'model','?')}")
    print(f"prompt: {args.prompt}\n--- trace ---")
    messages = [Message(role="system", content=SYSTEM),
                Message(role="user", content=args.prompt)]
    result = run_agent(messages, build_registry(), backend,
                       max_steps=args.max_steps, emit=_trace)
    print("--- final ---")
    print(result.final_text or "(no final text)")
    print(f"[stopped: {result.stopped_reason}, steps: {len(result.steps)}]")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--backend", default="ollama",
                    choices=["ollama", "fireworks", "openai"])
    ap.add_argument("--model", default="qwen3:32b")
    ap.add_argument("--base", default=None)
    ap.add_argument("--api-key", default="")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--max-steps", type=int, default=6)
    args = ap.parse_args()
    return selftest() if args.selftest else live(args)


if __name__ == "__main__":
    sys.exit(main())
