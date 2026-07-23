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
import logging
import re
import sqlite3
import zlib
from collections import Counter, defaultdict

import sqlglot
from sqlglot import exp
from sqlglot.tokens import Tokenizer, TokenType
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


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _rule_between_date_literals(tree):
    """BETWEEN is inclusive of the exact literal instant. On a DATE column
    that's harmless; on a DATETIME/TIMESTAMP column, `BETWEEN 'start' AND
    'end'` silently means `end 00:00:00` — everything after midnight on the
    last day is excluded. Can't know the column's real type statically, so
    this stays medium severity and phrases it as a caution, not a verdict."""
    for b in tree.find_all(exp.Between):
        low, high = b.args.get("low"), b.args.get("high")
        if (isinstance(low, exp.Literal) and low.is_string and _DATE_ONLY_RE.match(low.this)
                and isinstance(high, exp.Literal) and high.is_string and _DATE_ONLY_RE.match(high.this)):
            yield ("medium", "BETWEEN with bare date literals",
                   f"BETWEEN '{low.this}' AND '{high.this}' is inclusive of the exact instant "
                   f"'{high.this} 00:00:00' — if this column has a time component, everything after "
                   "midnight on the last day is silently excluded. If it's a DATE-only column this is "
                   f"fine; otherwise use `>= '{low.this}' AND < '<day after {high.this}>'` instead.")
            return


def _rule_order_by_ordinal(tree):
    for sel in tree.find_all(exp.Select):
        order = sel.args.get("order")
        if not order:
            continue
        for o in order.expressions:
            if isinstance(o, exp.Ordered) and isinstance(o.this, exp.Literal) and not o.this.is_string:
                yield ("low", "ORDER BY column position",
                       f"ORDER BY {o.this.this} sorts by the Nth selected column, not a named one — "
                       "reorder or add a column to the SELECT list later and the sort silently changes "
                       "to whatever's now in that position. Use the column name instead.")
                return


def _rule_scalar_subquery_no_limit(tree):
    """A subquery used as a single value in the SELECT list should be
    guaranteed to return at most one row. An aggregate with no GROUP BY
    always collapses to exactly one row regardless of LIMIT, so that's
    excluded — only a bare correlated subquery without LIMIT 1 is flagged,
    since most engines error (or pick an arbitrary row) if it ever returns
    more than one at runtime, usually well after the query looked fine in
    testing."""
    for sel in tree.find_all(exp.Select):
        for proj in sel.expressions:
            for sq in proj.find_all(exp.Subquery):
                inner = sq.this
                if not isinstance(inner, exp.Select):
                    continue
                if inner.args.get("limit"):
                    continue
                if any(isinstance(e, exp.AggFunc) for e in inner.expressions) and not inner.args.get("group"):
                    continue
                yield ("medium", "Scalar subquery without LIMIT 1",
                       "This subquery is used as a single value, but nothing guarantees it returns at "
                       "most one row — if it ever matches more than one, most engines error at runtime "
                       "(some silently pick an arbitrary row). Add LIMIT 1 if one row is genuinely expected.")
                return


def _rule_update_from_no_join_condition(tree):
    """UPDATE ... FROM t2 WHERE <condition that doesn't reference t2> is a
    cross join in disguise: every row of the target table gets updated
    once per row of t2, using whichever one the engine happens to pick.
    A bare UPDATE with no WHERE at all is already caught by
    _rule_delete_update_no_where — this only covers the sneakier case
    where a WHERE exists but never actually links the two tables. Uses a
    conservative signal (the FROM table's alias never appears anywhere in
    WHERE at all) to keep false positives low."""
    for u in tree.find_all(exp.Update):
        from_ = u.args.get("from_")
        if not from_:
            continue
        where = u.args.get("where")
        if not where:
            continue  # already flagged as UPDATE without WHERE
        from_aliases = {t.alias_or_name for t in from_.find_all(exp.Table)}
        where_aliases = {c.table for c in where.find_all(exp.Column) if c.table}
        if from_aliases and not (from_aliases & where_aliases):
            yield ("high", "UPDATE ... FROM without a join condition",
                   f"The WHERE clause never references `{next(iter(from_aliases))}` (the FROM table), "
                   "so nothing links it to the table being updated — this behaves like a cross join, "
                   "updating every target row once per row in the FROM table. Add a condition connecting "
                   "the two tables (e.g. WHERE t1.id = t2.id).")
            return


def _rule_aggregate_wraps_window(tree):
    """SUM(AVG(x) OVER (...)) — an aggregate function directly wrapping a
    window function's result. Rejected by Postgres/BigQuery/Snowflake at
    execution time ("aggregate function calls cannot contain window
    function calls"), but sqlglot's parser doesn't validate it (semantic,
    not syntactic), so it reports "valid" on a query that will fail to
    run. The reverse (a window function wrapping an aggregate, e.g.
    SUM(x) OVER (...)) is the normal, correct pattern and must not be
    flagged — only checks whether a Window is a descendant of an AggFunc,
    never the other way around."""
    for agg in tree.find_all(exp.AggFunc):
        if any(isinstance(d, exp.Window) for d in agg.find_all(exp.Window)):
            yield ("high", "Aggregate function wraps a window function",
                   "An aggregate (SUM/AVG/COUNT/...) directly containing a window function's result "
                   "(e.g. SUM(AVG(x) OVER (...))) is rejected at execution time by Postgres, BigQuery, and "
                   "Snowflake — aggregates can't contain window function calls. Compute the window function "
                   "in a subquery or CTE first, then aggregate over that.")
            return


def _rule_null_equality(tree):
    """`x = NULL` and `x != NULL` are always NULL (never TRUE), never match
    any row, and silently return zero rows instead of erroring — one of the
    most common beginner-through-advanced SQL mistakes. `IS [NOT] NULL` is
    the only correct way to test for NULL."""
    for eq in tree.find_all((exp.EQ, exp.NEQ)):
        left_null = isinstance(eq.expression, exp.Null)
        right_null = isinstance(eq.this, exp.Null)
        if left_null or right_null:
            op = "=" if isinstance(eq, exp.EQ) else "!="
            fix = "IS NULL" if isinstance(eq, exp.EQ) else "IS NOT NULL"
            yield ("high", f"`{op} NULL` never matches",
                   f"`{eq.sql()}` always evaluates to NULL (never TRUE) in SQL's three-valued logic, so "
                   f"this silently returns zero rows instead of erroring. Use `{fix}` instead.")
            return


def _rule_offset_no_order(tree):
    """OFFSET without ORDER BY has the same problem as the existing
    LIMIT-without-ORDER-BY rule: without a deterministic sort, most engines
    don't guarantee row order, so which rows get skipped (and which remain)
    can change between runs of the identical query."""
    for sel in tree.find_all(exp.Select):
        if sel.args.get("offset") and not sel.args.get("order"):
            yield ("medium", "OFFSET without ORDER BY",
                   "OFFSET skips rows in whatever order the engine happens to return them — without an "
                   "ORDER BY, that order isn't guaranteed, so which rows get skipped (and which come back) "
                   "can change between identical runs of this query. Add an ORDER BY.")
            return


def _rule_mixed_ordinal_and_name(tree):
    """GROUP BY 1, b or ORDER BY 1, b mixes an ordinal position with a named
    column in the same clause — easy to misread, and error-prone the same
    way a pure ordinal is (renumbers silently if the SELECT list changes),
    but only half the clause gives any hint of that risk at a glance."""
    for sel in tree.find_all(exp.Select):
        for clause_name, clause_label in (("group", "GROUP BY"), ("order", "ORDER BY")):
            clause = sel.args.get(clause_name)
            if not clause:
                continue
            exprs = clause.expressions if clause_name == "group" else clause.expressions
            items = [e.this if isinstance(e, exp.Ordered) else e for e in exprs]
            has_ordinal = any(isinstance(i, exp.Literal) and not i.is_string for i in items)
            has_name = any(not (isinstance(i, exp.Literal) and not i.is_string) for i in items)
            if has_ordinal and has_name:
                yield ("low", f"Mixed ordinal and column name in {clause_label}",
                       f"This {clause_label} mixes a column position (e.g. `1`) with a named column in the "
                       "same clause — easy to misread, and the ordinal still silently shifts if the SELECT "
                       "list changes. Use column names throughout, or positions throughout, not both.")
                return


def _rule_distinct_with_group_by(tree):
    """SELECT DISTINCT ... GROUP BY ... is redundant: GROUP BY already
    collapses each group to one row per SELECT-list combination, so
    DISTINCT on top does nothing except cost an extra dedup pass. Not
    wrong, just dead weight — worth flagging as a no-op."""
    for sel in tree.find_all(exp.Select):
        if sel.args.get("distinct") and sel.args.get("group"):
            yield ("low", "Redundant DISTINCT with GROUP BY",
                   "GROUP BY already collapses rows to one per group, so DISTINCT on top is a no-op that "
                   "just costs an extra deduplication pass. Safe to remove one or the other.")
            return


def _rule_unused_cte(tree):
    """A CTE defined in WITH but never referenced anywhere in the rest of
    the query is either dead code left over from editing, or a typo'd
    table name elsewhere that silently fell back to a real table/CTE
    instead of the intended one. Either way it's worth a flag."""
    with_ = tree.args.get("with_") or tree.args.get("with")
    if not with_:
        return
    cte_names = {c.alias_or_name.lower() for c in with_.expressions if c.alias_or_name}
    if not cte_names:
        return
    referenced = {t.name.lower() for t in tree.find_all(exp.Table)}
    unused = cte_names - referenced
    if unused:
        yield ("low", "CTE defined but never used",
               f"`{next(iter(unused))}` is defined in the WITH clause but never referenced anywhere in the "
               "query — likely dead code from an earlier edit, or a sign a table name elsewhere is misspelled "
               "and silently missed this CTE.")
        return


def _rule_unreferenced_join(tree):
    """A JOINed table whose alias never appears in the SELECT list, WHERE,
    GROUP BY, HAVING, or ORDER BY contributes nothing but row multiplication
    (or de-duplication, for a semi-join-shaped condition) — usually a sign
    the join is leftover from a previous version of the query, or that a
    column that should reference it was typo'd to reference something
    else instead."""
    for sel in tree.find_all(exp.Select):
        from_ = sel.args.get("from_")
        joins = sel.args.get("joins") or []
        if not from_ or not joins:
            continue
        for j in joins:
            tables = list(j.find_all(exp.Table))
            if not tables:
                continue
            alias = tables[0].alias_or_name
            if not alias:
                continue
            other_ons = [oj.args.get("on") for oj in joins if oj is not j and oj.args.get("on")]
            other_parts = list(other_ons)
            for key in ("where", "group", "having", "order"):
                part = sel.args.get(key)
                if part:
                    other_parts.append(part)
            other_parts.extend(sel.expressions)
            used_elsewhere = any(
                c.table == alias for part in other_parts for c in part.find_all(exp.Column)
            )
            if not used_elsewhere:
                yield ("medium", "Joined table never used",
                       f"`{alias}` is joined but its columns are never referenced anywhere else in the "
                       "query (SELECT, WHERE, GROUP BY, HAVING, ORDER BY) — if it's only there to filter or "
                       "multiply rows via the JOIN condition that may be intentional, but it's worth double-"
                       "checking this join is still needed.")
                return


def _rule_ambiguous_column_multi_table(tree):
    """An unqualified column reference in a query that joins 2+ tables is
    only safe if that column name exists in exactly one of them — something
    this tool can't verify without a schema. Even when it happens to work
    today, adding a same-named column to the other table later turns it
    ambiguous and breaks the query with no code change on this side."""
    for sel in tree.find_all(exp.Select):
        from_ = sel.args.get("from_")
        joins = sel.args.get("joins") or []
        if not from_ or len(joins) < 1:
            continue
        if not isinstance(from_.this, exp.Table) or any(not isinstance(j.this, exp.Table) for j in joins):
            continue  # UNNEST/LATERAL/table-valued-function sources aren't schema ambiguity risks
        table_count = 1 + len(joins)
        if table_count < 2:
            continue
        for proj in sel.expressions:
            for c in proj.find_all(exp.Column):
                if not c.table and not (c.find_ancestor(exp.Star)):
                    yield ("low", "Unqualified column with multiple tables joined",
                           f"`{c.sql()}` isn't qualified with a table name, but this query joins "
                           f"{table_count} tables — it works today only because the column happens to exist "
                           "in just one of them. Adding a same-named column to another joined table later "
                           "makes this ambiguous and breaks the query. Qualify it (e.g. `t.{c.sql()}`).")
                    return


def _rule_alias_in_own_over(tree):
    """SELECT ROW_NUMBER() OVER (PARTITION BY rn) AS rn — the alias `rn`
    can't refer to itself inside its own OVER() clause (aliases aren't
    visible within the same SELECT-list expression that defines them), so
    this either errors ("column rn does not exist") or, worse, silently
    resolves to an unrelated column of the same name from the FROM
    tables."""
    for sel in tree.find_all(exp.Select):
        for proj in sel.expressions:
            if not isinstance(proj, exp.Alias):
                continue
            alias_name = proj.alias
            if not alias_name:
                continue
            for win in proj.find_all(exp.Window):
                partition = win.args.get("partition_by") or []
                order = win.args.get("order")
                order_cols = order.expressions if order else []
                for c in list(partition) + list(order_cols):
                    ref = c.this if isinstance(c, exp.Ordered) else c
                    if isinstance(ref, exp.Column) and ref.name.lower() == alias_name.lower():
                        yield ("high", "Alias referenced inside its own OVER()",
                               f"`{alias_name}` is the alias being defined by this expression, but it's also "
                               f"referenced inside its own OVER() clause — aliases aren't visible within the "
                               "expression that defines them, so this either errors or silently resolves to "
                               "an unrelated column of the same name instead.")
                        return


def _rule_self_join_same_alias(tree):
    """`FROM employees JOIN employees ON ...` — the same table referenced
    twice in one FROM/JOIN scope with no distinguishing alias (or the same
    alias reused, which amounts to the same problem). sqlglot's parser
    doesn't validate this — it's semantic, not syntactic — but every real
    engine rejects it outright at execution time (Postgres: "table name
    ... specified more than once"; MySQL: "Not unique table/alias"), so
    this reports "valid" on a query that will fail to run, the same
    category as _rule_aggregate_wraps_window. Only looks at each SELECT's
    own direct FROM + JOIN tables (not tables inside nested subqueries,
    which are a different naming scope and never ambiguous with these)."""
    for sel in tree.find_all(exp.Select):
        from_ = sel.args.get("from_") or sel.args.get("from")
        direct_tables = []
        if from_ and isinstance(from_.this, exp.Table):
            direct_tables.append(from_.this)
        for j in sel.args.get("joins") or []:
            if isinstance(j.this, exp.Table):
                direct_tables.append(j.this)
        seen = set()
        for t in direct_tables:
            key = (t.name.lower(), t.alias_or_name.lower())
            if key in seen:
                yield ("high", "Self-join without a distinguishing alias",
                       f"`{t.name}` is referenced more than once in this FROM/JOIN with the same "
                       f"(or no) alias — every real engine rejects this outright at execution time "
                       "(\"table name specified more than once\"), even though it parses as valid SQL. "
                       "Give each reference to this table a distinct alias.")
                return
            seen.add(key)


def _rule_mixed_agg_no_group_by(tree):
    """SELECT dept, COUNT(*) FROM t (no GROUP BY) isn't just bad style —
    Postgres, BigQuery, and Snowflake reject it outright at execution
    time; MySQL in non-strict mode silently picks an arbitrary row's
    value for `dept` instead. sqlglot's parser doesn't validate this (it's
    a semantic check real engines do, not a syntax rule), so it parses
    as 'valid' even though most engines would refuse to run it.

    Scoped per-SELECT via the nearest-enclosing-Select check (not just
    `tree.find_all`) so a scalar subquery's own aggregate/columns in the
    SELECT list don't get attributed to the outer select, or vice versa.
    Window functions (COUNT(*) OVER (...)) don't require GROUP BY, so
    both aggregates and columns used only inside a window are excluded.
    Same for a column inside an aggregate's own FILTER (WHERE ...) clause
    (COUNT(*) FILTER (WHERE paid)) — that's scoped to the aggregate, not
    a separate grouping dimension."""
    for sel in tree.find_all(exp.Select):
        if sel.args.get("group"):
            continue
        has_plain_agg = any(
            not agg.find_ancestor(exp.Window)
            for proj in sel.expressions
            for agg in proj.find_all(exp.AggFunc)
            if agg.find_ancestor(exp.Select) is sel
        )
        has_bare_column = any(
            not col.find_ancestor(exp.AggFunc, exp.Window, exp.Filter)
            for proj in sel.expressions
            for col in proj.find_all(exp.Column)
            if col.find_ancestor(exp.Select) is sel
        )
        if has_plain_agg and has_bare_column:
            yield ("high", "Aggregate mixed with a non-grouped column",
                   "This mixes an aggregate (e.g. COUNT/SUM/AVG) with a plain column but has no GROUP BY — "
                   "Postgres, BigQuery, and Snowflake reject this outright at execution time; MySQL in "
                   "non-strict mode silently picks an arbitrary row's value instead. Add the column to "
                   "GROUP BY, or wrap it in an aggregate too.")
            return


def _rule_left_join_nullified_by_where(tree):
    """The classic silent-correctness bug: a WHERE clause that compares a
    column from the optional side of an outer join filters out every row
    where that side didn't match — NULL >= anything is never true — so the
    outer join quietly behaves like an INNER JOIN. Scoped per-SELECT
    (including inside CTEs) so a join in one CTE doesn't false-positive
    against an unrelated WHERE in another.

    Covers LEFT (optional side = the joined table), RIGHT (optional side =
    the preceding FROM table — the common single-join case; a RIGHT JOIN
    chained after other joins isn't specially handled), and FULL OUTER
    (optional side = both)."""
    for select in tree.find_all(exp.Select):
        optional_aliases = set()
        from_ = select.args.get("from_") or select.args.get("from")
        main_table = from_.this.alias_or_name if from_ else None
        for j in select.args.get("joins") or []:
            side = j.args.get("side")
            if side == "LEFT":
                optional_aliases.add(j.this.alias_or_name)
            elif side == "RIGHT" and main_table:
                optional_aliases.add(main_table)
            elif side == "FULL":
                optional_aliases.add(j.this.alias_or_name)
                if main_table:
                    optional_aliases.add(main_table)
        if not optional_aliases:
            continue
        where = select.args.get("where")
        if where is None:
            continue
        condition = where.this
        top_level = list(condition.flatten()) if isinstance(condition, exp.And) else [condition]
        # An explicit `alias.col IS NOT NULL` sitting alongside other
        # conditions ANDed at the top level is the standard, intentional way
        # to require a match on that alias (turning the LEFT JOIN into an
        # inner join on purpose) — any other top-level condition on that
        # same alias is guarded by it, not a silent accident.
        guarded_aliases = {
            c.table
            for cond in top_level
            if isinstance(cond, exp.Not) and isinstance(cond.this, exp.Is)
            for c in cond.this.this.find_all(exp.Column)
            if c.table
        }
        for cond in top_level:
            if isinstance(cond, exp.Or):
                continue  # OR-guarded — the safe pattern
            if isinstance(cond, exp.Is):
                continue  # `x IS NULL` — an explicit, intentional NULL check
            if isinstance(cond, exp.Not) and isinstance(cond.this, exp.Is):
                continue  # `x IS NOT NULL` itself — the guard, not the thing being guarded
            for col in cond.find_all(exp.Column):
                if col.table in guarded_aliases:
                    continue
                if col.table in optional_aliases:
                    yield ("high", "WHERE clause nullifies an outer JOIN",
                           f"WHERE filters on `{col.table}.{col.this.this}`, a column from the optional side of an outer join. "
                           "Rows with no match there have NULL here, and NULL compared to anything is never true — "
                           "so this silently drops every non-matching row, turning the outer join into an INNER JOIN. "
                           "Move this condition into the JOIN's ON clause instead, or guard it with an explicit "
                           "IS NULL check if you actually meant to require a match.")
                    return


def _rule_insert_no_columns(tree):
    for ins in tree.find_all(exp.Insert):
        if isinstance(ins.this, exp.Table):
            yield ("medium", "INSERT without a column list",
                   "INSERT INTO t VALUES (...) relies on the table's current column order — add or reorder a column later and this silently writes data into the wrong columns. List the target columns explicitly: INSERT INTO t (col1, col2) VALUES (...).")
            return


def detect_missing_comma(sql: str) -> list[dict]:
    """Token-level scan for a likely missing comma in a SELECT list: two
    bare identifiers back to back (`name amount`) with nothing between
    them. SQL grammar allows an alias without AS (`col alias` means
    `col AS alias`), so sqlglot's parser accepts this silently as valid —
    no syntax error, no AST-level rule can catch it, since the parse tree
    for a genuine `id AS x` and an accidental `name amount` looks identical.

    Deliberately NOT a tree-based rule (see RULES below) — it works on the
    raw token stream instead, scoped strictly to the region between each
    SELECT and its own matching FROM at the same paren depth (tracked via
    a stack, so nested subqueries in the SELECT list get their own scoped
    region). Never scans FROM/JOIN clauses, where `orders o` / `JOIN t t2`
    are completely normal implicit table aliases, not mistakes.

    Returns findings directly (not (severity, title, message) tuples like
    RULES) since each hit needs its own line number in the message."""
    try:
        tokens = Tokenizer().tokenize(sql)
    except Exception:
        return []

    hits = []  # (line, prev_text, tok_text)
    depth = 0
    select_depths = []  # stack: paren depth at which each open SELECT started
    prev = None  # previous token, only meaningful while actively scanning
    for tok in tokens:
        tt = tok.token_type
        if tt == TokenType.L_PAREN:
            depth += 1
            prev = None
            continue
        if tt == TokenType.R_PAREN:
            depth -= 1
            prev = None
            continue
        if tt == TokenType.SEMICOLON:
            select_depths = [d for d in select_depths if d < depth]
            prev = None
            continue
        if tt == TokenType.SELECT:
            select_depths.append(depth)
            prev = None
            continue
        active = bool(select_depths) and depth == select_depths[-1]
        if tt == TokenType.FROM and active:
            select_depths.pop()
            prev = None
            continue
        if active and tt == TokenType.VAR and prev is not None and prev.token_type == TokenType.VAR:
            hits.append((tok.line, prev.text, tok.text))
        prev = tok if active else None

    if not hits:
        return []
    locations = "; ".join(f"line {ln}: `{a}` → `{b}`" for ln, a, b in hits[:8])
    more = f" (+{len(hits) - 8} more)" if len(hits) > 8 else ""
    return [{
        "severity": "high",
        "title": "Possible missing comma",
        "message": (
            "Two bare words sit right next to each other with nothing between them — this parses as a "
            "valid implicit alias (`a b` means `a AS b`), so it's not a syntax error even if you meant two "
            f"separate columns. Check whether a comma is missing before each of these: {locations}{more}."
        ),
    }]


def _cte_names(tree) -> set:
    """Every name introduced by a WITH clause anywhere in the tree — these
    aren't real tables, so schema-aware checks must never flag a reference
    to one as unknown."""
    names = set()
    for select in tree.find_all(exp.Select):
        with_ = select.args.get("with_") or select.args.get("with")
        if with_:
            for cte in with_.expressions:
                if cte.alias_or_name:
                    names.add(cte.alias_or_name.lower())
    return names


def _rule_unknown_table(tree, schema: dict):
    """Only runs when the caller supplies a schema (table name -> column
    list). Flags every table reference that doesn't match anything in it —
    almost always a typo'd or dropped table name, the single most common
    real-world mistake a schema unlocks catching. Case-insensitive; skips
    CTE names, which are never "real" tables. Yields a single consolidated
    finding (like detect_missing_comma) rather than one per table, since
    check_sql's finding-dedup keys on title alone."""
    cte_names = _cte_names(tree)
    schema_lower = {t.lower() for t in schema}
    unknown = []
    for t in tree.find_all(exp.Table):
        name = t.name
        if not name or name.lower() in cte_names or name.lower() in unknown:
            continue
        if name.lower() not in schema_lower:
            unknown.append(name.lower())
    if unknown:
        names = ", ".join(f"`{n}`" for n in unknown)
        yield ("high", "Unknown table",
               f"{names} — doesn't match any table in the schema you provided. Check for a typo or a "
               "table that's been renamed/dropped.")


def _rule_unknown_column(tree, schema: dict):
    """Only runs when a schema is supplied. Flags every qualified column
    reference (`alias.col`) whose alias resolves to a table THAT IS in the
    schema, but whose column name isn't one of that table's known columns —
    a typo'd column name, or one that's been renamed/dropped since the
    query was written. Deliberately skips columns whose table doesn't
    resolve to a known schema table at all (already covered by
    _rule_unknown_table, and guessing here risks false positives on
    unresolvable aliases). Yields a single consolidated finding, same
    reasoning as _rule_unknown_table."""
    schema_lower = {t.lower(): {c.lower() for c in cols} for t, cols in schema.items()}
    alias_to_table = {t.alias_or_name: t.name for t in tree.find_all(exp.Table)}
    unknown = []
    for c in tree.find_all(exp.Column):
        if not c.table:
            continue
        real_table = alias_to_table.get(c.table)
        if not real_table or real_table.lower() not in schema_lower:
            continue
        col_name = c.this.this if hasattr(c.this, "this") else str(c.this)
        key = f"{c.table}.{col_name}"
        if col_name.lower() not in schema_lower[real_table.lower()] and key not in unknown:
            unknown.append(key)
    if unknown:
        refs = ", ".join(f"`{k}`" for k in unknown)
        yield ("high", "Unknown column",
               f"{refs} — the schema you provided doesn't have a column by that name on that table. Check "
               "for a typo or a column that's been renamed/dropped.")


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
    _rule_between_date_literals,
    _rule_order_by_ordinal,
    _rule_scalar_subquery_no_limit,
    _rule_update_from_no_join_condition,
    _rule_mixed_agg_no_group_by,
    _rule_aggregate_wraps_window,
    _rule_null_equality,
    _rule_offset_no_order,
    _rule_mixed_ordinal_and_name,
    _rule_distinct_with_group_by,
    _rule_unused_cte,
    _rule_unreferenced_join,
    _rule_ambiguous_column_multi_table,
    _rule_alias_in_own_over,
    _rule_self_join_same_alias,
]

SEV_WEIGHT = {"high": 30, "medium": 12, "low": 5}

# Common statement-starting keywords, used to catch beginner typos like
# "SELECTT" or "UPDTE" that a parser alone reports confusingly late.
STATEMENT_STARTERS = [
    "select", "insert", "update", "delete", "with", "create", "merge",
    "alter", "drop", "truncate", "explain", "grant", "revoke", "set", "show",
]


def _has_unsound_decorrelation_risk(tree) -> bool:
    """sqlglot's optimizer can decorrelate a subquery that combines an
    equality correlation (e.g. `e3.department_id = e.department_id`) with a
    second, independent correlated condition (e.g. `e3.salary > e.salary`)
    into a rewrite that checks each condition against the whole group
    separately instead of requiring one single row to satisfy both —
    confirmed wrong two different ways: an aggregate whose comparison
    silently gets dropped into a bogus outer MAX() filter, and an IN/NOT IN
    subquery where the equality-target and the comparison get decomposed
    into independent EXISTS-shaped checks, which can wrongly include rows
    that never actually satisfy both conditions on the same underlying row
    (verified with a concrete data example, not just structurally). EXISTS/
    NOT EXISTS don't have this problem — they only ever need one row to
    satisfy the whole conjunction, and confirmed correct by direct testing —
    so this check is deliberately scoped to skip subqueries used that way.
    Given how easy real correlated queries hit this shape (dept-relative
    rankings, running totals, "more than N in my group" checks), suppressing
    the optimizer suggestion outright for it is safer than trying to predict
    exactly when sqlglot's rewrite will or won't be sound."""
    for sub_sel in tree.find_all(exp.Select):
        if sub_sel.find_ancestor(exp.Exists):
            continue
        where = sub_sel.args.get("where")
        if not where:
            continue
        own_aliases = {t.alias_or_name for t in (sub_sel.args.get("from_") or sub_sel.args.get("from")).find_all(exp.Table)} if (sub_sel.args.get("from_") or sub_sel.args.get("from")) else set()
        conditions = list(where.this.flatten()) if isinstance(where.this, exp.And) else [where.this]
        has_correlated_equality = False
        has_correlated_non_equality = False
        for cond in conditions:
            cols = list(cond.find_all(exp.Column))
            if not any(c.table and c.table not in own_aliases for c in cols):
                continue
            if isinstance(cond, exp.EQ):
                has_correlated_equality = True
            else:
                has_correlated_non_equality = True
        # Multiple pure-equality correlations (a real multi-column join key)
        # are provably sound — verified via direct testing. The risk is
        # specifically a MIX of an equality correlation (used as the
        # decorrelation's GROUP BY / join key) with a separate non-equality
        # correlated condition (which the optimizer can't fold into that
        # same key correctly).
        if has_correlated_equality and has_correlated_non_equality:
            return True
    return False


def _left_join_optional_alias_pairs(tree):
    """Mirror _rule_left_join_nullified_by_where's own alias derivation, so
    the execution verifier below tests the exact same (main, optional)
    pairs the rule considered when it fired."""
    pairs = []
    for select in tree.find_all(exp.Select):
        from_ = select.args.get("from_") or select.args.get("from")
        main_table = from_.this.alias_or_name if from_ else None
        if not main_table:
            continue
        for j in select.args.get("joins") or []:
            side = j.args.get("side")
            if side == "LEFT":
                pairs.append((main_table, j.this.alias_or_name))
            elif side == "RIGHT":
                pairs.append((j.this.alias_or_name, main_table))
            elif side == "FULL":
                pairs.append((main_table, j.this.alias_or_name))
    return pairs


def _left_join_survives_when_unmatched(tree, main_alias, optional_alias) -> str:
    """Execution-based second opinion for the nullified-outer-join rule:
    build synthetic data where the main table has exactly one row and the
    joined table has NO matching row at all (the exact scenario the rule
    worries about), then check whether that unmatched main row survives
    the whole query. If it does, the WHERE clause is NULL-safe (e.g.
    COALESCE-guarded) and the rule's finding is a false positive — confirmed
    via a real, concrete gap: `WHERE COALESCE(o.status, 'none') != 'x'`
    still gets flagged by AST inspection alone, even though it can never
    actually drop an unmatched row. 'filtered_out' is deliberately NOT
    treated as confirmation of a bug — an intentional `IS NOT NULL`
    semi-join guard produces identical behavior to a genuine accidental
    nullify from data alone, so this can only ever suppress, never confirm."""
    alias_to_table = {t.alias_or_name: t.name for t in tree.find_all(exp.Table)}
    main_table = alias_to_table.get(main_alias)
    optional_table = alias_to_table.get(optional_alias)
    if not main_table or not optional_table:
        return "inconclusive"
    columns_by_table, unresolved = _schema_columns_for_verification(tree)
    if unresolved or len(columns_by_table) > 4:
        return "inconclusive"
    conn = sqlite3.connect(":memory:")
    try:
        cur = conn.cursor()
        for table, cols in columns_by_table.items():
            cols = sorted(cols)
            if not cols:
                continue
            col_defs = ", ".join(f'"{c}"' for c in cols)
            cur.execute(f'CREATE TABLE "{table}" ({col_defs})')
        conn.commit()
        main_cols = sorted(columns_by_table.get(main_table, ()))
        if main_cols:
            placeholders = ", ".join("?" for _ in main_cols)
            cur.execute(f'INSERT INTO "{main_table}" VALUES ({placeholders})', [111] * len(main_cols))
        conn.commit()  # optional_table stays empty -> guarantees no match
        sqlglot_logger = logging.getLogger("sqlglot")
        prev_level = sqlglot_logger.level
        sqlglot_logger.setLevel(logging.ERROR)
        try:
            sql = tree.sql(dialect="sqlite")
        finally:
            sqlglot_logger.setLevel(prev_level)
        rows = cur.execute(sql).fetchall()
        return "survives" if len(rows) > 0 else "filtered_out"
    except Exception:
        return "inconclusive"
    finally:
        conn.close()


def _left_join_nullify_finding_is_false_positive(tree) -> bool:
    if not isinstance(tree, exp.Select):
        return False
    for main_alias, optional_alias in _left_join_optional_alias_pairs(tree):
        if _left_join_survives_when_unmatched(tree, main_alias, optional_alias) == "survives":
            return True
    return False


def _schema_columns_for_verification(tree):
    """Map each real table name referenced in the query to the set of its
    columns the query actually touches, resolving aliases (including
    self-join aliases like e2/e3/e4 all pointing at the same table).
    Returns (columns_by_table, unresolved) — unresolved is True if some
    column couldn't be confidently attributed to a table (e.g. an
    unqualified column with 2+ tables in scope), which should abort
    verification rather than guess wrong."""
    alias_to_table = {}
    for t in tree.find_all(exp.Table):
        alias_to_table[t.alias_or_name] = t.name
    real_tables = set(alias_to_table.values())
    columns_by_table = defaultdict(set)
    unresolved = False
    for c in tree.find_all(exp.Column):
        name = c.this.this if hasattr(c.this, "this") else str(c.this)
        if c.table:
            real = alias_to_table.get(c.table)
            if real:
                columns_by_table[real].add(name)
            else:
                unresolved = True
        elif len(real_tables) == 1:
            columns_by_table[next(iter(real_tables))].add(name)
        else:
            unresolved = True
    return columns_by_table, unresolved


def _sqlite_verify_equivalent(orig_tree, opt_tree) -> str:
    """Second, independent line of defense against an unsound optimizer
    rewrite, on top of _has_unsound_decorrelation_risk: actually run both
    the original and optimized query against a small synthetic in-memory
    SQLite dataset and compare results. This catches unsound shapes the
    structural heuristic didn't anticipate — verification, not pattern
    matching — but only for the subset of SQL SQLite can execute (no
    ARRAY_AGG/UNNEST-based rewrites, no dialect-specific functions), so a
    great many queries will come back "inconclusive". That's fine: this is
    strictly an ADDITIVE safety net. Returns "different" (confirmed unsound
    — the caller must suppress), "equivalent", or "inconclusive" (couldn't
    verify either way — caller falls back to its other checks, never
    treated as a green light on its own)."""
    if not isinstance(orig_tree, exp.Select):
        return "inconclusive"  # only SELECT is safe to run read-only, throwaway data
    columns_by_table, unresolved = _schema_columns_for_verification(orig_tree)
    if unresolved or not columns_by_table or len(columns_by_table) > 4:
        return "inconclusive"
    conn = sqlite3.connect(":memory:")
    try:
        cur = conn.cursor()
        rows_per_table = 6
        for table, cols in columns_by_table.items():
            cols = sorted(cols)
            if not cols:
                continue
            col_defs = ", ".join(f'"{c}"' for c in cols)
            cur.execute(f'CREATE TABLE "{table}" ({col_defs})')
            placeholders = ", ".join("?" for _ in cols)
            for i in range(rows_per_table):
                # Deterministic (not random) synthetic data: row 0 is
                # all-NULL to exercise NULL-handling paths, other rows get
                # small, column-varying integers so equality AND comparison
                # predicates both produce a genuine mix of matches and
                # mismatches across rows of the same table.
                vals = [
                    None if i == 0 else (i * 31 + (zlib.crc32(c.encode()) % 97)) % 4 + 1
                    for c in cols
                ]
                cur.execute(f'INSERT INTO "{table}" VALUES ({placeholders})', vals)
        conn.commit()
        # sqlglot logs (doesn't raise) for constructs it can't translate to
        # sqlite (PIVOT, named table-alias columns, etc.) — expected and
        # frequent here since most real-world SQL uses functions sqlite
        # doesn't have; silence it so "inconclusive" stays truly silent.
        sqlglot_logger = logging.getLogger("sqlglot")
        prev_level = sqlglot_logger.level
        sqlglot_logger.setLevel(logging.ERROR)
        try:
            orig_sql = orig_tree.sql(dialect="sqlite")
            opt_sql = opt_tree.sql(dialect="sqlite")
        finally:
            sqlglot_logger.setLevel(prev_level)
        orig_rows = cur.execute(orig_sql).fetchall()
        opt_rows = cur.execute(opt_sql).fetchall()
        return "equivalent" if Counter(orig_rows) == Counter(opt_rows) else "different"
    except Exception:
        return "inconclusive"
    finally:
        conn.close()


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


_CONTENT_TOKENS = {TokenType.VAR, TokenType.NUMBER, TokenType.STRING, TokenType.R_PAREN}


def _syntax_error_hint(sql: str, line: int | None, col: int | None) -> str | None:
    """General hint for a query that failed to parse at all — separate from
    detect_missing_comma(), which only ever runs on SQL that already parsed
    successfully. Covers the two most common causes of a hard syntax error:
    unbalanced parentheses anywhere in the query, and two "content" tokens
    (a value/column/closing-paren) sitting right next to each other with no
    comma/operator/keyword between them near the failing position — e.g.
    `ch.level + 1\\n  ch.path || ...` (missing comma between two SELECT-list
    expressions) or `total_orders\\n  ch.category_id` (missing comma in a
    GROUP BY list). Best-effort: returns None rather than guessing wrong."""
    opens, closes = sql.count("("), sql.count(")")
    if opens != closes:
        kind = "closing ')'" if opens > closes else "opening '('"
        return f"Mismatched parentheses ({opens} '(' vs {closes} ')') — you're likely missing a {kind} somewhere."

    try:
        tokens = Tokenizer().tokenize(sql)
    except Exception:
        return None
    if not (line and col):
        return None
    # Find the token nearest the reported error position, then look just
    # behind it — the actual missing punctuation is usually one token
    # earlier than where the parser gave up.
    near = [t for t in tokens if t.line == line]
    if not near:
        return None
    idx_in_all = tokens.index(min(near, key=lambda t: abs(t.col - col)))
    for i in range(max(0, idx_in_all - 3), idx_in_all + 1):
        if i == 0:
            continue
        prev, cur = tokens[i - 1], tokens[i]
        if prev.token_type in _CONTENT_TOKENS and cur.token_type in _CONTENT_TOKENS:
            return (f"Line {cur.line}: `{prev.text}` is immediately followed by `{cur.text}` with nothing "
                    "between them — a comma or operator is likely missing there.")
    return None


def check_sql(sql: str, dialect: str = "bigquery", target_dialect: str | None = None, dbt_mode: bool = False,
              schema: dict | None = None) -> dict:
    """Run the full QueryDoctor diagnosis on one blob of SQL (may contain
    multiple statements). Returns the same shape the /api/check endpoint
    sends back, minus the HTTP/rate-limit wrapper. Never raises — parse
    errors and optimizer failures degrade into fields on the result dict.

    `schema`, if supplied, is a {table_name: [column_name, ...]} dict —
    entirely optional, and off by default. When present, it unlocks two
    extra checks (_rule_unknown_table, _rule_unknown_column) that no purely
    syntactic rule can make: does this table/column actually exist. These
    run as a separate pass, not through the static RULES list, since they
    need the schema argument every other rule doesn't take."""
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
                "hint": _typo_hint(sql) or _syntax_error_hint(sql, line, col),
            },
        }

    # 2. Lint
    findings = []
    for tree in trees:
        for rule in RULES:
            for sev, title, msg in rule(tree):
                if not any(f["title"] == title for f in findings):
                    findings.append({"severity": sev, "title": title, "message": msg})
        if schema:
            for rule in (_rule_unknown_table, _rule_unknown_column):
                for sev, title, msg in rule(tree, schema):
                    if not any(f["title"] == title for f in findings):
                        findings.append({"severity": sev, "title": title, "message": msg})
    findings.extend(detect_missing_comma(sql))
    if any(f["title"] == "WHERE clause nullifies an outer JOIN" for f in findings):
        # Execution-based second opinion, additive only: never adds a
        # finding, only removes one already confirmed as a false positive
        # (see _left_join_nullify_finding_is_false_positive's docstring).
        if any(_left_join_nullify_finding_is_false_positive(t) for t in trees):
            findings = [f for f in findings if f["title"] != "WHERE clause nullifies an outer JOIN"]
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
        if any(_has_unsound_decorrelation_risk(t) for t in trees):
            raise ValueError("skip optimization: unsound decorrelation risk")
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
            # Safety tripwire: sqlglot's optimizer can decorrelate certain
            # subqueries incorrectly (e.g. a COUNT(*) subquery whose
            # correlated predicate is a comparison rather than a plain
            # equality join key gets its filter silently dropped/relocated —
            # confirmed via direct testing, not hypothetical). Never present
            # a rewrite that introduces lint findings the original didn't
            # have; that's a sign the rewrite likely changed the query's
            # semantics, not just its shape.
            orig_titles = {f["title"] for f in findings}
            opt_findings = []
            for ot in opt_trees:
                for rule in RULES:
                    for sev, title, msg in rule(ot):
                        if not any(f["title"] == title for f in opt_findings):
                            opt_findings.append({"severity": sev, "title": title})
            new_titles = {f["title"] for f in opt_findings} - orig_titles
            if not new_titles:
                # Second, independent line of defense: actually execute
                # original vs. optimized against synthetic SQLite data
                # rather than only pattern-matching. Only ever suppresses
                # (a confirmed "different" result set) — "inconclusive"
                # (most queries, since SQLite can't run every rewrite
                # shape) never blocks a suggestion the checks above allowed.
                if not any(
                    _sqlite_verify_equivalent(t, ot) == "different"
                    for t, ot in zip(trees, opt_trees)
                ):
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
