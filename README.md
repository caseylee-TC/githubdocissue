# Docs Issue Finder

Finds open-source **documentation issues that welcome first-time contributors**, collects each project's contributing guide, summarizes what the project is, and writes everything to a local `dashboard.html`.

## Quick start

```bash
pip install -r requirements.txt

# A read-only GitHub token is enough (no scopes needed for public data)
export GITHUB_TOKEN=ghp_your_token_here

python docs_issue_finder.py --max-issues 30 --days 90
open dashboard.html        # macOS; use xdg-open on Linux, start on Windows
```

Without a token the script still works, but GitHub's unauthenticated limits are very low.

## Options

| Flag | Default | What it does |
|------|---------|--------------|
| `--max-issues N` | 30 | Maximum issues to collect |
| `--days N` | 90 | Only issues updated within the last N days |
| `--query "..."` | none | Extra GitHub search qualifiers, e.g. `"language:python"` or `"org:kubernetes"` |
| `--llm` | off | Use Claude for the project summary and contribution steps (needs `ANTHROPIC_API_KEY`) |
| `--no-cache` | off | Ignore the per-repo cache |
| `--out PATH` | `dashboard.html` | Where to write the dashboard |

## How the filter works

The search query (shown at the top of the dashboard) requires an issue to be:

- open, with **no assignee**, and **no linked pull request**
- in a repo that is **not archived**
- **updated within `--days`**
- labeled with any of `good first issue`, `good-first-issue`, `first-timers-only`
- **and** labeled with any of `documentation`, `docs`

To tune it, edit `FIRST_ISSUE_LABELS` and `DOC_LABELS` near the top of `docs_issue_finder.py`. Inside one `label:` qualifier GitHub treats comma-separated values as OR; separate `label:` qualifiers are AND.

## Using the dashboard

- Tick **help wanted** (pinned at the top, with a count) to show only issues that carry that label.
- **More labels** lists every other label in the results, with counts. Labels that every issue shares, such as the ones the search already requires, are hidden because they can't narrow anything.
- Spellings are merged, so `help-wanted`, `Help Wanted` and `help wanted` count as one label.
- With several labels ticked, choose **Match any** or **Match all**.
- These filters only narrow the issues the search already returned (`--max-issues`). If you want more `help wanted` results, raise `--max-issues`.

## What it collects per project

- Repo metadata: description, language, stars, topics, license
- README, reduced to a short "what is this" summary
- First contributing guide found, checked in this order: `CONTRIBUTING.md`, `.github/CONTRIBUTING.md`, `docs/CONTRIBUTING.md`, `CONTRIBUTING.rst`, `CONTRIBUTING`, `contributing.md`
- Heuristic flags from the guide: CLA required, DCO sign-off, claim the issue first, fork and PR workflow, tests or lint expected, commit message convention

With `--llm`, Claude writes the project summary and a short list of contribution steps instead. Set `ANTHROPIC_MODEL` to override the default model.

## Caching

Repo details and summaries are cached for 7 days in `.cache/repos.json`, so re-runs only fetch new repos. Delete the file or pass `--no-cache` to refresh.

## Known limits of the prototype

- Heuristic summaries are only as good as the README's first paragraph.
- Flags come from keyword matching, so treat them as hints and read the guide.
- Only the first 100 search results per page are fetched, and the Search API caps results at 1,000.
- A repo with no contributing guide is shown as such; the README or the issue thread may still explain how to contribute.

## Ideas for next iterations

- Ranking by docs toolchain match (Markdown, Sphinx, MkDocs, Docusaurus, Hugo)
- "New since last run" diff using a small SQLite file
- Maintainer responsiveness signals (time to first reply on recent PRs)
- Text-based matching for docs issues that carry no label
- Export a shortlist to Google Sheets