# Case Study: QueryDoctor

## The problem
SQL tools assume you already know SQL. Beginners paste a query, get
`Expected table name but got <Token token_type: TokenType.WHERE...>`, and give up.
I had already built an AI-powered SQL reviewer (sql-review-agent) for engineers — this
project asked the opposite question: **how much of a "SQL doctor" can you build with
zero LLM calls, zero API keys, and zero marginal cost per check?**

## Key decisions

**No AI, on purpose.** The engine is sqlglot (parser/transpiler) plus hand-written lint
rules over its AST. Deterministic output, no hallucination risk, runs free forever, and
the privacy story is trivial. Choosing *not* to use an LLM where it adds nothing is
itself an engineering decision — and a differentiator next to my LLM-based reviewer.

**Design for day-one beginners.** A parser reports where parsing *failed*, which is often
not where the *mistake* is. So QueryDoctor adds a pre-parse layer: the first token is
fuzzy-matched against statement keywords, turning token soup into
*"'SELCT' isn't a SQL command — did you mean SELECT?"* — plus a caret pointing at the
failing column, and every lint finding written in plain English with the fix.

**Health score as UX.** 25 severity-weighted lint checks collapse into a 0–100 score on
an animated ring. People who can't parse a lint list understand "34/100, needs attention."

## Hard problems worth remembering

1. **Honest boundaries.** Grammar-valid but semantically-wrong SQL (a window function in
   WHERE) passes the syntax gate — same as every static linter. Knowing and stating the
   tool's boundary beats pretending it doesn't exist.
2. **Optimizer noise.** sqlglot's optimizer adds cosmetic aliases (`1 AS "1"`), so naive
   before/after comparison shows a "suggestion" for every query. The card only appears
   when the rewrite differs *after* canonicalising away aliases, quoting, and whitespace —
   so `WHERE 1=1 AND a > 2+3` → `WHERE a > 5` shows, and no-ops stay silent.
3. **My own rate limiter caught my test suite.** An early battery tripped the
   60-checks-per-10-min limit — a genuine sign the limiter works, fixed by lifting the
   limit inside tests only.

## Verification
254-case pytest suite in CI: validity battery including rare per-dialect features
(QUALIFY, LATERAL FLATTEN, CONNECT BY, recursive CTEs, MERGE, window frames), **all 90
dialect-translation pairs**, lint rules, typo hints, and error paths.

## Results
Live at https://querydoctor-616665622891.asia-south1.run.app — 10 dialects, optimizer
suggestions, installable PWA, on-device history, ~₹0/month to run.
