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
from src.providers import ashby, greenhouse, lever, workday
from src.scoring import (DEFAULT_THRESHOLD, compile_profile, resolve_threshold,
                         score_posting)
from src.notify.base import DryRunChannel, dispatch
from src.notify.email import EmailChannel
from src.notify.stubs import PushChannel, SMSChannel

CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
PROVIDERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby,
             "workday": workday}

MAX_HYDRATE = 40          # bound description fetches per run
HYDRATE_MARGIN = 25       # rescore borderline postings with full text
DIGEST_WEEKDAY = 0        # Monday
DIGEST_SOFT_CAP = 60      # max postings listed in one digest email
DIGEST_PER_COMPANY = 6

# Directory of every currently-open match, committed each run so it is readable
# on a phone via GitHub without needing Pages (which private repos don't get free).
MATCHES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "MATCHES.md")
REPO_URL = os.environ.get(
    "REPO_URL", "https://github.com/zmp9/job-monitor").rstrip("/")
MATCHES_URL = f"{REPO_URL}/blob/main/MATCHES.md" 


def load_yaml(name: str) -> dict:
    with open(os.path.join(CONFIG_DIR, name)) as f:
        return yaml.safe_load(f) or {}


def report_dispatch(where: str, results: dict, n_items: int) -> None:
    """Surface per-channel send outcomes.

    Without this, dispatch() swallowed failures: a rejected email and a
    delivered one produced identical (silent) logs, so a broken notification
    path looked like a healthy run.
    """
    for channel, outcome in results.items():
        stream = sys.stderr if outcome not in ("sent", "disabled") else sys.stdout
        print(f"{where}: notify[{channel}] -> {outcome} ({n_items} item(s))", file=stream)
    if not any(o == "sent" for o in results.values()):
        print(f"{where}: WARNING nothing was delivered — "
              f"{n_items} item(s) found but no channel accepted them", file=sys.stderr)


def build_channels(dry_run: bool) -> list:
    if dry_run:
        return [DryRunChannel()]
    return [EmailChannel(), SMSChannel(), PushChannel()]


# ---------------------------------------------------------------------------
# aggregator
# ---------------------------------------------------------------------------
def run_aggregator(profile, boards_cfg, threshold, channels, dry_run, snapshot=False):
    compiled = compile_profile(profile)
    seen = state.load_seen()
    gated_log = state.load_gated()
    gated_keys = {g["key"] for g in gated_log}

    matches, standing, errors, new_count, total = [], [], [], 0, 0
    # Every fetched posting, not just the unseen ones: the dashboard needs the
    # full population to answer "how many match at threshold X".
    snapshot_rows = []

    for board in boards_cfg.get("boards", []):
        # `enabled: false` parks a source without deleting its config, so the
        # dashboard can toggle a flaky board off and back on.
        if board.get("enabled") is False:
            continue
        provider = PROVIDERS.get(board["provider"])
        if not provider:
            errors.append(f"{board['provider']}/{board['slug']}: unknown provider")
            continue
        try:
            postings = provider.fetch(board["slug"], board["company"],
                                      with_content=snapshot)
        except Exception as e:
            # One dead board must not kill the run.
            errors.append(f"{board['company']} ({board['provider']}): {type(e).__name__}: {e}")
            continue

        total += len(postings)
        if snapshot:
            snapshot_rows.extend({
                "provider": p.provider, "company": p.company, "job_id": p.job_id,
                "title": p.title, "location": p.location, "url": p.url,
                "posted_at": p.posted_at, "department": p.department,
                "description": p.description[:2500],
            } for p in postings)
        fresh = [p for p in postings if p.key not in seen]
        new_count += len(fresh)

        # Score every open posting, not only the unseen ones. Dedupe decides what
        # counts as *new*; it should not decide what you're allowed to see. Emails
        # were arriving with 2-4 items while ~40 matches sat open and invisible.
        for p in postings:
            if p.key in seen:
                r = score_posting(p, profile, compiled)
                if not r.excluded and not r.gated and r.score >= threshold:
                    standing.append((p, r))

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
    standing.sort(key=lambda x: -x[1].score)
    all_open = matches + standing

    if not dry_run or snapshot:
        write_matches_file(all_open, threshold, total)

    if all_open:
        body = render_matches(matches, standing, threshold, new_count, total, errors)
        html = render_matches_html(matches, standing, threshold, new_count, total, errors)
        if matches:
            subject = (f"[jobs] {len(matches)} new — top: {matches[0][0].title[:44]}"
                       f" (+{len(standing)} still open)")
        else:
            subject = f"[jobs] no new today — {len(standing)} still open"
        report_dispatch("aggregator", dispatch(channels, subject, body, html),
                        len(all_open))
    else:
        print(f"aggregator: no matches >= {threshold} "
              f"({new_count} new of {total} postings scanned)")

    if errors:
        print(f"aggregator: {len(errors)} board error(s):", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)

    if snapshot:
        state.save_snapshot(snapshot_rows)
        print(f"aggregator: snapshot written ({len(snapshot_rows)} postings) -> "
              f"{state.SNAPSHOT_FILE}")

    if not dry_run:
        state.save_seen(seen)
        state.save_gated(gated_log)
    return len(matches)


def write_matches_file(all_open, threshold, total) -> None:
    """Write MATCHES.md — the full ranked directory the emails link to."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = ["# Open matches", "",
             f"_{len(all_open)} postings scoring >= {threshold}, "
             f"of {total} scanned. Updated {ts}._", ""]
    by_company = {}
    for p, r in all_open:
        by_company.setdefault(p.company, []).append((p, r))
    lines += ["| Score | Role | Company | Location |", "|---:|---|---|---|"]
    for p, r in all_open:
        title = p.title.replace("|", "\\|")
        lines.append(f"| {r.score} | [{title}]({p.url}) | {p.company} | "
                     f"{(p.location or 'n/a').replace('|', '/')} |")
    lines += ["", "## By company", ""]
    for company in sorted(by_company, key=lambda c: -len(by_company[c])):
        items = by_company[company]
        lines.append(f"- **{company}** ({len(items)}) — top "
                     f"[{items[0][0].title}]({items[0][0].url}) at {items[0][1].score}")
    try:
        with open(MATCHES_FILE, "w") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        print(f"aggregator: could not write MATCHES.md: {e}", file=sys.stderr)


def render_matches(matches, standing, threshold, new_count, total, errors) -> str:
    """Plain-text body.

    Two sections: what's new since the last run, then every currently-open match.
    The standing list is compact — one line each — so a 40-match email stays
    scannable while still being complete.
    """
    lines = []
    if matches:
        lines.append(f"{len(matches)} NEW posting(s) scoring >= {threshold}:")
    else:
        lines.append(f"No new postings today. {len(standing)} match(es) still open.")
    lines.append(f"Scanned {total} open postings, {new_count} new since last run.")
    lines.append("")

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

    if standing:
        lines += ["", f"--- {len(standing)} other matches still open ---",
                  f"Full ranked directory: {MATCHES_URL}", ""]
        for p, r in standing[:10]:
            lines.append(f"[{r.score}] {p.title} — {p.company}"
                         f" ({p.location or 'n/a'})")
        if len(standing) > 10:
            lines.append(f"...and {len(standing) - 10} more — see the directory above.")
        lines.append("")

    if errors:
        lines += ["", f"{len(errors)} board(s) failed this run:"]
        lines += [f"  {e}" for e in errors]
    return "\n".join(lines)


def render_matches_html(matches, standing, threshold, new_count, total, errors) -> str:
    """HTML body.

    The previous email wrapped plain text in a single <pre>, which rendered as a
    terminal dump and left URLs as unclickable text in most clients. This builds
    real markup: anchored links, a score badge per row, and the standing list as
    a compact table.
    """
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    F = ("font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
         "Helvetica,Arial,sans-serif")
    out = [f'<div style="{F};font-size:15px;line-height:1.5;color:#1a1d21;'
           f'max-width:680px;margin:0 auto">']

    if matches:
        out.append(f'<p style="margin:0 0 4px"><strong>{len(matches)} new</strong> '
                   f'posting(s) scoring &ge; {threshold}'
                   + (f', <strong>{len(standing)}</strong> still open' if standing else '')
                   + '.</p>')
    else:
        out.append(f'<p style="margin:0 0 4px">No new postings today. '
                   f'<strong>{len(standing)}</strong> match(es) still open.</p>')
    out.append(f'<p style="margin:0 0 18px;color:#6b7280;font-size:13px">'
               f'Scanned {total} open postings, {new_count} new since last run.</p>')

    for p, r in matches:
        out.append(
            '<div style="border:1px solid #e5e7eb;border-radius:8px;padding:12px 14px;'
            'margin:0 0 10px">'
            f'<div style="font-size:12px;font-weight:700;color:#2b6cb0">{r.score}</div>'
            f'<div style="font-weight:600;margin:2px 0">'
            f'<a href="{esc(p.url)}" style="color:#111827;text-decoration:none">'
            f'{esc(p.title)}</a></div>'
            f'<div style="color:#4b5563;font-size:13px">{esc(p.company)} &middot; '
            f'{esc(p.location or "location not listed")}'
            + (f' &middot; posted {esc(p.posted_at[:10])}' if p.posted_at else '')
            + '</div>'
            f'<div style="color:#6b7280;font-size:12px;margin-top:6px">'
            + '<br>'.join(esc(x) for x in r.reasons) + '</div>'
            f'<div style="margin-top:8px"><a href="{esc(p.url)}" '
            f'style="color:#2b6cb0;font-size:13px">Open posting &rarr;</a></div>'
            '</div>')

    if standing:
        out.append(f'<h3 style="{F};font-size:13px;text-transform:uppercase;'
                   f'letter-spacing:.05em;color:#6b7280;margin:24px 0 8px">'
                   f'{len(standing)} other matches still open</h3>')
        out.append(f'<p style="margin:0 0 10px"><a href="{MATCHES_URL}" '
                   f'style="color:#2b6cb0;font-weight:600">'
                   f'Open the full ranked directory &rarr;</a></p>')
        out.append('<table style="width:100%;border-collapse:collapse;font-size:14px">')
        for p, r in standing[:10]:
            out.append(
                '<tr>'
                f'<td style="padding:6px 8px 6px 0;color:#2b6cb0;font-weight:700;'
                f'width:34px;vertical-align:top">{r.score}</td>'
                f'<td style="padding:6px 0;border-bottom:1px solid #f0f1f3">'
                f'<a href="{esc(p.url)}" style="color:#111827;text-decoration:none">'
                f'{esc(p.title)}</a>'
                f'<div style="color:#6b7280;font-size:12px">{esc(p.company)} &middot; '
                f'{esc(p.location or "n/a")}</div></td></tr>')
        out.append('</table>')
        if len(standing) > 10:
            out.append(f'<p style="margin:10px 0 0"><a href="{MATCHES_URL}" '
                       f'style="color:#2b6cb0;font-size:13px">and '
                       f'{len(standing) - 10} more &rarr;</a></p>')

    if errors:
        out.append(f'<p style="color:#b45309;font-size:13px;margin-top:20px">'
                   f'{len(errors)} board(s) failed this run:<br>'
                   + '<br>'.join(esc(e) for e in errors) + '</p>')

    out.append('</div>')
    return "".join(out)


# ---------------------------------------------------------------------------
# page monitor
# ---------------------------------------------------------------------------
def run_monitor(pages_cfg, channels, dry_run):
    stored = state.load_pages()
    stale_after = pages_cfg.get("stale_after_days", 45)
    changes, errors, stale = [], [], []

    for page in pages_cfg.get("pages", []):
        if page.get("enabled") is False:
            continue
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
    report_dispatch("digest",
                    dispatch(channels, f"[jobs] weekly gated digest — {len(recent)} filtered postings",
                             "\n".join(lines)), len(recent))
    return len(recent)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending")
    ap.add_argument("--only-boards", action="store_true")
    ap.add_argument("--only-pages", action="store_true")
    ap.add_argument("--digest", action="store_true", help="force the weekly gated digest")
    ap.add_argument("--threshold", type=int, default=None,
                    help="override the cutoff; else SCORE_THRESHOLD env, else profile.yaml")
    ap.add_argument("--snapshot", action="store_true",
                    help="cache every fetched posting to state/last_scan.json for dashboard.py")
    ap.add_argument("--test-email", action="store_true",
                    help="send one test message and report the outcome; skips all scanning")
    args = ap.parse_args()

    channels = build_channels(args.dry_run)

    if args.test_email:
        results = dispatch(channels, "[jobs] test message",
                           "If you are reading this, the notification path works.")
        report_dispatch("test", results, 1)
        return 0 if any(o == "sent" for o in results.values()) else 1

    profile = load_yaml("profile.yaml")
    # Precedence: CLI flag > SCORE_THRESHOLD env > profile.yaml > module default.
    if args.threshold is None:
        env = os.environ.get("SCORE_THRESHOLD", "").strip()
        args.threshold = int(env) if env.isdigit() else resolve_threshold(profile)
    failed = False

    if not args.only_pages:
        try:
            run_aggregator(profile, load_yaml("boards.yaml"), args.threshold,
                           channels, args.dry_run, snapshot=args.snapshot)
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
