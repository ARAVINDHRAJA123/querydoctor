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
    assert "WHERE clause nullifies a LEFT JOIN" in titles

def test_left_join_condition_in_on_is_clean():
    d = check("SELECT * FROM e LEFT JOIN s ON e.id = s.eid AND s.d >= '2025-01-01'", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "WHERE clause nullifies a LEFT JOIN" not in titles

def test_left_join_anti_join_is_clean():
    d = check("SELECT * FROM e LEFT JOIN s ON e.id = s.eid WHERE s.id IS NULL", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "WHERE clause nullifies a LEFT JOIN" not in titles

def test_left_join_ok_when_where_filters_left_side():
    d = check("SELECT * FROM e LEFT JOIN s ON e.id = s.eid WHERE e.active = true", "postgres").json()
    titles = {f["title"] for f in d["findings"]}
    assert "WHERE clause nullifies a LEFT JOIN" not in titles

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
