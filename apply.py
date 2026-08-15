#!/usr/bin/env python3
"""Build application packets for your current matches.

    python apply.py                  # top 15 unapplied matches
    python apply.py --top 40
    python apply.py --company AQR
    python apply.py --mark <url>     # record that you applied
    python apply.py --list           # show apply status

Reads state/last_scan.json (the tuning snapshot), so run ./tune --refresh first
if it is stale. Writes one markdown packet per posting into applications/ plus
an autofill bookmarklet.

Nothing here submits anything. See src/apply.py for why.
"""
import argparse
import json
import os
import re
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import state                                        # noqa: E402
from src.apply import (build_packet, load_applicant,          # noqa: E402
                       render_packet_md)
from src.providers.base import Posting                        # noqa: E402
from src.scoring import compile_profile, resolve_threshold, score_posting  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
APPLICANT_PATH = os.path.join(ROOT, "config", "applicant.yaml")
PROFILE_PATH = os.path.join(ROOT, "config", "profile.yaml")
OUT_DIR = os.path.join(ROOT, "applications")
APPLIED_PATH = os.path.join(ROOT, "state", "applied.json")

# Board slug lookup, so packets know which Greenhouse board to ask for questions.
BOARDS_PATH = os.path.join(ROOT, "config", "boards.yaml")


def slug_for(company: str) -> tuple[str, str]:
    with open(BOARDS_PATH) as f:
        for b in (yaml.safe_load(f) or {}).get("boards", []):
            if b.get("company") == company:
                return b.get("provider", ""), b.get("slug", "")
    return "", ""


def load_applied() -> dict:
    try:
        with open(APPLIED_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_applied(d: dict) -> None:
    os.makedirs(os.path.dirname(APPLIED_PATH), exist_ok=True)
    tmp = APPLIED_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, APPLIED_PATH)


def current_matches(limit_company=None):
    profile = yaml.safe_load(open(PROFILE_PATH))
    compiled = compile_profile(profile)
    threshold = resolve_threshold(profile)
    out = []
    for r in state.load_snapshot():
        if limit_company and limit_company.lower() not in r["company"].lower():
            continue
        p = Posting(r["provider"], r["company"], str(r["job_id"]), r["title"],
                    r["location"], r["url"], r.get("posted_at"),
                    r.get("department", ""), r.get("description", ""))
        res = score_posting(p, profile, compiled)
        if not res.excluded and not res.gated and res.score >= threshold:
            out.append((p, res.score))
    out.sort(key=lambda x: -x[1])
    return out


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:70]


BOOKMARKLET_JS = r"""
(function(){
 var D=%s;
 function norm(s){return (s||'').toLowerCase().replace(/\s+/g,' ').trim()}
 function labelFor(el){
  var t='';
  if(el.id){var l=document.querySelector('label[for="'+CSS.escape(el.id)+'"]'); if(l)t+=' '+l.innerText}
  var p=el.closest('label'); if(p)t+=' '+p.innerText;
  var w=el.closest('div,fieldset,li'); if(w){var h=w.querySelector('label,legend'); if(h)t+=' '+h.innerText}
  return norm(t+' '+(el.name||'')+' '+(el.placeholder||'')+' '+(el.getAttribute('aria-label')||''))
 }
 function setv(el,v){
  var s=Object.getOwnPropertyDescriptor(el.constructor.prototype,'value').set;
  s.call(el,v);
  el.dispatchEvent(new Event('input',{bubbles:true}));
  el.dispatchEvent(new Event('change',{bubbles:true}));
 }
 var n=0,skipped=0;
 document.querySelectorAll('input,textarea,select').forEach(function(el){
  if(el.type==='file'||el.type==='hidden'||el.disabled||el.readOnly)return;
  var L=labelFor(el);
  if(/gender|race|ethnic|veteran|disability|pronoun|hispanic|latino|self.?identif/.test(L)){skipped++;return}
  for(var i=0;i<D.length;i++){
   var rx=new RegExp(D[i][0]);
   if(rx.test(L)){
    if(el.tagName==='SELECT'){
     var want=norm(D[i][1]),hit=null;
     for(var o=0;o<el.options.length;o++){if(norm(el.options[o].text).indexOf(want)>=0){hit=el.options[o].value;break}}
     if(hit!==null){el.value=hit;el.dispatchEvent(new Event('change',{bubbles:true}));n++}
    } else if(!el.value){ setv(el,D[i][1]); n++ }
    break
   }
  }
 });
 alert('Autofilled '+n+' field(s).\n'+skipped+' self-identification field(s) left for you.\n\nFile uploads are never autofilled.\nReview everything before submitting.');
})();
"""


def build_bookmarklet(applicant: dict) -> str:
    """A bookmarklet, not an extension or a headless driver.

    It runs in your browser, on a page you opened, and fills visible text fields
    only — never file inputs, never self-identification, never submit. That keeps
    the human review step where it belongs.
    """
    ident, edu, wa, dflt = (applicant["identity"], applicant["education"],
                            applicant["work_authorization"], applicant["defaults"])
    pairs = [
        (r"preferred.*(first|name)", ident.get("preferred_name") or ident.get("first_name")),
        (r"\bfirst name\b|\bgiven name\b", ident.get("first_name")),
        (r"\blast name\b|\bsurname\b|family name", ident.get("last_name")),
        (r"e-?mail", ident.get("email")),
        (r"phone|mobile|telephone", ident.get("phone")),
        (r"linkedin", ident.get("linkedin")),
        (r"website|portfolio", ident.get("website")),
        (r"school|university|college", edu.get("school")),
        (r"\bmajor\b|field of study", edu.get("major")),
        (r"\bgpa\b|grade point", edu.get("gpa")),
        (r"graduation (year|date)|expected grad", edu.get("grad_year")),
        (r"\bdegree\b", edu.get("degree")),
        (r"\bsat\b", edu.get("sat")),
        (r"\bact\b", edu.get("act")),
        (r"\blocation\b|\bcity\b", ident.get("location")),
        (r"authorized to work|legally authorized", "Yes" if wa.get("authorized_to_work_us") else "No"),
        (r"sponsorship|visa", "Yes" if wa.get("will_require_sponsorship") else "No"),
        (r"how did you hear|how.*learn about", dflt.get("how_did_you_hear")),
    ]
    data = [[p, str(v)] for p, v in pairs if v]
    js = BOOKMARKLET_JS % json.dumps(data)
    return "javascript:" + re.sub(r"\s*\n\s*", "", js)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--company")
    ap.add_argument("--mark", help="record a posting URL as applied")
    ap.add_argument("--unmark")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    applied = load_applied()

    if args.mark:
        applied[args.mark] = {"applied_at": state.now()}
        save_applied(applied)
        print(f"marked applied: {args.mark}")
        return 0
    if args.unmark:
        applied.pop(args.unmark, None)
        save_applied(applied)
        print(f"unmarked: {args.unmark}")
        return 0
    if args.list:
        print(f"{len(applied)} application(s) recorded:")
        for url, meta in sorted(applied.items(), key=lambda x: x[1].get("applied_at", "")):
            print(f"  {meta.get('applied_at','')[:10]}  {url}")
        return 0

    applicant = load_applicant(APPLICANT_PATH)
    if not applicant:
        print("No config/applicant.yaml found.")
        print("Copy the template and fill it in:")
        print("    cp config/applicant.example.yaml config/applicant.yaml")
        print("It is gitignored — your phone number and GPA stay off git history.")
        return 1

    matches = current_matches(args.company)
    if not matches:
        print("No matches in the snapshot. Run ./tune --refresh first.")
        return 1

    todo = [(p, s) for p, s in matches if p.url not in applied][:args.top]
    os.makedirs(OUT_DIR, exist_ok=True)

    print(f"{len(matches)} match(es), {len(applied)} already applied. "
          f"Building {len(todo)} packet(s)...\n")
    index = ["# Application queue", "",
             f"_{len(todo)} to do, {len(applied)} already applied._", ""]
    for p, sc in todo:
        provider, slug = slug_for(p.company)
        pk = build_packet(p, sc, provider or p.provider, slug, applicant)
        name = f"{slugify(p.company)}--{slugify(p.title)}.md"
        with open(os.path.join(OUT_DIR, name), "w") as f:
            f.write(render_packet_md(pk))
        gap = len(pk["todo"])
        print(f"  [{sc:>3}] {p.company[:22]:<22} {pk['filled']:>2}/{pk['total']:<2} filled"
              f"{'  ' + str(gap) + ' need you' if gap else '  ready to review'}")
        index.append(f"- [{sc}] [{p.title}]({name}) — {p.company} "
                     f"({pk['filled']}/{pk['total']} filled)")

    with open(os.path.join(OUT_DIR, "README.md"), "w") as f:
        f.write("\n".join(index) + "\n")
    with open(os.path.join(OUT_DIR, "autofill-bookmarklet.txt"), "w") as f:
        f.write(build_bookmarklet(applicant) + "\n")

    print(f"\nPackets: {OUT_DIR}/")
    print(f"Bookmarklet: {OUT_DIR}/autofill-bookmarklet.txt")
    print("Mark one done with:  python apply.py --mark <url>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
