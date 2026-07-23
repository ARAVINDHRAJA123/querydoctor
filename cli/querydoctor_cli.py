#!/usr/bin/env python3
"""
QueryDoctor CLI — local batch linting, no server, no network call.

Runs the exact same lint engine as the web app and the GitHub Action
(backend/lint_engine.py — sqlglot-based, no LLM) against a glob of local
.sql files, for pre-commit hooks or local CI parity without needing the
Action's GitHub-specific plumbing. Only dependency is sqlglot.

Never modifies a file unless --fix is passed explicitly (matches the
sqlfluff `fix` convention) — the default is report-only, since silently
rewriting someone's SQL file on every run would be a good way to lose
their trust (and possibly their uncommitted work) the first time an
auto-fix is wrong.
"""

import argparse
import glob
import json
import sys

# lint_engine.py sits alongside this file as a synced vendored copy — see
# its own top comment and sync_vendor.sh. A real (non-editable) pip install
# can't reach across into ../backend, confirmed by testing one, so this
# copy has to actually ship inside the package rather than being imported
# by relative path.
from lint_engine import check_sql, parse_ddl_to_schema

SEV_ORDER = ["low", "medium", "high"]


def _load_schema(schema_path: str | None, ddl_path: str | None) -> tuple[dict | None, list[str]]:
    schema = None
    warnings = []
    if ddl_path:
        with open(ddl_path, "r", encoding="utf-8") as f:
            ddl_schema, ddl_warnings = parse_ddl_to_schema(f.read())
            schema = {**(schema or {}), **ddl_schema}
            warnings.extend(ddl_warnings)
    if schema_path:
        with open(schema_path, "r", encoding="utf-8") as f:
            explicit = json.load(f)
        schema = {**(schema or {}), **explicit}  # explicit schema file wins per-table, same as the API
    return schema, warnings


def _print_report(path: str, sql: str, result: dict, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps({"file": path, **result}, indent=2))
        return
    print(f"\n=== {path} ===")
    if not result.get("valid"):
        err = result.get("syntax_error", {})
        print(f"  SYNTAX ERROR (line {err.get('line')}, col {err.get('col')}): {err.get('message')}")
        if err.get("hint"):
            print(f"  hint: {err['hint']}")
        return
    print(f"  score: {result['score']}/100")
    for f in result.get("findings", []):
        print(f"  [{f['severity'].upper():6s}] {f['title']} — {f['message']}")
    if not result.get("findings"):
        print("  clean, no findings")
    if result.get("suppressed_by_noqa"):
        print(f"  suppressed by noqa: {', '.join(result['suppressed_by_noqa'])}")
    if result.get("schema_warnings"):
        for w in result["schema_warnings"]:
            print(f"  schema warning: {w}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="querydoctor", description="Lint .sql files with QueryDoctor's rule engine.")
    parser.add_argument("files", nargs="+", help="SQL file(s) or glob pattern(s) to check (use '-' to read stdin)")
    parser.add_argument("--dialect", default="bigquery", help="SQL dialect (default: bigquery)")
    parser.add_argument("--schema", help="Path to a JSON file: {\"table\": [\"col\", ...]}")
    parser.add_argument("--ddl", help="Path to a .sql file containing CREATE TABLE statements")
    parser.add_argument("--json", action="store_true", help="Emit one JSON object per file instead of a human-readable report")
    parser.add_argument("--fail-on", choices=["never", "low", "medium", "high"], default="high",
                         help="Exit non-zero if any file has a finding at/above this severity, or fails to parse (default: high)")
    parser.add_argument("--fix", action="store_true",
                         help="Write auto-fixed SQL back to each file in place (only the zero-ambiguity fixes; report-only otherwise)")
    args = parser.parse_args(argv)

    try:
        schema, schema_warnings = _load_schema(args.schema, args.ddl)
    except Exception as e:
        print(f"error: couldn't load --schema/--ddl: {e}", file=sys.stderr)
        return 2

    paths = []
    if args.files == ["-"]:
        paths = ["-"]
    else:
        for pattern in args.files:
            matched = glob.glob(pattern, recursive=True)
            paths.extend(matched if matched else [pattern])  # keep literal path so a typo'd filename still errors clearly

    if not paths:
        print("error: no files matched", file=sys.stderr)
        return 2

    worst_seen = -1  # index into SEV_ORDER, or -1 for nothing/invalid-signals-separately
    any_invalid = False
    threshold = SEV_ORDER.index(args.fail_on) if args.fail_on in SEV_ORDER else None

    for path in paths:
        try:
            if path == "-":
                sql = sys.stdin.read()
            else:
                with open(path, "r", encoding="utf-8") as f:
                    sql = f.read()
        except OSError as e:
            print(f"error: couldn't read {path}: {e}", file=sys.stderr)
            any_invalid = True
            continue

        result = check_sql(sql, dialect=args.dialect, schema=schema)
        if schema_warnings and not args.json:
            result.setdefault("schema_warnings", [])
            result["schema_warnings"] = list(dict.fromkeys(schema_warnings + result["schema_warnings"]))
        _print_report(path, sql, result, args.json)

        if not result.get("ok") or not result.get("valid"):
            any_invalid = True
            continue
        for f in result.get("findings", []):
            idx = SEV_ORDER.index(f["severity"])
            worst_seen = max(worst_seen, idx)

        if args.fix and result.get("auto_fixed_sql") and path != "-":
            with open(path, "w", encoding="utf-8") as f:
                f.write(result["auto_fixed_sql"].rstrip("\n") + "\n")
            print(f"  fixed and wrote {path} ({', '.join(result['auto_fixed_titles'])})")

    if any_invalid:
        return 1
    if threshold is not None and worst_seen >= threshold:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
