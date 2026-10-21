#!/usr/bin/env python3
"""Docs Issue Finder: find open-source documentation issues that welcome new contributors.

Pipeline:
  1. Search GitHub for open, unassigned "good first issue" + documentation issues.
  2. Filter out stale / claimed issues and archived repos.
  3. For each repo, fetch the README and the contributing guide (cached).
  4. Summarize the project and the contribution steps (heuristic, or LLM with --llm).
  5. Write a self-contained dashboard.html.

Usage:
  export GITHUB_TOKEN=ghp_...        # recommended (read-only public access is enough)
  python docs_issue_finder.py --max-issues 30 --days 90
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

API = "https://api.github.com"
HERE = Path(__file__).resolve().parent
CACHE_FILE = HERE / ".cache" / "repos.json"
CACHE_TTL_DAYS = 7
# Bump this when the cached fields or summary logic change; older entries are then refetched.
CACHE_VERSION = 2

# Label sets. GitHub treats comma-separated values inside one label: qualifier as OR,
# and separate label: qualifiers as AND. So: (any first-issue label) AND (any docs label).
FIRST_ISSUE_LABELS = ["good first issue", "good-first-issue", "first-timers-only"]
DOC_LABELS = ["documentation", "docs"]

CONTRIBUTING_PATHS = [
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    "CONTRIBUTING",
    "contributing.md",
]

# Heuristic flags detected in the contributing guide.
FLAG_PATTERNS = {
    "CLA required": r"\bCLA\b|contributor license agreement",
    "DCO sign-off": r"signed-off-by|\bDCO\b|developer certificate of origin",
    "Claim the issue first": r"(comment|let us know|ask|tell us).{0,60}(assign|claim|work on|pick up)|assign(ed)? (it )?(to )?(you|me)",
    "Fork and PR workflow": r"\bfork\b",
    "Tests or lint expected": r"\b(run|add|write)\b.{0,30}\btests?\b|\blint(er|ing)?\b",
    "Commit message convention": r"conventional commits|commit message",
}


# ----------------------------------------------------------------------------
# GitHub client
# ----------------------------------------------------------------------------
class GitHub:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "docs-issue-finder",
            }
        )
        if token:
            self.s.headers["Authorization"] = f"Bearer {token}"
        self.authed = bool(token)

    def get(self, path, params=None, raw=False, ok404=False):
        url = path if path.startswith("http") else API + path
        headers = {"Accept": "application/vnd.github.raw+json"} if raw else {}
        for _ in range(4):
            r = self.s.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 404 and ok404:
                return None
            limited = r.status_code == 429 or (
                r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0"
            )
            secondary = r.status_code == 403 and "retry-after" in r.headers
            if limited or secondary:
                if "retry-after" in r.headers:
                    wait = int(r.headers["retry-after"]) + 1
                else:
                    reset = int(r.headers.get("x-ratelimit-reset", time.time() + 60))
                    wait = max(reset - time.time(), 1) + 1
                if wait > 120:
                    sys.exit(
                        f"Rate limited for {int(wait)}s. Set GITHUB_TOKEN or try again later."
                    )
                print(f"  rate limited, waiting {int(wait)}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            if r.status_code == 403 and ok404:
                return None  # e.g. blocked/DMCA repo content
            r.raise_for_status()
            return r.text if raw else r.json()
        raise RuntimeError(f"Gave up on {url}")


# ----------------------------------------------------------------------------
# Step 1-2: discover and filter issues
# ----------------------------------------------------------------------------
def build_query(days, extra):
    since = (date.today() - timedelta(days=days)).isoformat()
    first = ",".join(f'"{l}"' for l in FIRST_ISSUE_LABELS)
    docs = ",".join(f'"{l}"' for l in DOC_LABELS)
    q = (
        f"is:issue is:open no:assignee -linked:pr archived:false "
        f"updated:>={since} label:{first} label:{docs}"
    )
    if extra:
        q += " " + extra
    return q


def search_issues(gh, query, max_issues):
    items, page = [], 1
    while len(items) < max_issues:
        data = gh.get(
            "/search/issues",
            params={
                "q": query,
                "sort": "updated",
                "order": "desc",
                "per_page": min(100, max_issues),
                "page": page,
            },
        )
        batch = data.get("items", [])
        items.extend(batch)
        if len(batch) < 100 or len(items) >= data.get("total_count", 0):
            break
        page += 1
    return items[:max_issues]


# ----------------------------------------------------------------------------
# Step 3-4: repo context (README, contributing guide, summaries) with cache
# ----------------------------------------------------------------------------
def load_cache():
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


def save_cache(cache):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def cache_fresh(entry):
    if entry.get("v") != CACHE_VERSION:
        return False
    try:
        fetched = datetime.fromisoformat(entry["fetched_at"])
    except Exception:
        return False
    return datetime.now(timezone.utc) - fetched < timedelta(days=CACHE_TTL_DAYS)


def clean_readme_paragraph(readme, limit=320):
    text = re.sub(r"<!--.*?-->", "", readme, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)  # html tags
    # Linked badges first ([![alt](img)](url)), then bare images, then empty-link leftovers.
    text = re.sub(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)", "", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[\s*\]\([^)]*\)", "", text)
    for para in re.split(r"\n\s*\n", text):
        p = " ".join(para.strip().split())
        if len(p) < 60 or p.startswith(("#", "|", "```", "---", "===", "* ", "- ")):
            continue
        p = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", p)  # links -> text
        p = p.replace("**", "")
        return p if len(p) <= limit else p[: limit - 1].rstrip() + "…"
    return ""


def analyze_contributing(text):
    if not text:
        return {"flags": [], "headings": []}
    flags = [
        name
        for name, pat in FLAG_PATTERNS.items()
        if re.search(pat, text, flags=re.I | re.S)
    ]
    headings = [
        m.group(1).strip("# ").strip()
        for m in re.finditer(r"^#{1,3}\s+(.+)$", text, flags=re.M)
    ][:8]
    return {"flags": flags, "headings": headings}


def llm_summarize(full_name, description, readme, contributing):
    """Optional: ask Claude for a project summary and contribution steps."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")
    prompt = (
        "You help a technical writer find open-source projects to contribute docs to.\n"
        f"Repository: {full_name}\nDescription: {description}\n\n"
        f"README (truncated):\n{readme[:6000]}\n\n"
        f"CONTRIBUTING guide (truncated):\n{contributing[:6000] or '(none found)'}\n\n"
        "Reply with JSON only, with keys:\n"
        '  "project_summary": 2-3 plain sentences on what the project is and who uses it,\n'
        '  "contribution_steps": list of at most 6 short steps a first-time contributor follows '
        "(empty list if no guide).\n"
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 600,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"]
        text = text[text.index("{") : text.rindex("}") + 1]
        data = json.loads(text)
        return {
            "project_summary": str(data.get("project_summary", "")).strip(),
            "contribution_steps": [str(s) for s in data.get("contribution_steps", [])][:6],
        }
    except Exception as e:
        print(f"  LLM summary failed ({e}); using heuristic summary.", file=sys.stderr)
        return None


def fetch_repo_context(gh, full_name, use_llm):
    meta = gh.get(f"/repos/{full_name}")
    readme = gh.get(f"/repos/{full_name}/readme", raw=True, ok404=True) or ""

    contributing, contributing_url = "", None
    for path in CONTRIBUTING_PATHS:
        text = gh.get(f"/repos/{full_name}/contents/{path}", raw=True, ok404=True)
        if text:
            contributing = text
            contributing_url = f"{meta['html_url']}/blob/{meta['default_branch']}/{path}"
            break

    analysis = analyze_contributing(contributing)
    llm = llm_summarize(full_name, meta.get("description") or "", readme, contributing) if use_llm else None

    description = meta.get("description") or ""
    para = clean_readme_paragraph(readme)
    if description and para and description.lower().rstrip(".") in para.lower():
        description = ""  # the README paragraph already repeats the repo description
    summary = (llm or {}).get("project_summary") or " ".join(s for s in [description, para] if s)

    return {
        "v": CACHE_VERSION,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "full_name": full_name,
        "url": meta["html_url"],
        "description": meta.get("description") or "",
        "language": meta.get("language") or "",
        "stars": meta.get("stargazers_count", 0),
        "topics": meta.get("topics", [])[:6],
        "license": (meta.get("license") or {}).get("spdx_id") or "",
        "summary": summary,
        "summary_source": "llm" if llm else "heuristic",
        "contributing_url": contributing_url,
        "contributing_flags": analysis["flags"],
        "contributing_headings": analysis["headings"],
        "contribution_steps": (llm or {}).get("contribution_steps", []),
    }


# ----------------------------------------------------------------------------
# Step 5: dashboard
# ----------------------------------------------------------------------------
def days_since(iso):
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return max((datetime.now(timezone.utc) - dt).days, 0)


def build_records(issues, repos):
    records = []
    for it in issues:
        full = it["repository_url"].split("/repos/")[1]
        repo = repos.get(full)
        if not repo:
            continue
        records.append(
            {
                "title": it["title"],
                "url": it["html_url"],
                "number": it["number"],
                "labels": [l["name"] for l in it.get("labels", [])],
                "comments": it.get("comments", 0),
                "updated_days": days_since(it["updated_at"]),
                "created_days": days_since(it["created_at"]),
                "repo": repo,
            }
        )
    return records


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Docs Issue Finder</title>
<style>
  :root { --bg:#fafaf9; --card:#fff; --text:#1c1917; --muted:#6b6560; --line:#e7e5e4; --accent:#0f766e; --chip:#f0efed; --warn:#b45309; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#131211; --card:#1c1a19; --text:#f1efed; --muted:#a39d97; --line:#2e2b29; --accent:#5eead4; --chip:#2a2725; --warn:#fbbf24; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { max-width: 920px; margin: 0 auto; padding: 24px 16px 64px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .sub { color: var(--muted); margin: 0 0 20px; font-size: 13px; }
  .controls { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:16px; }
  input, select { background:var(--card); color:var(--text); border:1px solid var(--line); border-radius:8px; padding:8px 10px; font:inherit; }
  input { flex:1 1 240px; }
  .count { color:var(--muted); font-size:13px; margin-bottom:10px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; margin-bottom:12px; }
  .card h2 { font-size:16px; margin:0 0 4px; }
  .card h2 a { color:var(--text); text-decoration:none; }
  .card h2 a:hover { color:var(--accent); text-decoration:underline; }
  .repo { font-size:13px; color:var(--muted); margin-bottom:8px; }
  .repo a { color:var(--accent); text-decoration:none; }
  .summary { margin:8px 0; }
  .chips { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0; }
  .chip { background:var(--chip); border-radius:999px; padding:2px 10px; font-size:12px; color:var(--muted); }
  .chip.flag { color:var(--warn); }
  details { margin-top:8px; font-size:14px; }
  summary { cursor:pointer; color:var(--accent); }
  ol, ul { margin:6px 0 0 18px; padding:0; }
  .meta { font-size:12px; color:var(--muted); margin-top:8px; }
  .empty { color:var(--muted); padding:40px 0; text-align:center; }
  .filters { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 14px; margin-bottom:16px; }
  .filters .row { display:flex; flex-wrap:wrap; align-items:center; gap:8px 16px; }
  .filters details { margin-top:10px; }
  .labelgrid { display:flex; flex-wrap:wrap; gap:6px 16px; margin-top:8px; }
  label.opt { display:inline-flex; align-items:center; gap:6px; cursor:pointer; font-size:13px; }
  label.opt.pin { font-size:14px; font-weight:600; }
  input[type=checkbox] { flex:none; width:auto; padding:0; margin:0; accent-color:var(--accent); }
  .n { color:var(--muted); font-weight:400; }
  button.link { background:none; border:none; color:var(--accent); cursor:pointer; font:inherit; font-size:13px; padding:0; }
  .chip.hl { background:var(--accent); color:var(--bg); }
</style>
</head>
<body>
<main>
  <h1>Docs Issue Finder</h1>
  <p class="sub" id="sub"></p>
  <div class="controls">
    <input id="q" type="search" placeholder="Filter by keyword (project, issue, topic)...">
    <select id="lang"><option value="">All languages</option></select>
    <select id="sort">
      <option value="updated">Recently updated</option>
      <option value="stars">Most stars</option>
      <option value="comments">Fewest comments</option>
    </select>
  </div>
  <div class="filters">
    <div class="row">
      <span id="pinned" class="row"></span>
      <select id="mode">
        <option value="any">Match any selected label</option>
        <option value="all">Match all selected labels</option>
      </select>
      <button class="link" id="clear" type="button">Clear labels</button>
    </div>
    <details id="more">
      <summary>More labels (<span id="morecount"></span>)</summary>
      <div class="labelgrid" id="labelgrid"></div>
    </details>
  </div>
  <div class="count" id="count"></div>
  <div id="list"></div>
</main>
<script>
const DATA = __DATA__;
const META = __META__;
const $ = id => document.getElementById(id);
$("sub").textContent = "Generated " + META.generated + " · query: " + META.query;

const langs = [...new Set(DATA.map(d => d.repo.language).filter(Boolean))].sort();
langs.forEach(l => { const o = document.createElement("option"); o.value = l; o.textContent = l; $("lang").appendChild(o); });

// ---- label filters ----
// Labels are normalised so "help-wanted", "Help Wanted" and "help wanted" count as one.
const ALIASES = { "docs": "documentation" };
const PINNED = ["help wanted"];
function norm(l) {
  const k = l.toLowerCase().replace(/[-_]+/g, " ").replace(/\s+/g, " ").trim();
  return ALIASES[k] || k;
}
DATA.forEach(d => { d.keys = [...new Set(d.labels.map(norm))]; });
const counts = {};
DATA.forEach(d => d.keys.forEach(k => { counts[k] = (counts[k] || 0) + 1; }));
const selected = new Set();

function labelOption(key, pinned) {
  const lab = document.createElement("label");
  lab.className = "opt" + (pinned ? " pin" : "");
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.value = key;
  cb.addEventListener("change", () => { if (cb.checked) selected.add(key); else selected.delete(key); render(); });
  lab.appendChild(cb);
  lab.appendChild(document.createTextNode(key + " "));
  const n = document.createElement("span");
  n.className = "n";
  n.textContent = "(" + (counts[key] || 0) + ")";
  lab.appendChild(n);
  return lab;
}
PINNED.forEach(k => $("pinned").appendChild(labelOption(k, true)));
// Labels that every issue carries (the search already requires them) can't filter anything, so hide them.
const others = Object.keys(counts)
  .filter(k => !PINNED.includes(k) && counts[k] < DATA.length)
  .sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
others.forEach(k => $("labelgrid").appendChild(labelOption(k, false)));
$("morecount").textContent = others.length;
if (!others.length) $("more").style.display = "none";
$("clear").addEventListener("click", () => {
  document.querySelectorAll(".filters input[type=checkbox]").forEach(c => { c.checked = false; });
  selected.clear();
  render();
});

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function link(href, text) { const a = el("a", "", text); a.href = href; a.target = "_blank"; a.rel = "noopener"; return a; }

function card(d) {
  const c = el("div", "card");
  const h = el("h2"); h.appendChild(link(d.url, d.title)); c.appendChild(h);
  const r = el("div", "repo");
  r.appendChild(link(d.repo.url, d.repo.full_name));
  r.appendChild(document.createTextNode(" · " + (d.repo.language || "n/a") + " · ★ " + d.repo.stars.toLocaleString() + (d.repo.license ? " · " + d.repo.license : "")));
  c.appendChild(r);
  c.appendChild(el("p", "summary", d.repo.summary || "No summary available."));

  const chips = el("div", "chips");
  d.labels.forEach(l => {
    const k = norm(l);
    chips.appendChild(el("span", "chip" + (PINNED.includes(k) || selected.has(k) ? " hl" : ""), l));
  });
  d.repo.contributing_flags.forEach(f => chips.appendChild(el("span", "chip flag", f)));
  c.appendChild(chips);

  const det = el("details");
  det.appendChild(el("summary", "", "How to contribute"));
  if (d.repo.contribution_steps.length) {
    const ol = el("ol"); d.repo.contribution_steps.forEach(s => ol.appendChild(el("li", "", s))); det.appendChild(ol);
  } else if (d.repo.contributing_headings.length) {
    det.appendChild(el("div", "", "Guide sections:"));
    const ul = el("ul"); d.repo.contributing_headings.forEach(s => ul.appendChild(el("li", "", s))); det.appendChild(ul);
  }
  const p = el("div", "meta");
  if (d.repo.contributing_url) p.appendChild(link(d.repo.contributing_url, "Read the contributing guide"));
  else p.textContent = "No contributing guide found. Check the README or ask in the issue.";
  det.appendChild(p);
  c.appendChild(det);

  c.appendChild(el("div", "meta", "#" + d.number + " · updated " + d.updated_days + "d ago · opened " + d.created_days + "d ago · " + d.comments + " comments · summary: " + d.repo.summary_source));
  return c;
}

function render() {
  const q = $("q").value.toLowerCase(), lang = $("lang").value, sort = $("sort").value;
  let rows = DATA.filter(d => {
    if (lang && d.repo.language !== lang) return false;
    if (selected.size) {
      const want = [...selected];
      const ok = $("mode").value === "all" ? want.every(k => d.keys.includes(k)) : want.some(k => d.keys.includes(k));
      if (!ok) return false;
    }
    if (!q) return true;
    return (d.title + " " + d.repo.full_name + " " + d.repo.summary + " " + d.repo.topics.join(" ")).toLowerCase().includes(q);
  });
  rows.sort((a, b) => sort === "stars" ? b.repo.stars - a.repo.stars : sort === "comments" ? a.comments - b.comments : a.updated_days - b.updated_days);
  $("count").textContent = rows.length + " of " + DATA.length + " issues";
  const list = $("list"); list.replaceChildren();
  if (!rows.length) list.appendChild(el("div", "empty", "No issues match."));
  rows.forEach(d => list.appendChild(card(d)));
}
["q", "lang", "sort", "mode"].forEach(id => $(id).addEventListener("input", render));
render();
</script>
</body>
</html>
"""


def write_dashboard(records, query, out_path):
    meta = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "query": query}
    safe = lambda obj: json.dumps(obj).replace("</", "<\\/")
    html_text = HTML_TEMPLATE.replace("__DATA__", safe(records)).replace("__META__", safe(meta))
    Path(out_path).write_text(html_text, encoding="utf-8")


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-issues", type=int, default=30, help="max issues to collect (default 30)")
    ap.add_argument("--days", type=int, default=90, help="only issues updated within N days (default 90)")
    ap.add_argument("--query", default="", help='extra GitHub search qualifiers, e.g. "language:python"')
    ap.add_argument("--llm", action="store_true", help="use Claude for summaries (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--no-cache", action="store_true", help="ignore the repo cache")
    ap.add_argument("--out", default=str(HERE / "dashboard.html"), help="output HTML path")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("Note: no GITHUB_TOKEN set. Unauthenticated limits are very low.", file=sys.stderr)
    if args.llm and not os.environ.get("ANTHROPIC_API_KEY"):
        print("Note: --llm needs ANTHROPIC_API_KEY; falling back to heuristic summaries.", file=sys.stderr)

    gh = GitHub(token)
    query = build_query(args.days, args.query)
    print(f"Searching: {query}")
    issues = search_issues(gh, query, args.max_issues)
    print(f"Found {len(issues)} issues.")

    cache = {} if args.no_cache else load_cache()
    repos = {}
    names = []
    for it in issues:
        n = it["repository_url"].split("/repos/")[1]
        if n not in names:
            names.append(n)

    for i, name in enumerate(names, 1):
        entry = cache.get(name)
        if entry and cache_fresh(entry):
            repos[name] = entry
            print(f"[{i}/{len(names)}] {name} (cached)")
            continue
        print(f"[{i}/{len(names)}] {name}")
        try:
            repos[name] = cache[name] = fetch_repo_context(gh, name, args.llm)
        except Exception as e:
            print(f"  skipped: {e}", file=sys.stderr)
        save_cache(cache)

    records = build_records(issues, repos)
    write_dashboard(records, query, args.out)
    print(f"Wrote {args.out} with {len(records)} issues from {len(repos)} repos.")


if __name__ == "__main__":
    main()