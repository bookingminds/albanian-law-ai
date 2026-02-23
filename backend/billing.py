"""2Checkout (Verifone) billing: Hosted Checkout URL + IPN webhook verification.

Flow:
  1. Backend builds a 2Checkout Buy-Link URL with the user's info
  2. Frontend redirects the user to that URL
  3. User pays on 2Checkout's hosted page
  4. 2Checkout sends IPN POST to /api/2checkout/webhook
  5. We verify the HMAC-MD5 signature and activate premium
"""

import hashlib
import hmac as _hmac
import logging
from datetime import datetime
from urllib.parse import urlencode, parse_qsl

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

_CHECKOUT_LIVE = "https://secure.2checkout.com/checkout/buy"
_CHECKOUT_SANDBOX = "https://sandbox.2checkout.com/checkout/buy"


def twoco_configured() -> bool:
    return bool(
        settings.TWOCO_SELLER_ID
        and settings.TWOCO_SECRET_KEY
        and settings.TWOCO_PRODUCT_ID
    )


# ── Hosted Checkout URL ──────────────────────────────────────

def build_checkout_url(user_id: int, email: str) -> str:
    """Build a 2Checkout Buy-Link URL for the Premium subscription.

    The URL contains the seller ID, product code, customer email,
    and an external reference (our user_id) that comes back in the IPN
    as REFNOEXT so we can match the payment to the user.
    """
    if not twoco_configured():
        raise RuntimeError(
            "2Checkout is not configured "
            "(missing TWOCO_SELLER_ID or TWOCO_SECRET_KEY)"
        )

    base = _CHECKOUT_SANDBOX if settings.TWOCO_SANDBOX else _CHECKOUT_LIVE

    params = {
        "merchant": settings.TWOCO_SELLER_ID,
        "tpl": "default",
        "prod": settings.TWOCO_PRODUCT_ID,
        "qty": "1",
        "type": "PRODUCT",
        "return-url": f"{settings.SERVER_URL}/app?subscription=success",
        "return-type": "redirect",
        "expiration": "",
        "order-ext-ref": f"user_{user_id}",
        "customer-ref": f"user_{user_id}",
        "customer-email": email,
        "currency": "EUR",
    }

    url = f"{base}?{urlencode(params)}"
    logger.info(f"2Checkout checkout URL built for user {user_id}")
    return url


# ── IPN Signature Verification ───────────────────────────────
#
# 2Checkout IPN sends a form-encoded POST. The HASH field is
# HMAC-MD5(secret, concat(len(val)+val for each field except HASH)).
# We must preserve the original field order from the POST body.

def _ipn_hmac(secret: str, message: str) -> str:
    """HMAC-MD5 hex digest (matches PHP hash_hmac('md5', ...))."""
    return _hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.md5,
    ).hexdigest()


def verify_ipn_signature(raw_body: bytes, secret: str) -> bool:
    """Verify a 2Checkout IPN request by recomputing the HASH.

    The raw POST body is parsed in order; for each value (except the
    HASH field) we prepend the byte-length of the value, then the value
    itself.  The result is HMAC-MD5'd with the IPN secret.
    """
    pairs = parse_qsl(raw_body.decode("utf-8"), keep_blank_values=True)

    hash_input = ""
    received_hash = ""

    for key, value in pairs:
        if key == "HASH":
            received_hash = value
            continue
        byte_len = len(value.encode("utf-8"))
        hash_input += str(byte_len) + value

    if not received_hash:
        logger.warning("IPN: no HASH field found in payload")
        return False

    computed = _ipn_hmac(secret, hash_input)
    ok = _hmac.compare_digest(computed.lower(), received_hash.lower())
    if not ok:
        logger.warning(
            f"IPN hash mismatch: computed={computed[:12]}… "
            f"received={received_hash[:12]}…"
        )
    return ok


def build_ipn_response(secret: str) -> str:
    """Build the acknowledgement that 2Checkout expects:

        <EPAYMENT>YYYYMMDDHHmmss|HASH</EPAYMENT>

    where HASH = HMAC-MD5(secret, len(date_str) + date_str).
    """
    now = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    hash_input = str(len(now)) + now
    h = _ipn_hmac(secret, hash_input)
    return f"<EPAYMENT>{now}|{h}</EPAYMENT>"


# ── IPN Payload Parsing ──────────────────────────────────────

def parse_ipn_body(raw_body: bytes) -> dict:
    """Parse the IPN form body into a dict (arrays become lists)."""
    pairs = parse_qsl(raw_body.decode("utf-8"), keep_blank_values=True)
    result: dict = {}
    for key, value in pairs:
        if key in result:
            existing = result[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                result[key] = [existing, value]
        else:
            result[key] = value
    return result


# ── IPN Event Processing ─────────────────────────────────────

async def process_ipn(data: dict) -> dict:
    """Process a verified 2Checkout IPN notification.

    Key IPN fields:
      REFNO          – 2Checkout order reference
      REFNOEXT       – our external ref ("user_42")
      ORDERSTATUS    – COMPLETE, CANCELED, REFUND, etc.
      CUSTOMEREMAIL  – buyer email (fallback for user matching)
      IPN_LICENSE_REF[0] – subscription/license reference
    """
    refno = data.get("REFNO", "")
    refnoext = data.get("REFNOEXT", "")
    order_status = data.get("ORDERSTATUS", "").upper()
    customer_email = (
        data.get("CUSTOMEREMAIL", "")
        or data.get("EMAIL", "")
        or data.get("FIRSTNAME_D", "")
    ).strip().lower()

    license_ref = ""
    lr = data.get("IPN_LICENSE_REF[]") or data.get("IPN_LICENSE_REF[0]", "")
    if isinstance(lr, list):
        license_ref = lr[0] if lr else ""
    else:
        license_ref = lr

    logger.info(
        f"IPN received: REFNO={refno} REFNOEXT={refnoext} "
        f"STATUS={order_status} EMAIL={customer_email}"
    )

    user = await _resolve_user(refnoext, customer_email)
    if not user:
        logger.warning(f"IPN: could not match user for REFNOEXT={refnoext} EMAIL={customer_email}")
        return {"processed": False, "reason": "user_not_found"}

    user_id = user["id"]

    if order_status in ("COMPLETE", ""):
        await update_user_billing(user_id, is_premium=True, subscription_status="active")
        await upsert_subscription(
            user_id=user_id,
            purchase_token=refno,
            product_id=settings.TWOCO_PRODUCT_ID,
            status="active",
            current_period_end=None,
            platform="2checkout",
        )
        await set_trial_used_on_subscription(user_id)
        logger.info(f"User {user_id} activated via 2Checkout (REFNO={refno})")
        return {"processed": True, "action": "activated", "user_id": user_id}

    elif order_status in ("CANCELED", "REFUND", "REVERSED"):
        await update_user_billing(user_id, is_premium=False, subscription_status="canceled")
        await upsert_subscription(
            user_id=user_id,
            purchase_token=refno,
            product_id=settings.TWOCO_PRODUCT_ID,
            status="canceled",
            current_period_end=None,
            platform="2checkout",
        )
        logger.info(f"User {user_id} deactivated via 2Checkout (REFNO={refno}, status={order_status})")
        return {"processed": True, "action": "deactivated", "user_id": user_id}

    else:
        logger.info(f"IPN ignored: status={order_status} for user {user_id}")
        return {"processed": True, "action": "ignored", "status": order_status}


async def _resolve_user(refnoext: str, email: str) -> dict | None:
    """Find the user by external reference or email."""
    if refnoext:
        uid_str = refnoext.replace("user_", "")
        if uid_str.isdigit():
            user = await get_user_by_id(int(uid_str))
            if user:
                return user

    if email:
        user = await get_user_by_email(email)
        if user:
            return user

    return None


# ── Billing Status ───────────────────────────────────────────

async def get_billing_status(user_id: int) -> dict:
    """Return complete billing state for a user."""
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

    gp_sub = await get_active_subscription(user_id)

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
        "google_play_subscription": {
            "status": gp_sub["status"],
            "current_period_end": str(gp_sub["current_period_end"]) if gp_sub.get("current_period_end") else "",
        } if gp_sub else None,
        "has_access": bool(
            user.get("is_admin")
            or user.get("is_premium")
            or in_trial
            or gp_sub
        ),
        "price_eur": settings.SUBSCRIPTION_PRICE_EUR,
        "twoco_configured": twoco_configured(),
    }
