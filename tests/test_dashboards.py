"""Tests for the dashboard panels API, infrastructure health and the incident view helpers."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mcp_servers.common import ToolError
from sentinelops.controlplane.dashboards import PANELS, PanelError, series, window
from sentinelops.controlplane.fleet import product_health
from sentinelops.controlplane.models import Alert, Incident, State
from sentinelops.controlplane.service import ControlPlane
from sentinelops.controlplane.store import InMemoryStore
from sentinelops.controlplane.views import diff_lines, incident_charts, incident_title, stepper
from sentinelops.controlplane.web import create_app

NOW = 1_800_000_000.0
ALLOWED = {"gateway", "orders", "payments"}


class RangeMetrics:
    """Prometheus stand-in: instant queries say healthy; range queries return a fixed series."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.queries: list[tuple[str, float, float, float]] = []

    def query(self, promql: str) -> list[dict]:
        return [{"metric": {}, "value": [0, "1" if promql.startswith("up") else "0"]}]

    def query_range(self, promql: str, start: float, end: float, step: float) -> list[dict]:
        if self.fail:
            raise ToolError("Prometheus is unreachable")
        self.queries.append((promql, start, end, step))
        values = [[start, "0.5"], [start + step, "NaN"], [start + 2 * step, "0.7"]]
        return [{"metric": {"service": "orders"}, "values": values}]


class Containers:
    def __init__(self, running: bool = True, health: str | None = "healthy") -> None:
        self.state = {"running": running, "health": health, "status": "running"}

    def status(self, service: str) -> dict[str, Any]:
        return {**self.state, "service": service}


def app(tmp_path: Path, metrics: Any = None, containers: Any = None) -> TestClient:
    plane = ControlPlane(InMemoryStore(), None, None, tmp_path / "runs")  # type: ignore[arg-type]
    return TestClient(
        create_app(plane, tmp_path / "none.jsonl", metrics=metrics, containers=containers)
    )


# panels


def test_series_aligns_points_and_drops_non_finite_values() -> None:
    data = series(RangeMetrics(), "requests", ["orders", "gateway"], ALLOWED, "1h", now=lambda: NOW)

    orders = next(s for s in data["series"] if s["label"] == "orders")
    gateway = next(s for s in data["series"] if s["label"] == "gateway")
    assert len(data["x"]) == len(orders["values"]) <= 301
    assert orders["values"][:3] == [0.5, None, 0.7]
    assert gateway["empty"] and not orders["empty"]
    assert data["x"][1] - data["x"][0] == data["step"]


def test_series_queries_only_the_requested_services() -> None:
    metrics = RangeMetrics()
    series(metrics, "errors", ["orders", "payments"], ALLOWED, "15m", now=lambda: NOW)

    promql = metrics.queries[0][0]
    assert 'service=~"orders|payments"' in promql and "gateway" not in promql


@pytest.mark.parametrize(
    ("panel", "services", "message"),
    [
        ("nope", ["orders"], "unknown panel"),
        ("requests", ["postgres"], "services must be"),
        ("requests", ['orders"} or vector(1) #'], "services must be"),
        ("requests", [], "services must be"),
    ],
)
def test_series_rejects_bad_input(panel: str, services: list[str], message: str) -> None:
    with pytest.raises(PanelError, match=message):
        series(RangeMetrics(), panel, services, ALLOWED, "1h", now=lambda: NOW)


def test_window_caps_points_and_span() -> None:
    start, end, step = window("24h", None, None, lambda: NOW)
    assert (end - start) / step <= 300 and step >= 5
    with pytest.raises(PanelError, match="24 hours"):
        window(None, NOW - 2 * 86_400, NOW, lambda: NOW)
    with pytest.raises(PanelError, match="range must be"):
        window("7d", None, None, lambda: NOW)


def test_every_panel_threshold_matches_its_alert_rule() -> None:
    rules = (Path(__file__).parents[1] / "infra" / "prometheus" / "alerts.yml").read_text()
    for panel in PANELS.values():
        if panel.alert:
            assert f"alert: {panel.alert}" in rules


def test_panel_api_status_codes(tmp_path: Path) -> None:
    client = app(tmp_path, RangeMetrics())

    assert client.get("/api/panels/requests?services=orders&range=1h").status_code == 200
    assert client.get("/api/panels/nope?services=orders&range=1h").status_code == 404
    assert client.get("/api/panels/requests?services=redis&range=1h").status_code == 400
    assert client.get("/api/panels/requests?services=orders&range=9y").status_code == 400

    down = app(tmp_path, RangeMetrics(fail=True)).get("/api/panels/requests?services=orders")
    assert down.status_code == 503 and "unreachable" in down.json()["error"]
    assert app(tmp_path).get("/api/panels/requests?services=orders").status_code == 503


# infrastructure health and pages


def test_infrastructure_health_comes_from_the_container() -> None:
    from sentinelops.catalog import load

    platform = load().product("platform")

    assert product_health(None, platform, Containers())[0] == "healthy"
    assert product_health(None, platform, Containers(running=False))[0] == "down"
    assert product_health(None, platform, Containers(health="unhealthy"))[0] == "degraded"
    assert product_health(None, platform, None)[0] == "unknown"


def test_pages_render_with_charts_and_static_assets(tmp_path: Path) -> None:
    client = app(tmp_path, RangeMetrics(), Containers())

    product = client.get("/products/orders").text
    assert 'data-panel="errors"' in product and "uPlot.iife.min.js" in product
    platform = client.get("/products/platform").text
    assert "no exporter" in platform and 'data-panel="' not in platform
    assert "Data Platform" in client.get("/").text
    assert client.get("/static/vendor/uplot/uPlot.iife.min.js").status_code == 200
    assert client.get("/products/orders?range=bogus").status_code == 200


def test_escalated_incident_explains_what_happens_next(tmp_path: Path) -> None:
    plane = ControlPlane(InMemoryStore(), None, None, tmp_path / "runs")  # type: ignore[arg-type]
    incident = plane.open("ctx", [Alert(name="HighMemoryUsage", service="orders")])
    plane.escalate(incident.id, State.OPEN, "no change explains the <b>memory</b> growth")
    client = TestClient(create_app(plane, tmp_path / "none.jsonl"))

    page = client.get(f"/incidents/{incident.id}").text

    assert "Escalated to commerce-team" in page and "no further automatic action" in page
    assert "&lt;b&gt;memory&lt;/b&gt;" in page and "<b>memory</b>" not in page


# view helpers


def test_stepper_marks_where_an_incident_escalated() -> None:
    now = datetime.now(UTC).isoformat()
    incident = Incident(
        id="i",
        alert_context="",
        state=State.ESCALATED,
        history=[
            {"at": now, "from": "OPEN", "to": "INVESTIGATING"},
            {"at": now, "from": "INVESTIGATING", "to": "ESCALATED"},
        ],
    )

    steps = {s["state"]: s["status"] for s in stepper(incident)}

    assert steps == {"OPEN": "done", "INVESTIGATING": "done", "ESCALATED": "failed"}


def test_incident_title_charts_and_diff() -> None:
    created = datetime(2026, 10, 14, 12, tzinfo=UTC)
    incident = Incident(
        id="i",
        alert_context="",
        created_at=created,
        updated_at=created + timedelta(minutes=3),
        state=State.RESOLVED,
        alerts=[
            Alert(name="HighErrorRate", service="gateway"),
            Alert(name="HighErrorRate", service="orders"),
        ],
    )

    assert incident_title(incident) == "HighErrorRate on gateway, orders"
    charts = incident_charts(incident, ALLOWED)
    assert charts is not None and charts["panels"][0] == "errors"
    assert charts["start"] == int((created - timedelta(minutes=15)).timestamp())
    assert [d["tone"] for d in diff_lines("roll back\n- A=1\n+ A=0")] == ["ctx", "del", "add"]
