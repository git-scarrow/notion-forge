"""
local-harness — a locally hosted clone of the harness Notion runs around
MiniMax M2.5 (`fireworks-minimax-m2.5`).

Two things are being replicated:

  1. Notion's agent loop  — a transcript of typed messages
     ([config, context, user, ...]) where the assistant reply is built from
     "steps" of type `agent-inference` (model text) and `agent-tool-result`
     (tool call). See server.py for the NDJSON patch-op wire format.

  2. MiniMax's *interleaved-thinking contract* — the model emits
     <think>...</think> before each tool call and reflects on tool output
     between calls. The harness MUST retain that thinking content across the
     tool-call rounds of the CURRENT user turn (and drop it from turns before
     the latest user message), or agentic quality degrades. Recommended
     sampling: temperature=1.0, top_p=0.95, top_k=40.

The real model (~230B MoE) does not fit a 16 GB GPU, so the default backend is
local Ollama `qwen3:32b` — also a <think>-tag interleaved-thinking model, the
same harness contract on different weights. Flip BACKEND to point at the real
Fireworks-hosted M2.5 or a vLLM box (see make_backend()).

Stdlib only.
"""

from __future__ import annotations

import json
import re
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# MiniMax-recommended sampling for M2/M2.5 (GitHub MiniMax-AI/MiniMax-M2).
DEFAULT_SAMPLING = {"temperature": 1.0, "top_p": 0.95, "top_k": 40}

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


# --------------------------------------------------------------------------- #
# Transcript model — internal canonical form (OpenAI-ish), with the Notion
# role vocabulary mapped on top of it (see from_notion_transcript / Step).
# --------------------------------------------------------------------------- #

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    """A single conversation message.

    role: system | user | assistant | tool
    thinking: model reasoning (assistant only) — the interleaved <think> body
    tool_calls: assistant tool-call requests
    tool_call_id / name: for role == "tool" results
    """
    role: str
    content: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""


@dataclass
class Step:
    """A Notion-style assistant step, emitted as the loop runs."""
    type: str          # "agent-inference" | "agent-tool-result"
    thinking: str = ""
    text: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_result: str = ""


# --------------------------------------------------------------------------- #
# Interleaved-thinking retention — the core MiniMax contract.
# --------------------------------------------------------------------------- #

def retain_interleaved_thinking(messages: list[Message]) -> list[Message]:
    """Return a copy of `messages` with thinking retained ONLY on assistant
    messages at/after the last user message (the current turn's tool rounds).

    This is the documented MiniMax nuance: keep <think> for the in-flight
    multi-step turn so the chain of thought is uninterrupted across tool
    calls, but discard thinking from completed prior turns.
    """
    last_user = max(
        (i for i, m in enumerate(messages) if m.role == "user"),
        default=-1,
    )
    out: list[Message] = []
    for i, m in enumerate(messages):
        if m.role == "assistant" and i < last_user:
            out.append(Message(role=m.role, content=m.content,
                               thinking="", tool_calls=list(m.tool_calls)))
        else:
            out.append(m)
    return out


# --------------------------------------------------------------------------- #
# Backends — normalize every provider to (thinking, content, tool_calls).
# --------------------------------------------------------------------------- #

@dataclass
class BackendResponse:
    thinking: str
    content: str
    tool_calls: list[ToolCall]


def _http_post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _split_think(content: str) -> tuple[str, str]:
    """Pull <think>...</think> out of free-form content."""
    thinks = _THINK_RE.findall(content or "")
    cleaned = _THINK_RE.sub("", content or "").strip()
    return ("\n".join(t.strip() for t in thinks).strip(), cleaned)


class Backend:
    name = "base"

    def chat(self, messages: list[Message], tools: list[dict]) -> BackendResponse:
        raise NotImplementedError


class OllamaBackend(Backend):
    """Local Ollama via its NATIVE /api/chat — cleanly separates
    message.thinking / message.content / message.tool_calls."""

    name = "ollama"

    def __init__(self, base: str = "http://localhost:11434",
                 model: str = "qwen3:32b", timeout: float = 600.0,
                 sampling: Optional[dict] = None):
        self.base = base.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.sampling = sampling or DEFAULT_SAMPLING

    def _serialize(self, messages: list[Message]) -> list[dict]:
        out = []
        for m in messages:
            if m.role == "tool":
                out.append({"role": "tool", "content": m.content,
                            "tool_name": m.name})
                continue
            d: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.role == "assistant" and m.thinking:
                d["thinking"] = m.thinking          # retained interleaved CoT
            if m.tool_calls:
                d["tool_calls"] = [
                    {"function": {"name": tc.name, "arguments": tc.arguments}}
                    for tc in m.tool_calls
                ]
            out.append(d)
        return out

    def chat(self, messages: list[Message], tools: list[dict]) -> BackendResponse:
        payload = {
            "model": self.model,
            "messages": self._serialize(messages),
            "tools": tools,
            "think": True,
            "stream": False,
            "options": dict(self.sampling),
        }
        resp = _http_post_json(f"{self.base}/api/chat", payload,
                               {"Content-Type": "application/json"}, self.timeout)
        msg = resp.get("message", {})
        thinking = msg.get("thinking", "") or ""
        content = msg.get("content", "") or ""
        if not thinking and "<think>" in content:      # some builds inline it
            thinking, content = _split_think(content)
        tcs = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            tcs.append(ToolCall(id=tc.get("id") or f"call_{i}",
                                name=fn.get("name", ""), arguments=args))
        return BackendResponse(thinking, content, tcs)


class OpenAIBackend(Backend):
    """OpenAI-compatible /v1/chat/completions — Fireworks (the real Notion
    path), vLLM, SGLang, llama.cpp, etc.

    For retained thinking we re-embed it as <think>...</think> in the assistant
    content, which is exactly MiniMax's "pass the history back in its original
    format" requirement.
    """

    name = "openai"

    def __init__(self, base: str, model: str, api_key: str = "",
                 timeout: float = 600.0, sampling: Optional[dict] = None):
        self.base = base.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.sampling = sampling or DEFAULT_SAMPLING

    def _serialize(self, messages: list[Message]) -> list[dict]:
        out = []
        for m in messages:
            if m.role == "tool":
                out.append({"role": "tool", "tool_call_id": m.tool_call_id,
                            "content": m.content})
                continue
            if m.role == "assistant":
                content = m.content
                if m.thinking:
                    content = f"<think>{m.thinking}</think>{content}"
                d: dict[str, Any] = {"role": "assistant", "content": content}
                if m.tool_calls:
                    d["tool_calls"] = [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.name,
                                      "arguments": json.dumps(tc.arguments)}}
                        for tc in m.tool_calls
                    ]
                out.append(d)
                continue
            out.append({"role": m.role, "content": m.content})
        return out

    def chat(self, messages: list[Message], tools: list[dict]) -> BackendResponse:
        payload = {
            "model": self.model,
            "messages": self._serialize(messages),
            "tools": tools,
            "stream": False,
            "temperature": self.sampling.get("temperature", 1.0),
            "top_p": self.sampling.get("top_p", 0.95),
        }
        if "top_k" in self.sampling:        # Fireworks/vLLM honor top_k; OpenAI ignores
            payload["top_k"] = self.sampling["top_k"]
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        resp = _http_post_json(f"{self.base}/chat/completions", payload,
                               headers, self.timeout)
        msg = resp["choices"][0]["message"]
        content = msg.get("content") or ""
        thinking = msg.get("reasoning_content") or msg.get("reasoning") or ""
        if not thinking and "<think>" in content:
            thinking, content = _split_think(content)
        tcs = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            tcs.append(ToolCall(id=tc.get("id") or f"call_{i}",
                                name=fn.get("name", ""), arguments=args))
        return BackendResponse(thinking, content, tcs)


class MockBackend(Backend):
    """Deterministic backend for testing the loop without an LLM.

    `script` is a list of BackendResponse to return in order. It also records
    every messages list it was handed, so tests can assert on what thinking was
    retained vs dropped at each round.
    """

    name = "mock"

    def __init__(self, script: list[BackendResponse]):
        self.script = list(script)
        self.seen: list[list[Message]] = []

    def chat(self, messages: list[Message], tools: list[dict]) -> BackendResponse:
        self.seen.append(retain_interleaved_thinking(messages))
        return self.script.pop(0)


def make_backend(kind: str = "ollama", **kw) -> Backend:
    """Backend switch.

    kind="ollama"   -> local qwen3:32b (default; what runs locally here)
    kind="fireworks"-> the real Notion path: MiniMax M2.5 on Fireworks
                       (needs api_key; base/model preset below)
    kind="openai"   -> any OpenAI-compatible base (vLLM/SGLang/llama.cpp)
    """
    if kind == "ollama":
        return OllamaBackend(**kw)
    if kind == "fireworks":
        return OpenAIBackend(
            base=kw.pop("base", "https://api.fireworks.ai/inference/v1"),
            model=kw.pop("model", "accounts/fireworks/models/minimax-m2p5"),
            **kw,
        )
    if kind == "openai":
        return OpenAIBackend(**kw)
    raise ValueError(f"unknown backend kind: {kind}")


# --------------------------------------------------------------------------- #
# Tool registry
# --------------------------------------------------------------------------- #

@dataclass
class Tool:
    name: str
    description: str
    parameters: dict          # JSON Schema (OpenAI function-params shape)
    fn: Callable[..., Any]


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict]:
        return [
            {"type": "function",
             "function": {"name": t.name, "description": t.description,
                          "parameters": t.parameters}}
            for t in self._tools.values()
        ]

    def execute(self, name: str, args: dict) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"unknown tool {name!r}"})
        try:
            result = tool.fn(**(args or {}))
        except Exception as exc:                       # surface tool errors as data
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
        return result if isinstance(result, str) else json.dumps(result)


# --------------------------------------------------------------------------- #
# Notion transcript ingestion
# --------------------------------------------------------------------------- #

def from_notion_transcript(transcript: list[dict]) -> list[Message]:
    """Convert a Notion-style transcript [config, context, user, ...] into the
    internal message list.

    - config  -> system message (workflow instructions / feature flags)
    - context -> folded into the system message (user/space info)
    - user    -> user message
    Assistant steps, if present, are reconstructed with thinking preserved.
    """
    system_parts: list[str] = []
    messages: list[Message] = []
    for entry in transcript:
        role = entry.get("role") or entry.get("type")
        if role == "config":
            instr = entry.get("instructions") or entry.get("content") or ""
            if instr:
                system_parts.append(instr)
        elif role == "context":
            ctx = entry.get("content") or entry.get("context") or ""
            if ctx:
                system_parts.append(f"[context]\n{ctx}")
        elif role == "user":
            messages.append(Message(role="user",
                                    content=entry.get("content", "")))
        elif role == "assistant":
            messages.append(Message(
                role="assistant",
                content=entry.get("content", ""),
                thinking=entry.get("thinking", ""),
            ))
    if system_parts:
        messages.insert(0, Message(role="system",
                                   content="\n\n".join(system_parts)))
    return messages


# --------------------------------------------------------------------------- #
# The agent loop
# --------------------------------------------------------------------------- #

EmitFn = Callable[[Step], None]


@dataclass
class RunResult:
    messages: list[Message]
    steps: list[Step]
    final_text: str
    stopped_reason: str       # "final" | "max_steps"


def run_agent(messages: list[Message],
              registry: ToolRegistry,
              backend: Backend,
              max_steps: int = 6,
              emit: Optional[EmitFn] = None) -> RunResult:
    """Notion-style interleaved-thinking agent loop.

    think -> (maybe) call tool(s) -> observe -> think -> ... -> answer.
    Thinking is retained across the tool rounds of the current turn via
    retain_interleaved_thinking() at every backend call.
    """
    steps: list[Step] = []
    tools = registry.schemas()

    def _emit(step: Step) -> None:
        steps.append(step)
        if emit:
            emit(step)

    for _ in range(max_steps):
        backend_msgs = retain_interleaved_thinking(messages)
        resp = backend.chat(backend_msgs, tools)

        assistant = Message(role="assistant", content=resp.content,
                            thinking=resp.thinking, tool_calls=resp.tool_calls)
        messages.append(assistant)
        _emit(Step(type="agent-inference", thinking=resp.thinking,
                   text=resp.content))

        if not resp.tool_calls:
            return RunResult(messages, steps, resp.content, "final")

        for tc in resp.tool_calls:
            result = registry.execute(tc.name, tc.arguments)
            messages.append(Message(role="tool", content=result,
                                    tool_call_id=tc.id, name=tc.name))
            _emit(Step(type="agent-tool-result", tool_name=tc.name,
                       tool_args=tc.arguments, tool_result=result))

    return RunResult(messages, steps,
                     messages[-1].content if messages else "", "max_steps")
