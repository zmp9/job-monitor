#!/usr/bin/env python3
"""Daily job-posting monitor + aggregator.

    python main.py --dry-run              # print instead of send
    python main.py --only-boards          # aggregator only
    python main.py --only-pages           # page monitor only
    python main.py --digest --dry-run     # force the weekly gated digest
    python main.py --threshold 55
"""
import argparse
import os
import sys
import traceback
from datetime import datetime, timezone

import yaml

from src import monitor, state
from src.providers import ashby, greenhouse, lever
from src.scoring import DEFAULT_THRESHOLD, compile_profile, score_posting
from src.notify.base import DryRunChannel, dispatch
from src.notify.email import EmailChannel
from src.notify.stubs import PushChannel, SMSChannel

CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
PROVIDERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}

MAX_HYDRATE = 40          # bound description fetches per run
HYDRATE_MARGIN = 25       # rescore borderline postings with full text
DIGEST_WEEKDAY = 0        # Monday
DIGEST_SOFT_CAP = 60      # max postings listed in one digest email
DIGEST_PER_COMPANY = 6


def load_yaml(name: str) -> dict:
    with open(os.path.join(CONFIG_DIR, name)) as f:
        return yaml.safe_load(f) or {}


def build_channels(dry_run: bool) -> list:
    if dry_run:
        return [DryRunChannel()]
    return [EmailChannel(), SMSChannel(), PushChannel()]


# ---------------------------------------------------------------------------
# aggregator
# ---------------------------------------------------------------------------
def run_aggregator(profile, boards_cfg, threshold, channels, dry_run):
    compiled = compile_profile(profile)
    seen = state.load_seen()
    gated_log = state.load_gated()
    gated_keys = {g["key"] for g in gated_log}

    matches, errors, new_count, total = [], [], 0, 0

    for board in boards_cfg.get("boards", []):
        provider = PROVIDERS.get(board["provider"])
        if not provider:
            errors.append(f"{board['provider']}/{board['slug']}: unknown provider")
            continue
        try:
            postings = provider.fetch(board["slug"], board["company"])
        except Exception as e:
            # One dead board must not kill the run.
            errors.append(f"{board['company']} ({board['provider']}): {type(e).__name__}: {e}")
            continue

        total += len(postings)
        fresh = [p for p in postings if p.key not in seen]
        new_count += len(fresh)

        scored, hydrate_queue = [], []
        for p in fresh:
            r = score_posting(p, profile, compiled)
            if r.excluded:
                continue
            if not p.description and (r.gated or r.score < threshold) and \
                    (r.gated or r.score >= threshold - HYDRATE_MARGIN):
                hydrate_queue.append(p)
            scored.append((p, r))

        for p in hydrate_queue[:MAX_HYDRATE]:
            try:
                provider.hydrate(p, board["slug"])
            except Exception:
                pass
        if hydrate_queue:
            scored = [(p, score_posting(p, profile, compiled)) for p, _ in scored]

        for p, r in scored:
            if r.excluded:
                continue
            if r.gated:
                if p.key not in gated_keys:
                    gated_log.append({
                        "key": p.key, "company": p.company, "title": p.title,
                        "location": p.location, "url": p.url, "first_seen": state.now(),
                    })
                    gated_keys.add(p.key)
                continue
            if r.score >= threshold:
                matches.append((p, r))

        for p in fresh:
            seen[p.key] = {"first_seen": state.now(), "title": p.title,
                           "company": p.company, "url": p.url}

    matches.sort(key=lambda x: -x[1].score)

    if matches:
        body = render_matches(matches, threshold, new_count, total, errors)
        subject = f"[jobs] {len(matches)} match{'es' if len(matches) != 1 else ''} — top: {matches[0][0].title[:50]}"
        dispatch(channels, subject, body)
    else:
        print(f"aggregator: no matches >= {threshold} "
              f"({new_count} new of {total} postings scanned)")

    if errors:
        print(f"aggregator: {len(errors)} board error(s):", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)

    if not dry_run:
        state.save_seen(seen)
        state.save_gated(gated_log)
    return len(matches)


def render_matches(matches, threshold, new_count, total, errors) -> str:
    lines = [f"{len(matches)} new posting(s) scored >= {threshold}.",
             f"Scanned {total} open postings, {new_count} new since last run.", ""]
    for p, r in matches:
        lines += [
            f"[{r.score}] {p.title}",
            f"  {p.company} — {p.location or 'location not listed'}",
            f"  {p.url}",
        ]
        if p.posted_at:
            lines.append(f"  posted: {p.posted_at[:10]}")
        for reason in r.reasons:
            lines.append(f"    {reason}")
        lines.append("")
    if errors:
        lines += ["", f"{len(errors)} board(s) failed this run:"]
        lines += [f"  {e}" for e in errors]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# page monitor
# ---------------------------------------------------------------------------
def run_monitor(pages_cfg, channels, dry_run):
    stored = state.load_pages()
    stale_after = pages_cfg.get("stale_after_days", 45)
    changes, errors, stale = [], [], []

    for page in pages_cfg.get("pages", []):
        try:
            res = monitor.check_page(page)
        except Exception as e:
            errors.append(f"{page['name']}: {type(e).__name__}: {e}")
            continue

        if res["empty"]:
            # Selector matched nothing — the page changed shape, or it went JS-only.
            errors.append(f"{page['name']}: selector '{page.get('selector','table')}' "
                          f"matched no content (page structure may have changed)")
            continue

        prev = stored.get(page["name"])
        if prev is None:
            stored[page["name"]] = {"hash": res["hash"], "region": res["region"],
                                    "first_seen": state.now(), "last_changed": state.now()}
            print(f"monitor: {page['name']}: baseline recorded ({len(res['region'])} chars)")
            continue

        if prev["hash"] != res["hash"]:
            added, removed = monitor.diff_rows(prev.get("region", ""), res["region"])
            changes.append((page, res, added, removed))
            stored[page["name"]] = {"hash": res["hash"], "region": res["region"],
                                    "first_seen": prev.get("first_seen", state.now()),
                                    "last_changed": state.now()}
        else:
            stored[page["name"]] = {**prev, "region": res["region"]}
            days = _days_since(prev.get("last_changed"))
            if days is not None and days >= stale_after:
                stale.append((page["name"], days))

    if changes:
        body = render_changes(changes, stale, errors)
        names = ", ".join(c[0]["name"] for c in changes)
        dispatch(channels, f"[pages] {len(changes)} page(s) changed: {names[:60]}", body)
    else:
        print(f"monitor: no changes across {len(pages_cfg.get('pages', []))} page(s)")

    for name, days in stale:
        print(f"monitor: {name} unchanged {days}d (>= {stale_after}d threshold)")
    if errors:
        print(f"monitor: {len(errors)} error(s):", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)

    if not dry_run:
        state.save_pages(stored)
    return len(changes)


def _days_since(iso: str | None):
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso)
        return int((datetime.now(timezone.utc) - then).total_seconds() // 86400)
    except Exception:
        return None


def render_changes(changes, stale, errors) -> str:
    lines = ["These sources lag the firms and serve cached content. Treat every",
             "line below as POSSIBLY OUTDATED — verify against the firm before acting.",
             ""]
    for page, res, added, removed in changes:
        lines += [f"### {page['name']}  (trust: {res['trust']})", f"{page['url']}", ""]
        if added:
            lines.append(f"  + {len(added)} row(s) added:")
            lines += [f"      {r[:160]}" for r in added]
        if removed:
            lines.append(f"  - {len(removed)} row(s) removed:")
            lines += [f"      {r[:160]}" for r in removed]
        if not added and not removed:
            lines.append("  (content reordered or reformatted; no row added or removed)")
        lines.append("")
    if stale:
        lines += ["", "Possibly stale (unchanged for a long time — may be a frozen cache):"]
        lines += [f"  {n}: unchanged {d} days" for n, d in stale]
    if errors:
        lines += ["", f"{len(errors)} page(s) failed this run:"]
        lines += [f"  {e}" for e in errors]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# weekly gated digest
# ---------------------------------------------------------------------------
def run_digest(channels, force=False):
    """Postings from your companies that scored nothing — so keyword gaps are
    visible instead of dying silently."""
    if not force and datetime.now(timezone.utc).weekday() != DIGEST_WEEKDAY:
        return 0
    gated = state.load_gated()
    cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
    recent = []
    for g in gated:
        try:
            if datetime.fromisoformat(g["first_seen"]).timestamp() >= cutoff:
                recent.append(g)
        except Exception:
            continue
    if not recent:
        print("digest: nothing gated in the last 7 days")
        return 0

    by_company = {}
    for g in recent:
        by_company.setdefault(g["company"], []).append(g)

    lines = [f"{len(recent)} posting(s) from your companies scored zero sector keywords",
             "and no catch-all title token, so they were never notified.",
             "Scan for keywords worth adding to profile.yaml.", ""]
    if len(recent) > DIGEST_SOFT_CAP:
        lines += [f"NOTE: {len(recent)} is large — this is the cold-start backlog, not a",
                  "typical week. Showing the busiest companies with a sample each.", ""]
    # Busiest companies first: a company filtering out a lot is where a keyword
    # gap is most likely to be hiding.
    shown = 0
    for company in sorted(by_company, key=lambda c: -len(by_company[c])):
        if shown >= DIGEST_SOFT_CAP:
            lines.append(f"... and {len(recent) - shown} more across other companies "
                         f"(full list in state/gated_log.json)")
            break
        items = by_company[company]
        lines.append(f"### {company} ({len(items)} filtered)")
        for g in items[:DIGEST_PER_COMPANY]:
            lines.append(f"  {g['title']}  —  {g['location'] or 'n/a'}")
            lines.append(f"    {g['url']}")
            shown += 1
        if len(items) > DIGEST_PER_COMPANY:
            lines.append(f"  ... +{len(items) - DIGEST_PER_COMPANY} more")
        lines.append("")
    dispatch(channels, f"[jobs] weekly gated digest — {len(recent)} filtered postings",
             "\n".join(lines))
    return len(recent)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending")
    ap.add_argument("--only-boards", action="store_true")
    ap.add_argument("--only-pages", action="store_true")
    ap.add_argument("--digest", action="store_true", help="force the weekly gated digest")
    ap.add_argument("--threshold", type=int,
                    default=int(os.environ.get("SCORE_THRESHOLD", DEFAULT_THRESHOLD)))
    args = ap.parse_args()

    channels = build_channels(args.dry_run)
    profile = load_yaml("profile.yaml")
    failed = False

    if not args.only_pages:
        try:
            run_aggregator(profile, load_yaml("boards.yaml"), args.threshold,
                           channels, args.dry_run)
        except Exception:
            traceback.print_exc()
            failed = True

    if not args.only_boards:
        try:
            run_monitor(load_yaml("pages.yaml"), channels, args.dry_run)
        except Exception:
            traceback.print_exc()
            failed = True

    if not args.only_pages:
        try:
            run_digest(channels, force=args.digest)
        except Exception:
            traceback.print_exc()
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
