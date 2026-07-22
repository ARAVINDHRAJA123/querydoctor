"""Hosted API billing — Razorpay (not Stripe: India-based accounts need an
invite/approval Stripe doesn't reliably grant) order-then-verify purchases,
API keys stored in Firestore.

Not a recurring subscription — Razorpay Subscriptions needs UPI autopay
mandates and more compliance overhead than a solo project needs right now.
Instead this sells a fixed-duration API key (30-day monthly or 365-day
annual plan, see PLANS below): same order-then-verify flow already
proven in spendstory's payments.py, and the
key is handed back directly in the verify response — no webhook, no email
delivery step, no gap between payment and the buyer having their key.

Free tier (no key, or /api/check with no Authorization header) is
untouched: same rate-limited, comment-only behavior as always. A valid,
unexpired key unlocks fail_on_severity (the endpoint can actually return a
"block" verdict) and lifts the per-IP rate limit.

Requires RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET to do anything real —
without them, checkout/verify endpoints return 503, same pattern as
spendstory.

Single plan, two durations (the old Team/Scale split was dropped — Scale
never actually unlocked anything different, a bug, not a feature):
  monthly: ₹499 / 30 days
  annual:  ₹4,999 / 365 days (~₹416/mo — 2 months free vs. paying monthly)
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from google.cloud import firestore

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")

CURRENCY = "INR"
# plan -> (price in paise, key duration in days)
PLANS = {
    "monthly": (49900, 30),
    "annual": (499900, 365),
}
REMINDER_WINDOW_DAYS = 3  # send a renewal reminder this many days before expiry

_db = None


def _client() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(database="(default)")
    return _db


def configured() -> bool:
    return bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)


def _new_key() -> str:
    return "qd_live_" + secrets.token_urlsafe(32)


class BillingError(Exception):
    pass


def create_order(plan: str) -> dict:
    """Creates a Razorpay order for the given plan ("monthly" or "annual").
    Returns the order dict (has "id", "amount", "currency") straight from
    Razorpay's API. Raises BillingError on any failure."""
    if not configured():
        raise BillingError("Payments are not configured on this server.")
    if plan not in PLANS:
        raise BillingError(f"Unknown plan: {plan}")
    amount, _ = PLANS[plan]

    body = json.dumps({
        "amount": amount,
        "currency": CURRENCY,
        "receipt": f"qd_{uuid4().hex[:12]}",
        "notes": {"plan": plan},
    }).encode()

    req = urllib.request.Request(
        "https://api.razorpay.com/v1/orders",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    auth = base64.b64encode(f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise BillingError(f"Razorpay order creation failed: {e.read().decode(errors='replace')}") from e
    except urllib.error.URLError as e:
        raise BillingError(f"Couldn't reach Razorpay: {e}") from e


def _verify_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """Razorpay's documented scheme: HMAC-SHA256 of "{order_id}|{payment_id}"
    using the key secret. This — not the order_id/payment_id themselves —
    is the only proof the payment actually happened."""
    if not (order_id and payment_id and signature):
        return False
    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_and_provision(order_id: str, payment_id: str, signature: str, plan: str, email: str = "") -> str:
    """Verifies the Razorpay payment signature and, if valid, provisions
    and returns a new API key good for that plan's duration. Raises
    BillingError if the signature doesn't check out. `email` is stored only
    to send the 3-day renewal reminder — optional, no reminder without it."""
    if not configured():
        raise BillingError("Payments are not configured on this server.")
    if plan not in PLANS:
        raise BillingError(f"Unknown plan: {plan}")
    if not _verify_signature(order_id, payment_id, signature):
        raise BillingError("Payment verification failed.")

    _, duration_days = PLANS[plan]
    key = _new_key()
    expires_at = datetime.now(timezone.utc) + timedelta(days=duration_days)
    _client().collection("querydoctor_api_keys").document(key).set({
        "plan": plan,
        "email": email,
        "razorpay_order_id": order_id,
        "razorpay_payment_id": payment_id,
        "created_at": datetime.now(timezone.utc),
        "expires_at": expires_at,
        "reminder_sent": False,
    })
    return key


def verify_api_key(key: str) -> dict | None:
    """Returns {"plan": ...} for a valid, unexpired key, or None. Any
    Firestore error (no credentials, no network, misconfigured project)
    fails open to "no key" rather than 500ing the request — an
    unreachable billing backend should degrade to the free tier, not
    break SQL checks."""
    if not key or not key.startswith("qd_live_"):
        return None
    try:
        doc = _client().collection("querydoctor_api_keys").document(key).get()
    except Exception:
        return None
    if not doc.exists:
        return None
    data = doc.to_dict()
    expires_at = data.get("expires_at")
    if expires_at is None or expires_at < datetime.now(timezone.utc):
        return None
    return {"plan": data.get("plan", "monthly")}


def keys_due_for_reminder() -> list[dict]:
    """Keys expiring within REMINDER_WINDOW_DAYS that haven't had a
    reminder sent yet. Data plumbing only — nothing calls this yet, since
    actually emailing a reminder needs an email provider (SendGrid/Resend/
    SMTP) that isn't wired up. Call this from whatever sends the email
    once one is chosen, then mark reminder_sent=True on each key sent."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=REMINDER_WINDOW_DAYS)
    docs = _client().collection("querydoctor_api_keys") \
        .where("reminder_sent", "==", False) \
        .where("expires_at", "<=", cutoff) \
        .where("expires_at", ">", now) \
        .stream()
    return [{"key": d.id, **d.to_dict()} for d in docs if d.to_dict().get("email")]
