"""Web UI and JSON API for incidents: review the evidence and the dry run, approve or reject.

Run with `make controlplane` (uvicorn, http://localhost:8080). Pages are server-rendered HTML;
every value is escaped. Approving starts execution and verification in the background.
"""

import json
from html import escape
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from mcp_servers.common import mask_secrets
from sentinelops.config import Settings
from sentinelops.controlplane import build_control_plane
from sentinelops.controlplane.actions import ActionError
from sentinelops.controlplane.models import Incident, State
from sentinelops.controlplane.service import ControlPlane
from sentinelops.controlplane.store import StaleTransition

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


def render_incident(plane: ControlPlane, incident: Incident) -> str:
    parts = [
        '<p><a href="/">&larr; all incidents</a></p>',
        f"<h1>{escape(incident.id)} {state_pill(incident.state)}</h1>",
        f'<p class="muted">opened {incident.created_at:%Y-%m-%d %H:%M:%S} UTC</p>',
        f'<div class="card"><pre>{escape(incident.alert_context)}</pre></div>',
    ]
    if incident.escalation_reason:
        parts.append(
            f'<div class="card bad"><b>Escalated:</b> {escape(incident.escalation_reason)}</div>'
        )
    tools = tool_records(plane, incident.id)
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
    return mask_secrets(data)


def create_app(plane: ControlPlane) -> FastAPI:
    app = FastAPI(title="SentinelOps control plane")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        rows = "".join(
            f'<tr><td><a href="/incidents/{escape(i.id)}">{escape(i.id)}</a></td><td>{state_pill(i.state)}</td>'
            f"<td>{escape(', '.join(f'{a.name}({a.service})' for a in i.alerts) or '-')}</td></tr>"
            for i in plane.store.list()
        )
        body = (
            "<h1>SentinelOps</h1><p class=muted>Incidents: evidence, proposed fix, human approval, "
            "verification.</p><div class=card><table><tr><th>Incident</th><th>State</th>"
            f"<th>Alerts</th></tr>{rows}</table></div>"
        )
        return page("SentinelOps", body)

    def get_or_404(incident_id: str) -> Incident:
        try:
            return plane.store.get(incident_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/incidents/{incident_id}", response_class=HTMLResponse)
    def incident_page(incident_id: str) -> HTMLResponse:
        incident = get_or_404(incident_id)
        busy = incident.state in (State.APPROVED, State.EXECUTING, State.VERIFYING)
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

    @app.get("/api/incidents/{incident_id}")
    def incident_json(incident_id: str) -> dict[str, Any]:
        return public(get_or_404(incident_id))

    return app


def main_app() -> FastAPI:
    """Factory for uvicorn (`--factory`): connects to the real control plane."""
    return create_app(build_control_plane(Settings()))
