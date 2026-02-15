"""PayPal subscription: create subscription, confirm return, webhook."""

import httpx
from fastapi import HTTPException
from datetime import datetime

from backend.config import settings
from backend.database import upsert_subscription_paypal, set_trial_used_on_subscription

BASE_URL_SANDBOX = "https://api-m.sandbox.paypal.com"
BASE_URL_LIVE = "https://api-m.paypal.com"

_SUBSCRIPTION_PRICE_EUR = 9.99


def _base_url() -> str:
    return BASE_URL_LIVE if (settings.PAYPAL_MODE or "").lower() == "live" else BASE_URL_SANDBOX


async def _get_access_token() -> str:
    """Get PayPal OAuth2 access token."""
    if not settings.PAYPAL_CLIENT_ID or not settings.PAYPAL_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="PayPal not configured")
    auth = (settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET)
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{_base_url()}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=auth,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )
    r.raise_for_status()
    data = r.json()
    return data["access_token"]


def _normalize_status(paypal_status: str) -> str:
    """Map PayPal subscription status to our status."""
    s = (paypal_status or "").upper()
    if s in ("ACTIVE", "APPROVAL_PENDING"):
        return "active"
    if s in ("CANCELLED", "CANCELED", "EXPIRED", "SUSPENDED"):
        return "canceled"
    return s.lower() if s else "active"


async def create_subscription(user_id: int, return_url: str, cancel_url: str) -> tuple[str, str]:
    """
    Create a PayPal subscription. Returns (approval_url, subscription_id).
    User must be sent to approval_url to approve; after approval PayPal redirects to return_url with token=subscription_id.
    """
    if not settings.PAYPAL_PLAN_ID:
        raise HTTPException(status_code=503, detail="PayPal plan not configured (PAYPAL_PLAN_ID)")
    token = await _get_access_token()
    payload = {
        "plan_id": settings.PAYPAL_PLAN_ID,
        "custom_id": str(user_id),
        "application_context": {
            "return_url": return_url,
            "cancel_url": cancel_url,
            "brand_name": "Albanian Law AI",
        },
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{_base_url()}/v1/billing/subscriptions",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"PayPal error: {r.text}")
    data = r.json()
    sub_id = data.get("id")
    if not sub_id:
        raise HTTPException(status_code=502, detail="PayPal did not return subscription id")
    approval_url = None
    for link in data.get("links", []):
        if link.get("rel") == "approve":
            approval_url = link.get("href")
            break
    if not approval_url:
        raise HTTPException(status_code=502, detail="PayPal did not return approval link")
    return approval_url, sub_id


async def get_subscription_details(subscription_id: str) -> dict | None:
    """Fetch subscription from PayPal and return dict with status, custom_id (user_id), next_billing_time."""
    token = await _get_access_token()
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{_base_url()}/v1/billing/subscriptions/{subscription_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


async def confirm_subscription(subscription_id: str) -> bool:
    """
    After user approved on PayPal, sync subscription to our DB.
    Returns True if subscription is active and was saved.
    """
    details = await get_subscription_details(subscription_id)
    if not details:
        return False
    status = _normalize_status(details.get("status"))
    custom_id = details.get("custom_id")
    if not custom_id:
        return False
    user_id = int(custom_id)
    # PayPal: start_time or billing_info.next_billing_time
    current_period_end = ""
    billing_info = details.get("billing_info") or {}
    next_billing = billing_info.get("next_billing_time")
    if next_billing:
        current_period_end = next_billing
    else:
        start_time = details.get("start_time")
        if start_time:
            # Assume 1 month from start
            try:
                dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                from datetime import timedelta
                end_dt = dt + timedelta(days=31)
                current_period_end = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                current_period_end = ""
    await upsert_subscription_paypal(user_id, subscription_id, status, current_period_end)
    if status == "active":
        await set_trial_used_on_subscription(user_id)
    return status == "active"


async def handle_webhook(body: dict) -> None:
    """Process PayPal webhook event. Verifies signature if PAYPAL_WEBHOOK_ID is set."""
    event_type = body.get("event_type")
    resource = body.get("resource", {}) or {}
    sub_id = resource.get("id")
    if not sub_id:
        return
    if event_type in ("BILLING.SUBSCRIPTION.ACTIVATED", "BILLING.SUBSCRIPTION.UPDATED"):
        details = await get_subscription_details(sub_id)
        if not details:
            return
        status = _normalize_status(details.get("status"))
        custom_id = details.get("custom_id")
        if not custom_id:
            return
        user_id = int(custom_id)
        billing_info = details.get("billing_info") or {}
        current_period_end = billing_info.get("next_billing_time") or ""
        await upsert_subscription_paypal(user_id, sub_id, status, current_period_end)
        if status == "active":
            await set_trial_used_on_subscription(user_id)
    elif event_type in ("BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.EXPIRED", "BILLING.SUBSCRIPTION.SUSPENDED"):
        custom_id = resource.get("custom_id")
        if custom_id:
            user_id = int(custom_id)
            await upsert_subscription_paypal(user_id, sub_id, "canceled", "")
