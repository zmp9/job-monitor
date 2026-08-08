"""Lever postings API adapter.

Endpoint verified live 2026-07-28, no auth required:
    https://api.lever.co/v0/postings/{slug}?mode=json

Descriptions come inline, so no hydrate step is needed. `createdAt` is epoch
milliseconds. `categories.team` is populated and usable as a department signal.
"""
from datetime import datetime, timezone

from .base import Posting
from ..http import get_json

LIST_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"


def _ts(ms) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None


def fetch(slug: str, company: str, with_content: bool = False) -> list[Posting]:
    # with_content is accepted for a uniform call site; descriptions are always inline here.
    data = get_json(LIST_URL.format(slug=slug))
    out = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location") or ""]
        out.append(Posting(
            provider="lever",
            company=company,
            job_id=str(j.get("id")),
            title=(j.get("text") or "").strip(),
            location="; ".join(x for x in locs if x),
            url=j.get("hostedUrl") or j.get("applyUrl") or "",
            posted_at=_ts(j.get("createdAt")),
            department=cats.get("team") or "",
            description=(j.get("descriptionPlain") or "")[:8000],
        ))
    return out


def hydrate(posting: Posting, slug: str) -> None:
    """No-op: Lever inlines descriptions in the list response."""
    return
