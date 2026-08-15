"""Application prep: turn a match into a near-complete, reviewable application.

What this does NOT do is submit. The board APIs are read-only for applicants —
submission requires employer credentials — and beyond that, several of these
forms carry an "I certify this information is accurate" attestation. Certifying
something you have not read is not a thing worth automating, and a detectably
automated application to a firm you actually want is worse than none.

So the split is: this fills in everything knowable and tells you precisely what
is left, and you review and press submit.

Greenhouse exposes the exact question set per posting (?questions=true), so its
packets list real fields with real answers. Lever, Ashby and Workday do not, so
those fall back to the standard field set plus the apply link — the browser
autofill still works there because it matches the live form's DOM.
"""
import os
import re

from .http import get_json

GH_QUESTIONS = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}?questions=true"

# Question-label patterns -> a resolver against the applicant profile.
# Ordered: first match wins, so specific patterns precede general ones.
FIELD_RULES = [
    (r"preferred.*(first|name)",      lambda a: a["identity"].get("preferred_name")
                                                or a["identity"].get("first_name")),
    (r"\bfirst name\b|\bgiven name\b", lambda a: a["identity"].get("first_name")),
    (r"\blast name\b|\bsurname\b|family name", lambda a: a["identity"].get("last_name")),
    (r"full name",                    lambda a: f"{a['identity'].get('first_name','')} "
                                                f"{a['identity'].get('last_name','')}".strip()),
    (r"e-?mail",                      lambda a: a["identity"].get("email")),
    (r"phone|mobile|telephone",       lambda a: a["identity"].get("phone")),
    (r"linkedin",                     lambda a: a["identity"].get("linkedin")),
    (r"website|portfolio|personal site", lambda a: a["identity"].get("website")),
    (r"\blocation\b|\bcity\b|where are you (located|based)",
                                      lambda a: a["identity"].get("location")),
    (r"school|university|college|institution", lambda a: a["education"].get("school")),
    (r"\bmajor\b|field of study|course of study", lambda a: a["education"].get("major")),
    (r"\bminor\b",                    lambda a: a["education"].get("minor")),
    (r"\bgpa\b|grade point",          lambda a: a["education"].get("gpa")),
    (r"graduation (year|date)|expected grad|when.*graduate",
                                      lambda a: a["education"].get("grad_year")),
    (r"\bdegree\b",                   lambda a: a["education"].get("degree")),
    (r"\bsat\b.*\bact\b|\bsat\b/\s*act|standardized test",
                                      lambda a: ", ".join(
                                          f"{k.upper()} {a['education'][k]}"
                                          for k in ("sat", "act") if a["education"].get(k))),
    (r"\bsat\b",                      lambda a: a["education"].get("sat")),
    (r"\bact\b",                      lambda a: a["education"].get("act")),
    (r"authorized to work|legally authorized|work authorization",
                                      lambda a: "Yes" if a["work_authorization"]
                                      .get("authorized_to_work_us") else "No"),
    (r"sponsorship|visa",             lambda a: "Yes" if a["work_authorization"]
                                      .get("will_require_sponsorship") else "No"),
    (r"how did you hear|how.*learn about", lambda a: a["defaults"].get("how_did_you_hear")),
]

# Never pre-filled: self-identification is the applicant's to answer or decline.
SELF_ID = re.compile(
    r"gender|race|ethnic|veteran|disability|pronoun|sexual orientation|"
    r"hispanic|latino|self.?identif|diversity", re.I)

FILE_FIELDS = re.compile(r"resume|cv\b|transcript|cover letter|portfolio file", re.I)


def load_applicant(path: str) -> dict | None:
    import yaml
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    for section in ("identity", "education", "work_authorization", "files", "defaults"):
        data.setdefault(section, {})
    return data


def resolve(label: str, applicant: dict) -> tuple[str, str]:
    """Return (value, status) for one question label.

    status is one of: filled | self_id | file | manual
    """
    if SELF_ID.search(label):
        return "", "self_id"

    low = label.lower()
    if FILE_FIELDS.search(label):
        files = applicant.get("files", {})
        if re.search(r"resume|cv\b", low):
            return os.path.expanduser(files.get("resume") or ""), "file"
        if "transcript" in low:
            return os.path.expanduser(files.get("transcript") or ""), "file"
        return "", "file"

    for pattern, fn in FIELD_RULES:
        if re.search(pattern, low):
            try:
                val = fn(applicant)
            except Exception:
                val = None
            if val:
                return str(val), "filled"
            return "", "manual"
    return "", "manual"


def fetch_questions(provider: str, slug: str, job_id: str) -> list[dict]:
    """Real question set where the provider publishes one, else []."""
    if provider != "greenhouse":
        return []
    try:
        d = get_json(GH_QUESTIONS.format(slug=slug, job_id=job_id))
        return d.get("questions") or []
    except Exception:
        return []


# Standard fields used when the provider publishes no question set.
FALLBACK_LABELS = [
    "First Name", "Last Name", "Email", "Phone", "Location",
    "School", "Major", "Degree", "Graduation Year", "GPA",
    "LinkedIn Profile", "Resume/CV",
    "Are you legally authorized to work in the US?",
    "Will you require sponsorship?",
]


def build_packet(posting, score, provider: str, slug: str, applicant: dict) -> dict:
    """Everything needed to complete one application."""
    questions = fetch_questions(provider, slug, posting.job_id)
    rows = []

    if questions:
        for q in questions:
            label = (q.get("label") or "").strip()
            if not label:
                continue
            fields = q.get("fields") or [{}]
            ftype = fields[0].get("type", "")
            value, status = resolve(label, applicant)
            opts = [o.get("label") for o in (fields[0].get("values") or [])][:12]
            rows.append({"label": label, "required": bool(q.get("required")),
                         "type": ftype, "value": value, "status": status,
                         "options": opts})
    else:
        for label in FALLBACK_LABELS:
            value, status = resolve(label, applicant)
            rows.append({"label": label, "required": True, "type": "input_text",
                         "value": value, "status": status, "options": []})

    filled = sum(1 for r in rows if r["status"] == "filled")
    todo = [r for r in rows if r["status"] in ("manual", "file")
            and (r["required"] or r["status"] == "file")]
    return {
        "title": posting.title, "company": posting.company, "url": posting.url,
        "location": posting.location, "score": score, "provider": provider,
        "rows": rows, "filled": filled, "total": len(rows),
        "todo": todo, "known_questions": bool(questions),
    }


def render_packet_md(pk: dict) -> str:
    lines = [f"# {pk['title']}", "",
             f"**{pk['company']}** · {pk['location'] or 'location not listed'} · "
             f"fit score {pk['score']}", "",
             f"[Open the application form]({pk['url']})", "",
             f"_{pk['filled']} of {pk['total']} fields pre-filled"
             + ("" if pk["known_questions"] else
                "; this provider does not publish its question set, so these are "
                "the standard fields — the live form may differ")
             + "._", ""]
    if pk["todo"]:
        lines += ["## Needs you", ""]
        for r in pk["todo"]:
            why = "upload" if r["status"] == "file" else "no stored answer"
            extra = f" — file: `{r['value']}`" if r["status"] == "file" and r["value"] else ""
            lines.append(f"- **{r['label']}** ({why}){extra}")
        lines.append("")
    lines += ["## All fields", "", "| Field | | Answer |", "|---|---|---|"]
    mark = {"filled": "✓", "manual": "—", "file": "↑", "self_id": "you"}
    for r in pk["rows"]:
        val = r["value"] or ("_your choice_" if r["status"] == "self_id" else "")
        req = " *" if r["required"] else ""
        lines.append(f"| {r['label']}{req} | {mark[r['status']]} | {val} |")
    lines += ["", "_* required. ✓ pre-filled · ↑ file upload · — needs an answer · "
              "`you` self-identification, never pre-filled._", "",
              "Review every field on the real form before submitting."]
    return "\n".join(lines)
