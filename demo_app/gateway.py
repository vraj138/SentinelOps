"""Gateway service: the public entry point. Forwards checkouts to the orders service."""

import logging
from collections.abc import Iterator
from typing import Annotated, Any

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from demo_app.common import DemoSettings, FeatureFlags, create_app, describe_http_error

logger = logging.getLogger(__name__)
settings = DemoSettings()
flags = FeatureFlags(settings.flags_path, "gateway")
app = create_app("gateway", settings)

V2_PRICING_DISCOUNT_PERCENT = 15


class CheckoutRequest(BaseModel):
    item: str = "widget"
    amount_cents: int = Field(default=1999, gt=0)


def get_orders_client() -> Iterator[httpx.Client]:
    """HTTP client for the orders service."""
    with httpx.Client(
        base_url=settings.orders_url, timeout=settings.http_timeout_seconds
    ) as client:
        yield client


def apply_v2_pricing(amount_cents: int) -> int:
    """New pricing rules behind the `checkout_v2_pricing` flag.

    Contains a real bug: the discount is applied as a whole number instead of a percentage,
    so every price drops to 0 and downstream validation rejects the order.
    """
    discount = amount_cents * V2_PRICING_DISCOUNT_PERCENT
    return max(amount_cents - discount, 0)


@app.post("/checkout")
def checkout(
    orders: Annotated[httpx.Client, Depends(get_orders_client)],
    request: CheckoutRequest | None = None,
) -> dict[str, Any]:
    """Place an order. Any failure from orders is surfaced as a 502, never hidden."""
    body = (request or CheckoutRequest()).model_dump()
    if flags.enabled("checkout_v2_pricing"):
        body["amount_cents"] = apply_v2_pricing(body["amount_cents"])
    try:
        response = orders.post("/orders", json=body)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        reason = describe_http_error(exc)
        logger.error("orders call failed: %s", reason)
        raise HTTPException(status_code=502, detail=f"orders call failed: {reason}") from exc
    return response.json()
