# QueryDoctor for VS Code (v0.1, not published)

Runs the current file (or your selection) through QueryDoctor's live API
and shows the diagnosis — health score, findings, syntax errors — in an
Output panel, without leaving the editor.

## What this deliberately is NOT (yet)

This is **not** inline squiggly-underline diagnostics. Most of
QueryDoctor's 34 lint rules don't carry a source line/column — only the
syntax-error path and the missing-comma detector do — so real per-finding
inline markers would need position-tracking added across every rule
first. Rather than fake that precision (pointing at the wrong line, or a
vague "somewhere in this query"), v0.1 is honest about the boundary: a
command you run, a report you read, same content as the web app. Real
inline diagnostics are a larger, separate effort for later.

## Commands

- **QueryDoctor: Check Current File** — runs the check, opens an Output
  panel with the score and findings.
- **QueryDoctor: Apply Safe Auto-Fixes** — if `auto_fixed_sql` comes back
  (only the small, zero-ambiguity fixes — see the main README), replaces
  the whole file with the fixed version. Does nothing if there's nothing
  safe to fix.
- **QueryDoctor: Show Optimized Query** — if there's an optimizer
  suggestion, opens it in a **new, separate** untitled document beside
  the original. Never edits your file — a rewrite suggestion is something
  to review, unlike the narrowly-scoped auto-fixes above.

## Settings

- `querydoctor.dialect` — which of the 10 supported dialects to check
  against (default `bigquery`).

## Privacy

Same model as the web app: your SQL is sent to QueryDoctor's hosted API
to be checked and is never stored. If that's not acceptable for your
files, use the [CLI](../cli/) instead — it runs entirely locally, no
network call at all.

## Install (not published to the Marketplace)

```bash
cd vscode-extension
npx @vscode/vsce package --no-dependencies
code --install-extension querydoctor-0.1.0.vsix
```

## Tests

Zero npm dependencies — only Node built-ins (`https`) plus a hand-written
`vscode` stub for the command-wiring tests, since the real `vscode` module
only resolves inside an actual extension host.

```bash
npm test
```

Some tests hit the real, live QueryDoctor API (not a mock) to prove the
whole chain actually works end to end.
