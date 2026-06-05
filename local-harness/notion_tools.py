"""Real notion-forge read tools for the local harness — Pattern A (live tool-calls).

Replaces the mock `lab_count` in tools.py. Each tool is a thin bridge into
notion-forge's credit-free Notion read functions (`cli/database_tools.py`),
which hit the plain Notion public API with `NOTION_TOKEN`. Notion meters
*Custom Agent runs*, not API reads — so every call here costs **zero Notion AI
credits**. Combined with local inference, this is a zero-credit Lab Query.

`cli/` is added to sys.path so we reuse the exact functions the MCP server wraps
(`mcp_server.describe_database` etc. are themselves thin wrappers over these).
`database_tools` reads `NOTION_TOKEN` at import time, so a missing token surfaces
as a tool-error string (which the harness feeds back to the model as data)
rather than crashing the loop.

Run via the cli venv so requests/etc. are importable:
    NOTION_TOKEN=... cli/.venv/bin/python local-harness/lab_query_local.py ...
"""
from __future__ import annotations

import json
import os
import sys

# Make cli/ importable so we can reuse notion-forge's read functions verbatim.
_CLI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cli")
if _CLI not in sys.path:
    sys.path.insert(0, _CLI)

from harness import Tool, ToolRegistry


def _err(exc: Exception) -> str:
    """Surface a tool failure as JSON data; the model reflects on it next round."""
    return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


def _dt():
    """Import cli/database_tools lazily (it reads NOTION_TOKEN at import)."""
    import database_tools
    return database_tools


def _describe_database(database_id: str) -> str:
    try:
        return _dt().describe_database(database_id)
    except Exception as exc:
        return _err(exc)


def _count_database(database_id: str, filter: str = "", exact: bool = False) -> str:
    try:
        return _dt().count_database(database_id, filter, exact)
    except Exception as exc:
        return _err(exc)


def _query_database(database_id: str, filter: str = "", sorts: str = "",
                    properties: str = "", limit: int = 50,
                    aggregate: bool = False) -> str:
    try:
        return _dt().query_database(
            database_id, filter=filter, sorts=sorts, properties=properties,
            limit=limit, aggregate=aggregate,
        )
    except Exception as exc:
        return _err(exc)


def build_registry() -> ToolRegistry:
    """The Lab Query core read surface (the `safe_for_lab_query` canonical reads).

    Matches lab_query.md's fallback allowlist: describe/count/query_database.
    """
    reg = ToolRegistry()
    reg.register(Tool(
        name="describe_database",
        description=("Show a Notion database's schema: property names, types, and "
                     "select/status options. Call this before query_database if "
                     "unsure of property names or filter types. database_id is a "
                     "dashed UUID (see the Lab Databases table in your instructions)."),
        parameters={"type": "object",
                    "properties": {"database_id": {"type": "string"}},
                    "required": ["database_id"]},
        fn=_describe_database))
    reg.register(Tool(
        name="count_database",
        description=("Count rows in a Notion database, optionally matching a JSON "
                     "filter. exact=false is a fast 0 / 1 / at-least-2 existence "
                     "check; exact=true pages the whole database for an exact total. "
                     "Use exact=true for any 'how many' total or filtered count."),
        parameters={"type": "object",
                    "properties": {
                        "database_id": {"type": "string"},
                        "filter": {
                            "type": "string",
                            "description": ("JSON Notion filter, e.g. "
                                            '{"property":"Status","status":'
                                            '{"equals":"Dispatch Ready"}}')},
                        "exact": {"type": "boolean"}},
                    "required": ["database_id"]},
        fn=_count_database))
    reg.register(Tool(
        name="query_database",
        description=("Query a Notion database; returns a compact markdown table, or "
                     "per-column statistics when aggregate=true (~90%% smaller). Use "
                     "a JSON filter and a small limit. Prefer count_database for "
                     "pure counts."),
        parameters={"type": "object",
                    "properties": {
                        "database_id": {"type": "string"},
                        "filter": {"type": "string"},
                        "sorts": {"type": "string"},
                        "properties": {"type": "string"},
                        "limit": {"type": "integer"},
                        "aggregate": {"type": "boolean"}},
                    "required": ["database_id"]},
        fn=_query_database))
    return reg
