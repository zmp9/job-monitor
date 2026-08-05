"""Greenhouse board API adapter.

Endpoint verified live 2026-07-28, no auth required:
    https://boards-api.greenhouse.io/v1/boards/{slug}/jobs

Note: the list endpoint returns `departments: []` for every board tested
(aqr, astranis, databricks, point72), so department is not a usable filter
here — unlike Ashby/Lever. Scoring falls back to title + description.

`?content=true` inlines full HTML descriptions but is heavy (~318KB for 48
jobs), so we fetch it lazily only for postings near the score threshold.
"""
import html
import re

from .base import Posting
from ..http import get_json

LIST_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
DETAIL_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"


def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", html.unescape(raw))
    return re.sub(r"\s+", " ", text).strip()


def fetch(slug: str, company: str) -> list[Posting]:
    data = get_json(LIST_URL.format(slug=slug))
    out = []
    for j in data.get("jobs", []):
        out.append(Posting(
            provider="greenhouse",
            company=company,
            job_id=str(j.get("id")),
            title=j.get("title", "").strip(),
            location=(j.get("location") or {}).get("name", "").strip(),
            url=j.get("absolute_url", ""),
            # first_published is when it actually went up; updated_at moves on edits.
            posted_at=j.get("first_published") or j.get("updated_at"),
            department=", ".join(d.get("name", "") for d in (j.get("departments") or [])),
        ))
    return out


def hydrate(posting: Posting, slug: str) -> None:
    """Fetch the full description for one posting, in place."""
    try:
        d = get_json(DETAIL_URL.format(slug=slug, job_id=posting.job_id))
        posting.description = _strip_html(d.get("content", ""))
    except Exception:
        pass  # description is a bonus signal; never fatal
