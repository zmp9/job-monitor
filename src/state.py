"""Persisted state, split across files so a crash in one subsystem cannot
corrupt the other's history.

Committed JSON (not the Actions cache) is deliberate: the cache is evicted after
7 days without a hit, and an eviction would make every posting look new and
re-notify the whole board list. Committing also gives a git history of when each
posting first appeared, which is useful for reading a firm's posting cadence.
"""
import json
import os
from datetime import datetime, timezone

STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")
SEEN_FILE = os.path.join(STATE_DIR, "seen_postings.json")
PAGES_FILE = os.path.join(STATE_DIR, "page_hashes.json")
GATED_FILE = os.path.join(STATE_DIR, "gated_log.json")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path: str, data) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)   # atomic; a killed run never leaves half a file


def load_seen() -> dict:
    return _load(SEEN_FILE, {})


def save_seen(d: dict) -> None:
    _save(SEEN_FILE, d)


def load_pages() -> dict:
    return _load(PAGES_FILE, {})


def save_pages(d: dict) -> None:
    _save(PAGES_FILE, d)


def load_gated() -> list:
    return _load(GATED_FILE, [])


def save_gated(items: list, keep_days: int = 30) -> None:
    """Trim the gated log so the committed file doesn't grow without bound."""
    cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
    kept = []
    for it in items:
        try:
            ts = datetime.fromisoformat(it["first_seen"]).timestamp()
        except Exception:
            ts = cutoff + 1
        if ts >= cutoff:
            kept.append(it)
    _save(GATED_FILE, kept)
