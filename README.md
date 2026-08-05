# job-monitor

Daily Summer 2027 internship monitor. Two subsystems, one cron:

- **Aggregator** — pulls open postings from 48 Greenhouse / Lever / Ashby boards, dedupes against state, scores each against `config/profile.yaml`, emails anything at or above the threshold.
- **Page monitor** — fetches 5 consulting-deadline aggregator pages, extracts the deadline tables, hashes them, emails a row-level diff on change.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python main.py --dry-run          # prints, sends nothing
```

Flags: `--dry-run`, `--only-boards`, `--only-pages`, `--digest`, `--threshold N`.

## Setup

Repo secrets (Settings → Secrets and variables → Actions):

| Secret | Needed for |
|---|---|
| `RESEND_API_KEY` | email (default backend) |
| `EMAIL_FROM`, `EMAIL_TO` | email, both backends |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | only if `EMAIL_BACKEND=smtp` |

Repo *variables* (not secrets): `SCORE_THRESHOLD` (default 45), `EMAIL_BACKEND` (default `resend`).

**Resend over Gmail SMTP.** Gmail needs an app password, silently rate-limits personal accounts, and Google periodically revokes app passwords — which fails at 7am inside a cron run you aren't watching. SMTP is implemented as a fallback if you'd rather not sign up.

## Scoring

Weights are constants at the top of `src/scoring.py`. Order of operations:

1. **Hard exclusions** — strong negatives in title (`software engineer`, `phd`, …), seniority terms (`senior`, `lead`, `manager`, …), non-US location, remote-only while `remote_ok: false`. Dropped entirely.
2. **Positive gate** — needs ≥1 sector keyword, *or* a catch-all title token (`intern`, `internship`, `summer`, `analyst`, `associate`). Failures go to the weekly gated digest, never silently dropped.
3. **Score** — 0–100, notified at ≥ threshold. Every notification lists the reasons that produced the score, so tuning is a feedback loop rather than guesswork.

Two subtleties worth knowing before you tune:

- **Seniority exclusions are waived for internship titles**, so `Product Manager Intern` survives while `Strategy Manager` is dropped. Anything that must be excluded *even for interns* (`phd`, `mba`) belongs in `strong_negatives`, which is never waived.
- **Overlapping keyword matches collapse to the longest.** Without this, `Strategy & Operations` matched `strategy`, `strategy & operations` and `strategy and operations` — three hits for one phrase, which ranked senior full-time roles above real internships.

## Page monitor

Whole-page hashing was tested and **fails** on 3 of 5 sources: hackingthecaseinterview rotates a `<meta csrf-token>` per request, and both managementconsulted pages re-inject Cloudflare email-obfuscation scripts with fresh hashes. Two fetches seconds apart produce different page hashes with zero content change — you'd get a false alert daily.

So the default selector is `table`, normalized to visible text row-wise. That was stable across the same test on all five. Override per-page in `config/pages.yaml` if a source changes shape.

The 5 aggregator blogs lag the firms and serve cached content. Every notification is banner-flagged **possibly outdated** — their job is to flag *change*, not to be correct. `stale_after_days` (default 45) also warns when a page hasn't moved in a long time, which is how you catch a frozen cache.

The 3 firm careers pages (Alton Aviation, Seabury, Novistra) are `trust: high` — they're primary sources with no board API, so a change there *is* the firm changing something. Each needs its own selector (`article`, `main`, `body` respectively); all three were verified content-bearing and hash-stable across back-to-back fetches.

## State

`state/` holds three committed JSON files: `seen_postings.json`, `page_hashes.json`, `gated_log.json`. Split so a crash in one subsystem can't corrupt the other's history; written atomically via temp-file rename.

Committed rather than the Actions cache: the cache is evicted after 7 days without a hit, and an eviction would make every posting look new and re-notify all 48 boards. Committing also gives a git history of when each posting first appeared.

## Coverage — read this before trusting the output

Every slug in `config/boards.yaml` was probed live (2026-07-28, expanded 2026-07-30) and returned >0 postings. A failing board logs to stderr and never kills the run.

**Consulting — 2 of 15 target firms reachable.**

| On a board API | Not reachable (ATS in parens) |
|---|---|
| Charles River Associates (GH) · AlixPartners (GH) | McKinsey · Bain · BCG · LEK · Kearney · ZS (no public API) · Deloitte · Strategy& · EY-Parthenon · Analysis Group (iCIMS) · Cornerstone (Workday/iCIMS) · Schwab (iCIMS) · ICF (Phenom) |

CRA is the best find on the list — it posts explicit *2027 Bachelor's/Master's* Economics and Business/Finance Consulting Analyst roles across Boston/NYC/Chicago. AlixPartners posts NYC Summer Analyst roles (Turnaround & Restructuring, Investigations & Disputes).

Two apparent hits were **rejected as false positives**: `greenhouse.io/bcg` contains only `Test Job Live` and `Voice AI Test - CSM`; `lever.co/oliverwyman` has two unrelated SF roles. Neither is real recruiting.

**Aviation / industrials — 0 of 8 legacy names reachable.** Every airline and prime is on an enterprise ATS: United (Phenom) · Delta (Avature) · Southwest (Phenom) · RTX (Phenom) · Boeing (Workday+Avature) · JetBlue (SuccessFactors) · GE Aerospace (no fingerprint). None expose a public JSON board. Coverage here is the newer aerospace/defense names already in config, plus **Anduril (2,138 jobs)**.

**Finance — boutiques were the wrong bet, funds were the right one.** None of the 10 named NYC boutique IB/M&A firms run any ATS at all — they are small enough to hire through personal networks. Of those, only **Novistra** has a careers page substantial enough to monitor. New fund coverage added: Squarepoint, Schonfeld, IMC, Akuna, Old Mission, Belvedere. Still unreachable: Goldman, JPMorgan, Morgan Stanley, Capital One (Workday+Eightfold), BNY, Citadel, Millennium, Bridgewater, Two Sigma, Optiver, SIG (iCIMS).

**Tech — as expected, the best coverage.** Stripe added (539 jobs). Rippling has no public board despite the fingerprint check. Amazon/Google/Microsoft remain gated.

**Not scraped by design:** LinkedIn, Handshake, and big-firm career portals. They block scrapers or require login — covered via Handshake alerts and firm talent networks instead.

## Not in v1

Tracker DB/UI · keyword learning from feedback · any login-gated source · SMS and push (seams exist in `src/notify/stubs.py` with per-channel TODOs; both report `disabled` until their secrets exist).
