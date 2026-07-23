"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { formatResult } = require("../src/formatResult");

test("clean query reports score and no findings", () => {
  const out = formatResult({ ok: true, valid: true, score: 100, findings: [] }, "query.sql");
  assert.match(out, /Health score: 100\/100/);
  assert.match(out, /Clean/);
});

test("findings are listed with severity and message", () => {
  const out = formatResult(
    { ok: true, valid: true, score: 70, findings: [{ severity: "high", title: "SELECT * used", message: "reads too much" }] },
    "query.sql"
  );
  assert.match(out, /\[HIGH\] SELECT \* used/);
  assert.match(out, /reads too much/);
});

test("syntax error shows line/col and hint, not a score", () => {
  const out = formatResult(
    { ok: true, valid: false, syntax_error: { line: 1, col: 5, message: "bad token", hint: "did you mean SELECT?" } },
    "query.sql"
  );
  assert.match(out, /line 1, col 5/);
  assert.match(out, /did you mean SELECT\?/);
  assert.doesNotMatch(out, /Health score/);
});

test("top-level error (ok: false) is reported plainly", () => {
  const out = formatResult({ ok: false, error: "Paste some SQL first." }, "query.sql");
  assert.match(out, /Paste some SQL first\./);
});

test("suppressed_by_noqa is surfaced", () => {
  const out = formatResult(
    { ok: true, valid: true, score: 100, findings: [], suppressed_by_noqa: ["SELECT * used"] },
    "query.sql"
  );
  assert.match(out, /suppressed by noqa: SELECT \* used/);
});

test("auto-fixable findings point at the apply-fix command", () => {
  const out = formatResult(
    {
      ok: true, valid: true, score: 70,
      findings: [{ severity: "high", title: "`= NULL` never matches", message: "..." }],
      auto_fixed_sql: "SELECT * FROM t WHERE x IS NULL",
      auto_fixed_titles: ["`= NULL` never matches"],
    },
    "query.sql"
  );
  assert.match(out, /Auto-fixable/);
  assert.match(out, /Apply Safe Auto-Fixes/);
});

test("optimizer suggestion points at the show-optimized command", () => {
  const out = formatResult(
    { ok: true, valid: true, score: 100, findings: [], optimized: "SELECT 1" },
    "query.sql"
  );
  assert.match(out, /Optimizer suggestion available/);
  assert.match(out, /Show Optimized Query/);
});
