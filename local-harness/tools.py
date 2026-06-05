"""Demo tools for the local harness.

Small, side-effect-free tools that force the model into a multi-round
interleaved-thinking loop. `lab_count` is a mock of notion-forge's
count_database so the demo resembles the real Lab Query agent's job; swap its
body for a real MCP call to turn this into a local Lab Query clone (see README).
"""

from __future__ import annotations

import ast
import datetime
import operator

from harness import Tool, ToolRegistry

# ---- safe arithmetic (no eval) ------------------------------------------- #
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("unsupported expression")


def calc(expression: str) -> str:
    """Evaluate a basic arithmetic expression."""
    value = _eval(ast.parse(expression, mode="eval").body)
    return str(value)


def now(timezone: str = "UTC") -> str:
    """Return the current date/time (UTC)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def wordcount(text: str) -> str:
    """Count words and characters in `text`."""
    return f"words={len(text.split())} chars={len(text)}"


# Mock "database" — stand-in for notion-forge count_database(exact=True).
_LAB_COUNTS = {
    "work_items_total": 581,
    "work_items_dispatch_ready": 22,
    "lab_projects_total": 37,
}


def lab_count(metric: str) -> str:
    """Exact count for a Lab metric. Valid metrics: work_items_total,
    work_items_dispatch_ready, lab_projects_total."""
    if metric not in _LAB_COUNTS:
        return f"error: unknown metric {metric!r}; valid: {list(_LAB_COUNTS)}"
    return f"{metric}={_LAB_COUNTS[metric]} (exact total)"


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool(
        name="calc",
        description="Evaluate a basic arithmetic expression like '19*23' or '22/581*100'.",
        parameters={"type": "object",
                    "properties": {"expression": {"type": "string"}},
                    "required": ["expression"]},
        fn=calc))
    reg.register(Tool(
        name="now",
        description="Get the current UTC date and time.",
        parameters={"type": "object",
                    "properties": {"timezone": {"type": "string"}}},
        fn=now))
    reg.register(Tool(
        name="wordcount",
        description="Count the words and characters in a piece of text.",
        parameters={"type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"]},
        fn=wordcount))
    reg.register(Tool(
        name="lab_count",
        description=("Exact count for a Lab metric. metric is one of: "
                     "work_items_total, work_items_dispatch_ready, lab_projects_total."),
        parameters={"type": "object",
                    "properties": {"metric": {"type": "string"}},
                    "required": ["metric"]},
        fn=lab_count))
    return reg
