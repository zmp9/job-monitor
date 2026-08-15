# job-monitor

Daily Summer 2027 internship monitor. Two subsystems, one cron:

- **Aggregator** — pulls open postings from 61 Greenhouse / Lever / Ashby / **Workday** boards, dedupes against state, scores each against `config/profile.yaml`, emails what's new and links to `MATCHES.md` for the full ranked list.
- **Page monitor** — fetches 8 pages (consulting-deadline aggregators plus firm careers pages with no board API), extracts the watched region, hashes it, emails a row-level diff on change.

> **Workday needs no headless browser.** Its portals render client-side, but each is backed by a public JSON endpoint (`/wday/cxs/{tenant}/{site}/jobs`). That reaches firms none of the other three cover — Boeing (748 postings, 15 Summer 2027 internships), Morgan Stanley, Capital One. Slug format is `tenant/wdhost/site`, copied from the careers-page URL. Gotcha: Workday reports `total` only on the first page and 0 thereafter, so pagination must capture it once rather than trust it per page.

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
| `NTFY_TOPIC` | phone push (optional) |

Repo *variables* (not secrets): `SCORE_THRESHOLD` (default 45), `EMAIL_BACKEND` (default `resend`).

### Phone push (ntfy)

Install the **ntfy** app (iOS/Android), then generate a topic and subscribe to it:

```bash
python -c "import secrets; print('zmp9-jobs-' + secrets.token_hex(8))"
```

Add that string as the `NTFY_TOPIC` repo secret, and subscribe to the same topic in the app. Every alert then arrives as a push alongside the email — count plus the top 3 matches, with the full list in the email.

> **Keep the topic secret.** ntfy topics are unauthenticated: anyone who knows or guesses the name can read your alerts. Use the random string above, never a guessable one, and never commit it.

Test it end to end with `python main.py --test-email` (sends on every enabled channel, no scanning).

**Resend over Gmail SMTP.** Gmail needs an app password, silently rate-limits personal accounts, and Google periodically revokes app passwords — which fails at 7am inside a cron run you aren't watching. SMTP is implemented as a fallback if you'd rather not sign up.

## Tuning dashboard

```bash
python main.py --dry-run --only-boards --snapshot   # ~5 min, sends nothing, writes no state
python dashboard.py                                 # localhost:8000
```

Adjust the threshold, the nine weights, and the keyword lists; every change re-scores the whole snapshot instantly so you see the new ranking *before* saving. Click a row for the score reasons. Keyword counts show how many postings each term matches — a term sitting at 0 is dead weight.

Save writes `config/profile.yaml` only. Review with `git diff` and commit yourself; the dashboard never commits or pushes.

**Sources tab** — manage what gets scraped. Add or remove boards and watched pages, or untick one to pause it without deleting its config (`enabled: false`, honoured by the runner). **Test** probes the live API and shows real postings before you save, and Add stays disabled until a test passes — so a mistyped slug can't reach config. Saves to `config/boards.yaml` and `config/pages.yaml`.

The preview calls the same `compile_profile()` / `score_posting()` the cron uses, so it can't drift from real behaviour. Server binds `127.0.0.1` only and adds no dependency — `requirements.txt` is installed on every CI run and the dashboard never runs there.

`state/last_scan.json` is gitignored (~13k postings with descriptions). Regenerate it whenever you want fresh data; the snapshot pulls Greenhouse with `?content=true` so descriptions are present for every provider, which the daily run skips as too heavy.

## Scoring

Weights live in the `weights:` block of `config/profile.yaml` (edited by the dashboard); `src/scoring.py` holds the same values as defaults, so a missing key falls back rather than breaking a run. Order of operations:

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
