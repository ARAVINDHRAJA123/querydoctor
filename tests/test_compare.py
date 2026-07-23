"""SQL Compare tests — every category, plus the fallback boundaries."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from lint_engine import compare_sql


def diff_messages(a, b, dialect="postgres"):
    r = compare_sql(a, b, dialect=dialect)
    return r, [d["message"] for d in r.get("differences", [])]


def test_identical_after_formatting():
    r, diffs = diff_messages("SELECT a, b FROM t", "SELECT   a,   b FROM t")
    assert r["comparable"] and r["detailed"] and r["identical"]
    assert diffs == []


def test_column_added():
    r, diffs = diff_messages("SELECT a FROM t", "SELECT a, b FROM t")
    assert any("b" in d and "added" in d for d in diffs)


def test_column_removed():
    r, diffs = diff_messages("SELECT a, b FROM t", "SELECT a FROM t")
    assert any("b" in d and "removed" in d for d in diffs)


def test_column_renamed():
    r, diffs = diff_messages("SELECT a AS x FROM t", "SELECT a AS y FROM t")
    assert any("aliased" in d for d in diffs)


def test_table_added_via_join():
    r, diffs = diff_messages("SELECT a.x FROM a", "SELECT a.x FROM a JOIN b ON a.id=b.id")
    assert any("b" in d and "added" in d for d in diffs)


def test_table_removed():
    r, diffs = diff_messages("SELECT a.x FROM a JOIN b ON a.id=b.id", "SELECT a.x FROM a")
    assert any("b" in d and "removed" in d for d in diffs)


def test_join_type_changed():
    r, diffs = diff_messages("SELECT a.x FROM a JOIN b ON a.id=b.id", "SELECT a.x FROM a LEFT JOIN b ON a.id=b.id")
    assert any("changed from" in d for d in diffs)


def test_join_condition_changed():
    r, diffs = diff_messages(
        "SELECT a.x FROM a JOIN b ON a.id=b.id",
        "SELECT a.x FROM a JOIN b ON a.id=b.id AND a.y=b.y",
    )
    assert any("join condition changed" in d.lower() for d in diffs)


def test_where_added():
    r, diffs = diff_messages("SELECT a FROM t", "SELECT a FROM t WHERE x=1")
    assert any("WHERE clause was added" in d for d in diffs)


def test_where_removed():
    r, diffs = diff_messages("SELECT a FROM t WHERE x=1", "SELECT a FROM t")
    assert any("WHERE clause was removed" in d for d in diffs)


def test_where_condition_added():
    r, diffs = diff_messages("SELECT a FROM t WHERE x=1", "SELECT a FROM t WHERE x=1 AND y=2")
    assert any("condition added" in d.lower() for d in diffs)


def test_where_condition_removed():
    r, diffs = diff_messages("SELECT a FROM t WHERE x=1 AND y=2", "SELECT a FROM t WHERE x=1")
    assert any("condition removed" in d.lower() for d in diffs)


def test_group_by_changed():
    r, diffs = diff_messages("SELECT a FROM t GROUP BY a", "SELECT a FROM t GROUP BY a, b")
    assert any("GROUP BY changed" in d for d in diffs)


def test_group_by_reordered_is_labeled_reordered_not_changed():
    r, diffs = diff_messages("SELECT a FROM t GROUP BY a, b", "SELECT a FROM t GROUP BY b, a")
    assert any("reordered" in d for d in diffs)


def test_order_by_changed():
    r, diffs = diff_messages("SELECT a FROM t ORDER BY a", "SELECT a FROM t ORDER BY a DESC")
    assert any("ORDER BY changed" in d for d in diffs)


def test_limit_added():
    r, diffs = diff_messages("SELECT a FROM t", "SELECT a FROM t LIMIT 10")
    assert any("LIMIT 10 was added" in d for d in diffs)


def test_limit_changed():
    r, diffs = diff_messages("SELECT a FROM t LIMIT 5", "SELECT a FROM t LIMIT 10")
    assert any("Changed from LIMIT 5 to LIMIT 10" in d for d in diffs)


# ── Fallback boundaries: never a wrong semantic claim ──

def test_syntax_error_on_a_is_not_comparable():
    r = compare_sql("SELCT a FROM t", "SELECT a FROM t", dialect="postgres")
    assert r["comparable"] is False
    assert r["reason"] == "a_invalid"


def test_syntax_error_on_b_is_not_comparable():
    r = compare_sql("SELECT a FROM t", "SELECT FROM", dialect="postgres")
    assert r["comparable"] is False
    assert r["reason"] == "b_invalid"


def test_empty_input_is_not_comparable():
    r = compare_sql("", "SELECT 1", dialect="postgres")
    assert r["comparable"] is False


def test_cte_falls_back_to_non_detailed():
    r = compare_sql(
        "WITH x AS (SELECT 1 a) SELECT a FROM x",
        "WITH x AS (SELECT 2 a) SELECT a FROM x",
        dialect="postgres",
    )
    assert r["comparable"] is True
    assert r["detailed"] is False
    assert r["identical"] is False


def test_cte_fallback_still_detects_identical():
    r = compare_sql(
        "WITH x AS (SELECT 1 a) SELECT a FROM x",
        "WITH x AS (SELECT   1   a) SELECT  a FROM x",
        dialect="postgres",
    )
    assert r["detailed"] is False
    assert r["identical"] is True


def test_multi_statement_falls_back_to_non_detailed():
    r = compare_sql("SELECT 1; SELECT 2", "SELECT 1; SELECT 3", dialect="postgres")
    assert r["comparable"] is True
    assert r["detailed"] is False


def test_non_select_falls_back_to_non_detailed():
    r = compare_sql("INSERT INTO t VALUES (1)", "INSERT INTO t VALUES (2)", dialect="postgres")
    assert r["comparable"] is True
    assert r["detailed"] is False


def test_compare_never_raises_on_garbage_input():
    # No exception, whatever comes back
    compare_sql("!!! not sql at all", "@@@ also not sql", dialect="postgres")
    compare_sql(None if False else "", "", dialect="postgres")


# ── Plain-English explainer ──
from lint_engine import explain_sql

def test_explain_simple_select():
    r = explain_sql("SELECT a, b FROM t WHERE x > 1 GROUP BY a ORDER BY a LIMIT 5", dialect="postgres")
    assert r["explainable"]
    assert "`a`, `b`" in r["summary"] and "grouped by" in r["summary"] and "first 5" in r["summary"]

def test_explain_join():
    r = explain_sql("SELECT * FROM a JOIN b ON a.id = b.id", dialect="postgres")
    assert r["explainable"]
    assert "joined with" in r["summary"]

def test_explain_aggregate_no_group_by():
    r = explain_sql("SELECT COUNT(*) FROM t", dialect="postgres")
    assert r["explainable"]
    assert "single aggregate result" in r["summary"]

def test_explain_cte_falls_back():
    r = explain_sql("WITH x AS (SELECT 1) SELECT * FROM x", dialect="postgres")
    assert not r["explainable"]
    assert r["reason"] == "too_complex"

def test_explain_syntax_error_falls_back():
    r = explain_sql("SELCT 1", dialect="postgres")
    assert not r["explainable"]
    assert r["reason"] == "invalid"
