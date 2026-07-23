"use strict";
// Pure formatting — no VS Code dependency, so it's directly unit-testable.
// Deliberately an output-panel report, NOT inline diagnostics: most of
// QueryDoctor's 34 rules carry no source line/column (only the syntax-error
// path and the missing-comma detector do), so faking precise inline
// squiggles would misrepresent where a problem actually is. Honest about
// that limit rather than papering over it.

function formatResult(result, fileLabel) {
  const lines = [`=== ${fileLabel} ===`, ""];

  if (!result.ok) {
    lines.push(`Error: ${result.error || "unknown error"}`);
    return lines.join("\n");
  }

  if (!result.valid) {
    const err = result.syntax_error || {};
    lines.push(`❌ Syntax error — line ${err.line}, col ${err.col}`);
    lines.push(err.message || "");
    if (err.hint) lines.push(`💡 ${err.hint}`);
    return lines.join("\n");
  }

  lines.push(`💯 Health score: ${result.score}/100`);
  lines.push("");

  const findings = result.findings || [];
  if (findings.length === 0) {
    lines.push("✅ Clean — no findings.");
  } else {
    for (const f of findings) {
      lines.push(`[${f.severity.toUpperCase()}] ${f.title}`);
      lines.push(`   ${f.message}`);
      lines.push("");
    }
  }

  if (result.suppressed_by_noqa && result.suppressed_by_noqa.length) {
    lines.push(`(suppressed by noqa: ${result.suppressed_by_noqa.join(", ")})`);
  }
  if (result.schema_warnings && result.schema_warnings.length) {
    for (const w of result.schema_warnings) lines.push(`⚠️  schema warning: ${w}`);
  }
  if (result.auto_fixed_titles && result.auto_fixed_titles.length) {
    lines.push("");
    lines.push(`✨ Auto-fixable: ${result.auto_fixed_titles.join(", ")}`);
    lines.push("Run 'QueryDoctor: Apply Safe Auto-Fixes' to apply.");
  }
  if (result.optimized) {
    lines.push("");
    lines.push("🚀 Optimizer suggestion available — run 'QueryDoctor: Show Optimized Query' to view it.");
  }

  return lines.join("\n");
}

module.exports = { formatResult };
