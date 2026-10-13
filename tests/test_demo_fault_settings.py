"""Tests for the demo services' fault-capable release settings and feature flags.

Each test turns one setting to its "bad" value and checks the service fails the way the
matching fault scenario expects, and that the default value keeps it healthy.
"""

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from demo_app import common, gateway, orders, payments
from demo_app.common import DemoSettings, FeatureFlags, describe_http_error


def write_flags(path: Path, flags: dict[str, bool], updated_by: str = "test") -> None:
    path.write_text(json.dumps({"flags": flags, "updated_by": updated_by}))


# payments: missing secret


def test_payments_refuses_to_start_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(payments.settings, "payments_api_key", None)

    with pytest.raises(RuntimeError, match="PAYMENTS_API_KEY is not set"):
        with TestClient(payments.app):
            pass


def test_payments_starts_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(payments.settings, "payments_api_key", SecretStr("key"))

    with TestClient(payments.app) as client:
        assert client.get("/health").status_code == 200


# payments: bad deploy (fraud check bug) and slow provider


def test_fraud_check_bug_fails_every_sampled_charge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(payments.settings, "payments_fraud_check_rate", 1.0)
    client = TestClient(payments.app, raise_server_exceptions=False)

    response = client.post("/charge", json={"order_id": 1, "amount_cents": 100})

    assert response.status_code == 500


def test_fraud_check_raises_key_error() -> None:
    with pytest.raises(KeyError, match="risk_score"):
        payments.run_fraud_check(payments.ChargeRequest(order_id=1, amount_cents=100))


# payments: slow external provider


def test_provider_conditions_default_to_no_latency(tmp_path: Path) -> None:
    assert payments.ProviderConditions(tmp_path / "missing.json").latency_ms() == 0


def test_provider_conditions_reload_when_file_changes(tmp_path: Path) -> None:
    path = tmp_path / "conditions.json"
    path.write_text(json.dumps({"latency_ms": 0}))
    conditions = payments.ProviderConditions(path)
    assert conditions.latency_ms() == 0

    path.write_text(json.dumps({"latency_ms": 1500}))
    path.touch()

    assert conditions.latency_ms() == 1500


def test_slow_provider_delays_charges_and_is_logged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "conditions.json"
    path.write_text(json.dumps({"latency_ms": 1500}))
    monkeypatch.setattr(payments, "provider", payments.ProviderConditions(path))
    clock = iter([0.0, 1.5])
    monkeypatch.setattr(payments.time, "perf_counter", lambda: next(clock))
    sleeps: list[float] = []
    monkeypatch.setattr(payments.time, "sleep", sleeps.append)

    with caplog.at_level(logging.WARNING, logger=payments.__name__):
        payments.call_provider(order_id=9)

    assert sleeps == [1.5]
    assert "payment provider slow: charge for order 9 took 1.50s" in caplog.text


# orders: memory leak (receipt cache) and connection leak (pool mode)


@pytest.fixture
def empty_receipt_cache() -> None:
    orders.receipt_cache.clear()


@pytest.mark.usefixtures("empty_receipt_cache")
def test_receipt_cache_is_bounded_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orders.settings, "orders_receipt_cache_max_items", 3)

    for order_id in range(10):
        orders.cache_receipt(order_id)

    assert list(orders.receipt_cache) == [7, 8, 9]


@pytest.mark.usefixtures("empty_receipt_cache")
def test_receipt_cache_grows_without_limit_when_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orders.settings, "orders_receipt_cache_max_items", 0)

    for order_id in range(10):
        orders.cache_receipt(order_id)

    assert len(orders.receipt_cache) == 10


class FakeConn:
    def __init__(self) -> None:
        self.events: list[str] = []

    def __enter__(self) -> "FakeConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.events.append("closed")

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")


def run_get_db() -> FakeConn:
    """Drive the get_db dependency through one successful request."""
    gen = orders.get_db()
    conn = next(gen)
    with pytest.raises(StopIteration):
        next(gen)
    return conn  # type: ignore[return-value]


def test_per_request_pool_closes_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orders, "connect", FakeConn)

    conn = run_get_db()

    assert conn.events == ["closed"]


def test_persistent_pool_leaks_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orders.settings, "orders_db_pool_mode", "persistent")
    monkeypatch.setattr(orders, "connect", FakeConn)
    monkeypatch.setattr(orders, "persistent_connections", [])

    conns = [run_get_db() for _ in range(3)]

    assert len(orders.persistent_connections) == 3
    assert all(conn.events == ["commit"] for conn in conns)


# gateway: bad feature flag


def test_v2_pricing_bug_zeroes_the_price() -> None:
    assert gateway.apply_v2_pricing(1999) == 0


@pytest.mark.parametrize(("flag_on", "expected_amount"), [(False, 1999), (True, 0)])
def test_gateway_applies_v2_pricing_only_when_flag_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flag_on: bool, expected_amount: int
) -> None:
    flags_file = tmp_path / "flags.json"
    write_flags(flags_file, {"checkout_v2_pricing": flag_on})
    monkeypatch.setattr(gateway, "flags", FeatureFlags(flags_file, "gateway"))
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://orders")
    gateway.app.dependency_overrides[gateway.get_orders_client] = lambda: client
    try:
        TestClient(gateway.app).post("/checkout")
    finally:
        gateway.app.dependency_overrides.clear()

    assert sent[0]["amount_cents"] == expected_amount


# feature flag reader


def test_flags_missing_file_means_all_off(tmp_path: Path) -> None:
    assert FeatureFlags(tmp_path / "missing.json", "test").enabled("anything") is False


def test_flags_reload_and_log_changes(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "flags.json"
    write_flags(path, {"new_ui": False})
    flags = FeatureFlags(path, "gateway")
    assert flags.enabled("new_ui") is False

    write_flags(path, {"new_ui": True}, updated_by="alice")
    path.touch()  # make sure the modification time changes on fast filesystems
    with caplog.at_level(logging.WARNING, logger=common.__name__):
        assert flags.enabled("new_ui") is True

    assert "feature flag new_ui changed False -> True (updated_by=alice)" in caplog.text


# shared helpers


def test_describe_http_error_includes_status_and_body() -> None:
    request = httpx.Request("POST", "http://orders/orders")
    response = httpx.Response(422, text="amount_cents must be > 0", request=request)
    error = httpx.HTTPStatusError("bad", request=request, response=response)

    assert describe_http_error(error) == ("422 from http://orders/orders: amount_cents must be > 0")


def test_release_setting_defaults_are_healthy() -> None:
    defaults = DemoSettings(_env_file=None)

    assert defaults.orders_receipt_cache_max_items > 0
    assert defaults.orders_db_pool_mode == "per_request"
    assert defaults.payments_fraud_check_rate == 0.0
