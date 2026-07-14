"""
QueryDoctor lint engine — pure sqlglot logic, no FastAPI/pydantic dependency.

This module is the single source of truth for SQL parsing, linting, scoring,
formatting and translation. It's imported by:
- backend/main.py (the web API / PWA backend)
- action/review_pr.py (the standalone GitHub Action, which must NOT need
  fastapi/pydantic installed just to lint a diff)

Nothing here talks HTTP, touches rate limiting, or knows about requests —
that's main.py's job. This file only knows SQL.
"""

import difflib
import re

import sqlglot
from sqlglot import exp
from sqlglot.optimizer import optimize as sqlglot_optimize
from sqlglot.optimizer.simplify import simplify as sqlglot_simplify

DIALECTS = ["bigquery", "postgres", "mysql", "snowflake", "spark", "sqlite", "tsql", "oracle", "duckdb", "redshift"]


# ── Lint rules ────────────────────────────────────────────────────────────────
# Each rule returns (severity, title, message) tuples. Severities: high, medium, low.

def _rule_select_star(tree):
    for sel in tree.find_all(exp.Select):
        for e in sel.expressions:
            if isinstance(e, exp.Star):
                yield ("medium", "SELECT * used",
                       "Selecting every column reads more data than you need — on cloud warehouses you pay for it. List only the columns you actually use.")
                return


def _rule_delete_update_no_where(tree):
    for node in tree.find_all(exp.Delete):
        if not node.args.get("where"):
            yield ("high", "DELETE without WHERE",
                   "This deletes EVERY row in the table. If that's not what you want, add a WHERE clause before running it.")
    for node in tree.find_all(exp.Update):
        if not node.args.get("where"):
            yield ("high", "UPDATE without WHERE",
                   "This updates EVERY row in the table. Add a WHERE clause to target only the rows you mean to change.")


def _rule_cross_join(tree):
    for j in tree.find_all(exp.Join):
        if (j.kind or "").upper() == "CROSS":
            yield ("high", "CROSS JOIN found",
                   "A cross join pairs every row of one table with every row of the other — result sizes explode fast. Make sure this is intentional; usually you want a JOIN ... ON condition.")


def _rule_join_no_condition(tree):
    for j in tree.find_all(exp.Join):
        if (j.kind or "").upper() != "CROSS" and not j.args.get("on") and not j.args.get("using"):
            yield ("high", "JOIN without ON condition",
                   "This join has no ON/USING condition, so it behaves like a cross join and multiplies rows. Add the joining condition.")


def _rule_limit_no_order(tree):
    for sel in tree.find_all(exp.Select):
        if sel.args.get("limit") and not sel.args.get("order"):
            yield ("medium", "LIMIT without ORDER BY",
                   "Without ORDER BY, the database may return a different set of rows each time. Add ORDER BY if you need consistent results.")
            return


def _rule_leading_wildcard(tree):
    for like in tree.find_all(exp.Like, exp.ILike):
        pat = like.expression
        if isinstance(pat, exp.Literal) and pat.is_string and pat.this.startswith("%"):
            yield ("medium", "LIKE starts with %",
                   f"LIKE '{pat.this}' can't use an index — the database scans every row. If possible, anchor the pattern at the start (e.g. 'abc%').")
            return


def _rule_not_in_subquery(tree):
    for n in tree.find_all(exp.Not):
        inner = n.this
        if isinstance(inner, exp.In) and inner.args.get("query"):
            yield ("medium", "NOT IN with a subquery",
                   "If the subquery returns even one NULL, NOT IN returns no rows at all — a classic silent bug. Use NOT EXISTS instead.")
            return


def _rule_func_on_column_in_where(tree):
    for sel in tree.find_all(exp.Select):
        where = sel.args.get("where")
        if not where:
            continue
        for fn in where.find_all(exp.Func):
            if isinstance(fn, (exp.Cast, exp.Date, exp.Upper, exp.Lower, exp.Substring)) and fn.find(exp.Column):
                yield ("low", "Function wrapped around a column in WHERE",
                       "Applying functions to a column in WHERE (e.g. DATE(col), UPPER(col)) prevents index use and partition pruning. Compare the raw column against a constant instead.")
                return


def _rule_union_vs_union_all(tree):
    for u in tree.find_all(exp.Union):
        if u.args.get("distinct", True) and not isinstance(u.parent, exp.Union):
            yield ("low", "UNION (not UNION ALL)",
                   "UNION removes duplicates, which costs an extra sort/shuffle. If duplicates are impossible or acceptable, UNION ALL is faster.")
            return


RULES = [
    _rule_delete_update_no_where,
    _rule_cross_join,
    _rule_join_no_condition,
    _rule_select_star,
    _rule_limit_no_order,
    _rule_leading_wildcard,
    _rule_not_in_subquery,
    _rule_func_on_column_in_where,
    _rule_union_vs_union_all,
]

SEV_WEIGHT = {"high": 30, "medium": 12, "low": 5}

# Common statement-starting keywords, used to catch beginner typos like
# "SELECTT" or "UPDTE" that a parser alone reports confusingly late.
STATEMENT_STARTERS = [
    "select", "insert", "update", "delete", "with", "create", "merge",
    "alter", "drop", "truncate", "explain", "grant", "revoke", "set", "show",
]


def _clean_parse_message(msg: str) -> str:
    """sqlglot errors embed raw token reprs like
    '<Token token_type: TokenType.WHERE, text: WHERE, ...>' — swap them for
    just the quoted word, and drop the duplicated position suffix."""
    msg = re.sub(r"<Token token_type[^>]*?text: ([^,>]+)[^>]*>", r"'\1'", msg)
    msg = re.sub(r"\.?\s*Line \d+, Col: \d+\.?\s*$", "", msg).strip()
    return msg


def _typo_hint(sql: str) -> str | None:
    m = re.match(r"\s*([A-Za-z_]+)", sql)
    if not m:
        return None
    word = m.group(1).lower()
    if word in STATEMENT_STARTERS:
        return None
    close = difflib.get_close_matches(word, STATEMENT_STARTERS, n=1, cutoff=0.7)
    if close:
        return f"'{m.group(1)}' isn't a SQL command — did you mean {close[0].upper()}?"
    return None


def check_sql(sql: str, dialect: str = "bigquery", target_dialect: str | None = None) -> dict:
    """Run the full QueryDoctor diagnosis on one blob of SQL (may contain
    multiple statements). Returns the same shape the /api/check endpoint
    sends back, minus the HTTP/rate-limit wrapper. Never raises — parse
    errors and optimizer failures degrade into fields on the result dict."""
    sql = sql.strip()
    dialect = dialect if dialect in DIALECTS else "bigquery"
    if not sql:
        return {"ok": False, "error": "Paste some SQL first."}

    # 1. Parse (syntax check)
    try:
        trees = sqlglot.parse(sql, read=dialect)
        trees = [t for t in trees if t is not None]
        if not trees:
            raise sqlglot.errors.ParseError("Empty statement")
    except sqlglot.errors.ParseError as e:
        err = e.errors[0] if getattr(e, "errors", None) else {}
        line, col = err.get("line"), err.get("col")
        src_lines = sql.splitlines()
        source_line = src_lines[line - 1] if line and line <= len(src_lines) else None
        return {
            "ok": True,
            "valid": False,
            "syntax_error": {
                "message": _clean_parse_message(str(e).split("\n")[0]),
                "line": line,
                "col": col,
                "highlight": err.get("highlight"),
                "source_line": source_line,
                "hint": _typo_hint(sql),
            },
        }

    # 2. Lint
    findings = []
    for tree in trees:
        for rule in RULES:
            for sev, title, msg in rule(tree):
                if not any(f["title"] == title for f in findings):
                    findings.append({"severity": sev, "title": title, "message": msg})
    findings.sort(key=lambda f: ["high", "medium", "low"].index(f["severity"]))

    score = max(0, 100 - sum(SEV_WEIGHT[f["severity"]] for f in findings))

    # 3. Format
    try:
        formatted = sqlglot.transpile(sql, read=dialect, write=dialect, pretty=True)
        formatted = ";\n\n".join(formatted) + (";" if len(formatted) > 1 else "")
    except Exception:
        formatted = None

    # 4. Optimized rewrite (deterministic, no schema required for the fallback
    # path). Full optimize() needs column-level schema; without one it can
    # raise — degrade gracefully: full optimize → simplify only → skip.
    optimized = None
    try:
        opt_trees = []
        for t in trees:
            try:
                ot = sqlglot_optimize(t.copy(), dialect=dialect)
            except Exception:
                ot = sqlglot_simplify(t.copy())
            opt_trees.append(ot)
        candidate = ";\n\n".join(ot.sql(dialect=dialect, pretty=True) for ot in opt_trees)

        def _canon(q: str) -> str:
            # Ignore cosmetic differences the optimizer introduces (auto-added
            # aliases like `1 AS "1"`, identifier quoting, whitespace) so the
            # card only appears for a REAL structural rewrite.
            q = re.sub(r'\s+AS\s+"[^"]*"', "", q, flags=re.I)
            q = q.replace('"', "").replace("`", "")
            return re.sub(r"\s+", " ", q).strip().rstrip(";").lower()

        if formatted and _canon(candidate) != _canon(formatted):
            optimized = candidate + (";" if len(opt_trees) > 1 else "")
    except Exception:
        optimized = None

    # 5. Optional translation
    translated = None
    if target_dialect and target_dialect in DIALECTS and target_dialect != dialect:
        try:
            out = sqlglot.transpile(sql, read=dialect, write=target_dialect, pretty=True)
            translated = ";\n\n".join(out) + (";" if len(out) > 1 else "")
        except Exception as e:
            translated = f"-- Couldn't translate: {str(e).splitlines()[0]}"

    return {
        "ok": True,
        "valid": True,
        "score": score,
        "findings": findings,
        "formatted": formatted,
        "optimized": optimized,
        "translated": translated,
        "statement_count": len(trees),
    }
