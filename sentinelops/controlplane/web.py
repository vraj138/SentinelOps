"""Web UI and JSON API for incidents: review the evidence and the dry run, approve or reject.

Run with `make controlplane` (uvicorn, http://localhost:8080). Pages are server-rendered Jinja
templates with autoescaping on, so every value from incidents, alerts and tools is escaped.
Charts load their data from `/api/panels/<id>`, which runs a fixed set of read-only queries.
Approving starts execution and verification in the background.
"""

import asyncio
import hashlib
import statistics
import threading
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from mcp_servers.common import ToolError, mask_secrets
from mcp_servers.docker_backend import DockerBackend
from mcp_servers.metrics import PrometheusBackend
from sentinelops.config import ROOT, Settings
from sentinelops.controlplane import build_control_plane
from sentinelops.controlplane.actions import ActionError
from sentinelops.controlplane.alerts import firing_alerts, still_firing_checker
from sentinelops.controlplane.dashboards import PANELS, RANGES, PanelError, selector, series
from sentinelops.controlplane.fleet import (
    Containers,
    Metrics,
    incident_counts,
    percent,
    product_health,
    seconds,
)
from sentinelops.controlplane.intake import COOLDOWN, AlertIntake
from sentinelops.controlplane.investigations import (
    InvestigationConflict,
    Investigations,
    SpendLedger,
)
from sentinelops.controlplane.models import REINVESTIGABLE, TERMINAL, Incident, State
from sentinelops.controlplane.service import ControlPlane
from sentinelops.controlplane.store import StaleTransition
from sentinelops.controlplane.views import (
    BUSY,
    STATE_TONE,
    STATUS_TONE,
    alert_groups,
    audit_records,
    diff_lines,
    human_seconds,
    incident_charts,
    incident_duration,
    incident_title,
    run_stats,
    service_colors,
    state_label,
    stepper,
    tool_calls,
)
from sentinelops.evals import report
from sentinelops.investigate import has_anthropic_credentials
from sentinelops.investigate import investigate as investigate_cli

HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"
RESULTS = ROOT / "evals" / "results" / "results.jsonl"
HEALTH_TTL_SECONDS = 5.0
REFRESH_OPTIONS = [("0", "Off"), ("15", "15s"), ("30", "30s"), ("60", "1m")]
BUSY_TEXT = {
    State.INVESTIGATING: "The agent is gathering evidence with read-only tools.",
    State.APPROVED: "Approved; the action is about to run.",
    State.EXECUTING: "Running the approved action.",
    State.VERIFYING: "Checking health. A failed check rolls the action back automatically.",
}
ACTIVE = {State.OPEN, *BUSY, State.PROPOSED}
SYSTEM_LABELS = {
    "sentinelops": "SentinelOps",
    "naive-agent": "Naive agent",
    "naive-rule": "Naive rule",
    "v1-shipped": "v1, as shipped",
    "v1-our-runbooks": "v1 + our runbooks",
}


def fmt_rps(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def asset_version() -> str:
    """Changes whenever a stylesheet or script changes, so browsers never use stale files."""
    digest = hashlib.sha256()
    for path in sorted(STATIC.rglob("*")):
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def make_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals.update(
        STATE_TONE=STATE_TONE,
        STATUS_TONE=STATUS_TONE,
        pct=percent,
        secs=seconds,
        fmt_rps=fmt_rps,
        asset_version=asset_version(),
        state_label=state_label,
        human_seconds=human_seconds,
    )
    return env


def public(incident: Incident) -> dict[str, Any]:
    """JSON view without the raw action payload, and with secret values masked."""
    data = incident.model_dump(mode="json")
    if data.get("action"):
        data["action"].pop("payload", None)
    data.pop("investigate_token", None)
    return mask_secrets(data)


def create_app(
    plane: ControlPlane,
    results_path: Path = RESULTS,
    metrics: Metrics | None = None,
    investigations: Investigations | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    containers: Containers | None = None,
    prometheus_url: str | None = None,
) -> FastAPI:
    """The control-plane web app.

    `metrics` powers live health (and charts, if it also supports range queries);
    `containers` reports infrastructure without an exporter; `investigations` runs the agent
    (None when no model credentials are configured).
    """
    app = FastAPI(title="SentinelOps control plane", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    env = make_environment()
    catalog = plane.catalog
    metric_services = set(catalog.app_services)
    colors = service_colors(catalog.app_services)
    health_cache: dict[str, Any] = {"at": -HEALTH_TTL_SECONDS}

    def snapshot() -> dict[str, Any]:
        """Health and firing alerts, cached briefly: every page shows them in the sidebar."""
        if time.monotonic() - health_cache["at"] > HEALTH_TTL_SECONDS:
            alerts, alerts_error = read_alerts()
            by_service: dict[str, list[str]] = defaultdict(list)
            for a in alerts or []:
                by_service[a["service"]].append(a["name"])
            health_cache.update(
                at=time.monotonic(),
                alerts=alerts,
                alerts_error=alerts_error,
                health={
                    p.id: product_health(metrics, p, containers, by_service)
                    for p in catalog.products
                },
            )
        return health_cache

    def fleet_health() -> dict[str, tuple[str, list[dict[str, Any]]]]:
        return snapshot()["health"]

    def firing() -> tuple[list[dict[str, Any]] | None, str | None]:
        cached = snapshot()
        return cached["alerts"], cached["alerts_error"]

    def read_alerts() -> tuple[list[dict[str, Any]] | None, str | None]:
        """(firing alerts, error). (None, None) when no alert source is configured."""
        if metrics is None or not hasattr(metrics, "alerts"):
            return None, None
        try:
            raw = metrics.alerts()
        except ToolError as exc:
            return None, str(exc)
        alerts = []
        for a in raw:
            if a.get("state") != "firing":
                continue
            since = a.get("activeAt")
            alerts.append(
                {
                    "name": a["labels"].get("alertname", ""),
                    "service": a["labels"].get("service", ""),
                    "severity": a["labels"].get("severity", "warning"),
                    "summary": a.get("annotations", {}).get("summary", ""),
                    "since": datetime.fromisoformat(since.replace("Z", "+00:00"))
                    if since
                    else None,
                }
            )
        return alerts, None

    def render(template: str, nav: str, crumbs: list[tuple[str, str | None]], **ctx: Any):
        health = fleet_health()
        incidents = plane.store.list(limit=500)
        sidebar = {
            "products": [
                {
                    "id": p.id,
                    "name": p.name,
                    "status": health[p.id][0],
                    "tone": STATUS_TONE.get(health[p.id][0], "neutral"),
                }
                for p in catalog.products
            ],
            "open_incidents": sum(1 for i in incidents if i.state not in TERMINAL),
        }
        html = env.get_template(template).render(
            nav=nav,
            crumbs=crumbs,
            sidebar=sidebar,
            catalog=catalog,
            prometheus_url=prometheus_url,
            autoreload=ctx.pop("autoreload", 0),
            title=ctx.pop("title", crumbs[-1][0]),
            **ctx,
        )
        return HTMLResponse(html)

    def rows(incidents: list[Incident]) -> list[dict[str, Any]]:
        return [
            {
                "incident": i,
                "title": incident_title(i),
                "duration": incident_duration(i),
                "stats": run_stats(audit_records(plane.runs_dir, i.id)),
            }
            for i in incidents
        ]

    def all_incidents() -> list[Incident]:
        return sorted(plane.store.list(limit=500), key=lambda i: i.created_at, reverse=True)

    @app.get("/", response_class=HTMLResponse)
    def overview() -> HTMLResponse:
        incidents = all_incidents()
        health = fleet_health()
        alerts, alerts_error = firing()
        products = []
        for p in catalog.products:
            overall, services = health[p.id]
            measured = [s for s in services if s.get("source") == "prometheus"]
            errors = [s["errors"] for s in measured if s.get("errors") is not None]
            p95 = [s["p95"] for s in measured if s.get("p95") is not None]
            rps = [s["rps"] for s in measured if s.get("rps") is not None]
            owned = [i for i in incidents if i.product == p.id]
            products.append(
                {
                    "product": p,
                    "overall": overall,
                    "services": services,
                    "counts": incident_counts(incidents, p.id),
                    "rps": fmt_rps(sum(rps)) if rps else "-",
                    "errors": percent(max(errors)) if errors else "-",
                    "errors_hot": bool(errors) and max(errors) > 0.05,
                    "p95": seconds(max(p95)) if p95 else "-",
                    "p95_hot": bool(p95) and max(p95) > 1.0,
                    "spark_services": [s for s in p.services if s in metric_services]
                    if metrics is not None and hasattr(metrics, "query_range")
                    else [],
                    "last": owned[0] if owned else None,
                }
            )
        resolved = [i for i in incidents if i.state == State.RESOLVED]
        durations = [(i.updated_at - i.created_at).total_seconds() for i in resolved]
        auto_rca = [
            i.investigation.alert_to_rca_seconds
            for i in incidents
            if i.investigation.trigger == "auto" and i.investigation.alert_to_rca_seconds
        ]
        stats = {
            "healthy": sum(1 for r in products if r["overall"] == "healthy"),
            "total": len(products),
            "open": sum(1 for i in incidents if i.state not in TERMINAL),
            "awaiting": sum(1 for i in incidents if i.state == State.PROPOSED),
            "firing": None if alerts is None else len(alerts),
            "mttr": human_seconds(statistics.median(durations)) if durations else None,
            "resolved": len(resolved),
            "escalated": sum(1 for i in incidents if i.state == State.ESCALATED),
            "self_resolved": sum(1 for i in incidents if i.state == State.SELF_RESOLVED),
            "alert_to_rca": human_seconds(statistics.median(auto_rca)) if auto_rca else None,
            "alert_to_rca_runs": len(auto_rca),
        }
        return render(
            "overview.html",
            "overview",
            [("Overview", None)],
            products=products,
            stats=stats,
            attention=rows([i for i in incidents if i.state in ACTIVE][:8]),
            alerts=alerts,
            alerts_error=alerts_error,
            unassigned=sum(1 for i in incidents if not i.product),
            service_count=len(catalog.services),
            queue=investigations.status() if investigations else None,
            now=datetime.now(UTC),
            autoreload=15,
        )

    @app.get("/incidents", response_class=HTMLResponse)
    def incident_list(product: str = "", team: str = "", state: str = "") -> HTMLResponse:
        incidents = all_incidents()
        if product:
            incidents = [i for i in incidents if i.product == product]
        if team:
            owned = {p.id for p in catalog.products if p.team == team}
            incidents = [i for i in incidents if i.product in owned]
        if state:
            incidents = [i for i in incidents if i.state.value == state.upper()]
        return render(
            "incidents.html",
            "incidents",
            [("Incidents", None)],
            rows=rows(incidents),
            product=product,
            state=state.upper(),
            states=list(State),
        )

    @app.get("/products/{product_id}", response_class=HTMLResponse)
    def product_page(
        product_id: str, range_key: str = Query("1h", alias="range"), refresh: str = "0"
    ) -> HTMLResponse:
        try:
            product = catalog.product(product_id)
        except StopIteration as exc:
            raise HTTPException(404, f"no product {product_id!r}") from exc
        if range_key not in RANGES:
            range_key = "1h"
        if refresh not in dict(REFRESH_OPTIONS):
            refresh = "0"
        overall, services = fleet_health()[product.id]
        incidents = all_incidents()
        owned = [i for i in incidents if i.product == product.id]
        impacting = [
            i for i in incidents if product.id in i.impacted_products and i.product != product.id
        ]
        chart_services = [s for s in product.services if s in metric_services]
        charts_on = bool(chart_services) and metrics is not None and hasattr(metrics, "query_range")
        panels = list(PANELS.values()) if charts_on else []
        prom_links = {}
        if prometheus_url and chart_services:
            sel = selector(chart_services)
            for panel in panels:
                query = {"g0.expr": panel.promql.format(sel=sel), "g0.tab": "graph"}
                query["g0.range_input"] = range_key
                prom_links[panel.id] = f"{prometheus_url}/query?{urlencode(query)}"
        markers = [
            {"t": int(i.created_at.timestamp()), "label": i.id.split("T")[-1][:6]}
            for i in owned + impacting
        ]
        alerts, _ = firing()
        return render(
            "product.html",
            f"product:{product.id}",
            [("Products", None), (product.name, None)],
            product=product,
            overall=overall,
            services=services,
            counts=incident_counts(incidents, product.id),
            owned=rows(owned),
            impacting=rows(impacting),
            panels=panels,
            metric_services=chart_services,
            colors={s: colors[s] for s in chart_services},
            markers=markers,
            prom_links=prom_links,
            product_alerts=[a for a in alerts or [] if a["service"] in product.services],
            ranges=list(RANGES),
            range_key=range_key,
            refresh=refresh,
            refresh_options=REFRESH_OPTIONS,
        )

    @app.get("/benchmark", response_class=HTMLResponse)
    def benchmark() -> HTMLResponse:
        records = report.load(results_path) if results_path.exists() else []
        summary = report.summarize(records) if records else {}
        order = {s: i for i, s in enumerate(SYSTEM_LABELS)}
        systems = sorted(summary, key=lambda s: (order.get(s, len(order)), s))
        metrics_rows: list[str] = []
        for row in summary.values():
            metrics_rows += [m for m in row if m not in metrics_rows]
        grades: dict[str, list[str]] = defaultdict(list)
        for r in records:
            if r["system"] == "sentinelops":
                grades[r["scenario_id"]].append(
                    r["score"]["action"] if r.get("score") else r["status"]
                )
        return render(
            "benchmark.html",
            "benchmark",
            [("Benchmark", None)],
            summary=summary,
            systems=systems,
            system_labels=SYSTEM_LABELS,
            metrics=metrics_rows,
            per_scenario=sorted(grades.items()),
            line_count=len(records),
            results_name=results_path.name,
        )

    def get_or_404(incident_id: str) -> Incident:
        try:
            return plane.store.get(incident_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/incidents/{incident_id}", response_class=HTMLResponse)
    def incident_page(incident_id: str) -> HTMLResponse:
        incident = get_or_404(incident_id)
        records = audit_records(plane.runs_dir, incident.id)
        owner_product = catalog.product(incident.product) if incident.product else None
        charts = None
        if metrics is not None and hasattr(metrics, "query_range"):
            charts = incident_charts(incident, metric_services)
        history = [
            {
                "at": datetime.fromisoformat(h["at"]).strftime("%H:%M:%S"),
                "label": State(h["to"]).value.title(),
            }
            for h in incident.history
        ]
        return render(
            "incident.html",
            "incidents",
            [("Incidents", "/incidents"), (incident.id, None)],
            title=incident_title(incident),
            incident=incident,
            terminal=incident.state in TERMINAL,
            duration=incident_duration(incident),
            steps=stepper(incident),
            busy=incident.state in BUSY,
            busy_text=BUSY_TEXT.get(incident.state, ""),
            agent_configured=investigations is not None,
            queue=investigations.status() if investigations else None,
            can_start=investigations is not None
            and incident.state == State.OPEN
            and incident.investigation.status not in ("queued", "running"),
            can_reinvestigate=investigations is not None
            and incident.state in REINVESTIGABLE
            and incident.inverse is None,
            owner_product=owner_product,
            impacted=[
                catalog.product(p).name for p in incident.impacted_products if p != incident.product
            ],
            stats=run_stats(records),
            tools=tool_calls(records),
            diff=diff_lines(incident.action.dry_run) if incident.action else [],
            charts=charts,
            chart_panels=[PANELS[p] for p in charts["panels"]] if charts else [],
            colors={s: colors[s] for s in charts["services"]} if charts else {},
            history=history,
            alert_groups=alert_groups(incident),
            cooldown_minutes=int(COOLDOWN.total_seconds() // 60),
            reopen_at=incident.updated_at + COOLDOWN,
        )

    @app.post("/incidents/{incident_id}/approve")
    def approve(
        incident_id: str,
        background: BackgroundTasks,
        approver: str = Form(...),
        fingerprint: str = Form(...),
    ) -> RedirectResponse:
        """The form must echo the fingerprint shown on the page: proof the approver saw this
        exact dry run, and a CSRF guard (another site can't read the page to learn it)."""
        incident = get_or_404(incident_id)
        if incident.action is None or fingerprint != incident.action.fingerprint:
            raise HTTPException(409, "the action changed or the form is stale; reload the page")
        try:
            plane.approve(incident_id, approver.strip())
        except (ActionError, StaleTransition) as exc:
            raise HTTPException(409, str(exc)) from exc
        background.add_task(plane.execute, incident_id)
        return RedirectResponse(f"/incidents/{incident_id}", status_code=303)

    @app.post("/incidents/{incident_id}/reject")
    def reject(
        incident_id: str, approver: str = Form(...), reason: str = Form(...)
    ) -> RedirectResponse:
        get_or_404(incident_id)
        try:
            plane.reject(incident_id, approver.strip(), reason.strip())
        except StaleTransition as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse(f"/incidents/{incident_id}", status_code=303)

    @app.post("/incidents/{incident_id}/investigate")
    def investigate_incident(incident_id: str, token: str = Form(...)) -> RedirectResponse:
        """Start or re-run an investigation by hand (paid). The token proves the click came
        from this page (CSRF); the queue refuses a second run while one is queued or running."""
        incident = get_or_404(incident_id)
        if investigations is None:
            raise HTTPException(409, "investigation is not configured (no Anthropic credentials)")
        if token != incident.investigate_token:
            raise HTTPException(409, "stale or forged form; reload the page")
        try:
            investigations.start_manual(incident_id)
        except InvestigationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse(f"/incidents/{incident_id}", status_code=303)

    @app.get("/api/incidents/{incident_id}")
    def incident_json(incident_id: str) -> dict[str, Any]:
        return public(get_or_404(incident_id))

    @app.get("/api/panels/{panel_id}", response_model=None)
    def panel_json(
        panel_id: str,
        services: str,
        range_key: str = Query("1h", alias="range"),
        start: float | None = None,
        end: float | None = None,
    ) -> dict[str, Any] | JSONResponse:
        """One dashboard panel's series. 404 unknown panel, 400 bad input, 503 Prometheus down."""
        if panel_id not in PANELS:
            raise HTTPException(404, f"unknown panel {panel_id!r}")
        if metrics is None or not hasattr(metrics, "query_range"):
            return JSONResponse({"error": "metrics are not configured"}, status_code=503)
        wanted = [s.strip() for s in services.split(",") if s.strip()]
        try:
            return series(metrics, panel_id, wanted, metric_services, range_key, start, end)
        except PanelError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ToolError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)

    return app


def main_app() -> FastAPI:
    """Factory for uvicorn (`--factory`): real control plane, live metrics, alert intake, and
    automatic investigations when Anthropic credentials are configured."""
    settings = Settings()
    plane = build_control_plane(settings)
    prometheus = PrometheusBackend(settings.prometheus_url)
    containers = DockerBackend(
        settings.docker_bin, settings.container_name_template, settings.container_events_path
    )

    def run(incident: Incident, cost_limit: float | None) -> Any:
        """One agent run. Automatic runs get a cost cap from the daily budget."""
        run_settings = settings
        if cost_limit is not None:
            cap = min(settings.agent_max_cost_usd, cost_limit)
            run_settings = settings.model_copy(update={"agent_max_cost_usd": cap})
        return asyncio.run(investigate_cli(run_settings, incident.alert_context, incident.id))

    investigations = None
    if has_anthropic_credentials(settings):
        investigations = Investigations(
            plane,
            run,
            still_firing_checker(settings.prometheus_url),
            SpendLedger(settings.runs_dir / "investigation-spend.jsonl"),
            auto=settings.auto_investigate,
            concurrency=settings.investigation_concurrency,
            daily_budget_usd=settings.auto_investigate_daily_budget_usd,
            max_run_cost_usd=settings.agent_max_cost_usd,
            repeat_window=timedelta(hours=settings.auto_investigate_repeat_hours),
        )
    intake = AlertIntake(
        plane,
        lambda: firing_alerts(settings.prometheus_url),
        on_open=investigations.on_opened if investigations else None,
        window=timedelta(minutes=settings.incident_group_window_minutes),
    )
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if investigations is not None:
            investigations.recover()
        if settings.controlplane_poll_alerts:
            threading.Thread(target=intake.run_forever, args=(stop,), daemon=True).start()
        yield
        stop.set()
        if investigations is not None:
            investigations.shutdown()

    return create_app(
        plane,
        metrics=prometheus,
        investigations=investigations,
        lifespan=lifespan,
        containers=containers,
        prometheus_url=settings.prometheus_url.rstrip("/"),
    )
