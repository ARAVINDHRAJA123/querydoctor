"use strict";
// Minimal stand-in for the `vscode` module's API surface used by
// extension.js, so its command-wiring logic can be exercised by a plain
// Node test without needing a real VS Code extension host.

const registeredCommands = {};
const state = {
  activeTextEditor: null,
  outputChannels: [],
  messages: { warning: [], error: [], info: [] },
  openedDocs: [],
  configValues: { "querydoctor.dialect": "bigquery" },
};

function reset() {
  for (const k of Object.keys(registeredCommands)) delete registeredCommands[k];
  state.activeTextEditor = null;
  state.outputChannels = [];
  state.messages = { warning: [], error: [], info: [] };
  state.openedDocs = [];
}

class Range {
  constructor(start, end) {
    this.start = start;
    this.end = end;
  }
}

const vscode = {
  __state: state,
  __reset: reset,
  Range,
  ViewColumn: { Beside: "Beside" },
  window: {
    get activeTextEditor() {
      return state.activeTextEditor;
    },
    showWarningMessage: (msg) => state.messages.warning.push(msg),
    showErrorMessage: (msg) => state.messages.error.push(msg),
    showInformationMessage: (msg) => state.messages.info.push(msg),
    createOutputChannel: (name) => {
      const channel = { name, lines: [], appendLine: (l) => channel.lines.push(l), clear: () => (channel.lines = []), show: () => {}, dispose: () => {} };
      state.outputChannels.push(channel);
      return channel;
    },
    showTextDocument: async (doc) => {
      state.openedDocs.push(doc);
      return doc;
    },
  },
  workspace: {
    getConfiguration: () => ({ get: (key, fallback) => state.configValues[`querydoctor.${key}`] ?? fallback }),
    openTextDocument: async (opts) => ({ content: opts.content, language: opts.language }),
  },
  commands: {
    registerCommand: (id, handler) => {
      registeredCommands[id] = handler;
      return { dispose: () => delete registeredCommands[id] };
    },
  },
  __registeredCommands: registeredCommands,
};

module.exports = vscode;
