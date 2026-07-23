"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const Module = require("module");

const vscodeStub = require("./vscode_stub");

// Redirect require('vscode') -> our stub, so extension.js (which does
// `const vscode = require("vscode")` at load time) can actually be loaded
// by plain Node — the real `vscode` module only resolves inside a real
// extension host, which this environment doesn't have.
const origResolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...args) {
  if (request === "vscode") return path.join(__dirname, "vscode_stub.js");
  return origResolve.call(this, request, ...args);
};

const checkSqlModulePath = require.resolve("../src/checkSql");
const checkSqlModule = require(checkSqlModulePath);

function freshExtension() {
  delete require.cache[require.resolve("../src/extension.js")];
  return require("../src/extension.js");
}

function fakeEditor(text, selectionEmpty = true) {
  return {
    document: { fileName: "test.sql", getText: (sel) => text },
    selection: { isEmpty: selectionEmpty },
  };
}

test("activate() registers exactly the 3 documented commands", () => {
  vscodeStub.__reset();
  const ext = freshExtension();
  const context = { subscriptions: [] };
  ext.activate(context);
  assert.equal(context.subscriptions.length, 3);
  assert.deepEqual(
    Object.keys(vscodeStub.__registeredCommands).sort(),
    ["querydoctor.applyAutoFixes", "querydoctor.check", "querydoctor.showOptimized"]
  );
});

test("check command warns when there is no active editor", async () => {
  vscodeStub.__reset();
  const ext = freshExtension();
  ext.activate({ subscriptions: [] });
  vscodeStub.__state.activeTextEditor = null;
  await vscodeStub.__registeredCommands["querydoctor.check"]();
  assert.equal(vscodeStub.__state.messages.warning.length, 1);
  assert.match(vscodeStub.__state.messages.warning[0], /no active editor/);
});

test("check command writes the formatted result to the output channel", async () => {
  vscodeStub.__reset();
  const originalCheckSql = checkSqlModule.checkSql;
  checkSqlModule.checkSql = async (sql, opts) => {
    assert.equal(sql, "SELECT 1");
    assert.equal(opts.dialect, "bigquery");
    return { ok: true, valid: true, score: 100, findings: [] };
  };
  try {
    const ext = freshExtension();
    ext.activate({ subscriptions: [] });
    vscodeStub.__state.activeTextEditor = fakeEditor("SELECT 1");
    await vscodeStub.__registeredCommands["querydoctor.check"]();
    assert.equal(vscodeStub.__state.outputChannels.length, 1);
    assert.match(vscodeStub.__state.outputChannels[0].lines.join("\n"), /Health score: 100\/100/);
  } finally {
    checkSqlModule.checkSql = originalCheckSql;
  }
});

test("check command shows an error message when the API call rejects", async () => {
  vscodeStub.__reset();
  const originalCheckSql = checkSqlModule.checkSql;
  checkSqlModule.checkSql = async () => {
    throw new Error("Couldn't reach QueryDoctor: network down");
  };
  try {
    const ext = freshExtension();
    ext.activate({ subscriptions: [] });
    vscodeStub.__state.activeTextEditor = fakeEditor("SELECT 1");
    await vscodeStub.__registeredCommands["querydoctor.check"]();
    assert.equal(vscodeStub.__state.messages.error.length, 1);
    assert.match(vscodeStub.__state.messages.error[0], /network down/);
    assert.equal(vscodeStub.__state.outputChannels.length, 0);
  } finally {
    checkSqlModule.checkSql = originalCheckSql;
  }
});

test("applyAutoFixes replaces the document text when a fix is available", async () => {
  vscodeStub.__reset();
  const originalCheckSql = checkSqlModule.checkSql;
  checkSqlModule.checkSql = async () => ({
    ok: true, valid: true, score: 70,
    findings: [{ severity: "high", title: "`= NULL` never matches", message: "..." }],
    auto_fixed_sql: "SELECT * FROM t WHERE x IS NULL",
    auto_fixed_titles: ["`= NULL` never matches"],
  });
  try {
    const ext = freshExtension();
    ext.activate({ subscriptions: [] });
    let replacedWith = null;
    const editor = fakeEditor("SELECT * FROM t WHERE x = NULL");
    editor.document.positionAt = (offset) => ({ offset });
    editor.edit = async (cb) => {
      cb({ replace: (range, text) => (replacedWith = text) });
      return true;
    };
    vscodeStub.__state.activeTextEditor = editor;
    await vscodeStub.__registeredCommands["querydoctor.applyAutoFixes"]();
    assert.equal(replacedWith, "SELECT * FROM t WHERE x IS NULL");
    assert.equal(vscodeStub.__state.messages.info.length, 1);
    assert.match(vscodeStub.__state.messages.info[0], /applied fixes/);
  } finally {
    checkSqlModule.checkSql = originalCheckSql;
  }
});

test("applyAutoFixes does nothing (no edit) when there's nothing safe to fix", async () => {
  vscodeStub.__reset();
  const originalCheckSql = checkSqlModule.checkSql;
  checkSqlModule.checkSql = async () => ({ ok: true, valid: true, score: 100, findings: [] });
  try {
    const ext = freshExtension();
    ext.activate({ subscriptions: [] });
    let editCalled = false;
    const editor = fakeEditor("SELECT 1");
    editor.edit = async () => {
      editCalled = true;
    };
    vscodeStub.__state.activeTextEditor = editor;
    await vscodeStub.__registeredCommands["querydoctor.applyAutoFixes"]();
    assert.equal(editCalled, false);
    assert.match(vscodeStub.__state.messages.info[0], /nothing safe to auto-fix/);
  } finally {
    checkSqlModule.checkSql = originalCheckSql;
  }
});

test("showOptimized opens a new document beside the original, never edits it", async () => {
  vscodeStub.__reset();
  const originalCheckSql = checkSqlModule.checkSql;
  checkSqlModule.checkSql = async () => ({ ok: true, valid: true, score: 100, findings: [], optimized: "SELECT 1 -- optimized" });
  try {
    const ext = freshExtension();
    ext.activate({ subscriptions: [] });
    let editCalled = false;
    const editor = fakeEditor("SELECT 1");
    editor.edit = async () => {
      editCalled = true;
    };
    vscodeStub.__state.activeTextEditor = editor;
    await vscodeStub.__registeredCommands["querydoctor.showOptimized"]();
    assert.equal(editCalled, false);
    assert.equal(vscodeStub.__state.openedDocs.length, 1);
    assert.equal(vscodeStub.__state.openedDocs[0].content, "SELECT 1 -- optimized");
  } finally {
    checkSqlModule.checkSql = originalCheckSql;
  }
});
