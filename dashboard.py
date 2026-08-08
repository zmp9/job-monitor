#!/usr/bin/env python3
"""Local tuning dashboard for the fit-scoring profile.

    python main.py --dry-run --only-boards --snapshot   # populate the cache (~4 min)
    python dashboard.py                                 # then tune at localhost:8000

Why local rather than hosted: tuning means *writing* config/profile.yaml, which
a static page can't do without handing credentials to a browser. Running on
your machine sidesteps auth entirely and lets the preview re-score in-process.

The preview calls the real compile_profile()/score_posting() from src.scoring —
never a reimplementation — so what you see here is exactly what the 7am cron
will do with the same profile.

Stdlib only. requirements.txt is installed on every CI run and this file never
runs there, so it must not add a dependency.
"""
import http.server
import json
import os
import socketserver
import sys
import time
import webbrowser

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import state                                            # noqa: E402
from src.providers.base import Posting                           # noqa: E402
from src.scoring import (WEIGHT_DEFAULTS, compile_profile,       # noqa: E402
                         normalize, resolve_threshold, score_posting)

ROOT = os.path.dirname(os.path.abspath(__file__))
PROFILE_PATH = os.path.join(ROOT, "config", "profile.yaml")
PORT = int(os.environ.get("DASHBOARD_PORT", "8000"))
TOP_N = 25

KEYWORD_FIELDS = ["positive_keywords", "negative_keywords",
                  "strong_negatives", "profile_signals"]

_SNAPSHOT: list = []
_NORM: list = []          # (title_n, body_n) parallel to _SNAPSHOT
_COUNT_CACHE: dict = {}   # keyword string -> postings matched


def load_snapshot_postings() -> list:
    """Rehydrate cached rows into Posting objects so scoring sees real inputs."""
    rows = state.load_snapshot()
    out = []
    for r in rows:
        out.append(Posting(
            provider=r.get("provider", ""), company=r.get("company", ""),
            job_id=str(r.get("job_id", "")), title=r.get("title", ""),
            location=r.get("location", ""), url=r.get("url", ""),
            posted_at=r.get("posted_at"), department=r.get("department", ""),
            description=r.get("description", ""),
        ))
    return out


def precompute_norms(postings: list) -> list:
    """Normalize every posting once.

    normalize() runs three regex substitutions over text up to ~2.6KB. Doing it
    per keyword per posting per keystroke made a preview take minutes; hoisting
    it out is the difference between a live control and a batch job.
    """
    return [(normalize(p.title), normalize(p.haystack())) for p in postings]


def keyword_counts(profile: dict) -> dict:
    """Postings matched per keyword, memoized by keyword string.

    Editing a list adds or removes terms but never changes what an existing term
    matches, so cached counts stay valid for the life of the snapshot.
    """
    counts = {}
    for field in KEYWORD_FIELDS:
        field_counts = {}
        for raw, pat in compile_terms_for(profile, field):
            if raw not in _COUNT_CACHE:
                _COUNT_CACHE[raw] = sum(
                    1 for tn, bn in _NORM if pat.search(tn) or pat.search(bn))
            field_counts[raw] = _COUNT_CACHE[raw]
        counts[field] = field_counts
    return counts


_SCORE_CACHE = {"key": None, "scored": [], "excluded": 0, "gated": 0}


def _scoring_key(profile: dict) -> str:
    """Everything that changes a score — deliberately excluding threshold.

    Threshold only filters already-computed scores, and it's the control that
    gets dragged most, so separating it turns threshold tuning into a re-filter
    rather than a 13k-posting rescore.
    """
    relevant = {k: v for k, v in profile.items() if k != "threshold"}
    return json.dumps(relevant, sort_keys=True, default=str)


def evaluate(profile: dict) -> dict:
    """Score the whole snapshot under `profile` and summarise."""
    threshold = resolve_threshold(profile)
    key = _scoring_key(profile)

    if _SCORE_CACHE["key"] != key:
        compiled = compile_profile(profile)
        scored, excluded, gated = [], 0, 0
        for p, (tn, bn) in zip(_SNAPSHOT, _NORM):
            r = score_posting(p, profile, compiled, title_n=tn, body_n=bn)
            if r.excluded:
                excluded += 1
            elif r.gated:
                gated += 1
            else:
                scored.append((p, r))
        scored.sort(key=lambda x: -x[1].score)
        _SCORE_CACHE.update(key=key, scored=scored, excluded=excluded, gated=gated)

    excluded, gated = _SCORE_CACHE["excluded"], _SCORE_CACHE["gated"]
    matches = [(p, r) for p, r in _SCORE_CACHE["scored"] if r.score >= threshold]

    counts = keyword_counts(profile)

    return {
        "threshold": threshold,
        "total": len(_SNAPSHOT),
        "matches": len(matches),
        "excluded": excluded,
        "gated": gated,
        "keyword_counts": counts,
        "top": [{
            "score": r.score, "title": p.title, "company": p.company,
            "location": p.location, "url": p.url, "reasons": r.reasons,
        } for p, r in matches[:TOP_N]],
    }


def compile_terms_for(profile: dict, field: str):
    from src.scoring import compile_terms
    return compile_terms(profile.get(field) or [])


def read_profile() -> dict:
    with open(PROFILE_PATH) as f:
        return yaml.safe_load(f) or {}


WEIGHT_LABELS = {
    "positive_title": "sector keyword in the title",
    "positive_body": "sector keyword in description/department only",
    "profile_signal": "something specific to my background",
    "preferred_location": "NYC / SF / Greenwich etc.",
    "timing_boost": "title names the 2027 cycle explicitly",
    "negative_title": "off-track keyword in the title",
    "negative_body": "off-track keyword in the body",
    "catchall_base": "cleared the gate on title shape alone",
    "internship": "title is an internship — the actual target",
}

# Section preambles re-emitted on save. yaml.safe_dump discards comments, and
# several of these document non-obvious, load-bearing behaviour (the internship
# waiver in particular) — losing them on the first save would be a real
# maintainability regression.
SECTION_NOTES = {
    "timing": [
        "# Hard exclusions matched against the title.",
        "# NOTE: `exclude` is skipped when the title is an internship, so",
        '# "Product Manager Intern" survives but "Strategy Manager" does not.',
    ],
    "negative_keywords": [
        "# Demote — not my track. Titles here lose the internship and timing",
        "# bonuses as well, so an off-track role cannot ride the cycle boost.",
    ],
    "strong_negatives": [
        "# If present in the TITLE, exclude outright (never scored).",
        "# Unlike timing.exclude these are NOT waived for internship titles,",
        '# which is why phd/mba live here: "PhD Research Scientist Intern" is dropped.',
    ],
    "profile_signals": ["# What makes me specifically a fit — smaller boost."],
    "sectors": ["# All weighted EQUALLY — no sector multipliers."],
}


def write_profile(profile: dict) -> None:
    """Rewrite profile.yaml, preserving the documented structure.

    yaml.safe_dump drops comments, so the file is rebuilt from a template:
    weights and threshold are emitted with inline labels, and each remaining
    top-level section is preceded by its note from SECTION_NOTES. Keys the
    dashboard doesn't manage are dumped verbatim.
    """
    w = {**WEIGHT_DEFAULTS, **(profile.get("weights") or {})}
    out = [
        "# Scoring weights and cutoff. Edited by dashboard.py; src/scoring.py holds the",
        "# same values as defaults, so removing a key here falls back rather than breaking.",
        "weights:",
    ]
    for k in WEIGHT_DEFAULTS:
        out.append(f"  {k}: {int(w[k])}".ljust(28) + f"# {WEIGHT_LABELS[k]}")
    out += ["", f"threshold: {int(resolve_threshold(profile))}".ljust(28)
                + "# notify at or above this"]

    for key, value in profile.items():
        if key in ("weights", "threshold"):
            continue
        out.append("")
        out.extend(SECTION_NOTES.get(key, []))
        out.append(yaml.safe_dump({key: value}, sort_keys=False, allow_unicode=True,
                                  default_flow_style=False, width=100).rstrip())

    tmp = PROFILE_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, PROFILE_PATH)   # atomic; a crash never truncates the config


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if self.path == "/api/profile":
            p = read_profile()
            return self._send(200, {"profile": p, "evaluation": evaluate(p),
                                    "weight_keys": list(WEIGHT_DEFAULTS)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            if self.path == "/api/preview":
                return self._send(200, evaluate(self._body().get("profile") or {}))
            if self.path == "/api/save":
                profile = self._body().get("profile") or {}
                write_profile(profile)
                return self._send(200, {"ok": True, "path": PROFILE_PATH})
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, *a):
        pass    # keep the console readable


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>job-monitor tuning</title>
<style>
:root{--bg:#0f1115;--fg:#e6e8eb;--mut:#9aa3ad;--line:#242a33;--card:#161a20;--acc:#5b9dd9;--good:#4ea87a;--bad:#c76b6b}
@media(prefers-color-scheme:light){:root{--bg:#f7f8fa;--fg:#1a1d21;--mut:#5f6873;--line:#dfe3e8;--card:#fff;--acc:#2b6cb0}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;gap:20px;align-items:baseline;flex-wrap:wrap;position:sticky;top:0;background:var(--bg);z-index:5}
h1{font-size:15px;margin:0;font-weight:600}
.stat{color:var(--mut);font-size:13px}
.stat b{color:var(--fg);font-variant-numeric:tabular-nums}
main{display:grid;grid-template-columns:340px 1fr;gap:20px;padding:20px;align-items:start}
@media(max-width:900px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;margin-bottom:16px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:0 0 10px}
label{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:5px 0;font-size:13px}
label span{color:var(--mut)}
input[type=number]{width:74px;background:var(--bg);border:1px solid var(--line);color:var(--fg);border-radius:5px;padding:4px 7px;font:inherit;font-variant-numeric:tabular-nums}
.kw{display:flex;align-items:center;gap:7px;padding:2px 0;font-size:13px}
.kw input{accent-color:var(--acc)}
.kw .n{margin-left:auto;color:var(--mut);font-variant-numeric:tabular-nums;font-size:12px}
.kw.dead .n{color:var(--bad)}
.kw.dead label{opacity:.55}
.row{display:flex;align-items:baseline;gap:9px;padding:8px 0;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}
.sc{font-variant-numeric:tabular-nums;font-weight:600;min-width:30px;text-align:right;color:var(--acc)}
.t{flex:1;min-width:0}
.t .ttl{font-weight:500}
.t .meta{color:var(--mut);font-size:12px}
.t .why{color:var(--mut);font-size:11.5px;margin-top:3px;display:none}
.row.open .why{display:block}
button{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:8px 14px;font:inherit;font-weight:500;cursor:pointer}
button.ghost{background:transparent;color:var(--mut);border:1px solid var(--line)}
button:disabled{opacity:.5;cursor:default}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.msg{font-size:12px;color:var(--good)}
.msg.err{color:var(--bad)}
.diff{font-variant-numeric:tabular-nums;font-size:12px}
.up{color:var(--good)}.down{color:var(--bad)}
a{color:inherit}
</style></head><body>
<header>
  <h1>job-monitor · tuning</h1>
  <div class="stat">matches <b id="s-match">–</b> of <b id="s-total">–</b>
    <span id="s-diff" class="diff"></span></div>
  <div class="stat">excluded <b id="s-excl">–</b> · gated <b id="s-gate">–</b></div>
  <div class="bar" style="margin-left:auto">
    <span id="msg" class="msg"></span>
    <button class="ghost" id="reset">Revert</button>
    <button id="save">Save to profile.yaml</button>
  </div>
</header>
<main>
 <div>
  <div class="card">
    <h2>Threshold</h2>
    <label>notify at or above <input type="number" id="threshold"></label>
  </div>
  <div class="card">
    <h2>Weights</h2>
    <div id="weights"></div>
  </div>
 </div>
 <div>
  <div class="card">
    <h2>Preview — top <span id="topn"></span> <span style="color:var(--mut);font-weight:400;text-transform:none;letter-spacing:0">(click a row for score reasons)</span></h2>
    <div id="rows"></div>
  </div>
  <div class="card">
    <h2>Keywords <span style="color:var(--mut);font-weight:400;text-transform:none;letter-spacing:0">— counts are postings matched in the snapshot; uncheck to remove</span></h2>
    <div id="kws"></div>
  </div>
 </div>
</main>
<script>
let P=null, ORIG=null, BASE=null, timer=null;
const $=id=>document.getElementById(id);
const FIELDS=["positive_keywords","negative_keywords","strong_negatives","profile_signals"];
const LABEL={positive_keywords:"positive",negative_keywords:"negative",strong_negatives:"strong negatives",profile_signals:"profile signals"};

function clone(o){return JSON.parse(JSON.stringify(o))}

async function boot(){
  const r=await fetch("/api/profile").then(r=>r.json());
  P=r.profile; ORIG=clone(r.profile);
  $("topn").textContent=r.evaluation.top.length;
  BASE=r.evaluation.matches;
  buildWeights(r.weight_keys); render(r.evaluation); paintKeywords(r.evaluation);
}

function buildWeights(keys){
  $("threshold").value=P.threshold??45;
  $("threshold").oninput=()=>{P.threshold=+$("threshold").value;schedule()};
  $("weights").innerHTML="";
  P.weights=P.weights||{};
  keys.forEach(k=>{
    const l=document.createElement("label");
    l.innerHTML=`<span>${k.replace(/_/g," ")}</span>`;
    const i=document.createElement("input");
    i.type="number"; i.value=P.weights[k];
    i.oninput=()=>{P.weights[k]=+i.value;schedule()};
    l.appendChild(i); $("weights").appendChild(l);
  });
}

function paintKeywords(ev){
  const c=$("kws"); c.innerHTML="";
  FIELDS.forEach(f=>{
    const h=document.createElement("div");
    h.style.cssText="margin:10px 0 4px;color:var(--mut);font-size:12px";
    h.textContent=LABEL[f]; c.appendChild(h);
    (P[f]||[]).forEach(kw=>{
      const n=(ev.keyword_counts[f]||{})[kw]??0;
      const d=document.createElement("div");
      d.className="kw"+(n===0?" dead":"");
      const id="k_"+f+"_"+btoa(unescape(encodeURIComponent(kw))).replace(/=/g,"");
      d.innerHTML=`<input type="checkbox" id="${id}" checked><label for="${id}">${kw}</label><span class="n">${n}</span>`;
      d.querySelector("input").onchange=e=>{
        if(!e.target.checked){P[f]=P[f].filter(x=>x!==kw)}
        else if(!P[f].includes(kw)){P[f].push(kw)}
        schedule();
      };
      c.appendChild(d);
    });
  });
}

function render(ev){
  $("s-match").textContent=ev.matches; $("s-total").textContent=ev.total;
  $("s-excl").textContent=ev.excluded; $("s-gate").textContent=ev.gated;
  const d=ev.matches-BASE, el=$("s-diff");
  el.textContent = d===0?"":(d>0?` +${d}`:` ${d}`);
  el.className="diff "+(d>0?"up":d<0?"down":"");
  const rows=$("rows"); rows.innerHTML="";
  if(!ev.top.length){rows.innerHTML='<div class="stat">no matches at this threshold</div>';return}
  ev.top.forEach(m=>{
    const r=document.createElement("div"); r.className="row";
    r.innerHTML=`<div class="sc">${m.score}</div><div class="t">
      <div class="ttl">${esc(m.title)}</div>
      <div class="meta">${esc(m.company)} · ${esc(m.location||"location not listed")}
        · <a href="${m.url}" target="_blank" rel="noopener">open</a></div>
      <div class="why">${m.reasons.map(esc).join("<br>")}</div></div>`;
    r.onclick=e=>{if(e.target.tagName!=="A")r.classList.toggle("open")};
    rows.appendChild(r);
  });
}
function esc(s){return String(s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))}

function schedule(){clearTimeout(timer);timer=setTimeout(preview,350)}
async function preview(){
  $("s-match").style.opacity=".45";
  const ev=await fetch("/api/preview",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({profile:P})}).then(r=>r.json());
  if(ev.error){note(ev.error,true);return}
  $("s-match").style.opacity="1";
  render(ev); paintKeywords(ev);
}
function note(t,bad){const m=$("msg");m.textContent=t;m.className="msg"+(bad?" err":"");
  setTimeout(()=>{m.textContent=""},4000)}

$("save").onclick=async()=>{
  $("save").disabled=true;
  const r=await fetch("/api/save",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({profile:P})}).then(r=>r.json());
  $("save").disabled=false;
  if(r.ok){ORIG=clone(P);BASE=+$("s-match").textContent;note("saved — review git diff, then commit")}
  else note(r.error||"save failed",true);
};
$("reset").onclick=()=>{P=clone(ORIG);boot2()};
async function boot2(){const r=await fetch("/api/preview",{method:"POST",
  headers:{"Content-Type":"application/json"},body:JSON.stringify({profile:P})}).then(r=>r.json());
  buildWeights(Object.keys(P.weights||{}));render(r);paintKeywords(r);note("reverted to last saved")}
boot();
</script></body></html>
"""


def main():
    global _SNAPSHOT, _NORM
    _SNAPSHOT = load_snapshot_postings()
    if not _SNAPSHOT:
        print("No snapshot found at", state.SNAPSHOT_FILE)
        print("Populate it first (about 4 minutes, sends nothing, writes no state):")
        print("    python main.py --dry-run --only-boards --snapshot")
        return 1

    print(f"Loaded {len(_SNAPSHOT)} postings from {state.SNAPSHOT_FILE}")
    print("Indexing (one-time, ~30s) so the browser never waits...", flush=True)
    t0 = time.time()
    _NORM = precompute_norms(_SNAPSHOT)
    warm = evaluate(read_profile())
    print(f"  ready in {time.time() - t0:.0f}s — {warm['matches']} matches at "
          f"threshold {warm['threshold']}, {warm['excluded']} excluded, "
          f"{warm['gated']} gated")
    url = f"http://127.0.0.1:{PORT}"
    print(f"Tuning dashboard: {url}   (ctrl-c to stop)")

    socketserver.TCPServer.allow_reuse_address = True
    # Bind loopback only: this serves an unauthenticated write endpoint.
    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
        try:
            webbrowser.open(url)
        except Exception:
            pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
