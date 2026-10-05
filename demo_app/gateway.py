"""Gateway service: the public entry point. Forwards checkouts to the orders service."""

import logging
from collections.abc import Iterator
from typing import Annotated, Any

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from demo_app.common import DemoSettings, create_app

logger = logging.getLogger(__name__)
settings = DemoSettings()
app = create_app("gateway", settings)


class CheckoutRequest(BaseModel):
    item: str = "widget"
    amount_cents: int = Field(default=1999, gt=0)


def get_orders_client() -> Iterator[httpx.Client]:
    """HTTP client for the orders service."""
    with httpx.Client(
        base_url=settings.orders_url, timeout=settings.http_timeout_seconds
    ) as client:
        yield client


@app.post("/checkout")
def checkout(
    orders: Annotated[httpx.Client, Depends(get_orders_client)],
    request: CheckoutRequest | None = None,
) -> dict[str, Any]:
    """Place an order. Any failure from orders is surfaced as a 502, never hidden."""
    body = request or CheckoutRequest()
    try:
        response = orders.post("/orders", json=body.model_dump())
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("orders call failed: %r", exc)
        raise HTTPException(status_code=502, detail=f"orders unavailable: {exc!r}") from exc
    return response.json()
