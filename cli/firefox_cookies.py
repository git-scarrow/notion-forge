"""
firefox_cookies.py — Locate the right Firefox ``cookies.sqlite`` on any OS.

Resolution order (first hit wins):

1. **Env override.** ``FIREFOX_COOKIES_DB`` points straight at a ``cookies.sqlite``;
   ``FIREFOX_PROFILE`` points at a profile *directory* or names a profile in
   ``profiles.ini``. Set these in a host's env file when auto-detection can't or
   shouldn't be trusted (e.g. a non-default profile, a sandboxed install).
2. **Auto-detect.** Pick the per-platform Firefox base dir, parse ``profiles.ini``
   to order candidate profiles (install/legacy default first, then by mtime), and
   let the caller's ``probe`` choose the one with a live session.

This is shared by ``cookie_extract`` (Notion) and ``claude_cookie_extract``
(Claude); each passes a domain-specific ``probe``.
"""

import configparser
import os
import sys
from pathlib import Path
from typing import Callable, Optional


def _base_dirs() -> list[Path]:
    """Firefox profile root(s) for the current OS."""
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "Firefox"]
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        return [Path(appdata) / "Mozilla" / "Firefox"] if appdata else []
    # Linux / BSD — bare install plus snap and flatpak sandboxes.
    return [
        home / ".mozilla" / "firefox",
        home / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
        home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
    ]


def _profiles_from_ini(base: Path) -> list[Path]:
    """Profile dirs under ``base``, default(s) first, then remaining by mtime.

    Reads ``profiles.ini`` for the install default (``[InstallXXXX] Default=``)
    and the legacy ``Default=1`` flag. Falls back to globbing if it can't parse.
    """
    ini = base / "profiles.ini"
    if not ini.is_file():
        # No ini (unusual) — just glob whatever profile dirs exist.
        return sorted(
            (p.parent for p in base.glob("*/cookies.sqlite")),
            key=lambda d: (d / "cookies.sqlite").stat().st_mtime,
            reverse=True,
        )

    cp = configparser.ConfigParser()
    try:
        cp.read(ini)
    except configparser.Error:
        return [p.parent for p in base.glob("*/cookies.sqlite")]

    def _resolve(rel: str, is_relative: bool) -> Path:
        return (base / rel) if is_relative else Path(rel)

    all_profiles: list[Path] = []
    defaults: list[Path] = []
    for name in cp.sections():
        sec = cp[name]
        if name.lower().startswith("install"):
            # Per-install default — the most authoritative pointer.
            d = sec.get("Default")
            if d:
                defaults.append(_resolve(d, True))
        elif "path" in {k.lower() for k in sec}:
            path = _resolve(sec.get("Path"), sec.get("IsRelative", "1") == "1")
            all_profiles.append(path)
            if sec.get("Default", "0") == "1":  # legacy default flag
                defaults.append(path)

    others = sorted(
        (p for p in all_profiles if p not in defaults),
        key=lambda d: (d / "cookies.sqlite").stat().st_mtime
        if (d / "cookies.sqlite").is_file()
        else 0,
        reverse=True,
    )
    # Preserve order, drop dupes.
    ordered, seen = [], set()
    for p in [*defaults, *others]:
        if p not in seen:
            ordered.append(p)
            seen.add(p)
    return ordered


def candidate_cookie_dbs() -> list[str]:
    """Ordered ``cookies.sqlite`` paths (most-likely first), existing files only.

    An env override short-circuits to a single forced path.
    """
    forced = _env_override()
    if forced:
        return [forced]

    dbs: list[str] = []
    for base in _base_dirs():
        if not base.is_dir():
            continue
        for prof in _profiles_from_ini(base):
            db = prof / "cookies.sqlite"
            if db.is_file():
                dbs.append(str(db))
    return dbs


def _env_override() -> Optional[str]:
    """Resolve ``FIREFOX_COOKIES_DB`` / ``FIREFOX_PROFILE`` to a db path, if set."""
    db = os.environ.get("FIREFOX_COOKIES_DB")
    if db:
        db = os.path.expanduser(db)
        if not os.path.isfile(db):
            raise FileNotFoundError(
                f"FIREFOX_COOKIES_DB is set to {db!r} but no file exists there."
            )
        return db

    prof = os.environ.get("FIREFOX_PROFILE")
    if prof:
        expanded = os.path.expanduser(prof)
        # A directory (absolute or relative to a base) → its cookies.sqlite.
        for cand in [Path(expanded), *(b / expanded for b in _base_dirs())]:
            if cand.is_dir() and (cand / "cookies.sqlite").is_file():
                return str(cand / "cookies.sqlite")
        # Otherwise treat it as a profile *name* in profiles.ini.
        for base in _base_dirs():
            for p in _profiles_from_ini(base):
                if p.name == prof and (p / "cookies.sqlite").is_file():
                    return str(p / "cookies.sqlite")
        raise FileNotFoundError(
            f"FIREFOX_PROFILE={prof!r} did not resolve to a profile with cookies.sqlite."
        )
    return None


def resolve_cookie_db(
    probe: Callable[[str], bool],
    not_found_hint: str = "log into the site",
) -> str:
    """Return the best ``cookies.sqlite`` for a domain.

    ``probe(db_path)`` returns True when that DB holds a live session (e.g. a
    ``sessionKey`` / ``token_v2`` cookie). The first candidate that passes wins;
    if none do, fall back to the first candidate so callers can raise their own
    "logged out" error after querying it.
    """
    candidates = candidate_cookie_dbs()
    if not candidates:
        raise FileNotFoundError(
            "No Firefox cookies.sqlite found. "
            f"Ensure Firefox is installed and you {not_found_hint}. "
            "Or set FIREFOX_COOKIES_DB / FIREFOX_PROFILE to point at your profile."
        )
    for db_path in candidates:
        try:
            if probe(db_path):
                return db_path
        except Exception:
            continue
    return candidates[0]
