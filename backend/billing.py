"""Hosted API billing — Razorpay (not Stripe: India-based accounts need an
invite/approval Stripe doesn't reliably grant) order-then-verify purchases,
API keys stored in Firestore.

Not a recurring subscription — Razorpay Subscriptions needs UPI autopay
mandates and more compliance overhead than a solo project needs right now.
Instead this sells a fixed-duration API key (default 30 days): same
order-then-verify flow already proven in spendstory's payments.py, and the
key is handed back directly in the verify response — no webhook, no email
delivery step, no gap between payment and the buyer having their key.

Free tier (no key, or /api/check with no Authorization header) is
untouched: same rate-limited, comment-only behavior as always. A valid,
unexpired key unlocks fail_on_severity (the endpoint can actually return a
"block" verdict) and lifts the per-IP rate limit.

Requires RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET to do anything real —
without them, checkout/verify endpoints return 503, same pattern as
spendstory. Prices are placeholders (owner's call to tune, like the
SpendStory ₹19 pricing decision) — see PRICES below.
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
KEY_DURATION_DAYS = 30
# Placeholder pricing — tune before actually selling. Paise (₹1 = 100 paise).
PRICES_PAISE = {
    "team": 99900,    # ₹999 / 30 days
    "scale": 249900,  # ₹2,499 / 30 days
}

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


def create_order(tier: str) -> dict:
    """Creates a Razorpay order for the given tier's 30-day pack. Returns
    the order dict (has "id", "amount", "currency") straight from
    Razorpay's API. Raises BillingError on any failure."""
    if not configured():
        raise BillingError("Payments are not configured on this server.")
    amount = PRICES_PAISE.get(tier)
    if amount is None:
        raise BillingError(f"Unknown tier: {tier}")

    body = json.dumps({
        "amount": amount,
        "currency": CURRENCY,
        "receipt": f"qd_{uuid4().hex[:12]}",
        "notes": {"tier": tier},
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


def verify_and_provision(order_id: str, payment_id: str, signature: str, tier: str) -> str:
    """Verifies the Razorpay payment signature and, if valid, provisions
    and returns a new API key good for KEY_DURATION_DAYS. Raises
    BillingError if the signature doesn't check out."""
    if not configured():
        raise BillingError("Payments are not configured on this server.")
    if not _verify_signature(order_id, payment_id, signature):
        raise BillingError("Payment verification failed.")

    key = _new_key()
    expires_at = datetime.now(timezone.utc) + timedelta(days=KEY_DURATION_DAYS)
    _client().collection("querydoctor_api_keys").document(key).set({
        "tier": tier,
        "razorpay_order_id": order_id,
        "razorpay_payment_id": payment_id,
        "created_at": datetime.now(timezone.utc),
        "expires_at": expires_at,
    })
    return key


def verify_api_key(key: str) -> dict | None:
    """Returns {"tier": ...} for a valid, unexpired key, or None. Any
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
    return {"tier": data.get("tier", "team")}
