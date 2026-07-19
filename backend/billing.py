"""Hosted API billing — Stripe subscriptions + Firestore-backed API keys.

Free tier (no key, or /api/check with no Authorization header) is untouched:
same rate-limited, comment-only behavior as always. A valid, active API key
unlocks fail_on_severity (the endpoint can actually return a "block" verdict
instead of just reporting findings) and lifts the per-IP rate limit.

Requires env vars to do anything real:
  STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_PRICE_ID_TEAM,
  STRIPE_PRICE_ID_SCALE, PUBLIC_APP_URL (for checkout success/cancel redirect)
Without them, checkout/webhook endpoints return 503 — same "not configured"
pattern already used for spendstory's Razorpay integration.
"""

import os
import secrets
from datetime import datetime, timezone

import stripe
from google.cloud import firestore

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_IDS = {
    "team": os.environ.get("STRIPE_PRICE_ID_TEAM", ""),
    "scale": os.environ.get("STRIPE_PRICE_ID_SCALE", ""),
}
PUBLIC_APP_URL = os.environ.get("PUBLIC_APP_URL", "https://querydoctor-616665622891.asia-south1.run.app")

_db = None


def _client() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(database="(default)")
    return _db


def configured() -> bool:
    return bool(STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET)


def _new_key() -> str:
    return "qd_live_" + secrets.token_urlsafe(32)


def create_checkout_session(tier: str, email: str | None = None) -> str:
    """Returns a Stripe Checkout URL for the given tier ('team' or 'scale')."""
    price_id = STRIPE_PRICE_IDS.get(tier)
    if not price_id:
        raise ValueError(f"Unknown or unconfigured tier: {tier}")
    stripe.api_key = STRIPE_SECRET_KEY
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        customer_email=email,
        metadata={"tier": tier},
        success_url=f"{PUBLIC_APP_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{PUBLIC_APP_URL}/billing/cancelled",
    )
    return session.url


def handle_webhook_event(payload: bytes, sig_header: str) -> dict:
    """Verifies and processes a Stripe webhook event. Provisions a key on
    checkout completion, deactivates it on subscription cancellation.
    Returns {"handled": bool, "type": str}."""
    stripe.api_key = STRIPE_SECRET_KEY
    event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    etype = event["type"]
    db = _client()

    if etype == "checkout.session.completed":
        session = event["data"]["object"]
        customer_id = session["customer"]
        subscription_id = session["subscription"]
        tier = session.get("metadata", {}).get("tier", "team")
        key = _new_key()
        db.collection("querydoctor_api_keys").document(key).set({
            "stripe_customer_id": customer_id,
            "stripe_subscription_id": subscription_id,
            "tier": tier,
            "active": True,
            "created_at": datetime.now(timezone.utc),
        })
        # In production this key needs to reach the buyer — e.g. email it via
        # the address on the Checkout session, or show it on the success page
        # keyed by session_id. Not wired to an email sender yet.
        return {"handled": True, "type": etype, "key": key}

    if etype == "customer.subscription.deleted":
        sub = event["data"]["object"]
        docs = db.collection("querydoctor_api_keys").where(
            "stripe_subscription_id", "==", sub["id"]
        ).stream()
        for doc in docs:
            doc.reference.update({"active": False})
        return {"handled": True, "type": etype}

    return {"handled": False, "type": etype}


def verify_api_key(key: str) -> dict | None:
    """Returns {"tier": ...} for a valid, active key, or None. Any Firestore
    error (no credentials, no network, misconfigured project) fails open to
    "no key" rather than 500ing the request — an unreachable billing backend
    should degrade to the free tier, not break SQL checks."""
    if not key or not key.startswith("qd_live_"):
        return None
    try:
        doc = _client().collection("querydoctor_api_keys").document(key).get()
    except Exception:
        return None
    if not doc.exists:
        return None
    data = doc.to_dict()
    if not data.get("active"):
        return None
    return {"tier": data.get("tier", "team")}
