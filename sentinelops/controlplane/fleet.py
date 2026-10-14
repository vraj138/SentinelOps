"""Fleet views: every product's live health, its team, and its incidents.

Live health comes from Prometheus. If Prometheus can't be reached, the page says "metrics
unavailable" instead of showing stale or invented numbers.
"""

import statistics
from html import escape
from typing import Any, Protocol

from mcp_servers.common import ToolError
from sentinelops.catalog import Product
from sentinelops.controlplane.models import TERMINAL, Incident, State
from sentinelops.controlplane.service import ControlPlane

ERROR_RATIO_MAX = 0.05
P95_MAX_SECONDS = 1.0


class Metrics(Protocol):
    def query(self, promql: str) -> list[dict]: ...


def _value(metrics: Metrics, promql: str) -> float | None:
    result = metrics.query(promql)
    return float(result[0]["value"][1]) if result else None


def service_health(metrics: Metrics, service: str) -> dict[str, Any]:
    """Up, 5xx ratio and p95 over the last minute, and a status derived from them."""
    up = _value(metrics, f'up{{job="demo",service="{service}"}}')
    errors = _value(
        metrics,
        f'(sum(rate(http_requests_total{{service="{service}",status=~"5.."}}[1m])) or vector(0))'
        f' / sum(rate(http_requests_total{{service="{service}",route!="/health"}}[1m]))',
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
    return {"service": service, "status": status, "errors": errors, "p95": p95}


def product_health(metrics: Metrics | None, product: Product) -> tuple[str, list[dict[str, Any]]]:
    """(overall status, per-service health). Never raises: unreachable metrics are a status."""
    if metrics is None:
        return "unknown", []
    try:
        services = [service_health(metrics, s) for s in product.services]
    except ToolError as exc:
        return "metrics unavailable", [{"service": s, "status": str(exc)} for s in product.services]
    statuses = {s["status"] for s in services}
    overall = "down" if "down" in statuses else "degraded" if "degraded" in statuses else "healthy"
    return overall, services


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


STATUS_CLASS = {"healthy": "ok", "degraded": "warn", "down": "bad"}


def percent(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}s"


def status_pill(status: str) -> str:
    return f'<span class="pill {STATUS_CLASS.get(status, "muted")}">{escape(status)}</span>'


def owner_label(plane: ControlPlane, incident: Incident) -> str:
    if not incident.product:
        return '<span class="muted">unassigned</span>'
    product = plane.catalog.product(incident.product)
    how = {
        "alerts": "likely, from alerts",
        "investigation": "from investigation",
        "operator": "set by operator",
    }.get(incident.owner_source or "", "")
    return (
        f'<a href="/products/{escape(product.id)}">{escape(product.name)}</a> '
        f'<span class="muted">{escape(product.team)} &middot; {escape(how)}</span>'
    )


def render_fleet(plane: ControlPlane, metrics: Metrics | None, incidents: list[Incident]) -> str:
    cards = []
    for product in plane.catalog.products:
        overall, services = product_health(metrics, product)
        counts = incident_counts(incidents, product.id)
        median = "-" if counts["median_minutes"] is None else f"{counts['median_minutes']:.1f} min"
        service_rows = "".join(
            f"<tr><td>{escape(s['service'])}</td><td>{status_pill(s['status'])}</td>"
            f"<td>{percent(s.get('errors'))}</td><td>{seconds(s.get('p95'))}</td></tr>"
            for s in services
        )
        cards.append(
            f'<div class="card"><h2 style="margin-top:0"><a href="/products/{escape(product.id)}">'
            f"{escape(product.name)}</a> {status_pill(overall)}</h2>"
            f'<p class="muted">{escape(product.team)} &middot; {escape(product.description)}</p>'
            f"<table><tr><th>service</th><th>status</th><th>5xx (1m)</th><th>p95 (1m)</th></tr>"
            f"{service_rows}</table>"
            f"<p><b>{counts['open']}</b> open &middot; "
            f"<b>{counts['awaiting']}</b> awaiting approval "
            f"&middot; {counts['resolved']} resolved &middot; {counts['escalated']} escalated "
            f"&middot; median time to resolve {median}"
            + (
                f' &middot; <span class="warn">impacted by {counts["impacting"]} open incident(s) '
                "owned elsewhere</span>"
                if counts["impacting"]
                else ""
            )
            + "</p></div>"
        )
    unassigned = sum(1 for i in incidents if not i.product)
    note = (
        f'<p class="muted">{unassigned} earlier test incident(s) predate the catalog and are '
        'listed under <a href="/incidents">all incidents</a> as unassigned.</p>'
        if unassigned
        else ""
    )
    return "".join(cards) + note


def render_fleet_product(
    plane: ControlPlane, metrics: Metrics | None, product: Product, incidents: list[Incident]
) -> str:
    """The health table and counts for one product (used on the product page)."""
    overall, services = product_health(metrics, product)
    counts = incident_counts(incidents, product.id)
    rows = "".join(
        f"<tr><td>{escape(s['service'])}</td><td>{status_pill(s['status'])}</td>"
        f"<td>{percent(s.get('errors'))}</td><td>{seconds(s.get('p95'))}</td></tr>"
        for s in services
    )
    median = "-" if counts["median_minutes"] is None else f"{counts['median_minutes']:.1f} min"
    return (
        "<div class=card><table><tr><th>service</th><th>status</th><th>5xx (1m)</th>"
        f"<th>p95 (1m)</th></tr>{rows}</table><p><b>{counts['open']}</b> open &middot; "
        f"<b>{counts['awaiting']}</b> awaiting approval &middot; {counts['resolved']} resolved "
        f"&middot; median time to resolve {median}</p></div>"
    )
