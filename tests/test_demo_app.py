"""Tests for the demo services. No Docker or Postgres needed: upstreams and the DB are faked."""

from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from demo_app import gateway, loadgen, orders, payments

SERVICES = {"gateway": gateway.app, "orders": orders.app, "payments": payments.app}


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """An httpx client whose requests go to `handler` instead of the network."""
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://upstream")


class FakeConnection:
    """Stands in for a psycopg connection: records queries, returns order id 42 for inserts."""

    def __init__(self) -> None:
        self.queries: list[tuple[str, Any]] = []

    def execute(self, query: str, params: Any = None) -> "FakeConnection":
        self.queries.append((query, params))
        return self

    def fetchone(self) -> tuple[int]:
        return (42,)


@pytest.fixture
def override() -> Iterator[Callable[[FastAPI, Callable[..., Any], Any], None]]:
    """Override a FastAPI dependency for one test, then restore it."""
    apps: list[FastAPI] = []

    def _override(app: FastAPI, dependency: Callable[..., Any], value: Any) -> None:
        app.dependency_overrides[dependency] = lambda: value
        apps.append(app)

    yield _override
    for app in apps:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("name", SERVICES)
def test_health(name: str) -> None:
    response = TestClient(SERVICES[name]).get("/health")

    assert response.status_code == 200
    assert response.json()["service"] == name


@pytest.mark.parametrize("name", SERVICES)
def test_metrics_report_requests_and_build_info(name: str) -> None:
    client = TestClient(SERVICES[name])
    client.get("/health")

    body = client.get("/metrics").text

    assert f'http_requests_total{{route="/health",service="{name}",status="200"}}' in body
    assert f'app_build_info{{service="{name}",version="dev"}} 1.0' in body


def test_payments_returns_charge() -> None:
    response = TestClient(payments.app).post("/charge", json={"order_id": 7, "amount_cents": 500})

    assert response.status_code == 200
    assert response.json()["order_id"] == 7
    assert response.json()["charge_id"].startswith("ch_")


def test_payments_rejects_non_positive_amount() -> None:
    response = TestClient(payments.app).post("/charge", json={"order_id": 7, "amount_cents": 0})

    assert response.status_code == 422


def test_orders_saves_and_charges(override: Callable[..., None]) -> None:
    db = FakeConnection()
    override(orders.app, orders.get_db, db)
    override(
        orders.app,
        orders.get_payments_client,
        mock_client(lambda request: httpx.Response(200, json={"charge_id": "ch_test"})),
    )

    response = TestClient(orders.app).post("/orders", json={"item": "book", "amount_cents": 900})

    assert response.status_code == 200
    assert response.json() == {
        "order_id": 42,
        "item": "book",
        "amount_cents": 900,
        "charge_id": "ch_test",
    }
    assert db.queries[0][0].startswith("INSERT INTO orders")
    assert db.queries[1][1] == ("ch_test", 42)


def test_orders_returns_502_when_payments_fails(override: Callable[..., None]) -> None:
    db = FakeConnection()
    override(orders.app, orders.get_db, db)
    override(orders.app, orders.get_payments_client, mock_client(lambda r: httpx.Response(503)))

    response = TestClient(orders.app).post("/orders", json={})

    assert response.status_code == 502
    assert "payments unavailable" in response.json()["detail"]
    assert len(db.queries) == 1


def test_gateway_forwards_checkout(override: Callable[..., None]) -> None:
    order = {"order_id": 1, "item": "widget", "amount_cents": 1999, "charge_id": "ch_x"}
    override(
        gateway.app,
        gateway.get_orders_client,
        mock_client(lambda r: httpx.Response(200, json=order)),
    )

    response = TestClient(gateway.app).post("/checkout")

    assert response.status_code == 200
    assert response.json() == order


def test_gateway_returns_502_when_orders_unreachable(override: Callable[..., None]) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    override(gateway.app, gateway.get_orders_client, mock_client(refuse))

    response = TestClient(gateway.app).post("/checkout")

    assert response.status_code == 502
    assert "ConnectError" in response.json()["detail"]


def test_loadgen_counts_failures() -> None:
    statuses = iter([200, 502, 200])
    client = mock_client(lambda request: httpx.Response(next(statuses)))

    failures = loadgen.run(client, requests_per_second=1000, iterations=3)

    assert failures == 1
