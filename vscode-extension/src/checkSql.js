"use strict";
// Pure, VS Code-independent HTTP client — kept separate from extension.js
// so it's actually unit-testable without a VS Code extension host.

const https = require("https");

const API_URL = "https://querydoctor-616665622891.asia-south1.run.app/api/check";
const MAX_SQL_CHARS = 100_000; // matches the backend's own cap — fail fast client-side
// instead of making a network round-trip just to get the same rejection back.
const TIMEOUT_MS = 15_000;

/**
 * @param {string} sql
 * @param {{dialect?: string, dbSchema?: object, ddl?: string}} [options]
 * @returns {Promise<object>} the parsed API response
 */
function checkSql(sql, options = {}) {
  if (!sql || !sql.trim()) {
    return Promise.reject(new Error("The active file is empty — nothing to check."));
  }
  if (sql.length > MAX_SQL_CHARS) {
    return Promise.reject(new Error(
      `File is ${sql.length} characters — QueryDoctor's limit is ${MAX_SQL_CHARS}. Check a smaller selection instead.`
    ));
  }

  const body = JSON.stringify({
    sql,
    dialect: options.dialect || "bigquery",
    db_schema: options.dbSchema,
    ddl: options.ddl,
  });

  return new Promise((resolve, reject) => {
    const url = new URL(API_URL);
    const req = https.request(
      {
        hostname: url.hostname,
        path: url.pathname,
        method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
        timeout: TIMEOUT_MS,
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => (data += chunk));
        res.on("end", () => {
          let parsed;
          try {
            parsed = JSON.parse(data);
          } catch (e) {
            reject(new Error(`QueryDoctor returned something that wasn't JSON (HTTP ${res.statusCode}).`));
            return;
          }
          // 4xx/5xx from the API still comes back as JSON with an "error"
          // field — surface that message rather than a generic HTTP error.
          if (res.statusCode >= 400 && res.statusCode < 600 && parsed && parsed.error) {
            reject(new Error(parsed.error));
            return;
          }
          resolve(parsed);
        });
      }
    );
    req.on("timeout", () => {
      req.destroy();
      reject(new Error(`QueryDoctor didn't respond within ${TIMEOUT_MS / 1000}s — check your connection and try again.`));
    });
    req.on("error", (err) => reject(new Error(`Couldn't reach QueryDoctor: ${err.message}`)));
    req.write(body);
    req.end();
  });
}

module.exports = { checkSql, MAX_SQL_CHARS, API_URL };
