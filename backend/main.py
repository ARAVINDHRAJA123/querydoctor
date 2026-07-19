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

from lint_engine import DIALECTS, check_sql

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
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; img-src 'self' data:; connect-src 'self'"
    )
    return resp


class CheckRequest(BaseModel):
    sql: str = Field(max_length=MAX_SQL_CHARS)
    dialect: str = "bigquery"
    target_dialect: str | None = None
    dbt_mode: bool = False


@app.post("/api/check")
async def check(req: CheckRequest, request: Request):
    fwd = request.headers.get("x-forwarded-for")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")
    if _rate_limited(ip):
        return JSONResponse({"ok": False, "error": "Too many checks right now — take a short break and try again."}, status_code=429)

    result = check_sql(req.sql, dialect=req.dialect, target_dialect=req.target_dialect, dbt_mode=req.dbt_mode)
    if not result.get("ok"):
        return JSONResponse(result, status_code=422)
    return JSONResponse(result)


@app.get("/api/dialects")
async def dialects():
    return {"dialects": DIALECTS}


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
