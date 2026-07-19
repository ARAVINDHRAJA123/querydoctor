/* QueryDoctor frontend: paste SQL → POST /api/check → render diagnosis. */

"use strict";

const $ = (id) => document.getElementById(id);

const DIALECT_LABELS = {
  bigquery: "BigQuery", postgres: "PostgreSQL", mysql: "MySQL", snowflake: "Snowflake",
  spark: "Spark SQL", sqlite: "SQLite", tsql: "SQL Server", oracle: "Oracle",
  duckdb: "DuckDB", redshift: "Redshift",
};

const SAMPLE = `SELECT *
FROM orders o
JOIN customers c
WHERE c.name LIKE '%kumar'
LIMIT 10`;

/* ── populate dialect pickers ── */
fetch("api/dialects").then((r) => r.json()).then(({ dialects }) => {
  for (const d of dialects) {
    $("dialect").add(new Option(DIALECT_LABELS[d] || d, d));
    $("target").add(new Option(DIALECT_LABELS[d] || d, d));
  }
  $("dialect").value = "bigquery";
});

$("btn-sample").addEventListener("click", () => { $("sql").value = SAMPLE; $("sql").focus(); });
$("btn-clear").addEventListener("click", () => {
  $("sql").value = "";
  $("results").hidden = true;
  setError("");
  $("sql").focus();
});
$("btn-check").addEventListener("click", check);
$("sql").addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") check();
});

document.querySelectorAll(".btn-copy").forEach((b) =>
  b.addEventListener("click", () => {
    navigator.clipboard.writeText($(b.dataset.target).textContent).then(() => {
      const old = b.textContent;
      b.textContent = "Copied ✓";
      setTimeout(() => (b.textContent = old), 1400);
    });
  }));

function setError(msg) {
  const el = $("error");
  el.hidden = !msg;
  el.textContent = msg || "";
  if (msg) { el.style.animation = "none"; void el.offsetWidth; el.style.animation = ""; }
}

async function check() {
  const sql = $("sql").value.trim();
  if (!sql) return setError("Paste some SQL first — or tap “Try a sample”.");
  setError("");

  const btn = $("btn-check");
  btn.disabled = true;
  btn.lastChild.textContent = " Examining… ";

  try {
    const res = await fetch("api/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sql, dialect: $("dialect").value, target_dialect: $("target").value || null }),
    });
    const d = await res.json();
    if (!res.ok || d.ok === false) return setError(d.error || "Something went wrong. Please try again.");
    addToHistory(sql, $("dialect").value, d.valid ? d.score : 0, d.valid);
    render(d);
  } catch {
    setError("Couldn't reach the doctor. Check your connection and try again.");
  } finally {
    btn.disabled = false;
    btn.lastChild.textContent = " Diagnose my SQL ";
  }
}

const RING_LEN = 326.7;

function render(d) {
  $("results").hidden = false;
  const cards = ["syntax-card", "findings-card", "formatted-card", "optimized-card", "translated-card"];
  cards.forEach((c) => ($(c).hidden = true));

  if (!d.valid) {
    setRing(0, "var(--neg)");
    $("score").textContent = "0";
    $("verdict-title").textContent = "That SQL won't run 🚑";
    $("verdict-sub").textContent = "There's a syntax problem — fix it below and diagnose again.";
    $("syntax-card").hidden = false;
    const se = d.syntax_error;
    $("syntax-msg").textContent = se.hint ||
      (se.line ? `Line ${se.line}, column ${se.col}: ${se.message}` : se.message);
    // Show the offending line with a caret pointing at the failing column.
    if (se.source_line) {
      const caret = se.col ? "\n" + " ".repeat(Math.max(0, se.col - 1)) + "^ problem is around here" : "";
      $("syntax-context").textContent = se.source_line + caret;
      $("syntax-context").hidden = false;
    } else {
      $("syntax-context").hidden = true;
    }
    scrollToResults();
    return;
  }

  // Score ring
  const score = d.score;
  const colour = score >= 80 ? "var(--pos)" : score >= 50 ? "var(--warn)" : "var(--neg)";
  setRing(score, colour);
  animateNumber($("score"), score);

  const n = d.findings.length;
  if (n === 0) {
    $("verdict-title").textContent = "Clean bill of health ✅";
    $("verdict-sub").textContent = "Valid syntax and none of our checks found a problem. Nice work!";
  } else {
    const worst = d.findings[0].severity;
    $("verdict-title").textContent =
      worst === "high" ? "Needs attention 🚨" : worst === "medium" ? "Mostly healthy, minor issues 🩹" : "Healthy, small tips 💡";
    $("verdict-sub").textContent = `${n} finding${n > 1 ? "s" : ""} below — each explained in plain English.`;
    $("findings-card").hidden = false;
    $("findings").innerHTML = d.findings.map((f, i) => `
      <li style="animation-delay:${i * 90}ms">
        <span class="sev ${f.severity}">${f.severity}</span>
        <div><b>${esc(f.title)}</b><p>${esc(f.message)}</p></div>
      </li>`).join("");
  }

  if (d.formatted) {
    $("formatted-card").hidden = false;
    $("formatted-code").textContent = d.formatted;
  }
  if (d.optimized) {
    $("optimized-card").hidden = false;
    $("optimized-code").textContent = d.optimized;
  }
  if (d.translated) {
    $("translated-card").hidden = false;
    $("translated-title").textContent = `🔁 In ${DIALECT_LABELS[$("target").value] || $("target").value}`;
    $("translated-code").textContent = d.translated;
  }
  scrollToResults();
}

function setRing(score, colour) {
  const ring = $("ring-val");
  ring.style.stroke = colour;
  // double rAF so the transition animates from the previous value
  requestAnimationFrame(() => requestAnimationFrame(() => {
    ring.style.strokeDashoffset = RING_LEN * (1 - score / 100);
  }));
}

function animateNumber(el, target) {
  const dur = 1000, t0 = performance.now();
  const step = (t) => {
    const p = Math.min(1, (t - t0) / dur), eased = 1 - Math.pow(1 - p, 3);
    el.textContent = Math.round(target * eased);
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function scrollToResults() {
  $("results").scrollIntoView({ behavior: "smooth", block: "start" });
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ── Check history (localStorage — this device only) ───────── */
const HIST_KEY = "qd-history";
const HIST_MAX = 30;

const loadHist = () => { try { return JSON.parse(localStorage.getItem(HIST_KEY)) || []; } catch { return []; } };
const saveHist = (h) => localStorage.setItem(HIST_KEY, JSON.stringify(h.slice(0, HIST_MAX)));

function addToHistory(sql, dialect, score, valid) {
  const h = loadHist();
  // de-dupe: same SQL+dialect replaces the old entry
  const filtered = h.filter((e) => !(e.sql === sql && e.dialect === dialect));
  filtered.unshift({ sql, dialect, score, valid, ts: Date.now() });
  saveHist(filtered);
  renderHistory();
}

function timeAgo(ts) {
  const s = Math.floor((Date.now() - ts) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} hr ago`;
  return new Date(ts).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}

function renderHistory() {
  const h = loadHist();
  $("history-empty").hidden = h.length > 0;
  $("history-list").innerHTML = h.map((e, i) => {
    const cls = !e.valid ? "bad" : e.score >= 80 ? "good" : e.score >= 50 ? "mid" : "bad";
    const label = e.valid ? e.score : "✗";
    return `<li data-i="${i}" title="Tap to load this query">
      <span class="h-score ${cls}">${label}</span>
      <div class="h-meta"><div class="h-sql">${esc(e.sql.replace(/\s+/g, " ").slice(0, 60))}</div>
      <div class="h-sub">${DIALECT_LABELS[e.dialect] || e.dialect} · ${timeAgo(e.ts)}</div></div>
    </li>`;
  }).join("");
  document.querySelectorAll("#history-list li").forEach((li) =>
    li.addEventListener("click", () => {
      const e = loadHist()[+li.dataset.i];
      if (!e) return;
      $("sql").value = e.sql;
      $("dialect").value = e.dialect;
      closeDrawer();
      check();
    }));
}

function openDrawer() {
  renderHistory();
  $("drawer").classList.add("open");
  $("drawer-backdrop").hidden = false;
  requestAnimationFrame(() => $("drawer-backdrop").classList.add("show"));
}
function closeDrawer() {
  $("drawer").classList.remove("open");
  $("drawer-backdrop").classList.remove("show");
  setTimeout(() => ($("drawer-backdrop").hidden = true), 300);
}
$("btn-history").addEventListener("click", () =>
  $("drawer").classList.contains("open") ? closeDrawer() : openDrawer());
$("drawer-backdrop").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
$("btn-clear-history").addEventListener("click", () => {
  localStorage.removeItem(HIST_KEY);
  renderHistory();
});
renderHistory();

/* ── Theme toggle ──────────────────────────────────────────── */
const THEME_KEY = "qd-theme";
const rootEl = document.documentElement;

const savedTheme = localStorage.getItem(THEME_KEY);
if (savedTheme === "light" || savedTheme === "dark") rootEl.dataset.theme = savedTheme;

function currentTheme() {
  return rootEl.dataset.theme ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}

function applyTheme(next) {
  rootEl.dataset.theme = next;
  localStorage.setItem(THEME_KEY, next);
  document.querySelector('meta[name="theme-color"]')
    .setAttribute("content", next === "dark" ? "#07100d" : "#f6faf8");
}

$("btn-theme").addEventListener("click", (ev) => {
  const next = currentTheme() === "dark" ? "light" : "dark";

  // Circular wipe from the button via the View Transitions API —
  // one composited animation, so text doesn't stutter through the change.
  if (document.startViewTransition && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    const r = ev.currentTarget.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    const radius = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
    const vt = document.startViewTransition(() => applyTheme(next));
    vt.ready.then(() => {
      document.documentElement.animate(
        { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`] },
        { duration: 600, easing: "cubic-bezier(.22,1,.36,1)", pseudoElement: "::view-transition-new(root)" },
      );
    });
  } else {
    applyTheme(next);
  }
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("sw.js"));
}

/* ── API key purchase (paid tier — Razorpay order-then-verify) ─────────
   Same flow as SpendStory's Excel export: create an order server-side,
   open Razorpay's own hosted Checkout (we never see card/UPI details),
   and on success send order_id/payment_id/signature to /api/billing/verify,
   which checks the signature server-side before ever handing back a key. */
function setPricingError(msg) {
  const el = $("pricing-error");
  el.hidden = !msg;
  el.textContent = msg || "";
}

document.querySelectorAll(".btn-buy").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const tier = btn.dataset.tier;
    if (typeof Razorpay === "undefined") {
      return setPricingError("Payment widget failed to load — check your connection and try again.");
    }
    setPricingError("");
    $("key-result").hidden = true;
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Starting checkout…";

    try {
      const res = await fetch("api/billing/create-order", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tier }),
      });
      const body = await res.json();
      if (!res.ok || !body.ok) throw new Error(body.error || "Couldn't start checkout.");

      const rzp = new Razorpay({
        key: body.key_id,
        amount: body.order.amount,
        currency: body.order.currency,
        order_id: body.order.id,
        name: "QueryDoctor",
        description: `${tier[0].toUpperCase()}${tier.slice(1)} API key — 30 days`,
        theme: { color: "#0ea371" },
        handler: (response) => verifyAndShowKey(response, tier),
        modal: { ondismiss: () => { btn.disabled = false; btn.textContent = original; } },
      });
      rzp.on("payment.failed", () => setPricingError("Payment failed. You have not been charged — please try again."));
      rzp.open();
      btn.textContent = original;
      btn.disabled = false;
    } catch (e) {
      setPricingError(e.message || "Couldn't reach the server. Please try again.");
      btn.disabled = false;
      btn.textContent = original;
    }
  });
});

// Guards against navigating away before the key is copied — it's shown
// exactly once and we don't store it in a recoverable form, so losing this
// moment means the buyer loses the key. Cleared the instant they copy it.
let keyUncopied = false;
window.addEventListener("beforeunload", (e) => {
  if (!keyUncopied) return;
  e.preventDefault();
  e.returnValue = "";
});

async function verifyAndShowKey(payment, tier) {
  setPricingError("");
  try {
    const res = await fetch("api/billing/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        razorpay_order_id: payment.razorpay_order_id,
        razorpay_payment_id: payment.razorpay_payment_id,
        razorpay_signature: payment.razorpay_signature,
        tier,
      }),
    });
    const body = await res.json();
    if (!res.ok || !body.ok) throw new Error(body.error || "Payment succeeded but the key couldn't be issued — please contact support.");
    $("key-value").textContent = body.api_key;
    $("key-result").hidden = false;
    keyUncopied = true;
    $("key-result").scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (e) {
    setPricingError(e.message);
  }
}

$("btn-copy-key").addEventListener("click", () => {
  navigator.clipboard.writeText($("key-value").textContent).then(() => {
    keyUncopied = false;
    const btn = $("btn-copy-key");
    const old = btn.textContent;
    btn.textContent = "Copied ✓";
    setTimeout(() => (btn.textContent = old), 1400);
  });
});
