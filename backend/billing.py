"""Paysera billing: One-time checkout + Callback verification.

Flow:
  1. Backend builds a signed Paysera redirect URL for 4.99 EUR (30 days premium)
  2. User is redirected to Paysera's hosted checkout page
  3. User pays via bank transfer, card, or other Paysera methods
  4. Paysera redirects user back to accepturl (/app?subscription=success)
  5. Paysera sends server-to-server callback to callbackurl (/api/paysera/callback)
  6. Backend verifies the callback signature, checks status=1, activates premium

Paysera docs: https://developers.paysera.com/en/checkout/integrations/integration-specification
"""

import base64
import hashlib
import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode, parse_qs, quote

from backend.config import settings
from backend.database import (
    get_user_by_id,
    get_user_by_email,
    update_user_billing,
    get_active_subscription,
    upsert_subscription,
    set_trial_used_on_subscription,
)

logger = logging.getLogger("rag.billing")

PREMIUM_DAYS = 30
PAYSERA_PAY_URL = "https://www.paysera.com/pay/"


def paysera_configured() -> bool:
    return bool(settings.PAYSERA_PROJECT_ID and settings.PAYSERA_PASSWORD)


def _encode_data(params: dict) -> str:
    """URL-encode params, base64 encode, then make URL-safe."""
    query = urlencode(params)
    b64 = base64.b64encode(query.encode("utf-8")).decode("utf-8")
    return b64.replace("/", "_").replace("+", "-")


def _sign(data: str) -> str:
    """Generate ss1 signature: md5(data + password)."""
    raw = data + settings.PAYSERA_PASSWORD
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _decode_callback_data(data_str: str) -> dict:
    """Decode Paysera callback data parameter."""
    safe = data_str.replace("-", "+").replace("_", "/")
    decoded = base64.b64decode(safe).decode("utf-8")
    parsed = parse_qs(decoded, keep_blank_values=True)
    return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}


def verify_callback(data_str: str, ss1: str) -> bool:
    """Verify Paysera callback signature."""
    expected = _sign(data_str)
    return ss1 == expected


# ── Create Checkout URL ──────────────────────────────────────

def create_checkout_url(user_id: int, email: str) -> str:
    """Build a signed Paysera checkout URL for one-time payment."""
    if not paysera_configured():
        raise RuntimeError("Paysera is not configured")

    price_cents = int(settings.SUBSCRIPTION_PRICE_EUR * 100)
    order_id = f"user_{user_id}_{int(datetime.utcnow().timestamp())}"

    accept_url = f"{settings.SERVER_URL}/app?subscription=success"
    cancel_url = f"{settings.SERVER_URL}/pricing?cancelled=true"
    callback_url = f"{settings.SERVER_URL}/api/paysera/callback"

    params = {
        "projectid": settings.PAYSERA_PROJECT_ID,
        "orderid": order_id,
        "accepturl": accept_url,
        "cancelurl": cancel_url,
        "callbackurl": callback_url,
        "version": "1.6",
        "amount": str(price_cents),
        "currency": "EUR",
        "country": "AL",
        "paytext": f"Albanian Law AI Premium - 30 dite (nr. [order_nr]) ([site_name])",
        "p_email": email,
        "test": "1" if settings.PAYSERA_TEST else "0",
    }

    data = _encode_data(params)
    sign = _sign(data)

    url = f"{PAYSERA_PAY_URL}?data={data}&sign={sign}"
    logger.info(f"Paysera checkout created: order={order_id} user={user_id} amount={price_cents}c EUR")
    return url


# ── Process Callback ─────────────────────────────────────────

async def process_callback(data_str: str, ss1: str) -> dict:
    """Process and verify a Paysera callback.

    Returns dict with processing result.
    """
    if not verify_callback(data_str, ss1):
        logger.warning("Paysera callback signature verification FAILED")
        return {"ok": False, "reason": "invalid_signature"}

    params = _decode_callback_data(data_str)
    logger.info(f"Paysera callback: {params}")

    status = params.get("status", "")
    order_id = params.get("orderid", "")
    test = params.get("test", "0")
    pay_amount = params.get("pay_amount", "")
    pay_currency = params.get("pay_currency", "")

    if test == "1" and not settings.PAYSERA_TEST:
        logger.warning(f"Paysera test callback received but PAYSERA_TEST is off: order={order_id}")
        return {"ok": False, "reason": "test_payment_rejected"}

    if str(status) != "1":
        logger.info(f"Paysera callback status={status} (not successful) for order={order_id}")
        return {"ok": True, "action": "ignored", "status": status}

    user = await _resolve_user_from_order(order_id, params.get("p_email", ""))
    if not user:
        logger.warning(f"Paysera callback: could not match user for order={order_id}")
        return {"ok": False, "reason": "user_not_found"}

    user_id = user["id"]
    premium_until = datetime.utcnow() + timedelta(days=PREMIUM_DAYS)

    await update_user_billing(user_id, is_premium=True, subscription_status="active")
    await upsert_subscription(
        user_id=user_id,
        purchase_token=order_id,
        product_id="paysera_onetime",
        status="active",
        current_period_end=premium_until.strftime("%Y-%m-%dT%H:%M:%S"),
        platform="paysera",
    )
    await set_trial_used_on_subscription(user_id)

    logger.info(
        f"User {user_id} activated via Paysera until {premium_until.date()} "
        f"(order={order_id}, amount={pay_amount} {pay_currency})"
    )
    return {"ok": True, "action": "activated", "user_id": user_id}


async def _resolve_user_from_order(order_id: str, email: str) -> dict | None:
    """Extract user_id from order_id format 'user_{id}_{timestamp}'."""
    if order_id and order_id.startswith("user_"):
        parts = order_id.split("_")
        if len(parts) >= 2 and parts[1].isdigit():
            user = await get_user_by_id(int(parts[1]))
            if user:
                return user
    if email:
        user = await get_user_by_email(email.strip().lower())
        if user:
            return user
    return None


# ── Billing Status ───────────────────────────────────────────

async def get_billing_status(user_id: int) -> dict:
    user = await get_user_by_id(user_id)
    if not user:
        return {"error": "User not found"}

    trial_ends_at = user.get("trial_ends_at")
    trial_used_at = user.get("trial_used_at")

    in_trial = False
    trial_expired = False
    if trial_ends_at and not trial_used_at:
        if isinstance(trial_ends_at, datetime):
            end = trial_ends_at.replace(tzinfo=None)
        else:
            try:
                end = datetime.fromisoformat(str(trial_ends_at).replace("Z", ""))
            except Exception:
                end = None
        if end and datetime.utcnow() < end:
            in_trial = True
        elif end:
            trial_expired = True

    sub = await get_active_subscription(user_id)

    return {
        "user_id": user_id,
        "email": user["email"],
        "is_admin": bool(user.get("is_admin")),
        "is_premium": bool(user.get("is_premium")),
        "subscription_status": user.get("subscription_status") or "",
        "trial": {
            "in_trial": in_trial,
            "trial_expired": trial_expired,
            "trial_ends_at": str(trial_ends_at) if trial_ends_at else None,
            "trial_used_at": str(trial_used_at) if trial_used_at else None,
        },
        "subscription": {
            "status": sub["status"],
            "platform": sub.get("platform", ""),
            "current_period_end": str(sub["current_period_end"]) if sub.get("current_period_end") else "",
        } if sub else None,
        "has_access": bool(
            user.get("is_admin")
            or user.get("is_premium")
            or in_trial
            or sub
        ),
        "price_eur": settings.SUBSCRIPTION_PRICE_EUR,
        "premium_days": PREMIUM_DAYS,
        "paysera_configured": paysera_configured(),
    }
