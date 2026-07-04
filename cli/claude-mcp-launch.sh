#!/usr/bin/env bash
# MCP server launcher for claude-projects — Claude.ai Project management tools.
#
# Auth is via Firefox session cookies (see claude_cookie_extract.py); this server
# reads NO 1Password secrets. It therefore must NOT be wrapped in `op run`.
#
# It previously did `op run --env-file ~/.env`, which eagerly resolves EVERY op://
# reference in the shared env file. One unresolvable reference (GITHUB_PAT =
# op://Remote Access Keys/GitHub PAT/token, not readable by the maintenance
# service account) aborted the entire launch, so the MCP server never started.
#
# CLAUDE_ORG_ID is honored if already present in the environment; otherwise the
# server discovers the org via the Claude.ai API. Neither path needs op.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/claude_mcp_server.py"
