"""Orders service: stores the order in Postgres, then charges it through payments."""

import logging
import os
from collections import OrderedDict
from collections.abc import Iterator
from typing import Annotated

import httpx
import psycopg
from fastapi import Depends, HTTPException
from prometheus_client import Gauge
from pydantic import BaseModel, Field

from demo_app.common import DemoSettings, create_app, describe_http_error

logger = logging.getLogger(__name__)
settings = DemoSettings()
app = create_app("orders", settings)

DB_CONNECTIONS = Gauge("db_connections_open", "Postgres connections held by this process.")
RECEIPT_CACHE_ITEMS = Gauge("receipt_cache_items", "Receipts held in the in-memory cache.")
RECEIPT_SIZE_BYTES = 128 * 1024  # a rendered PDF receipt

receipt_cache: OrderedDict[int, bytes] = OrderedDict()
persistent_connections: list[psycopg.Connection] = []


class OrderRequest(BaseModel):
    item: str = "widget"
    amount_cents: int = Field(default=1999, gt=0)


class OrderResponse(BaseModel):
    order_id: int
    item: str
    amount_cents: int
    charge_id: str


def connect() -> psycopg.Connection:
    """Open a Postgres connection."""
    return psycopg.connect(settings.database_url, connect_timeout=3)


def get_db() -> Iterator[psycopg.Connection]:
    """A Postgres connection for one request. Commits on success, rolls back on error.

    In `persistent` pool mode the connection is kept for reuse but never reused or closed,
    a connection leak that eventually exhausts Postgres `max_connections`.
    """
    if settings.orders_db_pool_mode == "persistent":
        conn = connect()
        persistent_connections.append(conn)
        DB_CONNECTIONS.set(len(persistent_connections))
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        return

    with connect() as conn:
        DB_CONNECTIONS.inc()
        try:
            yield conn
        finally:
            DB_CONNECTIONS.dec()


def get_payments_client() -> Iterator[httpx.Client]:
    """HTTP client for the payments service."""
    with httpx.Client(
        base_url=settings.payments_url, timeout=settings.http_timeout_seconds
    ) as client:
        yield client


def cache_receipt(order_id: int) -> None:
    """Keep a rendered receipt in memory, evicting the oldest past the configured limit.

    A limit of 0 means unlimited, so the cache grows until the container runs out of memory.
    """
    receipt_cache[order_id] = os.urandom(RECEIPT_SIZE_BYTES)
    limit = settings.orders_receipt_cache_max_items
    while limit and len(receipt_cache) > limit:
        receipt_cache.popitem(last=False)
    RECEIPT_CACHE_ITEMS.set(len(receipt_cache))


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
        reason = describe_http_error(exc)
        logger.error("payments call failed for order %s: %s", order_id, reason)
        raise HTTPException(status_code=502, detail=f"payments call failed: {reason}") from exc

    charge_id = response.json()["charge_id"]
    db.execute(
        "UPDATE orders SET status = 'paid', charge_id = %s WHERE id = %s", (charge_id, order_id)
    )
    cache_receipt(order_id)
    return OrderResponse(
        order_id=order_id,
        item=request.item,
        amount_cents=request.amount_cents,
        charge_id=charge_id,
    )
