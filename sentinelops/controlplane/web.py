"""Web UI and JSON API for incidents: review the evidence and the dry run, approve or reject.

Run with `make controlplane` (uvicorn, http://localhost:8080). Pages are server-rendered HTML;
every value is escaped. Approving starts execution and verification in the background.
"""

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from mcp_servers.common import mask_secrets
from mcp_servers.metrics import PrometheusBackend
from sentinelops.config import ROOT, Settings
from sentinelops.controlplane import build_control_plane
from sentinelops.controlplane.actions import ActionError
from sentinelops.controlplane.alerts import firing_alerts
from sentinelops.controlplane.fleet import (
    Metrics,
    owner_label,
    product_health,
    render_fleet,
    render_fleet_product,
    status_pill,
)
from sentinelops.controlplane.intake import AlertIntake
from sentinelops.controlplane.models import Incident, State
from sentinelops.controlplane.service import ControlPlane
from sentinelops.controlplane.store import StaleTransition
from sentinelops.evals import report
from sentinelops.investigate import has_anthropic_credentials
from sentinelops.investigate import investigate as investigate_cli

RESULTS = ROOT / "evals" / "results" / "results.jsonl"
STATE_COLORS = {
    State.RESOLVED: "ok",
    State.ESCALATED: "bad",
    State.PROPOSED: "warn",
    State.APPROVED: "busy",
    State.EXECUTING: "busy",
    State.VERIFYING: "busy",
}
CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e3e2dd;--ok:#1f7a4d;
--bad:#b3261e;--warn:#9a6700;--busy:#2f5fb3;--code:#f0efe9}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--muted:#9c9b94;
--line:#33332f;--ok:#5cc28f;--bad:#f2786d;--warn:#e0b35a;--busy:#7ea6f0;--code:#262624}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,system-ui,Segoe UI,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 64px}a{color:var(--busy)}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:12px 0}
.muted{color:var(--muted)}.pill{display:inline-block;padding:2px 10px;border-radius:999px;
font-size:12px;font-weight:600;border:1px solid currentColor}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.busy{color:var(--busy)}
pre{background:var(--code);padding:12px;border-radius:8px;overflow-x:auto;font-size:13px;margin:8px 0}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;color:var(--muted)}
.ev{font:12px ui-monospace,monospace;padding:1px 6px;border-radius:5px;background:var(--code);
text-decoration:none;margin-right:4px}
form{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px}
input{padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}
button{padding:8px 16px;border-radius:6px;border:0;font-weight:600;cursor:pointer;color:#fff}
.approve{background:var(--ok)}.reject{background:var(--bad)}
details{margin:6px 0}summary{cursor:pointer}
"""


def page(title: str, body: str, refresh: bool = False) -> HTMLResponse:
    meta = '<meta http-equiv="refresh" content="3">' if refresh else ""
    return HTMLResponse(
        f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" '
        f'content="width=device-width,initial-scale=1">{meta}<title>{escape(title)}</title>'
        f"<style>{CSS}</style></head><body><main>{body}</main></body></html>"
    )


def state_pill(state: State) -> str:
    return f'<span class="pill {STATE_COLORS.get(state, "muted")}">{escape(state.value)}</span>'


def tool_records(plane: ControlPlane, incident_id: str) -> dict[str, dict[str, Any]]:
    path = plane.runs_dir / incident_id / "audit.jsonl"
    if not path.exists():
        return {}
    records = [json.loads(line) for line in path.read_text().splitlines()]
    return {r["id"]: r for r in records if r.get("kind") == "tool_call"}


NAV = (
    '<p class="muted"><a href="/">Fleet</a> &middot; <a href="/incidents">Incidents</a> '
    '&middot; <a href="/benchmark">Benchmark</a></p>'
)


def audit_records(plane: ControlPlane, incident_id: str) -> list[dict[str, Any]]:
    path = plane.runs_dir / incident_id / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def run_stats(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """What the investigation cost: model calls, tool calls, tokens, dollars, seconds."""
    llm = [r for r in records if r.get("kind") == "llm_call"]
    if not llm:
        return None
    tools = [r for r in records if r.get("kind") == "tool_call"]
    starts = [r["ts"] for r in records if r.get("kind") == "run_start"]
    ends = [r["ts"] for r in records if r.get("kind") == "run_end"]
    seconds = None
    if starts and ends:
        seconds = (
            datetime.fromisoformat(ends[-1]) - datetime.fromisoformat(starts[0])
        ).total_seconds()
    return {
        "llm_calls": len(llm),
        "tool_calls": len(tools),
        "tool_errors": sum(1 for r in tools if r.get("is_error")),
        "tokens": sum(r["usage"]["total"] for r in llm),
        "cost_usd": sum(r["cost_usd"] or 0 for r in llm),
        "unpriced": sum(1 for r in llm if r["cost_usd"] is None),
        "seconds": seconds,
        "models": sorted({r["model"] for r in llm}),
    }


def format_cost(stats: dict[str, Any]) -> str:
    cost = f"${stats['cost_usd']:.3f}"
    return f"&ge;{cost}" if stats["unpriced"] else cost


def render_stats(stats: dict[str, Any]) -> str:
    cells = [
        ("model calls", str(stats["llm_calls"])),
        ("tool calls", f"{stats['tool_calls']} ({stats['tool_errors']} failed)"),
        ("tokens", f"{stats['tokens']:,}"),
        ("cost", format_cost(stats)),
        ("time", "-" if stats["seconds"] is None else f"{stats['seconds']:.0f}s"),
        ("model", escape(", ".join(stats["models"]))),
    ]
    row = "".join(f"<td><span class=muted>{k}</span><br><b>{v}</b></td>" for k, v in cells)
    return f"<h2>Investigation</h2><div class=card><table><tr>{row}</tr></table></div>"


def render_incident(plane: ControlPlane, incident: Incident) -> str:
    parts = [
        NAV,
        f"<h1>{escape(incident.id)} {state_pill(incident.state)}</h1>",
        f'<p class="muted">opened {incident.created_at:%Y-%m-%d %H:%M:%S} UTC</p>',
        f'<div class="card"><pre>{escape(incident.alert_context)}</pre></div>',
    ]
    impacted = [
        plane.catalog.product(p).name for p in incident.impacted_products if p != incident.product
    ]
    owner = (
        f'<div class="card"><b>Paged:</b> {owner_label(plane, incident)}'
        + (
            f'<p class="muted">Also impacted: {escape(", ".join(impacted))}. '
            "Their alerts are symptoms of this incident, not their own fault.</p>"
            if impacted and incident.owner_source == "investigation"
            else (
                f'<p class="muted">Also alerting: {escape(", ".join(impacted))}.</p>'
                if impacted
                else ""
            )
        )
        + "</div>"
    )
    parts.append(owner)
    if incident.state == State.OPEN:
        parts.append(
            f'<form method="post" action="/incidents/{escape(incident.id)}/investigate">'
            f'<input type="hidden" name="token" value="{escape(incident.investigate_token)}">'
            '<button class="approve">Investigate with the agent (~$0.10)</button>'
            '<span class="muted">Read-only: it gathers evidence and proposes a fix for approval.</span>'
            "</form>"
        )
    if incident.escalation_reason:
        parts.append(
            f'<div class="card bad"><b>Escalated:</b> {escape(incident.escalation_reason)}</div>'
        )
    tools = tool_records(plane, incident.id)
    stats = run_stats(audit_records(plane, incident.id))
    if stats:
        parts.append(render_stats(stats))
    if incident.rca:
        rca = incident.rca
        claims = "".join(
            f"<li>{escape(c.statement)} "
            + "".join(f'<a class="ev" href="#{escape(e)}">{escape(e)}</a>' for e in c.evidence)
            + "</li>"
            for c in rca.claims
        )
        gaps = "".join(f"<li>{escape(g)}</li>" for g in rca.data_gaps)
        parts.append(
            f'<h2>Root cause analysis</h2><div class="card"><p><b>{escape(rca.summary)}</b></p>'
            f"<p>{escape(rca.root_cause)}</p><p class=muted>service: {escape(rca.root_cause_service)}"
            f" &middot; confidence: {escape(rca.confidence)}</p><ul>{claims}</ul>"
            + (f"<p class=muted>Could not check:</p><ul>{gaps}</ul>" if gaps else "")
            + "</div>"
        )
    if incident.action and incident.proposal:
        parts.append(
            f"<h2>Proposed action</h2><div class=card><p><b>{escape(incident.action.kind)}</b> "
            f"{escape(incident.action.target)} &middot; proposed by {escape(incident.proposal.proposed_by)}"
            f"</p><p class=muted>{escape(incident.proposal.reason)}</p>"
            f"<pre>{escape(incident.action.dry_run)}</pre>"
            f'<p class="muted">fingerprint {escape(incident.action.fingerprint[:16])}&hellip; '
            "(approval is bound to exactly this change)</p>"
        )
        if incident.state == State.PROPOSED:
            parts.append(
                f'<form method="post" action="/incidents/{escape(incident.id)}/approve">'
                f'<input type="hidden" name="fingerprint" value="{escape(incident.action.fingerprint)}">'
                '<input name="approver" placeholder="your name" required>'
                '<button class="approve">Approve and execute</button></form>'
                f'<form method="post" action="/incidents/{escape(incident.id)}/reject">'
                '<input name="approver" placeholder="your name" required>'
                '<input name="reason" placeholder="reason" required>'
                '<button class="reject">Reject</button></form>'
            )
        if incident.approval:
            parts.append(
                f"<p>Approved by <b>{escape(incident.approval.approver)}</b> at "
                f"{incident.approval.at:%H:%M:%S} UTC</p>"
            )
        parts.append("</div>")
    if incident.verification:
        rows = "".join(
            f'<tr><td class="{"ok" if c.ok else "bad"}">{"PASS" if c.ok else "FAIL"}</td>'
            f"<td>{escape(c.name)}</td><td>{escape(c.detail)}</td></tr>"
            for c in incident.verification
        )
        rolled = (
            "<p class=bad>The action was rolled back automatically.</p>"
            if incident.rolled_back
            else ""
        )
        parts.append(f"<h2>Verification</h2><div class=card><table>{rows}</table>{rolled}</div>")
    if tools:
        items = "".join(
            f'<details id="{escape(tid)}"><summary><span class="ev">{escape(tid)}</span> '
            f'{escape(r["tool"])} <span class="{"bad" if r["is_error"] else "muted"}">'
            f"{'error' if r['is_error'] else ''}</span></summary>"
            f"<pre>{escape(json.dumps(r.get('arguments', {})))}\n\n{escape(str(r.get('result', ''))[:4000])}</pre></details>"
            for tid, r in tools.items()
        )
        parts.append(f"<h2>Evidence (tool calls)</h2><div class=card>{items}</div>")
    history = "".join(
        f"<tr><td>{escape(h['at'][11:19])}</td><td>{escape(str(h['from']))} &rarr; {escape(str(h['to']))}</td></tr>"
        for h in incident.history
    )
    parts.append(f"<h2>Timeline</h2><div class=card><table>{history}</table></div>")
    return "".join(parts)


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
    investigate: Callable[[str], None] | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    """The control-plane web app. `metrics` powers live health; `investigate(incident_id)`
    runs the agent for an incident already moved to INVESTIGATING."""
    app = FastAPI(title="SentinelOps control plane", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    def fleet() -> HTMLResponse:
        incidents = plane.store.list(limit=500)
        body = (
            "<h1>SentinelOps</h1>" + NAV + "<p class=muted>Every product's live health and "
            "incidents. Incidents are paged to the team that owns the root cause, not the team "
            "whose service alerted first.</p>" + render_fleet(plane, metrics, incidents)
        )
        return page("SentinelOps fleet", body, refresh=True)

    def incident_rows(incidents: list[Incident]) -> str:
        rows = []
        for i in incidents:
            stats = run_stats(audit_records(plane, i.id))
            cost = format_cost(stats) if stats else "-"
            impacted = ", ".join(p for p in i.impacted_products if p != i.product) or "-"
            rows.append(
                f'<tr><td><a href="/incidents/{escape(i.id)}">{escape(i.id)}</a></td>'
                f"<td>{state_pill(i.state)}</td><td>{owner_label(plane, i)}</td>"
                f"<td>{escape(impacted)}</td><td>{cost}</td></tr>"
            )
        return (
            "<div class=card><table><tr><th>Incident</th><th>State</th><th>Paged</th>"
            f"<th>Also impacted</th><th>Agent cost</th></tr>{''.join(rows)}</table></div>"
        )

    @app.get("/incidents", response_class=HTMLResponse)
    def incident_list(product: str = "", team: str = "", state: str = "") -> HTMLResponse:
        incidents = plane.store.list(limit=500)
        if product:
            incidents = [i for i in incidents if i.product == product]
        if team:
            owned = {p.id for p in plane.catalog.products if p.team == team}
            incidents = [i for i in incidents if i.product in owned]
        if state:
            incidents = [i for i in incidents if i.state.value == state.upper()]
        options = "".join(
            f'<option value="{escape(p.id)}"{" selected" if p.id == product else ""}>'
            f"{escape(p.name)}</option>"
            for p in plane.catalog.products
        )
        states = "".join(
            f"<option{' selected' if s.value == state.upper() else ''}>{s.value}</option>"
            for s in State
        )
        filters = (
            '<form method="get"><select name="product"><option value="">all products</option>'
            f'{options}</select><select name="state"><option value="">all states</option>'
            f"{states}</select><button class=approve>Filter</button></form>"
        )
        body = "<h1>Incidents</h1>" + NAV + filters + incident_rows(incidents)
        return page("Incidents", body)

    @app.get("/products/{product_id}", response_class=HTMLResponse)
    def product_page(product_id: str) -> HTMLResponse:
        try:
            product = plane.catalog.product(product_id)
        except StopIteration as exc:
            raise HTTPException(404, f"no product {product_id!r}") from exc
        overall, _ = product_health(metrics, product)
        incidents = plane.store.list(limit=500)
        owned = [i for i in incidents if i.product == product.id]
        impacting = [
            i for i in incidents if product.id in i.impacted_products and i.product != product.id
        ]
        deps = ", ".join(product.depends_on + product.external_dependencies) or "nothing"
        body = (
            f"<h1>{escape(product.name)} {status_pill(overall)}</h1>"
            + NAV
            + f'<p class="muted">{escape(product.team)} &middot; services: '
            f"{escape(', '.join(product.services))} &middot; calls: {escape(deps)}</p>"
            + render_fleet_product(plane, metrics, product, incidents)
            + "<h2>Incidents owned by this team</h2>"
            + incident_rows(owned)
            + "<h2>Other teams' incidents that impacted this product</h2>"
            + incident_rows(impacting)
        )
        return page(product.name, body)

    @app.get("/benchmark", response_class=HTMLResponse)
    def benchmark() -> HTMLResponse:
        body = "<h1>Benchmark</h1>" + NAV
        if not results_path.exists() or not report.load(results_path):
            body += (
                "<div class=card><p>No benchmark results yet.</p><p class=muted>Run "
                "<code>make bench-dry</code> (free) or <code>make bench "
                "ARGS=--confirm-spend</code>; results appear here.</p></div>"
            )
            return page("Benchmark", body)
        records = report.load(results_path)
        summary = report.summarize(records)
        systems = list(summary)
        metrics: list[str] = []
        for row in summary.values():
            metrics += [m for m in row if m not in metrics]
        head = "".join(f"<th>{escape(s)}</th>" for s in systems)
        rows = "".join(
            f"<tr><td>{escape(m)}</td>"
            + "".join(f"<td>{escape(summary[s].get(m, '-'))}</td>" for s in systems)
            + "</tr>"
            for m in metrics
        )
        body += (
            f'<p class="muted">{len(records)} result lines from {escape(results_path.name)}. '
            "Only runs where the fault produced an alert are scored.</p>"
            f"<div class=card><table><tr><th>metric</th>{head}</tr>{rows}</table></div>"
        )
        scenario_rows = "".join(
            "<tr>"
            + "".join(f"<td>{escape(c.strip())}</td>" for c in line.strip("|").split("|"))
            + "</tr>"
            for line in report.per_scenario(records)
        )
        body += (
            "<h2>Per scenario (SentinelOps proposed action)</h2><div class=card><table>"
            f"<tr><th>scenario</th><th>grade per run</th></tr>{scenario_rows}</table></div>"
        )
        return page("Benchmark", body)

    def get_or_404(incident_id: str) -> Incident:
        try:
            return plane.store.get(incident_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/incidents/{incident_id}", response_class=HTMLResponse)
    def incident_page(incident_id: str) -> HTMLResponse:
        incident = get_or_404(incident_id)
        busy = incident.state in (
            State.INVESTIGATING,
            State.APPROVED,
            State.EXECUTING,
            State.VERIFYING,
        )
        return page(incident.id, render_incident(plane, incident), refresh=busy)

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
    def investigate_incident(
        incident_id: str, background: BackgroundTasks, token: str = Form(...)
    ) -> RedirectResponse:
        """Start the agent (paid). The token proves the click came from this page (CSRF)."""
        incident = get_or_404(incident_id)
        if investigate is None:
            raise HTTPException(409, "investigation is not configured (no Anthropic credentials)")
        if token != incident.investigate_token:
            raise HTTPException(409, "stale or forged form; reload the page")
        try:
            plane.start_investigation(incident_id)
        except StaleTransition as exc:
            raise HTTPException(409, str(exc)) from exc
        background.add_task(investigate, incident_id)
        return RedirectResponse(f"/incidents/{incident_id}", status_code=303)

    @app.get("/api/incidents/{incident_id}")
    def incident_json(incident_id: str) -> dict[str, Any]:
        return public(get_or_404(incident_id))

    return app


def main_app() -> FastAPI:
    """Factory for uvicorn (`--factory`): real control plane, live metrics, alert intake, and
    the Investigate button when Anthropic credentials are configured."""
    settings = Settings()
    plane = build_control_plane(settings)
    prometheus = PrometheusBackend(settings.prometheus_url)

    def run_investigation(incident_id: str) -> None:
        incident = plane.store.get(incident_id)
        try:
            result = asyncio.run(investigate_cli(settings, incident.alert_context, incident_id))
        except Exception as exc:  # noqa: BLE001 - the incident escalates with the reason
            plane.escalate(incident_id, State.INVESTIGATING, f"investigation failed: {exc!r}")
            return
        plane.record_investigation(incident_id, result)

    intake = AlertIntake(plane, lambda: firing_alerts(settings.prometheus_url))
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.controlplane_poll_alerts:
            threading.Thread(target=intake.run_forever, args=(stop,), daemon=True).start()
        yield
        stop.set()

    return create_app(
        plane,
        metrics=prometheus,
        investigate=run_investigation if has_anthropic_credentials(settings) else None,
        lifespan=lifespan,
    )
