"""Ashby job-board API adapter.

Not in the original spec, added because a probe of 139 slugs found that several
target companies have migrated off Greenhouse/Lever onto Ashby — including Ramp,
Plaid, Notion, OpenAI, Boom Supersonic and REGENT. Building GH+Lever only would
silently drop them.

Endpoint verified live 2026-07-28, no auth required:
    https://api.ashbyhq.com/posting-api/job-board/{slug}

Descriptions come inline. `department` and `team` are both well populated, which
makes Ashby the one provider where department filtering is actually reliable.
"""
from .base import Posting
from ..http import get_json

LIST_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def fetch(slug: str, company: str) -> list[Posting]:
    data = get_json(LIST_URL.format(slug=slug))
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        # secondaryLocations entries are objects ({"location": ..., "address": {...}}),
        # not strings — stringifying them leaks raw dict repr into the location field.
        locs = [j.get("location") or ""]
        for sec in j.get("secondaryLocations") or []:
            locs.append(sec.get("location", "") if isinstance(sec, dict) else str(sec))
        dept = " / ".join(x for x in [j.get("department"), j.get("team")] if x)
        out.append(Posting(
            provider="ashby",
            company=company,
            job_id=str(j.get("id")),
            title=(j.get("title") or "").strip(),
            location="; ".join(str(x) for x in locs if x),
            url=j.get("jobUrl") or j.get("applyUrl") or "",
            posted_at=j.get("publishedAt"),
            department=dept,
            description=(j.get("descriptionPlain") or "")[:8000],
        ))
    return out


def hydrate(posting: Posting, slug: str) -> None:
    """No-op: Ashby inlines descriptions in the list response."""
    return
