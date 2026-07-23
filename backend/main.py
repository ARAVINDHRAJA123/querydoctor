"""
QueryDoctor API — stateless SQL health checks.

Paste SQL → get a diagnosis: syntax validation, lint findings with
plain-English explanations, formatted SQL, and optional dialect translation.
Nothing is stored; every check happens in memory and is forgotten.
"""

import os
import time
from collections import defaultdict, deque

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import billing
from lint_engine import DIALECTS, SEV_WEIGHT, check_sql, parse_ddl_to_schema, compare_sql, explain_sql

SEV_ORDER = ["low", "medium", "high"]  # ascending — index used for threshold comparison

MAX_SQL_CHARS = 100_000
RATE_LIMIT = 60             # checks per IP per window (checks are cheap)
RATE_WINDOW_S = 600
_hits: dict[str, deque] = defaultdict(deque)


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    q = _hits[ip]
    while q and now - q[0] > RATE_WINDOW_S:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")

app = FastAPI(title="QueryDoctor", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        # checkout.razorpay.com is Razorpay's own hosted payment widget —
        # it handles card/UPI details directly with Razorpay; we never see
        # or touch that data ourselves, only the payment result.
        "script-src 'self' https://checkout.razorpay.com; "
        "frame-src https://api.razorpay.com; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        "img-src 'self' data: https://*.razorpay.com; "
        "connect-src 'self' https://api.razorpay.com https://lumberjack.razorpay.com"
    )
    return resp


class CheckRequest(BaseModel):
    sql: str = Field(max_length=MAX_SQL_CHARS)
    dialect: str = "bigquery"
    target_dialect: str | None = None
    dbt_mode: bool = False
    fail_on_severity: str | None = None  # "low"/"medium"/"high" — paid-tier only
    db_schema: dict[str, list[str]] | None = None  # optional {table: [columns]} — unlocks unknown table/column checks
    ddl: str | None = Field(default=None, max_length=200_000)  # optional CREATE TABLE statements, auto-parsed into a schema


@app.post("/api/check")
async def check(req: CheckRequest, request: Request):
    key = None
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        key = billing.verify_api_key(auth[len("Bearer "):].strip())

    if key is None:
        fwd = request.headers.get("x-forwarded-for")
        ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")
        if _rate_limited(ip):
            return JSONResponse({"ok": False, "error": "Too many checks right now — take a short break and try again."}, status_code=429)

    schema_warnings = []
    schema = None
    if req.ddl:
        schema, schema_warnings = parse_ddl_to_schema(req.ddl, dialect=req.dialect)
    if req.db_schema:
        # Explicit db_schema wins per-table over anything derived from ddl —
        # it's the more deliberate, hand-curated source when both are given.
        schema = {**(schema or {}), **req.db_schema}
    if schema and (len(schema) > 200 or sum(len(cols) for cols in schema.values()) > 5000):
        return JSONResponse({"ok": False, "error": "Schema too large — max 200 tables / 5000 total columns."}, status_code=422)

    result = check_sql(req.sql, dialect=req.dialect, target_dialect=req.target_dialect, dbt_mode=req.dbt_mode, schema=schema)
    if schema_warnings:
        result["schema_warnings"] = schema_warnings
    if not result.get("ok"):
        return JSONResponse(result, status_code=422)

    # fail_on_severity actually blocking (not just reporting) is the paid feature.
    if key is not None and req.fail_on_severity in SEV_ORDER:
        threshold = SEV_ORDER.index(req.fail_on_severity)
        result["blocked"] = any(
            SEV_ORDER.index(f["severity"]) >= threshold for f in result.get("findings", [])
        )
    else:
        result["blocked"] = False

    return JSONResponse(result)


class CompareRequest(BaseModel):
    sql_a: str = Field(max_length=MAX_SQL_CHARS)
    sql_b: str = Field(max_length=MAX_SQL_CHARS)
    dialect: str = "bigquery"


@app.post("/api/compare")
async def compare(req: CompareRequest, request: Request):
    fwd = request.headers.get("x-forwarded-for")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")
    if _rate_limited(ip):
        return JSONResponse({"ok": False, "error": "Too many checks right now — take a short break and try again."}, status_code=429)

    result = compare_sql(req.sql_a, req.sql_b, dialect=req.dialect)
    return JSONResponse(result)


class ExplainRequest(BaseModel):
    sql: str = Field(max_length=MAX_SQL_CHARS)
    dialect: str = "bigquery"


@app.post("/api/explain")
async def explain(req: ExplainRequest, request: Request):
    fwd = request.headers.get("x-forwarded-for")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")
    if _rate_limited(ip):
        return JSONResponse({"ok": False, "error": "Too many checks right now — take a short break and try again."}, status_code=429)
    return JSONResponse(explain_sql(req.sql, dialect=req.dialect))


@app.get("/api/billing/verify-key")
async def billing_verify_key(request: Request):
    auth = request.headers.get("authorization", "")
    key = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
    result = billing.verify_api_key(key)
    return {"active": result is not None, "plan": (result or {}).get("plan")}


@app.get("/api/dialects")
async def dialects():
    return {"dialects": DIALECTS}


class CreateOrderRequest(BaseModel):
    plan: str


@app.post("/api/billing/create-order")
async def billing_create_order(req: CreateOrderRequest):
    if not billing.configured():
        return JSONResponse({"ok": False, "error": "Payments aren't configured yet."}, status_code=503)
    try:
        order = billing.create_order(req.plan)
    except billing.BillingError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=422)
    return {"ok": True, "order": order, "key_id": billing.RAZORPAY_KEY_ID}


class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    plan: str
    email: str = ""


@app.post("/api/billing/verify")
async def billing_verify(req: VerifyPaymentRequest):
    if not billing.configured():
        return JSONResponse({"ok": False, "error": "Payments aren't configured yet."}, status_code=503)
    try:
        key = billing.verify_and_provision(
            req.razorpay_order_id, req.razorpay_payment_id, req.razorpay_signature, req.plan, req.email
        )
    except billing.BillingError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=402)
    return {"ok": True, "api_key": key}


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
