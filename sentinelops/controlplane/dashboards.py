"""Dashboard panels: a fixed set of read-only PromQL queries, run over a time range.

The browser never sends PromQL. It asks for a panel by id, a set of catalog services and a
range; the queries live here. Threshold lines use the same expressions as the alert rules in
`infra/prometheus/alerts.yml`, so a line crossing its threshold means that alert is evaluating
true. Prometheus errors are raised, never turned into empty charts.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

RANGES = {"15m": 900, "1h": 3600, "3h": 10_800, "6h": 21_600, "24h": 86_400}
MAX_SPAN_SECONDS = 86_400
MAX_POINTS = 300
MIN_STEP_SECONDS = 5  # the scrape interval; a finer step only repeats samples


class RangeMetrics(Protocol):
    def query_range(
        self, promql: str, start: float, end: float, step: float
    ) -> list[dict[str, Any]]: ...


class PanelError(ValueError):
    """The request names an unknown panel or service, or an invalid range."""


@dataclass(frozen=True)
class Panel:
    id: str
    title: str
    unit: str  # reqps | ratio | seconds | bytes | count
    promql: str  # `{sel}` is replaced by the service selector
    description: str
    threshold: float | None = None
    alert: str | None = None
    optional: bool = False  # hidden on a dashboard when no service reports it


PANELS: dict[str, Panel] = {
    p.id: p
    for p in (
        Panel(
            "requests",
            "Request rate",
            "reqps",
            'sum by (service) (rate(http_requests_total{{{sel},route!="/health"}}[1m]))',
            "Requests per second, excluding health checks.",
        ),
        Panel(
            "errors",
            "5xx ratio",
            "ratio",
            '(sum by (service) (rate(http_requests_total{{{sel},status=~"5.."}}[1m]))'
            " or 0 * sum by (service) (rate(http_requests_total{{{sel}}}[1m])))"
            " / sum by (service) (rate(http_requests_total{{{sel}}}[1m]))",
            "Share of responses that are 5xx (the alert's expression; 0 when there are none).",
            threshold=0.05,
            alert="HighErrorRate",
        ),
        Panel(
            "latency",
            "p95 latency",
            "seconds",
            "histogram_quantile(0.95, sum by (service, le) "
            "(rate(http_request_duration_seconds_bucket{{{sel}}}[1m])))",
            "95th percentile response time (same expression as the alert).",
            threshold=1.0,
            alert="HighLatencyP95",
        ),
        Panel(
            "memory",
            "Resident memory",
            "bytes",
            'max by (service) (process_resident_memory_bytes{{job="demo",{sel}}})',
            "Process RSS. Containers are limited to 128 MiB.",
            threshold=100 * 1024 * 1024,
            alert="HighMemoryUsage",
        ),
        Panel(
            "restarts",
            "Restarts (15m window)",
            "count",
            'max by (service) (changes(app_start_time_seconds{{job="demo",{sel}}}[15m]))',
            "Process restarts in the trailing 15 minutes.",
            threshold=2,
            alert="FrequentRestarts",
        ),
        Panel(
            "db_connections",
            "DB connections open",
            "count",
            "max by (service) (db_connections_open{{{sel}}})",
            "Postgres connections held by the service's pool.",
            optional=True,
        ),
    )
}


def selector(services: list[str]) -> str:
    """A label matcher for these services (already validated against the catalog)."""
    if len(services) == 1:
        return f'service="{services[0]}"'
    return f'service=~"{"|".join(services)}"'


def window(
    range_key: str | None, start: float | None, end: float | None, now: Callable[[], float]
) -> tuple[float, float, float]:
    """(start, end, step) aligned to the step, with at most MAX_POINTS points."""
    if start is None or end is None:
        if range_key not in RANGES:
            raise PanelError(f"range must be one of {list(RANGES)}")
        end = now()
        start = end - RANGES[range_key]
    if end <= start:
        raise PanelError("end must be after start")
    if end - start > MAX_SPAN_SECONDS:
        raise PanelError("the range may span at most 24 hours")
    step = max(MIN_STEP_SECONDS, math.ceil((end - start) / MAX_POINTS))
    return math.floor(start / step) * step, math.floor(end / step) * step, step


def series(
    metrics: RangeMetrics,
    panel_id: str,
    services: list[str],
    allowed: set[str],
    range_key: str | None = "1h",
    start: float | None = None,
    end: float | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """One panel's data, shaped for uPlot: a shared time axis and one value list per service.

    Raises PanelError for bad input; lets the backend's ToolError through when Prometheus fails.
    """
    panel = PANELS.get(panel_id)
    if panel is None:
        raise PanelError(f"unknown panel {panel_id!r}")
    unknown = [s for s in services if s not in allowed]
    if not services or unknown:
        raise PanelError(f"services must be some of {sorted(allowed)}; got {unknown or 'none'}")
    t0, t1, step = window(range_key, start, end, now)
    result = metrics.query_range(panel.promql.format(sel=selector(services)), t0, t1, step)
    xs = [t0 + i * step for i in range(int((t1 - t0) / step) + 1)]
    index = {x: i for i, x in enumerate(xs)}
    by_service: dict[str, list[float | None]] = {s: [None] * len(xs) for s in services}
    for item in result:
        service = item.get("metric", {}).get("service")
        if service not in by_service:
            continue
        for ts, raw in item.get("values", []):
            i = index.get(float(ts))
            value = float(raw)
            if i is not None and math.isfinite(value):
                by_service[service][i] = value
    return {
        "panel": panel.id,
        "title": panel.title,
        "unit": panel.unit,
        "description": panel.description,
        "threshold": panel.threshold,
        "alert": panel.alert,
        "step": step,
        "x": xs,
        "series": [
            {"label": s, "values": v, "empty": all(x is None for x in v)}
            for s, v in by_service.items()
        ],
    }
