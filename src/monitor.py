"""Page-change monitor.

Design note, from measurement rather than assumption: hashing whole pages was
tested against back-to-back fetches of all five configured sources on
2026-07-28. Three of five differed between two fetches seconds apart, with zero
real content change — hackingthecaseinterview rotates a <meta csrf-token> per
request, and both managementconsulted pages re-inject Cloudflare
email-obfuscation scripts with fresh hashes. Whole-page hashing would have sent
a false "changed" alert for those three every single day.

Extracting only the <table> regions and normalizing to visible text was stable
across the same test on all five pages, so that is the default selector.
"""
import hashlib
import re

from bs4 import BeautifulSoup

from .http import get_text


def extract_region(html: str, selector: str = "table") -> str:
    """Return normalized visible text of the watched region."""
    soup = BeautifulSoup(html, "html.parser")
    nodes = soup.select(selector)
    if not nodes:
        return ""
    rows = []
    for node in nodes:
        # Row-wise so the diff can report which rows changed, not just "changed".
        trs = node.find_all("tr")
        if trs:
            for tr in trs:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                line = " | ".join(c for c in cells if c)
                if line:
                    rows.append(_norm(line))
        else:
            text = node.get_text(" ", strip=True)
            if text:
                rows.append(_norm(text))
    return "\n".join(rows)


def _norm(s: str) -> str:
    s = s.replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip()


def hash_region(region: str) -> str:
    return hashlib.sha256(region.encode("utf-8")).hexdigest()


def diff_rows(old: str, new: str, limit: int = 25) -> tuple[list[str], list[str]]:
    """Row-level add/remove. Blob diffing a 20KB table (managementconsulted) is
    unreadable in an email; row sets are what you actually want to see."""
    old_rows = [r for r in (old or "").split("\n") if r]
    new_rows = [r for r in (new or "").split("\n") if r]
    old_set, new_set = set(old_rows), set(new_rows)
    added = [r for r in new_rows if r not in old_set][:limit]
    removed = [r for r in old_rows if r not in new_set][:limit]
    return added, removed


def check_page(page: dict) -> dict:
    """Fetch and extract one page. Raises on network failure; caller isolates."""
    html = get_text(page["url"])
    region = extract_region(html, page.get("selector", "table"))
    return {
        "name": page["name"],
        "url": page["url"],
        "trust": page.get("trust", "low"),
        "region": region,
        "hash": hash_region(region),
        "empty": not region,
    }
