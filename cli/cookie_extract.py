"""
cookie_extract.py — Extract Notion session token from Firefox's SQLite cookie store.

Inspired by jamalex/notion-py (MIT) — cookie auth pattern.
https://github.com/jamalex/notion-py
"""

import glob
import os
import shutil
import sqlite3
import tempfile


def get_firefox_cookies_db() -> str:
    """Return path to the most recently modified Firefox cookies.sqlite."""
    pattern = os.path.expanduser("~/.mozilla/firefox/*/cookies.sqlite")
    candidates = glob.glob(pattern)
    if not candidates:
        raise FileNotFoundError(
            "No Firefox cookies.sqlite found. "
            "Ensure Firefox is installed and you have logged into notion.so."
        )
    return max(candidates, key=os.path.getmtime)


def get_auth() -> tuple[str, str | None]:
    """
    Extract token_v2 and notion_user_id from Firefox's Notion cookies
    in a single DB copy + connection.

    Returns (token_v2, user_id). user_id may be None.
    Raises ValueError if token_v2 is not found.
    """
    db_path = get_firefox_cookies_db()

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        shutil.copy2(db_path, tmp_path)
        conn = sqlite3.connect(tmp_path)
        try:
            rows = conn.execute(
                "SELECT name, value FROM moz_cookies "
                "WHERE host LIKE '%notion.so' AND name IN ('token_v2', 'notion_user_id') "
                "ORDER BY lastAccessed DESC"
            ).fetchall()
        finally:
            conn.close()
    finally:
        os.unlink(tmp_path)

    cookies = {name: value for name, value in rows}

    token = cookies.get("token_v2")
    if not token:
        raise ValueError(
            "token_v2 cookie not found for notion.so. "
            "Open Firefox, log into Notion, and try again."
        )

    return token, cookies.get("notion_user_id")


def get_token_v2() -> str:
    """Extract token_v2 from Firefox's Notion cookies."""
    token, _ = get_auth()
    return token


def get_user_id() -> str | None:
    """Extract the Notion user ID from the notion_user_id cookie."""
    _, user_id = get_auth()
    return user_id
