"""Shared setup for the demo services: settings, logging, feature flags, health, and metrics."""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests handled, by route and status code.",
    ["service", "route", "status"],
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["service", "route"],
    # Extra buckets between 1s and 2.5s so p95 estimates near the 1s alert threshold are accurate.
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 2.5, 5.0, 10.0),
)
BUILD_INFO = Gauge(
    "app_build_info",
    "Always 1; labels identify the running version of each service.",
    ["service", "version"],
)
START_TIME = Gauge(
    "app_start_time_seconds",
    "Unix time this service process started. Set once, so it only changes on a real restart.",
    ["service"],
)


class DemoSettings(BaseSettings):
    """Configuration for the demo services. Defaults match the Compose service names.

    The release settings below come from each service's file in the deploy repo. Their defaults
    are the healthy values; the fault injectors deploy bad values to break the stack.
    """

    app_version: str = "dev"
    orders_url: str = "http://orders:8000"
    payments_url: str = "http://payments:8000"
    gateway_url: str = "http://gateway:8000"
    database_url: str = "postgresql://orders_app:orders_app@postgres:5432/sentinelops"
    http_timeout_seconds: float = 3.0
    loadgen_rps: float = 3.0
    flags_path: Path = Path("/flags/flags.json")
    provider_conditions_path: Path = Path("/provider/conditions.json")

    # orders release settings
    orders_receipt_cache_max_items: int = Field(default=50, ge=0)  # 0 means unlimited
    orders_db_pool_mode: Literal["per_request", "persistent"] = "per_request"

    # payments release settings
    payments_api_key: SecretStr | None = None
    payments_fraud_check_rate: float = Field(default=0.0, ge=0.0, le=1.0)


class FeatureFlags:
    """Boolean feature flags read from a JSON file, reloaded whenever the file changes.

    File format: `{"flags": {"name": true}, "updated_by": "...", "updated_at": "..."}`.
    Every change is logged so flag flips leave evidence in the service logs.
    """

    def __init__(self, path: Path, service: str) -> None:
        self.path = path
        self.service = service
        self._flags: dict[str, bool] = {}
        self._mtime_ns: int | None = None
        self._warned_missing = False

    def enabled(self, name: str) -> bool:
        """Return whether `name` is on. Unknown flags are off."""
        self._reload_if_changed()
        return self._flags.get(name, False)

    def _reload_if_changed(self) -> None:
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            if not self._warned_missing:
                logger.warning(
                    "%s: flags file %s not found; all flags off", self.service, self.path
                )
                self._warned_missing = True
            self._flags, self._mtime_ns = {}, None
            return
        if mtime_ns == self._mtime_ns:
            return
        data: dict[str, Any] = json.loads(self.path.read_text())
        new_flags = {str(k): bool(v) for k, v in data.get("flags", {}).items()}
        for name in sorted(set(self._flags) | set(new_flags)):
            old, new = self._flags.get(name, False), new_flags.get(name, False)
            if self._mtime_ns is not None and old != new:
                logger.warning(
                    "%s: feature flag %s changed %s -> %s (updated_by=%s)",
                    self.service,
                    name,
                    old,
                    new,
                    data.get("updated_by", "unknown"),
                )
        self._flags, self._mtime_ns = new_flags, mtime_ns


def create_app(
    service: str,
    settings: DemoSettings,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    """Create a FastAPI app with `/health`, `/metrics`, and request metrics for `service`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app = FastAPI(title=service, lifespan=lifespan)
    BUILD_INFO.labels(service=service, version=settings.app_version).set(1)
    START_TIME.labels(service=service).set(time.time())

    @app.middleware("http")
    async def record_metrics(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path == "/metrics":
            return await call_next(request)
        start = time.perf_counter()
        status = "500"
        try:
            response = await call_next(request)
            status = str(response.status_code)
            return response
        finally:
            route = request.scope.get("route")
            route_label = getattr(route, "path", "unmatched")
            REQUESTS.labels(service=service, route=route_label, status=status).inc()
            LATENCY.labels(service=service, route=route_label).observe(time.perf_counter() - start)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": service, "version": settings.app_version}

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def describe_http_error(exc: httpx.HTTPError) -> str:
    """Summarize an upstream HTTP error, including the response body when there is one."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{exc.response.status_code} from {exc.request.url}: {exc.response.text[:300]}"
    return repr(exc)
