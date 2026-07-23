"""QueryDoctor API tests — run the release battery on every push."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import pytest
from fastapi.testclient import TestClient
import main
from main import app

main.RATE_LIMIT = 10**9  # the battery makes ~120 calls from one client IP

client = TestClient(app)

def check(sql, dialect="bigquery", target=None):
    r = client.post("/api/check", json={"sql": sql, "dialect": dialect, "target_dialect": target})
    return r

# ── validity battery ──
VALID = [
    ("bigquery", "SELECT 1"),
    ("bigquery", "SELECT x FROM t QUALIFY ROW_NUMBER() OVER (ORDER BY x) = 1"),
    ("bigquery", "SELECT ARRAY_AGG(STRUCT(a, b) ORDER BY a LIMIT 5) FROM t GROUP BY c"),
    ("bigquery", "SELECT x FROM t, UNNEST(arr) AS x"),
    ("bigquery", "SELECT SUM(x) OVER (PARTITION BY g ORDER BY d ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) FROM t"),
    ("bigquery", "MERGE t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET v = s.v WHEN NOT MATCHED THEN INSERT (id, v) VALUES (s.id, s.v)"),
    ("postgres", "SELECT j->>'name' FROM t, LATERAL jsonb_array_elements(t.data) AS j"),
    ("postgres", "SELECT DISTINCT ON (cust) cust, ts FROM orders ORDER BY cust, ts DESC"),
    ("postgres", "SELECT COUNT(*) FILTER (WHERE paid) FROM orders"),
    ("postgres", "WITH RECURSIVE nums AS (SELECT 1 AS n UNION ALL SELECT n+1 FROM nums WHERE n < 10) SELECT * FROM nums"),
    ("snowflake", "SELECT f.value FROM t, LATERAL FLATTEN(input => t.arr) f"),
    ("snowflake", "SELECT * FROM sales PIVOT (SUM(amt) FOR month IN ('Jan','Feb'))"),
    ("spark", "SELECT explode(items) AS item FROM orders"),
    ("tsql", "SELECT TOP 5 name FROM users ORDER BY score DESC"),
    ("oracle", "SELECT employee_id FROM employees START WITH manager_id IS NULL CONNECT BY PRIOR employee_id = manager_id"),
    ("duckdb", "SELECT [x + 1 FOR x IN [1,2,3]]"),
]
INVALID = [
    ("bigquery", "SELCT 1"),
    ("mysql", "SELECT FROM"),
    ("postgres", "SELECT SUM(x FROM t"),
    ("bigquery", "SELECT a FROM t JOIN ON x = y"),
]

@pytest.mark.parametrize("dialect,sql", VALID)
def test_valid(dialect, sql):
    d = check(sql, dialect).json()
    assert d["ok"] and d["valid"], d

@pytest.mark.parametrize("dialect,sql", INVALID)
def test_invalid(dialect, sql):
    d = check(sql, dialect).json()
    assert d["ok"] and not d["valid"], d

def test_typo_hint():
    d = check("selectt @ from t").json()
    assert "did you mean SELECT" in (d["syntax_error"]["hint"] or "")

def test_lint_rules_fire():
    d = check("SELECT * FROM orders o JOIN customers c WHERE c.name LIKE '%x' LIMIT 10").json()
    titles = {f["title"] for f in d["findings"]}
    assert {"SELECT * used", "JOIN without ON condition", "LIMIT without ORDER BY", "LIKE starts with %"} <= titles
    assert d["score"] < 50

def test_dangerous_delete():
    d = check("DELETE FROM t").json()
    assert d["findings"][0]["severity"] == "high"

def test_having_without_aggregate():
    d = check("SELECT dept, count(*) FROM t GROUP BY dept HAVING dept = 'eng'").json()
    titles = {f["title"] for f in d["findings"]}
    assert "HAVING used without an aggregate" in titles

def test_having_with_aggregate_is_clean():
    d = check("SELECT dept, count(*) c FROM t GROUP BY dept HAVING count(*) > 5").json()
    titles = {f["title"] for f in d["findings"]}
    assert "HAVING used without an aggregate" not in titles

def test_group_by_missing_column():
    d = check("SELECT user_id, name, count(*) FROM t GROUP BY user_id").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Selected column missing from GROUP BY" in titles

def test_group_by_complete_is_clean():
    d = check("SELECT user_id, count(*) FROM t GROUP BY user_id").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Selected column missing from GROUP BY" not in titles

def test_order_by_in_subquery_no_limit():
    d = check("SELECT * FROM (SELECT a FROM t ORDER BY a) x").json()
    titles = {f["title"] for f in d["findings"]}
    assert "ORDER BY in a subquery without LIMIT" in titles

def test_order_by_in_subquery_with_limit_is_clean():
    d = check("SELECT * FROM (SELECT a FROM t ORDER BY a LIMIT 10) x").json()
    titles = {f["title"] for f in d["findings"]}
    assert "ORDER BY in a subquery without LIMIT" not in titles

def test_case_no_else():
    d = check("SELECT CASE WHEN x = 1 THEN 'a' END FROM t").json()
    titles = {f["title"] for f in d["findings"]}
    assert "CASE without ELSE" in titles

def test_case_with_else_is_clean():
    d = check("SELECT CASE WHEN x = 1 THEN 'a' ELSE 'b' END FROM t").json()
    titles = {f["title"] for f in d["findings"]}
    assert "CASE without ELSE" not in titles

def test_join_on_1_equals_1():
    d = check("SELECT * FROM a JOIN b ON 1 = 1").json()
    titles = {f["title"] for f in d["findings"]}
    assert "JOIN ON 1=1" in titles

def test_join_on_true():
    d = check("SELECT * FROM a JOIN b ON TRUE").json()
    titles = {f["title"] for f in d["findings"]}
    assert "JOIN ON true" in titles

def test_join_on_real_condition_is_clean():
    d = check("SELECT * FROM a JOIN b ON a.id = b.id").json()
    titles = {f["title"] for f in d["findings"]}
    assert "JOIN ON 1=1" not in titles and "JOIN ON true" not in titles

def test_coalesce_in_equality():
    d = check("SELECT * FROM t WHERE COALESCE(a, '') = COALESCE(b, '')").json()
    titles = {f["title"] for f in d["findings"]}
    assert "COALESCE in an equality comparison" in titles

def test_window_no_order():
    d = check("SELECT ROW_NUMBER() OVER (PARTITION BY g) FROM t").json()
    titles = {f["title"] for f in d["findings"]}
    assert "ROW_NUMBER/LAG/LEAD without ORDER BY" in titles

def test_window_with_order_is_clean():
    d = check("SELECT ROW_NUMBER() OVER (PARTITION BY g ORDER BY d) FROM t").json()
    titles = {f["title"] for f in d["findings"]}
    assert "ROW_NUMBER/LAG/LEAD without ORDER BY" not in titles

def test_insert_no_columns():
    d = check("INSERT INTO t VALUES (1, 2)").json()
    titles = {f["title"] for f in d["findings"]}
    assert "INSERT without a column list" in titles

def test_insert_with_columns_is_clean():
    d = check("INSERT INTO t (a, b) VALUES (1, 2)").json()
    titles = {f["title"] for f in d["findings"]}
    assert "INSERT without a column list" not in titles

def test_dbt_mode_strips_jinja():
    r = client.post("/api/check", json={"sql": "SELECT * FROM {{ ref('orders') }} WHERE {{ var('x') }} = 1", "dbt_mode": True})
    d = r.json()
    assert d["ok"] and d["valid"], d

def test_dbt_mode_off_syntax_errors_on_jinja():
    r = client.post("/api/check", json={"sql": "SELECT * FROM {{ ref('orders') }}", "dbt_mode": False})
    d = r.json()
    assert d["ok"] and not d["valid"], d

def test_fail_on_severity_ignored_without_api_key():
    d = check("DELETE FROM t").json()
    assert d.get("blocked") is False

def test_billing_create_order_blocked_when_not_configured():
    r = client.post("/api/billing/create-order", json={"plan": "monthly"})
    assert r.status_code == 503

def test_billing_verify_blocked_when_not_configured():
    r = client.post("/api/billing/verify", json={
        "razorpay_order_id": "o1", "razorpay_payment_id": "p1",
        "razorpay_signature": "s1", "plan": "monthly",
    })
    assert r.status_code == 503

def test_billing_verify_rejects_bad_signature(monkeypatch):
    import billing
    monkeypatch.setattr(billing, "RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setattr(billing, "RAZORPAY_KEY_SECRET", "secret123")
    r = client.post("/api/billing/verify", json={
        "razorpay_order_id": "o1", "razorpay_payment_id": "p1",
        "razorpay_signature": "forged", "plan": "monthly",
    })
    assert r.status_code == 402

def test_billing_verify_provisions_key_on_valid_signature(monkeypatch):
    import billing, hmac, hashlib
    monkeypatch.setattr(billing, "RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setattr(billing, "RAZORPAY_KEY_SECRET", "secret123")
    sig = hmac.new(b"secret123", b"o1|p1", hashlib.sha256).hexdigest()
    provisioned = {}
    def fake_set(self, data):
        provisioned.update(data)
    class FakeDoc:
        def set(self, data):
            provisioned.update(data)
    class FakeCollection:
        def document(self, key):
            provisioned["_key"] = key
            return FakeDoc()
    class FakeClient:
        def collection(self, name):
            return FakeCollection()
    monkeypatch.setattr(billing, "_client", lambda: FakeClient())
    r = client.post("/api/billing/verify", json={
        "razorpay_order_id": "o1", "razorpay_payment_id": "p1",
        "razorpay_signature": sig, "plan": "monthly",
    })
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["api_key"].startswith("qd_live_")
    assert provisioned["plan"] == "monthly"

def test_check_rejects_invalid_bearer_key():
    r = client.post("/api/check", json={"sql": "SELECT 1"}, headers={"Authorization": "Bearer qd_live_bogus"})
    assert r.status_code == 200
    assert r.json()["ok"] is True

def test_left_join_nullified_by_where():
    d = check("SELECT * FROM e LEFT JOIN s ON e.id = s.eid WHERE s.d >= '2025-01-01'", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "WHERE clause nullifies an outer JOIN" in titles

def test_left_join_condition_in_on_is_clean():
    d = check("SELECT * FROM e LEFT JOIN s ON e.id = s.eid AND s.d >= '2025-01-01'", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "WHERE clause nullifies an outer JOIN" not in titles

def test_left_join_anti_join_is_clean():
    d = check("SELECT * FROM e LEFT JOIN s ON e.id = s.eid WHERE s.id IS NULL", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "WHERE clause nullifies an outer JOIN" not in titles

def test_left_join_ok_when_where_filters_left_side():
    d = check("SELECT * FROM e LEFT JOIN s ON e.id = s.eid WHERE e.active = true", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "WHERE clause nullifies an outer JOIN" not in titles

def test_right_join_nullified_by_where():
    d = check("SELECT * FROM a RIGHT JOIN b ON a.id = b.id WHERE a.status = 'x'", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "WHERE clause nullifies an outer JOIN" in titles

def test_full_join_nullified_by_where_either_side():
    d1 = check("SELECT * FROM a FULL OUTER JOIN b ON a.id = b.id WHERE a.status = 'x'", "postgres").json()
    d2 = check("SELECT * FROM a FULL OUTER JOIN b ON a.id = b.id WHERE b.status = 'x'", "postgres").json()
    assert "WHERE clause nullifies an outer JOIN" in {f["title"] for f in d1["findings"]}
    assert "WHERE clause nullifies an outer JOIN" in {f["title"] for f in d2["findings"]}

def test_between_bare_date_literals():
    d = check("SELECT * FROM t WHERE d BETWEEN '2024-01-01' AND '2024-01-31'", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "BETWEEN with bare date literals" in titles

def test_between_numeric_is_clean():
    d = check("SELECT * FROM t WHERE n BETWEEN 1 AND 10", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "BETWEEN with bare date literals" not in titles

def test_between_datetime_literals_is_clean():
    d = check("SELECT * FROM t WHERE d BETWEEN '2024-01-01 00:00:00' AND '2024-01-31 23:59:59'", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "BETWEEN with bare date literals" not in titles

def test_order_by_ordinal():
    d = check("SELECT a, b FROM t ORDER BY 1", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "ORDER BY column position" in titles

def test_order_by_column_name_is_clean():
    d = check("SELECT a, b FROM t ORDER BY a", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "ORDER BY column position" not in titles

def test_scalar_subquery_no_limit():
    d = check("SELECT id, (SELECT name FROM t2 WHERE t2.id = t1.id) AS n FROM t1", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Scalar subquery without LIMIT 1" in titles

def test_scalar_subquery_with_limit_is_clean():
    d = check("SELECT id, (SELECT name FROM t2 WHERE t2.id = t1.id LIMIT 1) AS n FROM t1", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Scalar subquery without LIMIT 1" not in titles

def test_scalar_subquery_aggregate_is_clean():
    d = check("SELECT id, (SELECT MAX(amt) FROM t2 WHERE t2.id = t1.id) AS m FROM t1", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Scalar subquery without LIMIT 1" not in titles

def test_update_from_missing_join_condition():
    d = check("UPDATE t1 SET x = t2.y FROM t2 WHERE t1.active = true", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "UPDATE ... FROM without a join condition" in titles

def test_update_from_with_join_condition_is_clean():
    d = check("UPDATE t1 SET x = t2.y FROM t2 WHERE t1.id = t2.id", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "UPDATE ... FROM without a join condition" not in titles

def test_update_from_no_where_only_flags_existing_rule():
    d = check("UPDATE t1 SET x = t2.y FROM t2", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert titles == {"UPDATE without WHERE"}

def test_missing_comma_detected():
    d = check("SELECT id, name amount, created_at FROM orders", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Possible missing comma" in titles

def test_missing_comma_reports_line_number():
    sql = "SELECT\n  id,\n  name\n  amount,\n  created_at\nFROM orders"
    d = check(sql, "postgres").json()
    msg = next(f["message"] for f in d["findings"] if f["title"] == "Possible missing comma")
    assert "line 3" in msg or "Line 3" in msg or "line 4" in msg

def test_explicit_as_alias_is_clean():
    d = check("SELECT name AS amount, id AS x FROM t", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Possible missing comma" not in titles

def test_from_join_implicit_aliases_are_clean():
    d = check("SELECT o.id, o.total FROM orders o JOIN customers c ON o.cust_id = c.id", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Possible missing comma" not in titles

def test_missing_comma_across_semicolon_statements():
    d = check("SELECT a, b FROM t1; SELECT x y FROM t2", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Possible missing comma" in titles

def test_mixed_agg_no_group_by():
    d = check("SELECT dept, COUNT(*) FROM employees", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Aggregate mixed with a non-grouped column" in titles

def test_mixed_agg_with_group_by_is_clean():
    d = check("SELECT dept, COUNT(*) FROM employees GROUP BY dept", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Aggregate mixed with a non-grouped column" not in titles

def test_agg_alone_is_clean():
    d = check("SELECT COUNT(*) FROM employees", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Aggregate mixed with a non-grouped column" not in titles

def test_agg_filter_clause_is_clean():
    d = check("SELECT COUNT(*) FILTER (WHERE paid) FROM orders", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Aggregate mixed with a non-grouped column" not in titles

def test_agg_window_function_is_clean():
    d = check("SELECT dept, COUNT(*) OVER (PARTITION BY dept) FROM employees", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Aggregate mixed with a non-grouped column" not in titles

def test_aggregate_wraps_window():
    d = check("SELECT SUM(AVG(x) OVER (PARTITION BY y)) FROM t GROUP BY y", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Aggregate function wraps a window function" in titles

def test_window_wraps_aggregate_is_clean():
    d = check("SELECT SUM(x) OVER (PARTITION BY y) FROM t", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Aggregate function wraps a window function" not in titles

def test_empty_input():
    r = check("   ")
    assert r.status_code == 422 and r.json()["ok"] is False

# ── all-pairs translation ──
SRC = {
    "bigquery":  "SELECT SAFE_DIVIDE(a, b) AS r, FORMAT_DATE('%Y-%m', d) AS ym FROM t WHERE d >= '2026-01-01'",
    "postgres":  "SELECT COALESCE(a, 0)::text, NOW() - INTERVAL '7 days' FROM t LIMIT 5",
    "mysql":     "SELECT IFNULL(a, 0), DATE_FORMAT(d, '%Y-%m'), GROUP_CONCAT(tag) FROM t GROUP BY a, d",
    "snowflake": "SELECT IFF(a > 0, 'pos', 'neg'), TO_VARCHAR(d, 'YYYY-MM') FROM t",
    "spark":     "SELECT date_format(d, 'yyyy-MM'), collect_list(x) FROM t GROUP BY d",
    "sqlite":    "SELECT IFNULL(a, 0), strftime('%Y-%m', d) FROM t",
    "tsql":      "SELECT TOP 10 ISNULL(a, 0), FORMAT(d, 'yyyy-MM') FROM t ORDER BY a",
    "oracle":    "SELECT NVL(a, 0), TO_CHAR(d, 'YYYY-MM') FROM t WHERE ROWNUM <= 10",
    "duckdb":    "SELECT COALESCE(a, 0), strftime(d, '%Y-%m') FROM t USING SAMPLE 10",
    "redshift":  "SELECT NVL(a, 0), TO_CHAR(d, 'YYYY-MM'), LISTAGG(tag, ',') FROM t GROUP BY a, d",
}

@pytest.mark.parametrize("src", list(SRC))
@pytest.mark.parametrize("dst", list(SRC))
def test_translation_pair(src, dst):
    if src == dst:
        pytest.skip("same dialect")
    d = check(SRC[src], src, dst).json()
    assert d["valid"], f"{src} source didn't parse"
    tr = d["translated"] or ""
    assert tr.strip() and not tr.startswith("-- Couldn't translate"), f"{src}->{dst}: {tr[:80]}"

def test_null_equality_eq():
    d = check("SELECT * FROM t WHERE x = NULL", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "`= NULL` never matches" in titles

def test_null_equality_neq():
    d = check("SELECT * FROM t WHERE x != NULL", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "`!= NULL` never matches" in titles

def test_null_equality_is_null_is_clean():
    d = check("SELECT * FROM t WHERE x IS NULL", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert not any("never matches" in t for t in titles)

def test_offset_without_order_by():
    d = check("SELECT * FROM t LIMIT 10 OFFSET 20", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "OFFSET without ORDER BY" in titles

def test_offset_with_order_by_is_clean():
    d = check("SELECT * FROM t ORDER BY id LIMIT 10 OFFSET 20", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "OFFSET without ORDER BY" not in titles

def test_mixed_ordinal_and_name_group_by():
    d = check("SELECT a, b, count(*) FROM t GROUP BY 1, b", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Mixed ordinal and column name in GROUP BY" in titles

def test_pure_ordinal_group_by_not_flagged_by_new_rule():
    d = check("SELECT a, b, count(*) FROM t GROUP BY 1, 2", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Mixed ordinal and column name in GROUP BY" not in titles

def test_distinct_with_group_by():
    d = check("SELECT DISTINCT a, b FROM t GROUP BY a, b", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Redundant DISTINCT with GROUP BY" in titles

def test_distinct_without_group_by_is_clean():
    d = check("SELECT DISTINCT a, b FROM t", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Redundant DISTINCT with GROUP BY" not in titles

def test_unused_cte():
    d = check("WITH x AS (SELECT 1 AS a) SELECT * FROM t", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "CTE defined but never used" in titles

def test_used_cte_is_clean():
    d = check("WITH x AS (SELECT 1 AS a) SELECT * FROM x", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "CTE defined but never used" not in titles

def test_unreferenced_joined_table():
    d = check("SELECT a.x FROM a JOIN b ON a.id = b.id", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Joined table never used" in titles

def test_referenced_joined_table_is_clean():
    d = check("SELECT a.x, b.y FROM a JOIN b ON a.id = b.id", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Joined table never used" not in titles

def test_ambiguous_unqualified_column_multi_table():
    d = check("SELECT x FROM a JOIN b ON a.id = b.id", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Unqualified column with multiple tables joined" in titles

def test_qualified_column_multi_table_is_clean():
    d = check("SELECT a.x FROM a JOIN b ON a.id = b.id", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Unqualified column with multiple tables joined" not in titles

def test_unnest_source_not_flagged_ambiguous():
    d = check("SELECT x FROM t, UNNEST(arr) AS x", "bigquery").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Unqualified column with multiple tables joined" not in titles

def test_alias_referenced_in_own_over():
    d = check("SELECT ROW_NUMBER() OVER (PARTITION BY rn) AS rn FROM t", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Alias referenced inside its own OVER()" in titles

def test_over_partition_by_other_column_is_clean():
    d = check("SELECT ROW_NUMBER() OVER (PARTITION BY dept) AS rn FROM t", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Alias referenced inside its own OVER()" not in titles

def test_left_join_where_is_not_null_guard_is_clean():
    # `o.id IS NOT NULL` after a LEFT JOIN is the standard, intentional way
    # to require a match — parses as NOT(IS(...)) in sqlglot, which the
    # nullified-join rule must recognize as a safe guard, not the bug.
    d = check("SELECT o.id FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.id IS NOT NULL", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "WHERE clause nullifies an outer JOIN" not in titles

def test_left_join_where_guarded_by_is_not_null_on_same_alias_is_clean():
    # A condition on the optional-side alias ANDed alongside an explicit
    # `alias IS NOT NULL` guard on that same alias is intentional (turns
    # the LEFT JOIN into an inner join on purpose) — not the silent bug.
    d = check(
        "SELECT o.id FROM customers c LEFT JOIN orders o ON o.customer_id = c.id "
        "WHERE o.amount > 100 AND o.id IS NOT NULL", "postgres"
    ).json()
    titles = {f["title"] for f in d["findings"]}
    assert "WHERE clause nullifies an outer JOIN" not in titles

def test_left_join_where_without_guard_still_flagged():
    d = check("SELECT o.id FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.amount > 100", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "WHERE clause nullifies an outer JOIN" in titles

def test_optimizer_suppresses_unsound_decorrelation_rewrite():
    # sqlglot's optimizer can decorrelate a COUNT(*) subquery whose
    # correlated predicate is a comparison (not a plain equality join key)
    # incorrectly — it silently drops the comparison from the count and
    # relocates it to an outer WHERE using MAX(), which changes the query's
    # results (confirmed by direct testing, not hypothetical). QueryDoctor
    # must never present that rewrite as a trustworthy suggestion.
    sql = (
        "SELECT e.employee_id, "
        "(SELECT COUNT(*) FROM employees e3 WHERE e3.department_id = e.department_id "
        "AND e3.salary > e.salary) AS rank_in_dept "
        "FROM employees e"
    )
    d = check(sql, "postgres").json()
    assert d["optimized"] is None

@pytest.mark.parametrize("agg,op", [
    ("COUNT(*)", ">"), ("COUNT(*)", "<"), ("COUNT(*)", "!="),
    ("SUM(e3.salary)", ">"), ("AVG(e3.salary)", ">"),
    ("MIN(e3.salary)", ">"), ("MAX(e3.salary)", ">"),
])
def test_optimizer_suppresses_all_aggregate_comparison_decorrelation_variants(agg, op):
    # The same unsound-decorrelation bug reproduces for every aggregate
    # function (not just COUNT) and every comparison operator — verified
    # broadly rather than assuming COUNT/`>` was the only broken shape.
    sql = (
        f"SELECT e.employee_id, (SELECT {agg} FROM employees e3 "
        f"WHERE e3.department_id = e.department_id AND e3.salary {op} e.salary) AS r "
        "FROM employees e"
    )
    d = check(sql, "postgres").json()
    assert d["optimized"] is None

def test_optimizer_allows_sound_exists_decorrelation():
    # EXISTS with a correlated comparison predicate is decorrelated via a
    # different (array-based) code path that IS sound — the tripwire must
    # not suppress this just because the rewrite happens to use a LEFT
    # JOIN guarded by an IS NOT NULL check.
    sql = (
        "SELECT e.employee_id FROM employees e WHERE EXISTS "
        "(SELECT 1 FROM employees e3 WHERE e3.department_id = e.department_id "
        "AND e3.salary > e.salary)"
    )
    d = check(sql, "postgres").json()
    assert d["optimized"] is not None

def test_optimizer_allows_sound_in_subquery_with_filter():
    sql = (
        "SELECT e.employee_id FROM employees e WHERE e.department_id IN "
        "(SELECT e3.department_id FROM employees e3 WHERE e3.salary > 50000)"
    )
    d = check(sql, "postgres").json()
    assert d["optimized"] is not None

def test_optimizer_suppresses_in_subquery_equality_plus_comparison():
    # A THIRD distinct unsound rewrite, worse than the aggregate one: an
    # IN subquery combining an equality correlation (dept) with a separate
    # comparison correlation (salary) gets decomposed into two independent
    # array checks — verified with concrete sample data that this wrongly
    # includes rows that never satisfy both conditions on the same
    # underlying row (e.g. every employee ends up "IN" their own
    # never-matching set, because the id-array is no longer filtered by
    # the salary condition once decorrelated).
    sql = (
        "SELECT e.employee_id FROM employees e WHERE e.employee_id IN "
        "(SELECT e3.employee_id FROM employees e3 WHERE e3.department_id = e.department_id "
        "AND e3.salary > e.salary)"
    )
    d = check(sql, "postgres").json()
    assert d["optimized"] is None

def test_optimizer_suppresses_not_in_subquery_equality_plus_comparison():
    sql = (
        "SELECT e.employee_id FROM employees e WHERE e.employee_id NOT IN "
        "(SELECT e3.employee_id FROM employees e3 WHERE e3.department_id = e.department_id "
        "AND e3.salary > e.salary)"
    )
    d = check(sql, "postgres").json()
    assert d["optimized"] is None

def test_optimizer_allows_pure_multi_equality_correlation():
    # Multiple correlated conditions are fine as long as they're all plain
    # equalities (a real multi-column join key) — only a MIX of equality
    # and non-equality correlation is the unsound shape.
    sql = (
        "SELECT e.employee_id, (SELECT COUNT(*) FROM employees e3 "
        "WHERE e3.department_id = e.department_id AND e3.manager_id = e.manager_id) AS r "
        "FROM employees e"
    )
    d = check(sql, "postgres").json()
    assert d["optimized"] is not None

def test_sqlite_verify_independently_confirms_known_unsound_rewrite():
    # The structural heuristic already suppresses this one, so this test
    # exercises the SQLite execution oracle directly to prove it's a real,
    # independent second line of defense — not dead code that happens to
    # agree with the structural check every time.
    from backend.lint_engine import _sqlite_verify_equivalent
    import sqlglot
    from sqlglot.optimizer import optimize
    sql = (
        "SELECT e.employee_id, (SELECT COUNT(*) FROM employees e3 "
        "WHERE e3.department_id = e.department_id AND e3.salary > e.salary) AS r "
        "FROM employees e"
    )
    t = sqlglot.parse_one(sql, read="postgres")
    ot = optimize(t.copy(), dialect="postgres")
    assert _sqlite_verify_equivalent(t, ot) == "different"

def test_sqlite_verify_confirms_sound_rewrite_as_equivalent():
    from backend.lint_engine import _sqlite_verify_equivalent
    import sqlglot
    from sqlglot.optimizer import optimize
    sql = "SELECT * FROM employees WHERE 1=1 AND salary > 5000"
    t = sqlglot.parse_one(sql, read="postgres")
    ot = optimize(t.copy(), dialect="postgres")
    assert _sqlite_verify_equivalent(t, ot) == "equivalent"

def test_sqlite_verify_inconclusive_for_array_based_rewrite():
    # EXISTS decorrelates via ARRAY_AGG/UNNEST, which sqlite can't run —
    # must come back "inconclusive", never crash, never wrongly suppress.
    from backend.lint_engine import _sqlite_verify_equivalent
    import sqlglot
    from sqlglot.optimizer import optimize
    sql = (
        "SELECT e.employee_id FROM employees e WHERE EXISTS "
        "(SELECT 1 FROM employees e3 WHERE e3.department_id = e.department_id "
        "AND e3.salary > e.salary)"
    )
    t = sqlglot.parse_one(sql, read="postgres")
    ot = optimize(t.copy(), dialect="postgres")
    assert _sqlite_verify_equivalent(t, ot) == "inconclusive"

def test_left_join_coalesce_guarded_where_is_clean():
    # COALESCE(o.status, 'none') is NULL-safe — an unmatched row's o.status
    # becomes 'none', which never equals 'cancelled', so it survives the
    # filter. The AST-only rule can't see this; the execution-based second
    # opinion (_left_join_nullify_finding_is_false_positive) should.
    d = check(
        "SELECT c.id, o.id FROM customers c LEFT JOIN orders o ON o.customer_id = c.id "
        "WHERE COALESCE(o.status, 'none') != 'cancelled'", "postgres"
    ).json()
    titles = {f["title"] for f in d["findings"]}
    assert "WHERE clause nullifies an outer JOIN" not in titles

def test_left_join_genuine_nullify_still_flagged_after_execution_check():
    d = check("SELECT o.id FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.amount > 100", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "WHERE clause nullifies an outer JOIN" in titles

def test_unknown_table_flagged_with_schema():
    r = client.post("/api/check", json={
        "sql": "SELECT * FROM employeez", "dialect": "postgres",
        "db_schema": {"employees": ["employee_id", "salary"]},
    })
    d = r.json()
    titles = {f["title"] for f in d["findings"]}
    assert "Unknown table" in titles

def test_unknown_column_flagged_with_schema():
    r = client.post("/api/check", json={
        "sql": "SELECT e.employee_id, e.salery FROM employees e", "dialect": "postgres",
        "db_schema": {"employees": ["employee_id", "salary"]},
    })
    d = r.json()
    titles = {f["title"] for f in d["findings"]}
    assert "Unknown column" in titles

def test_known_table_and_column_clean_with_schema():
    r = client.post("/api/check", json={
        "sql": "SELECT e.employee_id, e.salary FROM employees e", "dialect": "postgres",
        "db_schema": {"employees": ["employee_id", "salary"]},
    })
    d = r.json()
    titles = {f["title"] for f in d["findings"]}
    assert "Unknown table" not in titles and "Unknown column" not in titles

def test_no_schema_means_no_schema_checks():
    r = client.post("/api/check", json={"sql": "SELECT * FROM employeez"})
    d = r.json()
    titles = {f["title"] for f in d["findings"]}
    assert "Unknown table" not in titles

def test_schema_too_large_rejected():
    big_schema = {f"table{i}": ["a"] for i in range(250)}
    r = client.post("/api/check", json={"sql": "SELECT 1", "db_schema": big_schema})
    assert r.status_code == 422

def test_unknown_table_and_column_consolidated_into_one_finding_each():
    r = client.post("/api/check", json={
        "sql": "SELECT a.x, b.y FROM tablea a JOIN tableb b ON a.id = b.id", "dialect": "postgres",
        "db_schema": {"employees": ["employee_id"]},
    })
    d = r.json()
    unknown_table_findings = [f for f in d["findings"] if f["title"] == "Unknown table"]
    assert len(unknown_table_findings) == 1
    assert "tablea" in unknown_table_findings[0]["message"] and "tableb" in unknown_table_findings[0]["message"]

def test_self_join_no_alias_flagged():
    d = check("SELECT * FROM employees JOIN employees ON employees.manager_id = employees.employee_id", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Self-join without a distinguishing alias" in titles

def test_self_join_same_alias_reused_flagged():
    d = check("SELECT * FROM employees e JOIN employees e ON e.manager_id = e.employee_id", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Self-join without a distinguishing alias" in titles

def test_self_join_distinct_aliases_is_clean():
    d = check("SELECT e1.employee_id FROM employees e1 JOIN employees e2 ON e1.manager_id = e2.employee_id", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Self-join without a distinguishing alias" not in titles

def test_leaked_alias_qualifier_flagged():
    d = check(
        "SELECT e.id, (SELECT AVG(e.e2.salary) FROM emp e2 WHERE e2.id = e.id) AS r FROM emp e",
        "bigquery"
    ).json()
    titles = {f["title"] for f in d["findings"]}
    assert "Table alias leaked into a column qualifier" in titles

def test_normal_qualified_column_not_flagged_as_leaked_alias():
    d = check("SELECT e2.salary FROM emp e2", "bigquery").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Table alias leaked into a column qualifier" not in titles

def test_real_bigquery_dataset_qualifier_not_flagged():
    d = check("SELECT x FROM `mydataset.mytable`", "bigquery").json()
    titles = {f["title"] for f in d["findings"]}
    assert "Table alias leaked into a column qualifier" not in titles

def test_scalar_subquery_aggregate_with_optimizer_style_alias_is_clean():
    # The optimizer's own qualify pass wraps a bare AggFunc in an Alias
    # (`AVG(x) AS _col_0`) — the rule must unwrap it, not treat an aliased
    # aggregate as if it weren't one (a real false positive found while
    # investigating a user-reported query).
    d = check(
        "SELECT e.id, (SELECT AVG(x) AS agg_col FROM t2 WHERE t2.id = e.id) AS r FROM t e",
        "bigquery"
    ).json()
    titles = {f["title"] for f in d["findings"]}
    assert "Scalar subquery without LIMIT 1" not in titles

def test_leaked_alias_qualifier_consolidates_multiple_distinct_leaks():
    # Real bug found during a broader audit: the rule's own dedup keyed on
    # the leaked alias name alone, so a second distinct leaked reference
    # under the SAME leaked alias (e.g. e.e2.x and e.e3.y both leaking "e")
    # was silently dropped even before reaching check_sql's outer dedup.
    d = check(
        "SELECT e.id, (SELECT AVG(e.e2.x) FROM t2 e2) AS r1, (SELECT AVG(e.e3.y) FROM t3 e3) AS r2 FROM emp e",
        "bigquery"
    ).json()
    leaked = [f for f in d["findings"] if f["title"] == "Table alias leaked into a column qualifier"]
    assert len(leaked) == 1
    assert "e.e2.x" in leaked[0]["message"] and "e.e3.y" in leaked[0]["message"]

def test_noqa_blanket_suppresses_everything():
    d = check("SELECT * FROM t -- noqa", "postgres").json()
    assert d["findings"] == []
    assert "SELECT * used" in d["suppressed_by_noqa"]

def test_noqa_specific_title_suppresses_only_that_finding():
    d = check("SELECT * FROM t WHERE x = NULL -- noqa: SELECT * used", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "SELECT * used" not in titles
    assert any("never matches" in t for t in titles)

def test_noqa_case_insensitive_title_match():
    d = check("SELECT * FROM t -- noqa: select * used", "postgres").json()
    assert d["findings"] == []

def test_noqa_misspelled_title_is_a_no_op():
    d = check("SELECT * FROM t -- noqa: not a real rule name", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "SELECT * used" in titles

def test_noqa_inside_string_literal_is_not_a_directive():
    d = check("SELECT '-- noqa' AS x, * FROM t", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "SELECT * used" in titles

def test_noqa_does_not_suppress_syntax_errors():
    d = check("SELCT * FROM t -- noqa", "postgres").json()
    assert d["ok"] and not d["valid"]

def test_no_noqa_comment_leaves_findings_and_suppressed_list_untouched():
    d = check("SELECT * FROM t", "postgres").json()
    assert d["suppressed_by_noqa"] == []
    assert any(f["title"] == "SELECT * used" for f in d["findings"])

def test_ddl_auto_derives_schema_and_catches_unknown_column():
    r = client.post("/api/check", json={
        "sql": "SELECT e.employee_id, e.salery FROM employees e", "dialect": "postgres",
        "ddl": "CREATE TABLE employees (employee_id INT, salary DECIMAL(10,2));",
    })
    d = r.json()
    titles = {f["title"] for f in d["findings"]}
    assert "Unknown column" in titles

def test_ddl_and_explicit_schema_merge_with_explicit_winning():
    ddl = "CREATE TABLE employees (employee_id INT, salary DECIMAL(10,2));"
    r = client.post("/api/check", json={
        "sql": "SELECT e.bonus FROM employees e", "dialect": "postgres", "ddl": ddl,
        "db_schema": {"employees": ["employee_id", "salary", "bonus"]},
    })
    d = r.json()
    titles = {f["title"] for f in d["findings"]}
    assert "Unknown column" not in titles

def test_malformed_ddl_statement_reported_as_warning_not_crash():
    r = client.post("/api/check", json={"sql": "SELECT 1", "dialect": "postgres", "ddl": "CREATE TABLE BROKEN (((;"})
    assert r.status_code == 200
    d = r.json()
    assert d.get("schema_warnings")

def test_ddl_with_one_bad_statement_still_derives_the_good_ones():
    ddl = (
        "CREATE TABLE employees (employee_id INT);"
        "CREATE TABLE BROKEN SYNTAX HERE (((;"
        "CREATE TABLE departments (id INT);"
    )
    r = client.post("/api/check", json={
        "sql": "SELECT d.bogus FROM departments d", "dialect": "postgres", "ddl": ddl,
    })
    d = r.json()
    titles = {f["title"] for f in d["findings"]}
    assert "Unknown column" in titles  # departments table was still captured despite the broken middle statement

def test_no_ddl_no_schema_means_no_schema_warnings_key():
    r = client.post("/api/check", json={"sql": "SELECT 1"})
    d = r.json()
    assert "schema_warnings" not in d

def test_auto_fix_null_equality():
    r = check("SELECT * FROM t WHERE x = NULL", "postgres").json()
    assert r["auto_fixed_titles"] == ["`= NULL` never matches"]
    assert "IS NULL" in r["auto_fixed_sql"] and "= NULL" not in r["auto_fixed_sql"]

def test_auto_fix_not_null_equality():
    r = check("SELECT * FROM t WHERE x != NULL", "postgres").json()
    assert r["auto_fixed_titles"] == ["`!= NULL` never matches"]
    assert "IS NULL" in r["auto_fixed_sql"]

def test_auto_fix_redundant_distinct():
    r = check("SELECT DISTINCT a, b FROM t GROUP BY a, b", "postgres").json()
    assert "Redundant DISTINCT with GROUP BY" in r["auto_fixed_titles"]
    assert "DISTINCT" not in r["auto_fixed_sql"]

def test_auto_fix_case_no_else():
    r = check("SELECT CASE WHEN x = 1 THEN 'a' END FROM t", "postgres").json()
    assert "CASE without ELSE" in r["auto_fixed_titles"]
    assert "ELSE" in r["auto_fixed_sql"]

def test_auto_fix_leaked_alias_qualifier():
    r = check("SELECT e.id, (SELECT AVG(e.e2.x) FROM t2 e2) AS r FROM emp e", "bigquery").json()
    assert "Table alias leaked into a column qualifier" in r["auto_fixed_titles"]
    assert "e.e2.x" not in r["auto_fixed_sql"]

def test_auto_fix_none_offered_for_clean_query():
    r = check("SELECT 1", "postgres").json()
    assert r["auto_fixed_sql"] is None
    assert r["auto_fixed_titles"] == []

def test_auto_fix_not_offered_for_noqa_suppressed_finding():
    r = check("SELECT * FROM t WHERE x = NULL -- noqa", "postgres").json()
    assert r["auto_fixed_sql"] is None
    assert r["auto_fixed_titles"] == []

def test_auto_fix_result_reparses_cleanly_and_finding_is_gone():
    r = check("SELECT * FROM t WHERE x = NULL", "postgres").json()
    r2 = check(r["auto_fixed_sql"], "postgres").json()
    assert r2["valid"]
    assert "`= NULL` never matches" not in {f["title"] for f in r2["findings"]}

def test_auto_fix_combines_multiple_independent_fixes_in_one_query():
    sql = (
        "SELECT DISTINCT e.id, CASE WHEN e.status = 1 THEN 'active' END AS s "
        "FROM employees e WHERE e.manager_id = NULL GROUP BY e.id, e.status"
    )
    r = check(sql, "bigquery").json()
    assert set(r["auto_fixed_titles"]) == {
        "Redundant DISTINCT with GROUP BY", "CASE without ELSE", "`= NULL` never matches"
    }
    r2 = check(r["auto_fixed_sql"], "bigquery").json()
    assert r2["valid"]

def test_cli_vendored_lint_engine_is_in_sync():
    # cli/lint_engine.py is a synced copy (see its own top comment and
    # cli/sync_vendor.sh) because a real pip install can't import across
    # into ../backend — confirmed by testing an actual install, which
    # crashed before this vendoring was added. This test is the guard
    # against someone editing backend/lint_engine.py and forgetting to
    # re-run sync_vendor.sh, which would otherwise drift silently.
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "backend", "lint_engine.py")) as f:
        real = f.read()
    with open(os.path.join(here, "..", "cli", "lint_engine.py")) as f:
        vendored = f.read()
    # Strip the vendored copy's leading docstring note (everything up to
    # and including the first blank line after its closing triple-quote)
    # before comparing — that note doesn't exist in the real file.
    marker = '"""\n\n'
    idx = vendored.find(marker)
    assert idx != -1, "vendored copy is missing its expected header note"
    vendored_body = vendored[idx + len(marker):]
    assert vendored_body == real, (
        "cli/lint_engine.py is out of sync with backend/lint_engine.py — "
        "run cli/sync_vendor.sh and commit the result"
    )

def test_ddl_parse_error_has_no_ansi_escape_codes():
    # Real bug found while testing the frontend: sqlglot's raw exception
    # text can include a second line with ANSI terminal escape codes
    # highlighting the failing token — fine for the CLI's terminal output,
    # garbled and literal in a JSON API response rendered in a browser.
    from backend.lint_engine import parse_ddl_to_schema
    schema, warnings = parse_ddl_to_schema("CREATE TABLE BROKEN (((;", dialect="postgres")
    assert schema == {}
    assert len(warnings) == 1
    assert "\x1b" not in warnings[0]
    assert "\n" not in warnings[0]

def test_compare_endpoint_detects_a_diff():
    r = client.post("/api/compare", json={"sql_a": "SELECT a FROM t", "sql_b": "SELECT a, b FROM t", "dialect": "postgres"})
    d = r.json()
    assert d["ok"] and d["comparable"] and d["detailed"]
    assert any("added" in diff["message"] for diff in d["differences"])

def test_compare_endpoint_syntax_error_reports_reason():
    r = client.post("/api/compare", json={"sql_a": "SELCT 1", "sql_b": "SELECT 1", "dialect": "postgres"})
    d = r.json()
    assert d["comparable"] is False
    assert d["reason"] == "a_invalid"

def test_compare_endpoint_rejects_oversized_input():
    r = client.post("/api/compare", json={"sql_a": "SELECT 1", "sql_b": "x" * 200_001, "dialect": "postgres"})
    assert r.status_code == 422
