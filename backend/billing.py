"""PayPal billing: Subscriptions API + Webhook verification.

Flow:
  1. Backend creates a PayPal subscription via REST API
  2. Returns the approval URL to the frontend
  3. User approves on PayPal's hosted page
  4. PayPal sends webhook POST to /api/paypal/webhook
  5. We verify the webhook signature and activate premium
  6. User is redirected back to /app?subscription=success
"""

import hashlib
import logging
from datetime import datetime

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


def _api_base() -> str:
    return _API_SANDBOX if settings.PAYPAL_SANDBOX else _API_LIVE


def paypal_configured() -> bool:
    return bool(
        settings.PAYPAL_CLIENT_ID
        and settings.PAYPAL_CLIENT_SECRET
        and settings.PAYPAL_PLAN_ID
    )


# ── OAuth2 Access Token ─────────────────────────────────────

async def _get_access_token() -> str:
    """Get a PayPal OAuth2 access token using client credentials."""
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


# ── Create Subscription ─────────────────────────────────────

async def create_subscription(user_id: int, email: str) -> str:
    """Create a PayPal subscription and return the approval URL.

    The custom_id field carries our user_id so we can match the
    webhook event back to the correct user.
    """
    if not paypal_configured():
        raise RuntimeError("PayPal is not configured (missing CLIENT_ID, SECRET, or PLAN_ID)")

    token = await _get_access_token()
    url = f"{_api_base()}/v1/billing/subscriptions"

    payload = {
        "plan_id": settings.PAYPAL_PLAN_ID,
        "custom_id": f"user_{user_id}",
        "subscriber": {
            "email_address": email,
        },
        "application_context": {
            "brand_name": "Albanian Law AI",
            "locale": "sq-AL",
            "shipping_preference": "NO_SHIPPING",
            "user_action": "SUBSCRIBE_NOW",
            "return_url": f"{settings.SERVER_URL}/app?subscription=success",
            "cancel_url": f"{settings.SERVER_URL}/pricing?cancelled=true",
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
        if link.get("rel") == "approve":
            approval_url = link["href"]
            break

    if not approval_url:
        logger.error(f"PayPal subscription created but no approval link: {data}")
        raise RuntimeError("PayPal did not return an approval URL")

    logger.info(f"PayPal subscription {data.get('id')} created for user {user_id}")
    return approval_url


# ── Webhook Signature Verification ──────────────────────────

async def verify_webhook_signature(
    headers: dict,
    raw_body: bytes,
) -> bool:
    """Verify a PayPal webhook event using the Notifications API.

    PayPal recommends server-side verification via their API rather
    than manual signature checking.
    """
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
    """Process a verified PayPal webhook event.

    Key event types:
      BILLING.SUBSCRIPTION.ACTIVATED  – subscription is now active
      BILLING.SUBSCRIPTION.CANCELLED  – user cancelled
      BILLING.SUBSCRIPTION.SUSPENDED  – payment failed / suspended
      BILLING.SUBSCRIPTION.EXPIRED    – subscription expired
      PAYMENT.SALE.COMPLETED          – recurring payment received
    """
    event_type = event.get("event_type", "")
    resource = event.get("resource", {})

    subscription_id = resource.get("id", "")
    custom_id = resource.get("custom_id", "")
    subscriber = resource.get("subscriber", {})
    subscriber_email = subscriber.get("email_address", "").strip().lower()
    status_detail = resource.get("status", "").upper()

    logger.info(
        f"PayPal webhook: type={event_type} sub_id={subscription_id} "
        f"custom_id={custom_id} email={subscriber_email} status={status_detail}"
    )

    user = await _resolve_user(custom_id, subscriber_email)
    if not user:
        logger.warning(f"Webhook: could not match user for custom_id={custom_id} email={subscriber_email}")
        return {"processed": False, "reason": "user_not_found"}

    user_id = user["id"]

    if event_type in (
        "BILLING.SUBSCRIPTION.ACTIVATED",
        "BILLING.SUBSCRIPTION.RENEWED",
        "PAYMENT.SALE.COMPLETED",
    ):
        await update_user_billing(user_id, is_premium=True, subscription_status="active")
        await upsert_subscription(
            user_id=user_id,
            purchase_token=subscription_id,
            product_id=settings.PAYPAL_PLAN_ID,
            status="active",
            current_period_end=None,
            platform="paypal",
        )
        await set_trial_used_on_subscription(user_id)
        logger.info(f"User {user_id} activated via PayPal (sub={subscription_id})")
        return {"processed": True, "action": "activated", "user_id": user_id}

    elif event_type in (
        "BILLING.SUBSCRIPTION.CANCELLED",
        "BILLING.SUBSCRIPTION.SUSPENDED",
        "BILLING.SUBSCRIPTION.EXPIRED",
    ):
        await update_user_billing(user_id, is_premium=False, subscription_status="canceled")
        await upsert_subscription(
            user_id=user_id,
            purchase_token=subscription_id,
            product_id=settings.PAYPAL_PLAN_ID,
            status="canceled",
            current_period_end=None,
            platform="paypal",
        )
        logger.info(f"User {user_id} deactivated via PayPal (sub={subscription_id}, event={event_type})")
        return {"processed": True, "action": "deactivated", "user_id": user_id}

    else:
        logger.info(f"PayPal webhook ignored: event_type={event_type} for user {user_id}")
        return {"processed": True, "action": "ignored", "event_type": event_type}


async def _resolve_user(custom_id: str, email: str) -> dict | None:
    """Find the user by custom_id (user_X) or email."""
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
        "paypal_configured": paypal_configured(),
    }
