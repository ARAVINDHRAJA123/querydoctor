#!/usr/bin/env python3
"""
QueryDoctor PR Reviewer — standalone GitHub Action script.

Finds the .sql files changed in a pull request, runs them through
QueryDoctor's lint engine (backend/lint_engine.py — same rules as the
live web app, sqlglot-based, no LLM, no external calls), and posts a
single PR comment summarizing the findings. Comment-only in v1: this
script never fails the build / blocks the merge, no matter what it finds.

Only dependency: sqlglot (installed by action.yml). Everything else here
is Python stdlib + the GitHub REST API, so this can run in any repo
without the target repo needing FastAPI, pydantic, or a QueryDoctor
deployment of its own.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
from lint_engine import check_sql  # noqa: E402

COMMENT_MARKER = "<!-- querydoctor-action -->"
SEV_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🔵"}


def _env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        print(f"::error::Missing required environment variable {name}")
        sys.exit(0)  # comment-only mode: never fail the build
    return val


def _api_request(url: str, token: str, method: str = "GET", data: dict | None = None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        print(f"::warning::GitHub API {method} {url} failed: {e.code} {e.read().decode()[:500]}")
        return None


def _glob_to_regex(pattern: str) -> re.Pattern:
    """Minimal glob translator that treats '**/' as zero-or-more path
    segments (so '**/*.sql' also matches a root-level 'foo.sql'), unlike
    fnmatch which requires a literal '/' whenever the pattern has one."""
    i, n, out = 0, len(pattern), ""
    while i < n:
        if pattern[i:i + 3] == "**/":
            out += "(?:.*/)?"
            i += 3
        elif pattern[i:i + 2] == "**":
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(f"^{out}$")


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(_glob_to_regex(pat).match(path) for pat in patterns)


def get_pr_number(event_path: str) -> int | None:
    with open(event_path) as f:
        event = json.load(f)
    pr = event.get("pull_request") or event.get("issue")
    return pr["number"] if pr else None


def get_changed_files(api_base: str, repo: str, pr_number: int, token: str, patterns: list[str]) -> list[str]:
    files, page = [], 1
    while True:
        url = f"{api_base}/repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}"
        batch = _api_request(url, token)
        if not batch:
            break
        for f in batch:
            if f.get("status") == "removed":
                continue
            path = f["filename"]
            if _matches_any(path, patterns):
                files.append(path)
        if len(batch) < 100:
            break
        page += 1
    return files


def build_report(results: dict[str, dict]) -> str:
    lines = [
        COMMENT_MARKER,
        "## 🩺 QueryDoctor — SQL review",
        "",
    ]
    total_findings = sum(len(r.get("findings", [])) for r in results.values() if r.get("valid"))
    syntax_errors = sum(1 for r in results.values() if r.get("valid") is False)

    if total_findings == 0 and syntax_errors == 0:
        lines.append(f"All {len(results)} changed SQL file(s) look clean. No lint findings, no syntax errors. ✅")
        lines.append("")
        lines.append(COMMENT_MARKER)
        return "\n".join(lines)

    lines.append(f"Checked **{len(results)}** changed SQL file(s) — "
                 f"**{syntax_errors}** syntax error(s), **{total_findings}** lint finding(s).")
    lines.append("")

    for path, result in results.items():
        if not result.get("ok"):
            continue
        if result.get("valid") is False:
            err = result["syntax_error"]
            lines.append(f"### ❌ `{path}` — syntax error")
            loc = f" (line {err['line']})" if err.get("line") else ""
            lines.append(f"{err['message']}{loc}")
            if err.get("hint"):
                lines.append(f"> {err['hint']}")
            lines.append("")
            continue

        findings = result.get("findings", [])
        score = result.get("score", 100)
        if not findings:
            continue
        lines.append(f"### `{path}` — score {score}/100")
        for f in findings:
            emoji = SEV_EMOJI.get(f["severity"], "⚪")
            lines.append(f"- {emoji} **{f['title']}** ({f['severity']}) — {f['message']}")
        lines.append("")

    lines.append("_Powered by [QueryDoctor](https://querydoctor-616665622891.asia-south1.run.app) "
                  "— sqlglot-based, no LLM, nothing leaves this workflow run._")
    lines.append("")
    lines.append(COMMENT_MARKER)
    return "\n".join(lines)


def upsert_comment(api_base: str, repo: str, pr_number: int, token: str, body: str):
    url = f"{api_base}/repos/{repo}/issues/{pr_number}/comments?per_page=100"
    existing = _api_request(url, token) or []
    mine = next((c for c in existing if COMMENT_MARKER in (c.get("body") or "")), None)
    if mine:
        _api_request(f"{api_base}/repos/{repo}/issues/comments/{mine['id']}", token, "PATCH", {"body": body})
        print(f"Updated existing review comment (id={mine['id']}).")
    else:
        _api_request(url.split("?")[0], token, "POST", {"body": body})
        print("Posted new review comment.")


def main():
    token = _env("GITHUB_TOKEN")
    event_path = _env("GITHUB_EVENT_PATH")
    repo = _env("GITHUB_REPOSITORY")
    api_base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    dialect = os.environ.get("INPUT_DIALECT", "bigquery")
    patterns = [p.strip() for p in os.environ.get("INPUT_FILE_PATTERNS", "**/*.sql").split(",") if p.strip()]
    workspace = os.environ.get("GITHUB_WORKSPACE", ".")

    pr_number = get_pr_number(event_path)
    if pr_number is None:
        print("Not a pull_request event — nothing to review. Exiting quietly.")
        return

    changed = get_changed_files(api_base, repo, pr_number, token, patterns)
    if not changed:
        print("No SQL files changed in this PR — nothing to review.")
        return

    results = {}
    for path in changed:
        full_path = os.path.join(workspace, path)
        try:
            with open(full_path, encoding="utf-8", errors="replace") as f:
                sql = f.read()
        except OSError as e:
            print(f"::warning::Couldn't read {path}: {e}")
            continue
        results[path] = check_sql(sql, dialect=dialect)

    if not results:
        return

    report = build_report(results)
    print(report)
    upsert_comment(api_base, repo, pr_number, token, report)


if __name__ == "__main__":
    main()
