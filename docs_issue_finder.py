#!/usr/bin/env python3
"""GitHub Docs Issue Finder: find open-source documentation issues that welcome new contributors.

Pipeline:
  1. Search GitHub for open, unassigned documentation issues labeled "help wanted" (required
     by default; see --no-help-wanted). Beginner labels such as "good first issue" are not
     required; the dashboard has a checkbox for them.
  2. Filter out stale / claimed issues and archived repos, and drop issues that an open or
     merged pull request references.
  3. For each repo, fetch the README and the contributing guide (cached).
  4. Summarize the project and the contribution steps (heuristic, or LLM with --llm).
  5. Write a self-contained dashboard.html.

Usage:
  export GITHUB_TOKEN=ghp_...        # recommended (read-only public access is enough)
  python docs_issue_finder.py --max-issues 150 --days 90
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
CACHE_VERSION = 3  # bump when the cached repo fields or summary rules change

# Label sets. GitHub treats comma-separated values inside one label: qualifier as OR,
# and separate label: qualifiers as AND. So: (any first-issue label) AND (any docs label).
FIRST_ISSUE_LABELS = ["good first issue", "good-first-issue", "first-timers-only"]
DOC_LABELS = ["documentation", "docs"]
# Required by default: projects that tag an issue "help wanted" are explicitly asking for contributors.
# Add spellings here (e.g. "help-wanted") if you find the search is missing them. Each extra value is
# another OR, and GitHub caps how many AND/OR/NOT operators one search may use.
HELP_WANTED_LABELS = ["help wanted"]
# Issues with any of these labels are skipped: not accepted, not ready, already taken, or about to close.
# Matching ignores case, treats "-", "_" and spaces alike, and ignores a "status:" or "triage/" style
# prefix, so "Needs-Approval" and "status: blocked" match too. Whole labels only, never words inside one.
EXCLUDE_LABELS = [
    "invalid", "wontfix", "won't fix", "duplicate", "not planned", "spam",
    "needs approval", "blocked", "on hold", "needs design", "needs info", "needs more info",
    "waiting for author", "awaiting response",
    "in progress", "claimed", "assigned",
    "stale",
    # bounty programs and bot-generated issues
    "stellar wave", "drips wave", "150 points", "auto dispatch", "origin:worker",
]

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
def build_query(days, extra, require_help_wanted=True):
    # Beginner labels aren't part of the search; the dashboard filters on them instead.
    since = (date.today() - timedelta(days=days)).isoformat()
    docs = ",".join(f'"{l}"' for l in DOC_LABELS)
    q = (
        f"is:issue is:open no:assignee -linked:pr archived:false "
        f"created:>={since} label:{docs}"
    )
    if require_help_wanted:
        q += " label:" + ",".join(f'"{l}"' for l in HELP_WANTED_LABELS)
    if extra:
        q += " " + extra
    return q


def search_issues(gh, query, per_page=100):
    """Yield search results one at a time, fetching the next page only when it's needed.

    The Search API returns at most 1,000 results for a query.
    """
    page, seen = 1, 0
    while seen < 1000:
        data = gh.get(
            "/search/issues",
            params={"q": query, "sort": "updated", "order": "desc", "per_page": per_page, "page": page},
        )
        batch = data.get("items", [])
        yield from batch
        seen += len(batch)
        if len(batch) < per_page or seen >= data.get("total_count", 0):
            return
        page += 1


def referencing_pr(gh, issue):
    """Return the URL of an open or merged PR that references the issue, else None.

    The search's -linked:pr only catches formally linked PRs (closing keywords or
    the Development sidebar). The timeline also shows PRs that just mention the
    issue. A PR closed without merging is treated as abandoned and doesn't count.
    """
    url = issue["url"] + "/timeline"
    page = 1
    while True:
        events = gh.get(url, params={"per_page": 100, "page": page}, ok404=True) or []
        for ev in events:
            if ev.get("event") != "cross-referenced":
                continue
            src = (ev.get("source") or {}).get("issue") or {}
            pr = src.get("pull_request")
            if pr and (src.get("state") == "open" or pr.get("merged_at")):
                return src.get("html_url")
        if len(events) < 100:
            return None
        page += 1


def _label_key(label):
    return re.sub(r"[-_\s]+", " ", label.lower()).strip()


EXCLUDE_KEYS = {_label_key(l) for l in EXCLUDE_LABELS}


def excluded_label(issue):
    """Return the first label on the issue that's in EXCLUDE_LABELS, else None."""
    for lab in issue.get("labels", []):
        name = lab["name"] if isinstance(lab, dict) else lab
        key = _label_key(name)
        if key in EXCLUDE_KEYS or _label_key(re.split(r"[:/]", key)[-1]) in EXCLUDE_KEYS:
            return name
    return None


def collect_issues(gh, query, max_issues):
    """Go through the search results until max_issues issues pass the filters.

    An issue is skipped if it has an EXCLUDE_LABELS label (checked first, since
    it costs no API call) or an open or merged PR references it.
    Returns (issues, skipped), where skipped counts both kinds.
    """
    issues, skipped = [], 0
    for it in search_issues(gh, query, per_page=min(100, max_issues * 2)):
        label = excluded_label(it)
        if label:
            skipped += 1
            print(f"  skipping {it['html_url']}: labeled {label}")
            continue
        pr = referencing_pr(gh, it)
        if pr:
            skipped += 1
            print(f"  skipping {it['html_url']}: referenced by {pr}")
            continue
        issues.append(it)
        if len(issues) >= max_issues:
            break
    return issues, skipped


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


NAV_SEPARATORS = re.compile(r"\s+[|·•]\s+")
FLAG_EMOJI = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")
# Captions and calls to action rather than a description of the project.
NOT_A_SUMMARY = re.compile(
    r"\b(star (this|the|us)|give (us|it) a star|watch the|screenshots?|gif|video|mp4|click|logo)\b", re.I
)


def clean_paragraph(para):
    """Turn one README paragraph into plain text, or "" if it's navigation rather than prose."""
    lines = [re.sub(r"^\s*>\s?", "", line) for line in para.strip().splitlines()]  # blockquote markers
    p = " ".join(" ".join(lines).split())
    if not p or p.startswith(("#", "|", "```", "---", "===", "* ", "- ", "+ ")) or re.match(r"\d+\. ", p):
        return ""
    if len(FLAG_EMOJI.findall(p)) >= 2:
        return ""  # a language switcher made of flag emojis
    p = re.sub(r"\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)", r"\1", p)  # [text](url) -> text
    p = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", p)  # [text][ref] -> text
    p = re.sub(r"`([^`]*)`", r"\1", p)  # inline code
    p = re.sub(r"(\*\*|__)(.+?)\1", r"\2", p)  # bold
    p = re.sub(r"(?<![\w*])[*_](\S(?:.*?\S)?)[*_](?![\w*])", r"\1", p)  # italics
    # Rows like "English | Español | Deutsch" or "Website · Docs · Contribute" are navigation.
    # Keep only the parts that read like sentences.
    parts = NAV_SEPARATORS.split(p)
    if len(parts) > 1:
        p = " ".join(part for part in parts if len(part.split()) >= 5)
    p = " ".join(p.split())
    if len(p) < 40 or p.endswith(":") or NOT_A_SUMMARY.search(p):
        return ""  # too short, a lead-in to a code block or list, or a caption / call to action
    return p


def readme_paragraphs(readme, limit=6):
    """The README's first few paragraphs that read like prose, with markdown and badges stripped."""
    text = re.sub(r"<!--.*?-->", "", readme, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)  # html tags
    # Badges and images: linked ([![alt](img)](url)), reference-style ([![alt][img]][url]), then bare ones.
    text = re.sub(r"\[!\[[^\]]*\](?:\([^)]*\)|\[[^\]]*\])\](?:\([^)]*\)|\[[^\]]*\])", "", text)
    text = re.sub(r"!\[[^\]]*\](?:\([^)]*\)|\[[^\]]*\])", "", text)
    text = re.sub(r"^\s*\[[^\]]+\]:\s*\S+.*$", "", text, flags=re.M)  # reference link definitions
    text = re.sub(r"\[\s*\]\([^)]*\)", "", text)
    found = []
    for para in re.split(r"\n\s*\n", text):
        p = clean_paragraph(para)
        if p:
            found.append(p)
            if len(found) >= limit:
                break
    return found


def _words(text):
    return {w.rstrip("s") for w in re.findall(r"[a-z0-9][a-z0-9-]+", text.lower()) if len(w) > 2}


def build_summary(description, readme, limit=320):
    """The repo description plus the README's first prose paragraph, without repeating itself."""
    description = " ".join(description.split())
    desc_words = _words(description)
    # Use the first README paragraph that adds something: drop sentences that mostly restate
    # the description, and move on to the next paragraph if nothing is left.
    para = ""
    for candidate in readme_paragraphs(readme):
        sentences = re.split(r"(?<=[.!?])\s+", candidate)
        para = " ".join(
            s for s in sentences if not desc_words or not (w := _words(s)) or len(w & desc_words) / len(w) < 0.6
        )
        if para:
            break
    # Drop the description when the README already covers it (e.g. it's just the project name).
    if desc_words and para and len(desc_words & _words(para)) / len(desc_words) >= 0.8:
        description = ""
    if description and para and description[-1] not in ".!?…":
        description += "."
    summary = " ".join(s for s in [description, para] if s)
    if len(summary) <= limit:
        return summary
    # Too long: end at the last full sentence that fits, or failing that at a word boundary.
    cut = summary[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= limit // 2:
        return cut[: end + 1]
    return cut[: cut.rfind(" ")].rstrip(" ,;:—-") + "…"


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

    summary = (llm or {}).get("project_summary") or build_summary(meta.get("description") or "", readme)

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
                "beginner": any(
                    l["name"].lower() in {f.lower() for f in FIRST_ISSUE_LABELS} for l in it.get("labels", [])
                ),
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
<title>GitHub Docs Issue Finder</title>
<style>
  :root { --bg:#fafaf9; --card:#fff; --text:#1c1917; --muted:#6b6560; --line:#e7e5e4; --accent:#0f766e; --chip:#f0efed; --warn:#b45309; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#131211; --card:#1c1a19; --text:#f1efed; --muted:#a39d97; --line:#2e2b29; --accent:#5eead4; --chip:#2a2725; --warn:#fbbf24; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { max-width: 920px; margin: 0 auto; padding: 24px 16px 64px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .sub { color: var(--muted); margin: 0 0 4px; font-size: 13px; }
  .sub.desc { color: var(--text); font-size: 14px; }
  details.query { margin: 0 0 20px; font-size: 12px; }
  details.query summary { color: var(--muted); }
  details.query code { display:block; margin-top:6px; padding:8px 10px; background:var(--chip); border-radius:8px; color:var(--muted); overflow-wrap:anywhere; }
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
  .issues { list-style:none; margin:12px 0 0; padding:0; border-top:1px solid var(--line); }
  .issue { padding:10px 0; border-bottom:1px solid var(--line); }
  .issue:last-child { border-bottom:none; padding-bottom:0; }
  .issue h3 { font-size:14px; font-weight:600; margin:0; overflow-wrap:anywhere; }
  .issue h3 a { color:var(--text); text-decoration:none; }
  .issue h3 a:hover { color:var(--accent); text-decoration:underline; }
  .issue .num { color:var(--muted); font-weight:400; }
  .issue .chips { margin:4px 0 0; align-items:center; }
  .issue .meta { margin-top:0; }
  .head { display:flex; align-items:baseline; justify-content:space-between; gap:12px; }
  .head > :first-child { min-width:0; }
  button.link.hide { color:var(--muted); font-size:12px; white-space:nowrap; }
  button.link.hide:hover { color:var(--accent); text-decoration:underline; }
</style>
</head>
<body>
<main>
  <h1>GitHub Docs Issue Finder</h1>
  <p class="sub" id="sub"></p>
  <p class="sub desc" id="desc"></p>
  <details class="query"><summary>Show the exact GitHub search query</summary><code id="query"></code></details>
  <div class="controls">
    <input id="q" type="search" placeholder="Filter by keyword (project, issue, topic)...">
    <select id="lang"><option value="">All languages</option></select>
    <select id="sort">
      <option value="updated">Recently updated</option>
      <option value="stars">Most stars</option>
      <option value="comments">Fewest comments</option>
      <option value="newest">Newest issues</option>
      <option value="oldest">Oldest issues</option>
    </select>
  </div>
  <div class="filters">
    <div class="row">
      <label class="opt pin" id="beginnerwrap"><input type="checkbox" id="beginner" checked> good first issue <span class="n" id="beginnercount"></span></label>
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
// Header: when the data was gathered (in the viewer's time zone), what was searched for, and the raw query.
const gen = new Date(META.generated);
const genText = isNaN(gen) ? META.generated : gen.toLocaleString(undefined, {
  year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short",
});
$("sub").textContent = "Data gathered " + genText + " · " + DATA.length + " issues" +
  (META.skipped ? " · " + META.skipped + " skipped" : "");
$("desc").textContent = META.description || "";
if (!META.description) $("desc").style.display = "none";
$("query").textContent = META.query;

function ago(days) { return days === 0 ? "today" : days === 1 ? "yesterday" : days + " days ago"; }

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
// Beginner labels ("good first issue" and its spellings) are handled by the top checkbox, not "More labels".
const BEGINNER = new Set((META.beginner_labels || []).map(norm));
const counts = {};
DATA.forEach(d => d.keys.forEach(k => { counts[k] = (counts[k] || 0) + 1; }));
const selected = new Set();
// Total issues per repo in the current "good first issue" view, so a card can say "2 of 4 issues"
// when the other filters hide some. Recomputed in render().
let repoTotals = {};
// Issues and repos hidden with the "Hide" buttons. Not saved: reloading the page brings them back.
const hiddenIssues = new Set(), hiddenRepos = new Set();
function hideButton(text, onHide) {
  const b = el("button", "link hide", text);
  b.type = "button";
  b.addEventListener("click", () => { onHide(); render(); });
  return b;
}

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
PINNED.forEach(k => {
  // If every issue already has this label (the search requires help wanted by default), it can't filter anything.
  if (DATA.length && counts[k] === DATA.length) return;
  $("pinned").appendChild(labelOption(k, true));
});
// Labels that every issue carries (the search already requires them) can't filter anything, so hide them.
const others = Object.keys(counts)
  .filter(k => !PINNED.includes(k) && !BEGINNER.has(k) && counts[k] < DATA.length)
  .sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
others.forEach(k => $("labelgrid").appendChild(labelOption(k, false)));
$("morecount").textContent = others.length;
if (!others.length) $("more").style.display = "none";
// The "good first issue" checkbox: ticked shows only issues with a beginner label, unticked shows
// every issue. It's hidden when it couldn't change anything (all or none have the label).
const nBeginner = DATA.filter(d => d.beginner).length;
const splitBeginner = nBeginner > 0 && nBeginner < DATA.length;
const alsoLabels = (META.beginner_labels || []).filter(l => l.toLowerCase() !== "good first issue");
$("beginnercount").textContent = "(" + nBeginner + ")" + (alsoLabels.length ? " · includes " + alsoLabels.join(", ") : "");
if (!splitBeginner) $("beginnerwrap").style.display = "none";
$("beginner").addEventListener("change", render);
$("clear").addEventListener("click", () => {
  document.querySelectorAll("#pinned input, #labelgrid input").forEach(c => { c.checked = false; });
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

function issueItem(d) {
  const li = el("li", "issue");
  const head = el("div", "head");
  const h = el("h3"); h.appendChild(link(d.url, d.title)); h.appendChild(el("span", "num", " #" + d.number)); head.appendChild(h);
  head.appendChild(hideButton("Hide", () => hiddenIssues.add(d.url)));
  li.appendChild(head);
  const chips = el("div", "chips");
  // Highlight only the labels the current filters ask for: ticked label boxes, and the
  // beginner labels while the "good first issue" box is ticked.
  const beginnerOn = splitBeginner && $("beginner").checked;
  d.labels.forEach(l => {
    const k = norm(l);
    chips.appendChild(el("span", "chip" + (selected.has(k) || (beginnerOn && BEGINNER.has(k)) ? " hl" : ""), l));
  });
  chips.appendChild(el("span", "meta", "updated " + ago(d.updated_days) + " · opened " + ago(d.created_days) + " · " + d.comments + (d.comments === 1 ? " comment" : " comments")));
  li.appendChild(chips);
  return li;
}

function repoCard(g) {
  const repo = g.repo, total = repoTotals[repo.full_name], n = g.issues.length;
  const c = el("div", "card");
  const head = el("div", "head");
  const h = el("h2"); h.appendChild(link(repo.url, repo.full_name)); head.appendChild(h);
  head.appendChild(hideButton("Hide repo", () => hiddenRepos.add(repo.full_name)));
  c.appendChild(head);
  const issueCount = (n < total ? n + " of " + total : n) + (total === 1 ? " issue" : " issues");
  c.appendChild(el("div", "repo", (repo.language || "n/a") + " · ★ " + repo.stars.toLocaleString() + (repo.license ? " · " + repo.license : "") + " · " + issueCount + " · summary: " + repo.summary_source));
  c.appendChild(el("p", "summary", repo.summary || "No summary available."));

  if (repo.contributing_flags.length) {
    const flags = el("div", "chips");
    repo.contributing_flags.forEach(f => flags.appendChild(el("span", "chip flag", f)));
    c.appendChild(flags);
  }

  const det = el("details");
  det.appendChild(el("summary", "", "How to contribute"));
  if (repo.contribution_steps.length) {
    const ol = el("ol"); repo.contribution_steps.forEach(s => ol.appendChild(el("li", "", s))); det.appendChild(ol);
  } else if (repo.contributing_headings.length) {
    det.appendChild(el("div", "", "Guide sections:"));
    const ul = el("ul"); repo.contributing_headings.forEach(s => ul.appendChild(el("li", "", s))); det.appendChild(ul);
  }
  const p = el("div", "meta");
  if (repo.contributing_url) p.appendChild(link(repo.contributing_url, "Read the contributing guide"));
  else p.textContent = "No contributing guide found. Check the README or ask in the issue.";
  det.appendChild(p);
  c.appendChild(det);

  const ul = el("ul", "issues");
  g.issues.forEach(d => ul.appendChild(issueItem(d)));
  c.appendChild(ul);
  return c;
}

function render() {
  const q = $("q").value.toLowerCase(), lang = $("lang").value, sort = $("sort").value;
  // The "good first issue" checkbox decides which issues are counted at all; the other filters narrow that.
  const isHidden = d => hiddenIssues.has(d.url) || hiddenRepos.has(d.repo.full_name);
  const pool = DATA.filter(d => !isHidden(d) && !(splitBeginner && $("beginner").checked && !d.beginner));
  repoTotals = {};
  pool.forEach(d => { repoTotals[d.repo.full_name] = (repoTotals[d.repo.full_name] || 0) + 1; });
  let rows = pool.filter(d => {
    if (lang && d.repo.language !== lang) return false;
    if (selected.size) {
      const want = [...selected];
      const ok = $("mode").value === "all" ? want.every(k => d.keys.includes(k)) : want.some(k => d.keys.includes(k));
      if (!ok) return false;
    }
    if (!q) return true;
    return (d.title + " " + d.repo.full_name + " " + d.repo.summary + " " + d.repo.topics.join(" ")).toLowerCase().includes(q);
  });
  // Issues inside a repo share its stars, so "Most stars" falls back to recently updated there.
  // Lower sorts first. "Newest"/"Oldest" go by when the issue was opened, not last updated.
  const issueScore = d => sort === "comments" ? d.comments : sort === "newest" ? d.created_days
    : sort === "oldest" ? -d.created_days : d.updated_days;
  rows.sort((a, b) => issueScore(a) - issueScore(b));

  // Group the matching issues by repo; rows are already sorted, so each group's first issue is its best.
  const byRepo = new Map();
  rows.forEach(d => {
    const name = d.repo.full_name;
    if (!byRepo.has(name)) byRepo.set(name, { repo: d.repo, issues: [] });
    byRepo.get(name).issues.push(d);
  });
  const groups = [...byRepo.values()];
  const score = g => sort === "stars" ? -g.repo.stars : issueScore(g.issues[0]);
  groups.sort((a, b) => score(a) - score(b) || b.issues.length - a.issues.length || a.repo.full_name.localeCompare(b.repo.full_name));

  // Out of everything the search returned, minus anything hidden (hidden items are counted separately).
  const kept = DATA.filter(d => !isHidden(d));
  const keptRepos = new Set(kept.map(d => d.repo.full_name)).size;
  $("count").textContent = rows.length + " of " + kept.length + " issues in " + groups.length + " of " + keptRepos + " repos";
  const nHidden = DATA.length - kept.length;
  if (nHidden) {
    $("count").append(" · " + nHidden + " hidden" + (hiddenRepos.size ? " (" + hiddenRepos.size + (hiddenRepos.size === 1 ? " repo" : " repos") + ")" : "") + " · ");
    const undo = el("button", "link", "Unhide all");
    undo.type = "button";
    undo.addEventListener("click", () => { hiddenIssues.clear(); hiddenRepos.clear(); render(); });
    $("count").appendChild(undo);
  }
  const list = $("list"); list.replaceChildren();
  if (!rows.length) list.appendChild(el("div", "empty", "No issues match."));
  groups.forEach(g => list.appendChild(repoCard(g)));
}
["q", "lang", "sort", "mode"].forEach(id => $(id).addEventListener("input", render));
render();
</script>
</body>
</html>
"""


def describe_search(days, extra, require_help_wanted=True):
    """A plain-English version of build_query() for the dashboard header."""
    since = date.today() - timedelta(days=days)
    labels = " or ".join(DOC_LABELS)
    if require_help_wanted:
        labels += " and " + " or ".join(HELP_WANTED_LABELS)
    text = f"Open, unassigned issues labeled {labels}, opened since {since:%b} {since.day}, {since.year}, with no pull request and no blocking label such as wontfix or needs approval"
    if extra:
        text += f", also matching {extra}"
    return text + "."


def write_dashboard(records, query, out_path, description="", skipped=0):
    meta = {
        # UTC with an offset, so the page can show it in the viewer's own time zone.
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "query": query,
        "description": description,
        "skipped": skipped,
        "beginner_labels": FIRST_ISSUE_LABELS,
    }
    safe = lambda obj: json.dumps(obj).replace("</", "<\\/")
    html_text = HTML_TEMPLATE.replace("__DATA__", safe(records)).replace("__META__", safe(meta))
    Path(out_path).write_text(html_text, encoding="utf-8")


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--max-issues", type=int, default=150, help="max issues to collect, newest activity first (default 150)"
    )
    ap.add_argument("--days", type=int, default=90, help="only issues opened within the last N days (default 90)")
    ap.add_argument("--query", default="", help='extra GitHub search qualifiers, e.g. "language:python"')
    ap.add_argument(
        "--no-help-wanted",
        action="store_true",
        help='do not require the "help wanted" label (it is required by default)',
    )
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
    query = build_query(args.days, args.query, require_help_wanted=not args.no_help_wanted)
    print(f"Searching: {query}")
    issues, skipped = collect_issues(gh, query, args.max_issues)
    print(f"Collected {len(issues)} matching issues (skipped {skipped}).")

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
    description = describe_search(args.days, args.query, require_help_wanted=not args.no_help_wanted)
    write_dashboard(records, query, args.out, description, skipped)
    print(f"Wrote {args.out} with {len(records)} issues from {len(repos)} repos.")


if __name__ == "__main__":
    main()