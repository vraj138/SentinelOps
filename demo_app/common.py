"""Shared setup for the demo services: settings, health check, and Prometheus metrics."""

import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic_settings import BaseSettings

REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests handled, by route and status code.",
    ["service", "route", "status"],
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["service", "route"],
)
BUILD_INFO = Gauge(
    "app_build_info",
    "Always 1; labels identify the running version of each service.",
    ["service", "version"],
)


class DemoSettings(BaseSettings):
    """Configuration shared by the demo services. Defaults match the Compose service names."""

    app_version: str = "dev"
    orders_url: str = "http://orders:8000"
    payments_url: str = "http://payments:8000"
    gateway_url: str = "http://gateway:8000"
    database_url: str = "postgresql://sentinelops:sentinelops@postgres:5432/sentinelops"
    http_timeout_seconds: float = 3.0
    loadgen_rps: float = 3.0


def create_app(service: str, settings: DemoSettings) -> FastAPI:
    """Create a FastAPI app with `/health`, `/metrics`, and request metrics for `service`."""
    app = FastAPI(title=service)
    BUILD_INFO.labels(service=service, version=settings.app_version).set(1)

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
