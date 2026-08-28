"""Workday job-board API adapter.

Workday career portals render client-side, so the HTML looks empty to a scraper
— but every one of them is backed by a public, unauthenticated JSON endpoint:

    POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    body: {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}

This is why the project does not need a headless browser. Verified live
2026-08-14 against Boeing (748 postings, incl. "Boeing Summer 2027 Internship
Program (Paid)"), Morgan Stanley (704) and Capital One (1136+) — none of which
are reachable through Greenhouse/Lever/Ashby.

Config slug format is "tenant/wdhost/site", e.g. "boeing/wd1/EXTERNAL_CAREERS".
Find it by opening a firm's careers page and copying the myworkdayjobs.com URL:
    https://boeing.wd1.myworkdayjobs.com/en-US/EXTERNAL_CAREERS
                ^tenant  ^wdhost         ^site

Limitations worth knowing:
  - `limit` is capped at 20 server-side; 100 returns HTTP 400. Listing a large
    board therefore costs total/20 requests, which is why MAX_PAGES exists.
  - The list response carries no description, so body-keyword scoring sees only
    title + location until hydrate() runs.
  - `postedOn` is human text ("Posted Today", "Posted 30+ Days Ago"), not a date.
"""
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .base import Posting
from ..http import get_json_post

PAGE = 20            # server-side maximum; larger values 400
MAX_PAGES = 60       # bound one board to ~1200 postings per run

# Server-side `searchText` looked like the way to skip pulling 2000 postings to
# keep 40, but it is a fuzzy substring match, not a filter: searchText="intern"
# on Citi returns "S.V.P. International LFO" and "International Data Strategy",
# and `total` stays 2000 either way. Same trap the keyword matcher documents —
# 'intern' hitting 'International'. So boards are still fetched whole, and the
# cost is paid with concurrency instead. Kept as a parameter for callers that
# want it; the default asks for everything.
SEARCH_TERMS = [""]

# One request is ~2s and offsets are deterministic once the first page reports
# `total`, so the remaining pages are fetched together rather than in series.
PAGE_WORKERS = 8


def _parse_slug(slug: str) -> tuple[str, str, str]:
    parts = [p for p in slug.split("/") if p]
    if len(parts) != 3:
        raise ValueError(
            f"workday slug must be 'tenant/wdhost/site', got {slug!r}")
    return parts[0], parts[1], parts[2]


def _posted_at(text: str) -> str | None:
    """Best-effort ISO date from Workday's relative wording.

    Approximate by design: it feeds the 'posted' line in notifications, never
    dedupe (which keys on job id), so a day's drift is harmless.
    """
    if not text:
        return None
    t = text.lower()
    now = datetime.now(timezone.utc)
    if "today" in t:
        return now.date().isoformat()
    if "yesterday" in t:
        return (now - timedelta(days=1)).date().isoformat()
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return (now - timedelta(days=int(m.group(1)))).date().isoformat()
    m = re.search(r"(\d+)\+?\s*month", t)
    if m:
        return (now - timedelta(days=30 * int(m.group(1)))).date().isoformat()
    return None


def fetch(slug: str, company: str, with_content: bool = False,
          search_terms: list[str] | None = None) -> list[Posting]:
    out, seen = [], set()
    for term in (search_terms if search_terms is not None else SEARCH_TERMS):
        for p in _fetch_one(slug, company, term):
            if p.job_id not in seen:
                seen.add(p.job_id)
                out.append(p)
    return out


def _fetch_one(slug: str, company: str, search_text: str = "") -> list[Posting]:
    tenant, wd, site = _parse_slug(slug)
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"

    def page(offset):
        data = get_json_post(api, {"appliedFacets": {}, "limit": PAGE,
                                   "offset": offset, "searchText": search_text})
        return data.get("jobPostings") or [], data.get("total") or 0

    # `total` is only populated on the first page; later pages report 0. Trusting
    # it per-page ends pagination after two pages (Boeing returned 40 of 748), so
    # capture it once from page 1 and derive every remaining offset from it.
    first, total = page(0)
    if not first:
        return []
    n_pages = min(MAX_PAGES, -(-total // PAGE) if total else 1)
    rows = list(first)
    if n_pages > 1:
        offsets = [i * PAGE for i in range(1, n_pages)]
        with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as ex:
            for more, _ in ex.map(lambda o: page(o), offsets):
                rows.extend(more)

    out = []
    for j in rows:
        path = j.get("externalPath") or ""
        req_id = ""
        bullets = j.get("bulletFields") or []
        if bullets:
            req_id = str(bullets[0])
        # externalPath already carries the requisition suffix and is stable;
        # fall back to it when bulletFields is empty so keys stay unique.
        out.append(Posting(
            provider="workday",
            company=company,
            job_id=req_id or path.rsplit("/", 1)[-1],
            title=(j.get("title") or "").strip(),
            location=(j.get("locationsText") or "").strip(),
            url=f"{base}/en-US/{site}{path}",
            posted_at=_posted_at(j.get("postedOn")),
            department="",
            description="",
        ))
    return out


def hydrate(posting: Posting, slug: str) -> None:
    """Fetch one posting's description.

    One request per posting, so the caller must keep this bounded — the daily
    run already caps it via MAX_HYDRATE.
    """
    try:
        tenant, wd, site = _parse_slug(slug)
        path = posting.url.split(f"/en-US/{site}", 1)[-1]
        d = get_json_post(
            f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}",
            None)
        info = d.get("jobPostingInfo") or {}
        raw = info.get("jobDescription") or ""
        posting.description = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()
    except Exception:
        pass  # description is a bonus signal; never fatal
