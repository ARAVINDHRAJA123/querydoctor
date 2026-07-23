"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { checkSql, MAX_SQL_CHARS } = require("../src/checkSql");

test("rejects empty input without a network call", async () => {
  await assert.rejects(() => checkSql(""), /empty/);
  await assert.rejects(() => checkSql("   "), /empty/);
});

test("rejects oversized input without a network call", async () => {
  const huge = "a".repeat(MAX_SQL_CHARS + 1);
  await assert.rejects(() => checkSql(huge), /limit/);
});

// These hit the real, live QueryDoctor API — same integration point the
// extension actually uses in production, not a mock standing in for it.
// Skipped automatically if there's no network in this environment.
test("real API call: clean query", { skip: process.env.QD_SKIP_NETWORK_TESTS ? "no network" : false }, async () => {
  const result = await checkSql("SELECT 1", { dialect: "postgres" });
  assert.equal(result.ok, true);
  assert.equal(result.valid, true);
  assert.equal(result.score, 100);
});

test("real API call: known finding fires", { skip: process.env.QD_SKIP_NETWORK_TESTS ? "no network" : false }, async () => {
  const result = await checkSql("SELECT * FROM t WHERE x = NULL", { dialect: "postgres" });
  assert.equal(result.valid, true);
  const titles = result.findings.map((f) => f.title);
  assert.ok(titles.includes("`= NULL` never matches"));
  assert.ok(result.auto_fixed_sql.includes("IS NULL"));
});

test("real API call: syntax error surfaces a hint", { skip: process.env.QD_SKIP_NETWORK_TESTS ? "no network" : false }, async () => {
  const result = await checkSql("SELCT 1", { dialect: "postgres" });
  assert.equal(result.valid, false);
  assert.match(result.syntax_error.hint, /did you mean SELECT/);
});
