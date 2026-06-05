"""
Locally hosted instance — exposes the harness over a Notion-shaped NDJSON
streaming endpoint.

POST /agent/run
  body:  {"traceId": "...", "spaceId": "...",
          "transcript": [ {"role":"config","instructions":"..."},
                          {"role":"context","content":"..."},
                          {"role":"user","content":"..."} ]}
  reply: application/x-ndjson stream of patch ops mirroring Notion's protocol:
           {"o":"a","step":{"type":"agent-inference"|"agent-tool-result", ...}}
           {"o":"x","path":"steps/<i>/thinking","value":"..."}   (append)
           {"o":"x","path":"steps/<i>/text","value":"..."}
         final line: {"o":"p","path":"status","value":"done","final": "..."}

  o:"a" = add step, o:"x" = append, o:"p" = replace  (Notion's op vocabulary)

GET /  -> health + active backend/model.

Run:  python local-harness/server.py [--port 8088] [--backend ollama] [--model qwen3:32b]
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from harness import (Step, from_notion_transcript, make_backend, run_agent)
from tools import build_registry

BACKEND = None       # set in main()
REGISTRY = build_registry()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):       # quiet
        pass

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/"):
            self._json(200, {"ok": True, "backend": BACKEND.name,
                             "model": getattr(BACKEND, "model", "?"),
                             "tools": [s["function"]["name"]
                                       for s in REGISTRY.schemas()]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/agent/run":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return

        transcript = req.get("transcript", [])
        messages = from_notion_transcript(transcript)
        if not any(m.role == "user" for m in messages):
            self._json(400, {"error": "transcript has no user message"})
            return

        # Begin NDJSON stream.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        idx = {"n": -1}

        def write_chunk(obj: dict):
            line = (json.dumps(obj) + "\n").encode()
            self.wfile.write(f"{len(line):X}\r\n".encode() + line + b"\r\n")
            self.wfile.flush()

        def emit(step: Step):
            idx["n"] += 1
            i = idx["n"]
            write_chunk({"o": "a", "step": {"type": step.type}})
            if step.type == "agent-inference":
                if step.thinking:
                    write_chunk({"o": "x", "path": f"steps/{i}/thinking",
                                 "value": step.thinking})
                if step.text:
                    write_chunk({"o": "x", "path": f"steps/{i}/text",
                                 "value": step.text})
            else:  # agent-tool-result
                write_chunk({"o": "x", "path": f"steps/{i}/tool",
                             "value": {"name": step.tool_name,
                                       "args": step.tool_args,
                                       "result": step.tool_result}})

        result = run_agent(messages, REGISTRY, BACKEND, emit=emit)
        write_chunk({"o": "p", "path": "status", "value": "done",
                     "stopped": result.stopped_reason,
                     "final": result.final_text})
        self.wfile.write(b"0\r\n\r\n")          # end chunked
        self.wfile.flush()


def main():
    global BACKEND
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--backend", default="ollama",
                    choices=["ollama", "fireworks", "openai"])
    ap.add_argument("--model", default="qwen3:32b")
    ap.add_argument("--base", default=None)
    ap.add_argument("--api-key", default="")
    args = ap.parse_args()

    kw = {"model": args.model}
    if args.base:
        kw["base"] = args.base
    if args.api_key:
        kw["api_key"] = args.api_key
    if args.backend == "ollama":
        kw.pop("api_key", None)
    BACKEND = make_backend(args.backend, **kw)

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"local-harness on http://127.0.0.1:{args.port}  "
          f"backend={BACKEND.name} model={getattr(BACKEND,'model','?')}")
    print(f"  health: curl http://127.0.0.1:{args.port}/")
    print(f"  run:    curl -N http://127.0.0.1:{args.port}/agent/run -d "
          f"'{{\"transcript\":[{{\"role\":\"user\",\"content\":\"...\"}}]}}'")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
