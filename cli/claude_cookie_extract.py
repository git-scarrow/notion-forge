"""
cookie_extract.py — Extract Claude.ai cookies from Firefox's SQLite cookie store.

Adapted from notion-forge/cli/cookie_extract.py (MIT).
"""

import os
import shutil
import sqlite3
import tempfile

import firefox_cookies


def _query_claude_cookies(db_path: str) -> list[tuple[str, str]]:
    """Return all Claude.ai cookies from a copied Firefox SQLite DB."""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        shutil.copy2(db_path, tmp_path)
        conn = sqlite3.connect(tmp_path)
        try:
            return conn.execute(
                "SELECT name, value FROM moz_cookies "
                "WHERE host LIKE '%claude.ai' "
                "ORDER BY lastAccessed DESC"
            ).fetchall()
        finally:
            conn.close()
    finally:
        os.unlink(tmp_path)


def _has_claude_session(db_path: str) -> bool:
    """True if this DB holds a live claude.ai session (a sessionKey cookie)."""
    try:
        rows = _query_claude_cookies(db_path)
    except (OSError, sqlite3.Error):
        return False
    return bool(dict(rows).get("sessionKey"))


def _get_firefox_cookies_db() -> str:
    """Return the best Firefox cookies.sqlite for Claude auth (any OS)."""
    return firefox_cookies.resolve_cookie_db(
        _has_claude_session, not_found_hint="have logged into claude.ai"
    )


def get_all_cookies() -> dict[str, str]:
    """Extract all Claude.ai cookies from Firefox as a dict."""
    db_path = _get_firefox_cookies_db()
    rows = _query_claude_cookies(db_path)
    return {name: value for name, value in rows}


def get_cookie_header() -> str:
    """Build a full Cookie header string for Claude.ai requests."""
    cookies = get_all_cookies()
    if "sessionKey" not in cookies:
        raise ValueError(
            "sessionKey cookie not found for claude.ai. "
            "Open Firefox, log into Claude, and try again."
        )
    return "; ".join(f"{k}={v}" for k, v in cookies.items())
