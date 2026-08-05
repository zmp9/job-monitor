"""Fit scoring. All tuning knobs live in the WEIGHTS block below.

Pipeline per posting:
    1. hard exclusions  -> dropped, never notified, never digested
    2. positive gate    -> if it fails, posting goes to the weekly gated digest
    3. weighted score   -> 0-100, notified if >= threshold
"""
import re

# ---------------------------------------------------------------------------
# WEIGHTS — tune these.
# ---------------------------------------------------------------------------
W_POSITIVE_TITLE = 18      # sector keyword in the title
W_POSITIVE_BODY = 6        # sector keyword in description/department only
W_PROFILE_SIGNAL = 4       # something specific to Zaid's background
W_PREFERRED_LOCATION = 8   # NYC / SF / Greenwich etc.
W_TIMING_BOOST = 15        # title names the 2027 cycle explicitly
W_NEGATIVE_TITLE = -30     # off-track keyword in the title
W_NEGATIVE_BODY = -6       # off-track keyword in the body
W_CATCHALL_BASE = 10       # cleared the gate on title shape alone, no sector hit
W_INTERNSHIP = 25          # title is an internship — the actual target

CAP_POSITIVE_HITS = 3      # diminishing returns past this many
CAP_SIGNAL_HITS = 4
CAP_NEGATIVE_HITS = 3

DEFAULT_THRESHOLD = 45     # notify at or above this

# Titles with these tokens clear the gate even with zero sector keywords, so a
# strong-fit role with an unusual title still surfaces. Clearing the gate only
# means the posting gets *scored* — the threshold still filters it.
CATCHALL_TITLE_TOKENS = ["intern", "internship", "summer", "analyst", "associate"]

# A narrower set meaning "this really is an internship". Used for two things:
#   - the W_INTERNSHIP bonus
#   - exempting a title from the seniority exclusions below, so that a genuine
#     "Product Manager Intern" survives while "Strategy Manager" does not.
INTERNSHIP_TITLE_TOKENS = ["intern", "internship", "summer", "co op", "coop"]

# ---------------------------------------------------------------------------

US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia", "washington dc",
}
US_ABBREV = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}
US_MARKERS = {"united states", "usa", "u.s.", "u.s.a.", "us", "america", "nationwide"}
US_CITIES = {
    "new york", "new york city", "nyc", "brooklyn", "manhattan", "san francisco",
    "bay area", "palo alto", "mountain view", "menlo park", "sunnyvale",
    "san jose", "santa clara", "redwood city", "oakland", "los angeles",
    "san diego", "seattle", "bellevue", "redmond", "boston", "cambridge",
    "chicago", "austin", "dallas", "houston", "denver", "boulder", "atlanta",
    "miami", "philadelphia", "pittsburgh", "detroit", "minneapolis", "phoenix",
    "portland", "salt lake city", "nashville", "charlotte", "greenwich",
    "stamford", "hoboken", "jersey city", "arlington", "mclean", "reston",
    "el segundo", "hawthorne", "long beach", "irvine", "torrance",
}
NON_US_COUNTRIES = {
    "canada", "mexico", "brazil", "argentina", "chile", "colombia", "peru",
    "united kingdom", "uk", "england", "scotland", "ireland", "london",
    "dublin", "france", "paris", "germany", "berlin", "munich", "spain",
    "madrid", "barcelona", "portugal", "lisbon", "netherlands", "amsterdam",
    "belgium", "brussels", "switzerland", "zurich", "geneva", "italy", "milan",
    "rome", "sweden", "stockholm", "norway", "oslo", "denmark", "copenhagen",
    "finland", "helsinki", "poland", "warsaw", "krakow", "czech", "prague",
    "austria", "vienna", "romania", "bucharest", "greece", "athens", "israel",
    "tel aviv", "india", "bangalore", "bengaluru", "mumbai", "delhi",
    "hyderabad", "pune", "chennai", "gurgaon", "china", "beijing", "shanghai",
    "shenzhen", "hong kong", "taiwan", "taipei", "japan", "tokyo", "korea",
    "seoul", "singapore", "malaysia", "kuala lumpur", "indonesia", "jakarta",
    "philippines", "manila", "vietnam", "thailand", "bangkok", "australia",
    "sydney", "melbourne", "new zealand", "south africa", "nigeria", "kenya",
    "egypt", "cairo", "uae", "dubai", "abu dhabi", "saudi", "riyadh", "qatar",
    "turkey", "istanbul", "ukraine", "sao paulo", "são paulo", "bogota",
    "buenos aires", "santiago", "lima", "toronto", "vancouver", "montreal",
    "ottawa", "calgary", "guadalajara", "costa rica", "panama", "uruguay",
}
REMOTE_RE = re.compile(r"\bremote\b|\banywhere\b|\bdistributed\b", re.I)


def normalize(text: str) -> str:
    """Fold punctuation so 'FP&A' / 'FP and A' / 'full-stack' / 'full stack' match."""
    t = (text or "").lower()
    t = t.replace("&", " and ")
    t = re.sub(r"[/\-_,()\[\]]+", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def compile_terms(terms: list[str]) -> list[tuple[str, re.Pattern]]:
    """Word-boundary patterns. Substring matching would fire 'Internal Audit'
    and 'International Communications' on the token 'intern' — measured on real
    boards, that was 95 of 314 apparent matches."""
    out = []
    for t in terms or []:
        n = normalize(t)
        if not n:
            continue
        # Allow common inflections on the final word, so "software engineer" also
        # matches "Software Engineering" and "engineers". Without this, a plain
        # \b...\b pattern failed on every "... Engineering Internship" title,
        # because \b does not fall between "engineer" and "ing".
        body = re.escape(n).replace(r"\ ", r"\s+")
        out.append((t, re.compile(r"\b" + body + r"(?:ing|ers|ers?|s|es)?\b")))
    return out


def find_terms(terms: list[tuple[str, re.Pattern]], text: str) -> list[str]:
    """Matched keywords with overlapping matches collapsed to the longest.

    Without this, the single phrase 'Strategy & Operations' matches 'strategy',
    'strategy & operations' AND 'strategy and operations' — three hits for one
    phrase, which inflated senior full-time roles above real internships.
    Longest span wins; anything overlapping it is dropped.
    """
    hits = []
    for raw, pat in terms:
        m = pat.search(text)
        if m:
            hits.append((raw, m.start(), m.end()))
    hits.sort(key=lambda h: -(h[2] - h[1]))
    kept: list[tuple[str, int, int]] = []
    for raw, s, e in hits:
        if any(s < ke and e > ks for _, ks, ke in kept):
            continue
        kept.append((raw, s, e))
    return [h[0] for h in kept]


def _segments(location: str) -> list[str]:
    return [s.strip() for s in re.split(r"[;|]|\bor\b", location or "") if s.strip()]


def classify_location(location: str, profile: dict) -> tuple[str, bool]:
    """Return (verdict, is_preferred) where verdict is us | non_us | remote | unknown.

    Real strings seen on these boards: 'Bellevue, Washington; Mountain View,
    California', 'Remote - USA', 'Greenwich, CT', 'São Paulo, Brazil'.
    """
    loc_cfg = profile.get("location", {}) or {}
    preferred = [normalize(p) for p in (loc_cfg.get("preferred") or [])]
    segs = _segments(location)
    if not segs:
        return "unknown", False

    verdicts, is_pref = [], False
    for seg in segs:
        n = normalize(seg)
        tokens = [p.strip() for p in re.split(r"\s{2,}|,", n) if p.strip()]
        blob = " " + n + " "

        if any(re.search(r"\b" + re.escape(p) + r"\b", blob) for p in preferred if p):
            is_pref = True

        us = (
            any(t in US_STATES or t in US_ABBREV or t in US_MARKERS or t in US_CITIES
                for t in tokens)
            or any(re.search(r"\b" + re.escape(m) + r"\b", blob) for m in US_MARKERS)
            or any(re.search(r"\b" + re.escape(c) + r"\b", blob) for c in US_CITIES)
        )
        non_us = any(re.search(r"\b" + re.escape(c) + r"\b", blob)
                     for c in NON_US_COUNTRIES)

        if us and not non_us:
            verdicts.append("us")
        elif non_us and not us:
            verdicts.append("non_us")
        elif REMOTE_RE.search(seg):
            verdicts.append("remote")
        else:
            verdicts.append("unknown")

    if "us" in verdicts:
        return "us", is_pref
    if all(v == "remote" for v in verdicts):
        return "remote", is_pref
    if all(v == "non_us" for v in verdicts):
        return "non_us", is_pref
    return "unknown", is_pref


class Result:
    def __init__(self):
        self.score = 0
        self.excluded = False
        self.exclude_reason = ""
        self.gated = False
        self.reasons: list[str] = []

    def __repr__(self):
        state = "EXCLUDED" if self.excluded else ("GATED" if self.gated else self.score)
        return f"<Result {state}>"


def score_posting(posting, profile: dict, compiled: dict) -> Result:
    r = Result()
    title_n = normalize(posting.title)
    body_n = normalize(posting.haystack())

    # --- 1. hard exclusions -------------------------------------------------
    for raw, pat in compiled["strong_negatives"]:
        if pat.search(title_n):
            r.excluded = True
            r.exclude_reason = f"strong negative in title: {raw}"
            return r

    is_internship = any(re.search(r"\b" + t + r"\b", title_n)
                        for t in INTERNSHIP_TITLE_TOKENS)

    # Seniority exclusions do not apply to internship titles, so a genuine
    # "Product Manager Intern" survives while "Strategy Manager" is dropped.
    if not is_internship:
        for raw, pat in compiled["timing_exclude"]:
            if pat.search(title_n):
                r.excluded = True
                r.exclude_reason = f"seniority/timing exclusion: {raw}"
                return r

    verdict, is_pref = classify_location(posting.location, profile)
    remote_ok = (profile.get("location", {}) or {}).get("remote_ok", False)
    if verdict == "non_us":
        r.excluded = True
        r.exclude_reason = f"non-US location: {posting.location}"
        return r
    if verdict == "remote" and not remote_ok:
        r.excluded = True
        r.exclude_reason = f"remote-only and remote_ok is false: {posting.location}"
        return r

    # --- 2. positive gate ---------------------------------------------------
    pos_title = find_terms(compiled["positive"], title_n)
    pos_body = [raw for raw in find_terms(compiled["positive"], body_n)
                if raw not in pos_title]
    catchall = [t for t in CATCHALL_TITLE_TOKENS
                if re.search(r"\b" + t + r"\b", title_n)]

    if not pos_title and not pos_body:
        if not catchall:
            r.gated = True
            r.exclude_reason = "no sector keyword and no catch-all title token"
            return r
        r.score += W_CATCHALL_BASE
        r.reasons.append(f"+{W_CATCHALL_BASE} catch-all title token: {', '.join(catchall)}")

    # --- 3. scoring ---------------------------------------------------------
    if pos_title:
        hits = pos_title[:CAP_POSITIVE_HITS]
        pts = W_POSITIVE_TITLE * len(hits)
        r.score += pts
        r.reasons.append(f"+{pts} title keywords: {', '.join(hits)}")
    if pos_body:
        hits = pos_body[:max(0, CAP_POSITIVE_HITS - len(pos_title))]
        if hits:
            pts = W_POSITIVE_BODY * len(hits)
            r.score += pts
            r.reasons.append(f"+{pts} body keywords: {', '.join(hits)}")

    neg_title = find_terms(compiled["negative"], title_n)
    neg_body = [raw for raw in find_terms(compiled["negative"], body_n)
                if raw not in neg_title]
    if neg_title:
        hits = neg_title[:CAP_NEGATIVE_HITS]
        pts = W_NEGATIVE_TITLE * len(hits)
        r.score += pts
        r.reasons.append(f"{pts} title negatives: {', '.join(hits)}")
    if neg_body:
        hits = neg_body[:CAP_NEGATIVE_HITS]
        pts = W_NEGATIVE_BODY * len(hits)
        r.score += pts
        r.reasons.append(f"{pts} body negatives: {', '.join(hits)}")

    # W_INTERNSHIP and W_TIMING_BOOST are fit *amplifiers*: they say "right cycle,
    # right stage", not "right track". Applying them to a title that already hit a
    # negative amplified the wrong roles — "Hardware Engineer Intern - Summer 2027"
    # reached 58, above the Databricks PM internship. Off-track titles get the
    # penalty without the amplifiers.
    amplify = not neg_title
    if is_internship and amplify:
        r.score += W_INTERNSHIP
        r.reasons.append(f"+{W_INTERNSHIP} internship title")
    elif is_internship:
        r.reasons.append(f"internship/timing bonuses withheld (negative in title)")

    signals = find_terms(compiled["signals"], body_n)
    if signals:
        hits = signals[:CAP_SIGNAL_HITS]
        pts = W_PROFILE_SIGNAL * len(hits)
        r.score += pts
        r.reasons.append(f"+{pts} profile signals: {', '.join(hits)}")

    if is_pref:
        r.score += W_PREFERRED_LOCATION
        r.reasons.append(f"+{W_PREFERRED_LOCATION} preferred location: {posting.location}")

    if amplify:
        for raw, pat in compiled["timing_boost"]:
            if pat.search(title_n):
                r.score += W_TIMING_BOOST
                r.reasons.append(f"+{W_TIMING_BOOST} target cycle in title: {raw}")
                break

    r.score = max(0, min(100, r.score))
    return r


def compile_profile(profile: dict) -> dict:
    timing = profile.get("timing", {}) or {}
    return {
        "positive": compile_terms(profile.get("positive_keywords")),
        "negative": compile_terms(profile.get("negative_keywords")),
        "strong_negatives": compile_terms(profile.get("strong_negatives")),
        "signals": compile_terms(profile.get("profile_signals")),
        "timing_exclude": compile_terms(timing.get("exclude")),
        "timing_boost": compile_terms(timing.get("boost")),
    }
