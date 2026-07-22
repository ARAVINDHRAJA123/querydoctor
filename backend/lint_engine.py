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


# ── dbt/Jinja stubber ────────────────────────────────────────────────────────
# NOT a Jinja engine — evaluates nothing. Swaps common dbt Jinja constructs for
# valid placeholder SQL so sqlglot sees plain SQL instead of false syntax
# errors on {{ ref(...) }} etc. Each replacement keeps the same newline count
# as what it replaced, so line numbers in lint/syntax-error output still
# point at roughly the right spot in the ORIGINAL file.

_JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)
_JINJA_REF_RE = re.compile(r"\{\{\s*ref\(\s*((?:'[^']*'|\"[^\"]*\")(?:\s*,\s*(?:'[^']*'|\"[^\"]*\"))*)\s*\)\s*\}\}")
_JINJA_SOURCE_RE = re.compile(r"\{\{\s*source\(\s*((?:'[^']*'|\"[^\"]*\")(?:\s*,\s*(?:'[^']*'|\"[^\"]*\"))*)\s*\)\s*\}\}")
_JINJA_VAR_RE = re.compile(r"\{\{\s*var\(.*?\)\s*\}\}", re.DOTALL)
_JINJA_EXPR_RE = re.compile(r"\{\{.*?\}\}", re.DOTALL)   # any remaining {{ ... }}
_JINJA_BLOCK_RE = re.compile(r"\{%.*?%\}", re.DOTALL)    # {% if/set/macro/... %}
_QUOTED_ARG_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


def _preserve_lines(matched: str, replacement: str) -> str:
    return replacement + "\n" * matched.count("\n")


def strip_jinja(sql: str) -> str:
    """Best-effort dbt/Jinja stubber. {{ ref(...) }}/{{ source(...) }} become
    bare placeholder identifiers, {{ var(...) }} becomes a placeholder string
    literal, any other {{ ... }} expression or {% ... %} block is removed
    entirely. Good enough to lint a dbt model's structure; not a substitute
    for `dbt compile` if you need the ACTUAL rendered query."""
    def ref_repl(m):
        args = [a.strip("'\"") for a in _QUOTED_ARG_RE.findall(m.group(1))]
        return _preserve_lines(m.group(0), "dbt_ref__" + "_".join(args))

    def source_repl(m):
        args = [a.strip("'\"") for a in _QUOTED_ARG_RE.findall(m.group(1))]
        return _preserve_lines(m.group(0), "dbt_src__" + "_".join(args))

    def var_repl(m):
        return _preserve_lines(m.group(0), "'DBT_VAR'")

    def expr_repl(m):
        return _preserve_lines(m.group(0), "dbt_expr")

    def strip_repl(m):
        return _preserve_lines(m.group(0), "")

    sql = _JINJA_COMMENT_RE.sub(strip_repl, sql)
    sql = _JINJA_REF_RE.sub(ref_repl, sql)
    sql = _JINJA_SOURCE_RE.sub(source_repl, sql)
    sql = _JINJA_VAR_RE.sub(var_repl, sql)
    sql = _JINJA_EXPR_RE.sub(expr_repl, sql)
    sql = _JINJA_BLOCK_RE.sub(strip_repl, sql)
    return sql


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


def _rule_having_no_aggregate(tree):
    for sel in tree.find_all(exp.Select):
        having = sel.args.get("having")
        if having and not having.find(exp.AggFunc):
            yield ("medium", "HAVING used without an aggregate",
                   "HAVING with no aggregate function just filters rows — that's what WHERE is for, and WHERE filters before grouping, which is cheaper. Move this condition to WHERE.")
            return


def _rule_order_by_in_subquery(tree):
    for sel in tree.find_all(exp.Select):
        if sel.args.get("order") and not sel.args.get("limit") and isinstance(sel.parent, (exp.Subquery, exp.CTE)):
            yield ("low", "ORDER BY in a subquery without LIMIT",
                   "Most engines discard ORDER BY inside a subquery/CTE unless it's paired with LIMIT — the sort runs but the outer query sees no guaranteed order. Move the ORDER BY to the outermost query, or add a LIMIT here if you meant to cap rows.")
            return


def _rule_group_by_missing_column(tree):
    for sel in tree.find_all(exp.Select):
        group = sel.args.get("group")
        if not group:
            continue
        group_keys = {g.sql().lower() for g in group.expressions}
        for e in sel.expressions:
            col = e.this if isinstance(e, exp.Alias) else e
            if isinstance(col, exp.Column) and not col.find(exp.AggFunc) and col.sql().lower() not in group_keys:
                yield ("high", "Selected column missing from GROUP BY",
                       f"'{col.sql()}' is selected but not aggregated or listed in GROUP BY. Most engines reject this outright; some (like older MySQL) silently pick an arbitrary row's value — add it to GROUP BY or wrap it in an aggregate.")
                return


def _rule_case_no_else(tree):
    for case in tree.find_all(exp.Case):
        if case.args.get("default") is None:
            yield ("medium", "CASE without ELSE",
                   "A CASE with no ELSE returns NULL for any row that doesn't match a WHEN — easy to miss and a common source of silent NULLs downstream. Add an explicit ELSE, even if it's just ELSE NULL to show it's intentional.")
            return


def _rule_join_on_tautology(tree):
    for j in tree.find_all(exp.Join):
        on = j.args.get("on")
        if isinstance(on, exp.Boolean) and on.this:
            yield ("high", "JOIN ON true",
                   "This join condition is always true — every row pairs with every row, a Cartesian product. Usually leftover from debugging; add the real join condition.")
            return
        if isinstance(on, exp.EQ) and isinstance(on.this, exp.Literal) and isinstance(on.expression, exp.Literal) and on.this.this == on.expression.this:
            yield ("high", "JOIN ON 1=1",
                   "This join condition is always true — every row pairs with every row, a Cartesian product. Usually leftover from debugging; add the real join condition.")
            return


def _rule_coalesce_in_equality(tree):
    for eq in tree.find_all(exp.EQ):
        if isinstance(eq.this, exp.Coalesce) or isinstance(eq.expression, exp.Coalesce):
            yield ("medium", "COALESCE in an equality comparison",
                   "COALESCE(col, '') = COALESCE(other, '') silently treats NULL and the fallback value as equal — two NULLs won't match here even though this pattern can make it look like they do (or a real '' value now falsely matches a NULL). Handle NULLs explicitly with IS NULL / IS NOT NULL instead.")
            return


def _rule_window_no_order(tree):
    ordered_funcs = (exp.RowNumber, exp.Lag, exp.Lead)
    for win in tree.find_all(exp.Window):
        if isinstance(win.this, ordered_funcs) and not win.args.get("order"):
            yield ("medium", "ROW_NUMBER/LAG/LEAD without ORDER BY",
                   "Without an ORDER BY inside the window, the row ordering (and so the result) isn't guaranteed — the same query can return different values on different runs. Add ORDER BY inside the OVER (...) clause.")
            return


def _rule_left_join_nullified_by_where(tree):
    """The classic silent-correctness bug: a WHERE clause that compares a
    column from the optional (right) side of a LEFT JOIN filters out every
    row where that side didn't match — NULL >= anything is never true — so
    the LEFT JOIN quietly behaves like an INNER JOIN. Scoped per-SELECT
    (including inside CTEs) so a join in one CTE doesn't false-positive
    against an unrelated WHERE in another."""
    for select in tree.find_all(exp.Select):
        left_aliases = {
            j.this.alias_or_name
            for j in (select.args.get("joins") or [])
            if j.args.get("side") == "LEFT"
        }
        if not left_aliases:
            continue
        where = select.args.get("where")
        if where is None:
            continue
        condition = where.this
        top_level = list(condition.flatten()) if isinstance(condition, exp.And) else [condition]
        for cond in top_level:
            if isinstance(cond, (exp.Or, exp.Is)):
                continue  # OR-guarded or an explicit IS [NOT] NULL check — the safe patterns
            for col in cond.find_all(exp.Column):
                if col.table in left_aliases:
                    yield ("high", "WHERE clause nullifies a LEFT JOIN",
                           f"WHERE filters on `{col.table}.{col.this.this}`, a column from the LEFT-joined side. "
                           "Rows with no match there have NULL here, and NULL compared to anything is never true — "
                           "so this silently drops every non-matching row, turning the LEFT JOIN into an INNER JOIN. "
                           "Move this condition into the JOIN's ON clause instead, or guard it with an explicit "
                           "IS NULL check if you actually meant to require a match.")
                    return


def _rule_insert_no_columns(tree):
    for ins in tree.find_all(exp.Insert):
        if isinstance(ins.this, exp.Table):
            yield ("medium", "INSERT without a column list",
                   "INSERT INTO t VALUES (...) relies on the table's current column order — add or reorder a column later and this silently writes data into the wrong columns. List the target columns explicitly: INSERT INTO t (col1, col2) VALUES (...).")
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
    _rule_having_no_aggregate,
    _rule_group_by_missing_column,
    _rule_order_by_in_subquery,
    _rule_case_no_else,
    _rule_join_on_tautology,
    _rule_coalesce_in_equality,
    _rule_window_no_order,
    _rule_insert_no_columns,
    _rule_left_join_nullified_by_where,
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


def check_sql(sql: str, dialect: str = "bigquery", target_dialect: str | None = None, dbt_mode: bool = False) -> dict:
    """Run the full QueryDoctor diagnosis on one blob of SQL (may contain
    multiple statements). Returns the same shape the /api/check endpoint
    sends back, minus the HTTP/rate-limit wrapper. Never raises — parse
    errors and optimizer failures degrade into fields on the result dict."""
    sql = sql.strip()
    dialect = dialect if dialect in DIALECTS else "bigquery"
    if not sql:
        return {"ok": False, "error": "Paste some SQL first."}
    if dbt_mode:
        sql = strip_jinja(sql)

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
