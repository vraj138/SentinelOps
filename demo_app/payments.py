"""Payments service: the end of the request chain. Returns a fake charge."""

import json
import logging
import random
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel, Field

from demo_app.common import DemoSettings, create_app

logger = logging.getLogger(__name__)
settings = DemoSettings()

SLOW_PROVIDER_SECONDS = 1.0


class ProviderConditions:
    """How the external payment provider is behaving right now.

    The provider is outside our control, so its behavior lives in a file that is not part of
    any deploy: `{"latency_ms": 0}`. Reloaded whenever the file changes.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._latency_ms = 0
        self._mtime_ns: int | None = None

    def latency_ms(self) -> int:
        """Extra time the provider currently takes to answer a charge."""
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self._latency_ms, self._mtime_ns = 0, None
            return 0
        if mtime_ns != self._mtime_ns:
            self._latency_ms = int(json.loads(self.path.read_text()).get("latency_ms", 0))
            self._mtime_ns = mtime_ns
        return self._latency_ms


provider = ProviderConditions(settings.provider_conditions_path)


def check_api_key(current: DemoSettings) -> None:
    """Refuse to start without the payment provider API key."""
    if current.payments_api_key is None or not current.payments_api_key.get_secret_value():
        raise RuntimeError("PAYMENTS_API_KEY is not set; payments cannot reach the provider")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    check_api_key(settings)
    logger.info("payments %s started", settings.app_version)
    yield


app = create_app("payments", settings, lifespan=lifespan)


class ChargeRequest(BaseModel):
    order_id: int
    amount_cents: int = Field(gt=0)


class ChargeResponse(BaseModel):
    charge_id: str
    order_id: int
    status: str


def run_fraud_check(request: ChargeRequest) -> None:
    """Score the charge for fraud risk.

    Contains a real bug: it reads a `risk_score` signal that is never populated, so every
    sampled charge raises KeyError. Disabled by default (`payments_fraud_check_rate=0`).
    """
    signals = {"order_id": request.order_id, "amount_cents": request.amount_cents}
    if signals["risk_score"] > 0.8:
        raise ValueError(f"charge for order {request.order_id} flagged as fraud")


@app.post("/charge")
def charge(request: ChargeRequest) -> ChargeResponse:
    """Charge the order through the (simulated) payment provider."""
    if random.random() < settings.payments_fraud_check_rate:
        run_fraud_check(request)
    call_provider(request.order_id)
    return ChargeResponse(
        charge_id=f"ch_{uuid.uuid4().hex[:12]}", order_id=request.order_id, status="succeeded"
    )


def call_provider(order_id: int) -> None:
    """Simulated call to the external payment provider. Slow calls are logged."""
    start = time.perf_counter()
    latency_ms = provider.latency_ms()
    if latency_ms:
        time.sleep(latency_ms / 1000)
    elapsed = time.perf_counter() - start
    if elapsed > SLOW_PROVIDER_SECONDS:
        logger.warning("payment provider slow: charge for order %s took %.2fs", order_id, elapsed)
