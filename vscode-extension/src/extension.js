"use strict";
const vscode = require("vscode");
const { checkSql } = require("./checkSql");
const { formatResult } = require("./formatResult");

let outputChannel;

function getActiveSql() {
  const editor = vscode.window.activeTextEditor;
  if (!editor) return null;
  const sel = editor.selection;
  const text = sel && !sel.isEmpty ? editor.document.getText(sel) : editor.document.getText();
  return { text, fileName: editor.document.fileName || "(untitled)" };
}

function getDialect() {
  return vscode.workspace.getConfiguration("querydoctor").get("dialect", "bigquery");
}

async function runCheck() {
  const active = getActiveSql();
  if (!active) {
    vscode.window.showWarningMessage("QueryDoctor: no active editor to check.");
    return null;
  }
  try {
    const result = await checkSql(active.text, { dialect: getDialect() });
    return { result, fileName: active.fileName };
  } catch (err) {
    vscode.window.showErrorMessage(`QueryDoctor: ${err.message}`);
    return null;
  }
}

async function checkCurrentFile() {
  const outcome = await runCheck();
  if (!outcome) return;
  if (!outputChannel) outputChannel = vscode.window.createOutputChannel("QueryDoctor");
  outputChannel.clear();
  outputChannel.appendLine(formatResult(outcome.result, outcome.fileName));
  outputChannel.show(true);
}

async function applyAutoFixes() {
  const editor = vscode.window.activeTextEditor;
  const outcome = await runCheck();
  if (!outcome || !editor) return;
  const { result } = outcome;
  if (!result.auto_fixed_sql) {
    vscode.window.showInformationMessage("QueryDoctor: nothing safe to auto-fix in this file.");
    return;
  }
  const fullRange = new vscode.Range(
    editor.document.positionAt(0),
    editor.document.positionAt(editor.document.getText().length)
  );
  await editor.edit((editBuilder) => editBuilder.replace(fullRange, result.auto_fixed_sql));
  vscode.window.showInformationMessage(`QueryDoctor: applied fixes for ${result.auto_fixed_titles.join(", ")}.`);
}

async function showOptimized() {
  const outcome = await runCheck();
  if (!outcome) return;
  const { result } = outcome;
  if (!result.optimized) {
    vscode.window.showInformationMessage("QueryDoctor: no optimizer suggestion for this query.");
    return;
  }
  // Opens as a NEW untitled document rather than touching the original file
  // — the optimizer's rewrite is a suggestion to review, not something to
  // silently apply, unlike the narrowly-scoped auto-fixes above.
  const doc = await vscode.workspace.openTextDocument({ content: result.optimized, language: "sql" });
  await vscode.window.showTextDocument(doc, { viewColumn: vscode.ViewColumn.Beside });
}

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand("querydoctor.check", checkCurrentFile),
    vscode.commands.registerCommand("querydoctor.applyAutoFixes", applyAutoFixes),
    vscode.commands.registerCommand("querydoctor.showOptimized", showOptimized)
  );
}

function deactivate() {
  if (outputChannel) outputChannel.dispose();
}

module.exports = { activate, deactivate };
