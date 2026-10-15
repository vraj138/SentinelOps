"""Fleet data: every product's live health, its team, and its incidents. No HTML here.

Application services are measured from Prometheus. Shared infrastructure (Postgres) has no
exporter in this stack, so its status comes from the container's own health check instead.
If a source can't be reached, the status says so rather than showing stale or invented numbers.
"""

import statistics
from typing import Any, Protocol

from mcp_servers.common import ToolError
from sentinelops.catalog import Product
from sentinelops.controlplane.models import TERMINAL, Incident, State

ERROR_RATIO_MAX = 0.05
P95_MAX_SECONDS = 1.0
SEVERITY = {"down": 3, "degraded": 2, "unknown": 1, "metrics unavailable": 1, "healthy": 0}


class Metrics(Protocol):
    def query(self, promql: str) -> list[dict]: ...


class Containers(Protocol):
    def status(self, service: str) -> dict[str, Any]: ...


def _value(metrics: Metrics, promql: str) -> float | None:
    result = metrics.query(promql)
    return float(result[0]["value"][1]) if result else None


def service_health(metrics: Metrics, service: str) -> dict[str, Any]:
    """Up, request rate, 5xx ratio and p95 over the last minute, and a status from them."""
    up = _value(metrics, f'up{{job="demo",service="{service}"}}')
    traffic = f'http_requests_total{{service="{service}",route!="/health"}}'
    rps = _value(metrics, f"sum(rate({traffic}[1m]))")
    errors = _value(
        metrics,
        f'(sum(rate(http_requests_total{{service="{service}",status=~"5.."}}[1m])) or vector(0))'
        f" / sum(rate({traffic}[1m]))",
    )
    p95 = _value(
        metrics,
        "histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket"
        f'{{service="{service}",route!="/health"}}[1m])))',
    )
    if up != 1:
        status = "down"
    elif (errors or 0) > ERROR_RATIO_MAX or (p95 or 0) > P95_MAX_SECONDS:
        status = "degraded"
    else:
        status = "healthy"
    return {
        "service": service,
        "status": status,
        "source": "prometheus",
        "rps": rps,
        "errors": errors,
        "p95": p95,
    }


def container_health(containers: Containers | None, service: str) -> dict[str, Any]:
    """Status from Docker's view of the container and its health check (no metrics needed)."""
    base = {"service": service, "source": "container health check"}
    if containers is None:
        return {**base, "status": "unknown", "detail": "no metrics exporter"}
    try:
        state = containers.status(service)
    except ToolError as exc:
        return {**base, "status": "unknown", "detail": str(exc)}
    health = state.get("health")
    if not state.get("running"):
        status = "down"
    elif health in ("unhealthy", "starting"):
        status = "degraded"
    else:
        status = "healthy"
    return {
        **base,
        "status": status,
        "detail": f"{state.get('status')}, health check {health or 'none'}",
        "container": state,
    }


def with_alerts(health: dict[str, Any], firing: list[str]) -> dict[str, Any]:
    """A firing alert overrides a healthy reading: ServiceDown means down, others degraded."""
    if not firing:
        return health
    floor = "down" if "ServiceDown" in firing else "degraded"
    status = max(health["status"], floor, key=lambda st: SEVERITY.get(st, 1))
    return {**health, "status": status, "alerts": firing}


def product_health(
    metrics: Metrics | None,
    product: Product,
    containers: Containers | None = None,
    alerts: dict[str, list[str]] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """(overall status, per-service health). Never raises: unreachable sources are a status.

    `alerts` maps a service to the names of its firing alerts, so the status never says
    healthy while an alert on that service is firing.
    """
    services = _service_health(metrics, product, containers)
    if services is None:
        return "unknown", []
    services = [with_alerts(s, (alerts or {}).get(s["service"], [])) for s in services]
    overall = max((s["status"] for s in services), key=lambda st: SEVERITY.get(st, 1))
    return overall, services


def _service_health(
    metrics: Metrics | None, product: Product, containers: Containers | None
) -> list[dict[str, Any]] | None:
    if product.infrastructure:
        return [container_health(containers, s) for s in product.services]
    if metrics is None:
        return None
    try:
        return [service_health(metrics, s) for s in product.services]
    except ToolError as exc:
        return [
            {"service": s, "status": "metrics unavailable", "detail": str(exc)}
            for s in product.services
        ]


def incident_counts(incidents: list[Incident], product_id: str) -> dict[str, Any]:
    owned = [i for i in incidents if i.product == product_id]
    resolved = [i for i in owned if i.state == State.RESOLVED]
    minutes = [(i.updated_at - i.created_at).total_seconds() / 60 for i in resolved]
    return {
        "open": sum(1 for i in owned if i.state not in TERMINAL),
        "awaiting": sum(1 for i in owned if i.state == State.PROPOSED),
        "resolved": len(resolved),
        "escalated": sum(1 for i in owned if i.state == State.ESCALATED),
        "impacting": sum(
            1
            for i in incidents
            if product_id in i.impacted_products
            and i.product != product_id
            and i.state not in TERMINAL
        ),
        "median_minutes": statistics.median(minutes) if minutes else None,
    }


def percent(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}s"
