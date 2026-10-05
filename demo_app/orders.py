"""Orders service: stores the order in Postgres, then charges it through payments."""

import logging
from collections.abc import Iterator
from typing import Annotated

import httpx
import psycopg
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from demo_app.common import DemoSettings, create_app

logger = logging.getLogger(__name__)
settings = DemoSettings()
app = create_app("orders", settings)


class OrderRequest(BaseModel):
    item: str = "widget"
    amount_cents: int = Field(default=1999, gt=0)


class OrderResponse(BaseModel):
    order_id: int
    item: str
    amount_cents: int
    charge_id: str


def get_db() -> Iterator[psycopg.Connection]:
    """Open one Postgres connection per request. Commits on success, rolls back on error."""
    with psycopg.connect(settings.database_url, connect_timeout=3) as conn:
        yield conn


def get_payments_client() -> Iterator[httpx.Client]:
    """HTTP client for the payments service."""
    with httpx.Client(
        base_url=settings.payments_url, timeout=settings.http_timeout_seconds
    ) as client:
        yield client


@app.post("/orders")
def create_order(
    request: OrderRequest,
    db: Annotated[psycopg.Connection, Depends(get_db)],
    payments: Annotated[httpx.Client, Depends(get_payments_client)],
) -> OrderResponse:
    """Insert the order, charge it, and mark it paid. Payment failures return 502."""
    row = db.execute(
        "INSERT INTO orders (item, amount_cents, status) VALUES (%s, %s, 'pending') RETURNING id",
        (request.item, request.amount_cents),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=500, detail="insert returned no order id")
    order_id = row[0]

    try:
        response = payments.post(
            "/charge", json={"order_id": order_id, "amount_cents": request.amount_cents}
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("payments call failed for order %s: %r", order_id, exc)
        raise HTTPException(status_code=502, detail=f"payments unavailable: {exc!r}") from exc

    charge_id = response.json()["charge_id"]
    db.execute(
        "UPDATE orders SET status = 'paid', charge_id = %s WHERE id = %s", (charge_id, order_id)
    )
    return OrderResponse(
        order_id=order_id,
        item=request.item,
        amount_cents=request.amount_cents,
        charge_id=charge_id,
    )
