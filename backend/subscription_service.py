"""Stripe subscription: checkout session, webhook, and status."""

import stripe
from fastapi import HTTPException

from backend.config import settings
from backend.database import (
    get_user_by_id,
    set_stripe_customer_id,
    upsert_subscription,
    get_active_subscription,
    set_trial_used_on_subscription,
)

stripe.api_key = settings.STRIPE_SECRET_KEY

# Single plan: €9.99/month
SUBSCRIPTION_PRICE_EUR = 9.99


async def create_checkout_session(user_id: int, email: str, success_url: str, cancel_url: str) -> str:
    """Create Stripe Checkout Session for subscription. Returns session URL."""
    if not settings.STRIPE_SECRET_KEY or not settings.STRIPE_PRICE_ID:
        raise HTTPException(
            status_code=503,
            detail="Payment system not configured (STRIPE_SECRET_KEY, STRIPE_PRICE_ID)",
        )
    user = await get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        customer = stripe.Customer.create(email=email)
        customer_id = customer.id
        await set_stripe_customer_id(user_id, customer_id)

    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        line_items=[{"price": settings.STRIPE_PRICE_ID, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"user_id": str(user_id)},
        subscription_data={"metadata": {"user_id": str(user_id)}},
    )
    return session.url


async def get_subscription_status(user_id: int) -> dict | None:
    """Return active subscription info for user, or None."""
    sub = await get_active_subscription(user_id)
    if not sub:
        return None
    return {
        "status": sub["status"],
        "current_period_end": sub["current_period_end"],
        "price_eur": SUBSCRIPTION_PRICE_EUR,
    }


async def handle_webhook_event(event: stripe.Event):
    """Process Stripe webhook event. Caller verifies signature."""
    if event.type == "checkout.session.completed":
        session = event.data.object
        sub_id = session.get("subscription")
        if not sub_id:
            return
        subscription = stripe.Subscription.retrieve(sub_id)
        await _upsert_from_stripe_subscription(subscription)
    elif event.type == "customer.subscription.updated":
        await _upsert_from_stripe_subscription(event.data.object)
    elif event.type == "customer.subscription.deleted":
        sub = event.data.object
        meta = getattr(sub, "metadata", None) or {}
        user_id = meta.get("user_id") if isinstance(meta, dict) else getattr(meta, "user_id", None)
        if user_id:
            user_id = int(user_id)
            await upsert_subscription(
                user_id=user_id,
                stripe_subscription_id=sub.id,
                status="canceled",
                current_period_end="",
            )


def _get(obj, key, default=None):
    """Get attribute or dict key from Stripe object."""
    if hasattr(obj, "get") and callable(getattr(obj, "get")):
        return obj.get(key, default)
    return getattr(obj, key, default)


async def _upsert_from_stripe_subscription(subscription):
    meta = _get(subscription, "metadata") or {}
    user_id = meta.get("user_id") if isinstance(meta, dict) else getattr(meta, "user_id", None)
    if not user_id:
        return
    user_id = int(user_id)
    status = _get(subscription, "status") or "active"
    current_period_end = ""
    period_end = _get(subscription, "current_period_end")
    if period_end:
        from datetime import datetime
        current_period_end = datetime.utcfromtimestamp(period_end).isoformat()
    price_id = ""
    items = _get(subscription, "items") or {}
    data = items.get("data", []) if isinstance(items, dict) else getattr(items, "data", [])
    if data:
        first = data[0] if isinstance(data, list) else data
        price_obj = _get(first, "price") or {}
        price_id = price_obj.get("id", "") if isinstance(price_obj, dict) else getattr(price_obj, "id", "")
    sub_id = _get(subscription, "id") or ""
    await upsert_subscription(
        user_id,
        sub_id,
        status=status,
        current_period_end=current_period_end,
        stripe_price_id=price_id,
    )
    if status in ("active", "trialing"):
        await set_trial_used_on_subscription(user_id)
