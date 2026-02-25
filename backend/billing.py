"""PayPal billing: One-time Orders API + Webhook verification.

Flow (one-time payment, Albanian-card compatible):
  1. Backend creates a PayPal Order for 4.99 EUR (30 days premium)
  2. Returns the approval URL to the frontend
  3. User approves on PayPal's hosted page (card or PayPal balance)
  4. User is redirected back to /api/billing/capture?token=ORDER_ID
  5. Backend captures the order and activates premium for 30 days
  6. PayPal also sends webhook for PAYMENT.CAPTURE.COMPLETED
"""

import logging
from datetime import datetime, timedelta

import httpx

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

_API_LIVE = "https://api-m.paypal.com"
_API_SANDBOX = "https://api-m.sandbox.paypal.com"

PREMIUM_DAYS = 30


def _api_base() -> str:
    return _API_SANDBOX if settings.PAYPAL_SANDBOX else _API_LIVE


def paypal_configured() -> bool:
    return bool(
        settings.PAYPAL_CLIENT_ID
        and settings.PAYPAL_CLIENT_SECRET
    )


# ── OAuth2 Access Token ─────────────────────────────────────

async def _get_access_token() -> str:
    url = f"{_api_base()}/v1/oauth2/token"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            url,
            data={"grant_type": "client_credentials"},
            auth=(settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET),
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


# ── Create Order (one-time payment) ─────────────────────────

async def create_order(user_id: int, email: str) -> str:
    """Create a PayPal Order for one-time 4.99 EUR payment.

    Returns the approval URL where the user completes the payment.
    custom_id carries our user_id for matching.
    """
    if not paypal_configured():
        raise RuntimeError("PayPal is not configured")

    token = await _get_access_token()
    url = f"{_api_base()}/v2/checkout/orders"

    price = f"{settings.SUBSCRIPTION_PRICE_EUR:.2f}"

    payload = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "amount": {
                    "currency_code": "EUR",
                    "value": price,
                },
                "description": f"Albanian Law AI Premium - {PREMIUM_DAYS} dite",
                "custom_id": f"user_{user_id}",
            }
        ],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    "brand_name": "Albanian Law AI",
                    "locale": "sq-AL",
                    "shipping_preference": "NO_SHIPPING",
                    "user_action": "PAY_NOW",
                    "return_url": f"{settings.SERVER_URL}/api/billing/capture",
                    "cancel_url": f"{settings.SERVER_URL}/pricing?cancelled=true",
                },
            },
        },
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    approval_url = ""
    for link in data.get("links", []):
        if link.get("rel") == "payer-action":
            approval_url = link["href"]
            break

    if not approval_url:
        logger.error(f"PayPal order created but no approval link: {data}")
        raise RuntimeError("PayPal did not return an approval URL")

    logger.info(f"PayPal order {data.get('id')} created for user {user_id} ({price} EUR)")
    return approval_url


# ── Capture Order ────────────────────────────────────────────

async def capture_order(order_token: str) -> dict:
    """Capture a PayPal order after user approval.

    Returns dict with capture status and user info.
    """
    token = await _get_access_token()
    url = f"{_api_base()}/v2/checkout/orders/{order_token}/capture"

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        if resp.status_code == 422:
            body = resp.json()
            details = body.get("details", [])
            if any(d.get("issue") == "ORDER_ALREADY_CAPTURED" for d in details):
                logger.info(f"Order {order_token} was already captured")
                return {"already_captured": True}
        resp.raise_for_status()
        data = resp.json()

    status = data.get("status", "")
    custom_id = ""
    payer_email = data.get("payer", {}).get("email_address", "").lower()

    for pu in data.get("purchase_units", []):
        for capture in pu.get("payments", {}).get("captures", []):
            custom_id = capture.get("custom_id", "") or pu.get("custom_id", "")
            break
        if not custom_id:
            custom_id = pu.get("custom_id", "")

    logger.info(f"PayPal capture: order={order_token} status={status} custom_id={custom_id} email={payer_email}")

    if status != "COMPLETED":
        return {"captured": False, "status": status}

    user = await _resolve_user(custom_id, payer_email)
    if not user:
        logger.warning(f"Capture: could not match user for custom_id={custom_id} email={payer_email}")
        return {"captured": True, "activated": False, "reason": "user_not_found"}

    user_id = user["id"]
    premium_until = datetime.utcnow() + timedelta(days=PREMIUM_DAYS)

    await update_user_billing(user_id, is_premium=True, subscription_status="active")
    await upsert_subscription(
        user_id=user_id,
        purchase_token=order_token,
        product_id="paypal_onetime",
        status="active",
        current_period_end=premium_until.strftime("%Y-%m-%dT%H:%M:%S"),
        platform="paypal",
    )
    await set_trial_used_on_subscription(user_id)
    logger.info(f"User {user_id} activated via PayPal until {premium_until.date()} (order={order_token})")

    return {"captured": True, "activated": True, "user_id": user_id, "premium_until": str(premium_until.date())}


# ── Webhook Signature Verification ──────────────────────────

async def verify_webhook_signature(headers: dict, raw_body: bytes) -> bool:
    webhook_id = settings.PAYPAL_WEBHOOK_ID
    if not webhook_id:
        logger.warning("PAYPAL_WEBHOOK_ID not set — skipping signature verification")
        return True

    token = await _get_access_token()
    url = f"{_api_base()}/v1/notifications/verify-webhook-signature"

    verify_payload = {
        "auth_algo": headers.get("paypal-auth-algo", ""),
        "cert_url": headers.get("paypal-cert-url", ""),
        "transmission_id": headers.get("paypal-transmission-id", ""),
        "transmission_sig": headers.get("paypal-transmission-sig", ""),
        "transmission_time": headers.get("paypal-transmission-time", ""),
        "webhook_id": webhook_id,
        "webhook_event": __import__("json").loads(raw_body),
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            url,
            json=verify_payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        if resp.status_code != 200:
            logger.warning(f"PayPal webhook verification API returned {resp.status_code}")
            return False
        result = resp.json()

    status = result.get("verification_status", "")
    if status != "SUCCESS":
        logger.warning(f"PayPal webhook signature FAILED: {status}")
        return False

    return True


# ── Webhook Event Processing ────────────────────────────────

async def process_webhook_event(event: dict) -> dict:
    event_type = event.get("event_type", "")
    resource = event.get("resource", {})

    logger.info(f"PayPal webhook: type={event_type}")

    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        custom_id = resource.get("custom_id", "")
        payer_email = ""
        amount = resource.get("amount", {})
        capture_id = resource.get("id", "")

        user = await _resolve_user(custom_id, payer_email)
        if not user:
            logger.warning(f"Webhook: could not match user for custom_id={custom_id}")
            return {"processed": False, "reason": "user_not_found"}

        user_id = user["id"]
        premium_until = datetime.utcnow() + timedelta(days=PREMIUM_DAYS)

        await update_user_billing(user_id, is_premium=True, subscription_status="active")
        await upsert_subscription(
            user_id=user_id,
            purchase_token=capture_id,
            product_id="paypal_onetime",
            status="active",
            current_period_end=premium_until.strftime("%Y-%m-%dT%H:%M:%S"),
            platform="paypal",
        )
        await set_trial_used_on_subscription(user_id)
        logger.info(f"Webhook: User {user_id} activated until {premium_until.date()}")
        return {"processed": True, "action": "activated", "user_id": user_id}

    elif event_type == "PAYMENT.CAPTURE.REFUNDED":
        custom_id = resource.get("custom_id", "")
        user = await _resolve_user(custom_id, "")
        if user:
            await update_user_billing(user["id"], is_premium=False, subscription_status="refunded")
            logger.info(f"Webhook: User {user['id']} deactivated (refund)")
            return {"processed": True, "action": "deactivated"}
        return {"processed": False, "reason": "user_not_found"}

    else:
        logger.info(f"PayPal webhook ignored: event_type={event_type}")
        return {"processed": True, "action": "ignored", "event_type": event_type}


async def _resolve_user(custom_id: str, email: str) -> dict | None:
    if custom_id:
        uid_str = custom_id.replace("user_", "")
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
        "paypal_configured": paypal_configured(),
    }
