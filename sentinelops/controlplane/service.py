"""The control plane: moves incidents through the state machine and records every step.

OPEN -> INVESTIGATING -> PROPOSED -> APPROVED -> EXECUTING -> VERIFYING -> RESOLVED
              |                                                       \\-> ESCALATED
              \\-> SELF_RESOLVED (the alert cleared before the analysis finished)
Approval exists only here (web page / CLI), never as an agent tool. Investigations are
read-only, so they may start without a person (see `investigations.py`); actions may not.
"""

import secrets
from datetime import UTC, datetime
from pathlib import Path

from mcp_servers.common import mask_secrets
from sentinelops import routing
from sentinelops.agent.audit import AuditLog
from sentinelops.agent.investigator import InvestigationResult
from sentinelops.catalog import Catalog
from sentinelops.catalog import load as load_catalog
from sentinelops.controlplane.actions import ActionBackend, ActionError
from sentinelops.controlplane.alerts import describe
from sentinelops.controlplane.models import (
    REINVESTIGABLE,
    Alert,
    Approval,
    CheckResult,
    Incident,
    ProposedAction,
    State,
)
from sentinelops.controlplane.store import IncidentStore, StaleTransition
from sentinelops.controlplane.verify import Verifier


def new_incident_id() -> str:
    """Sortable and unique even when several incidents open in the same second."""
    return datetime.now(UTC).strftime("INC-%Y%m%dT%H%M%SZ-") + secrets.token_hex(2)


class ControlPlane:
    def __init__(
        self,
        store: IncidentStore,
        actions: ActionBackend,
        verifier: Verifier,
        runs_dir: Path,
        catalog: Catalog | None = None,
    ) -> None:
        self.store = store
        self.actions = actions
        self.verifier = verifier
        self.runs_dir = runs_dir
        self.catalog = catalog or load_catalog()

    def audit(self, incident_id: str) -> AuditLog:
        """The incident's audit log; the investigation writes to the same file."""
        return AuditLog(self.runs_dir / incident_id / "audit.jsonl")

    def open(
        self, alert_context: str, alerts: list[Alert], incident_id: str | None = None
    ) -> Incident:
        services = [a.service for a in alerts if a.service]
        likely_owner = routing.owner_from_alerts(self.catalog, services)
        incident = Incident(
            id=incident_id or new_incident_id(),
            alert_context=alert_context,
            alerts=alerts,
            product=likely_owner.id if likely_owner else None,
            owner_source="alerts" if likely_owner else None,
            impacted_products=routing.impacted(self.catalog, services),
        )
        self.store.create(incident)
        self.audit(incident.id).record(
            "incident_opened",
            incident=incident.id,
            alerts=[a.model_dump() for a in alerts],
            likely_owner=incident.product,
            impacted_products=incident.impacted_products,
        )
        return incident

    def _owner_updates(self, incident_id: str, service: str, source: str) -> dict:
        """Re-route the incident to the product that owns `service`, if the catalog knows it."""
        owner = routing.owner_from_root_cause(self.catalog, service)
        if owner is None:
            return {}
        previous = self.store.get(incident_id).product
        if owner.id != previous:
            self.audit(incident_id).record(
                "rerouted",
                from_product=previous,
                to_product=owner.id,
                team=owner.team,
                reason=f"{source} points to {service}",
            )
        return {"product": owner.id, "owner_source": source}

    def attach_alerts(self, incident_id: str, alerts: list[Alert]) -> Incident:
        """Add related alerts to an open incident instead of opening another one.

        Until the investigation starts, the alert text it will read is updated too.
        """

        def change(incident: Incident) -> dict:
            known = {a.key for a in incident.alerts}
            merged = incident.alerts + [a for a in alerts if a.key not in known]
            services = [a.service for a in merged if a.service]
            updates: dict = {
                "alerts": merged,
                "impacted_products": routing.impacted(self.catalog, services),
            }
            if incident.investigation.status in (None, "queued", "waiting"):
                updates["alert_context"] = describe(merged)
                if incident.owner_source in (None, "alerts"):
                    owner = routing.owner_from_alerts(self.catalog, services)
                    updates["product"] = owner.id if owner else incident.product
            return updates

        for _ in range(3):  # the state can move under us; re-read and retry
            state = self.store.get(incident_id).state
            try:
                updated = self.store.update(incident_id, state, change)
                break
            except StaleTransition:
                continue
        else:
            raise StaleTransition(f"incident {incident_id} kept changing; alerts not attached")
        self.audit(incident_id).record(
            "alerts_attached", alerts=[a.model_dump(mode="json") for a in alerts]
        )
        return updated

    def set_investigation(
        self, incident_id: str, expected: State, event: str, **fields
    ) -> Incident:
        """Update the investigation bookkeeping (queued, waiting, ...) without a state change."""
        updated = self.store.update(
            incident_id,
            expected,
            lambda i: {"investigation": i.investigation.model_copy(update=fields)},
        )
        self.audit(incident_id).record(event, **{k: _plain(v) for k, v in fields.items()})
        return updated

    def reopen_for_investigation(self, incident_id: str, trigger: str = "manual") -> Incident:
        """Send a proposed, escalated or self-resolved incident back to OPEN, queued for a new
        investigation. The old proposal is cleared at once, so it can't be approved meanwhile.
        Refused once an action has been executed: a person handles that incident."""
        incident = self.store.get(incident_id)
        if incident.state not in REINVESTIGABLE or incident.inverse is not None:
            raise StaleTransition(f"incident {incident_id} can't be re-investigated now")
        info = incident.investigation.model_copy(
            update={"status": "queued", "trigger": trigger, "note": None, "queued_at": _now()}
        )
        self.audit(incident_id).record("reinvestigation_requested", trigger=trigger)
        return self._move(
            incident_id,
            incident.state,
            State.OPEN,
            rca=None,
            proposal=None,
            action=None,
            approval=None,
            escalation_reason=None,
            investigation=info,
        )

    def start_investigation(self, incident_id: str, trigger: str = "manual") -> Incident:
        incident = self.store.get(incident_id)
        info = incident.investigation.model_copy(
            update={
                "status": "running",
                "trigger": trigger,
                "note": None,
                "runs": incident.investigation.runs + 1,
                "started_at": _now(),
            }
        )
        self.audit(incident_id).record("investigation_started", trigger=trigger, run=info.runs)
        return self._move(incident_id, State.OPEN, State.INVESTIGATING, investigation=info)

    def record_investigation(
        self,
        incident_id: str,
        result: InvestigationResult,
        still_firing: bool | None = None,
    ) -> Incident:
        """Attach the agent's result; propose its action, or escalate.

        If the incident's alerts stopped firing before the analysis finished (`still_firing` is
        False), the analysis is kept but nothing is proposed: the incident is SELF_RESOLVED.
        Unknown (None, e.g. Prometheus down) is never treated as resolved.
        """
        incident = self.store.get(incident_id)
        finished = _now()
        alert_seen = (finished - incident.created_at).total_seconds()
        starts = [a.since for a in incident.alerts if a.since]
        info = incident.investigation.model_copy(
            update={
                "status": "done",
                "finished_at": finished,
                "alert_to_rca_seconds": round(alert_seen, 1),
            }
        )
        self.audit(incident_id).record(
            "investigation_finished",
            trigger=info.trigger,
            status=result.status,
            still_firing=still_firing,
            alert_to_rca_seconds=round(alert_seen, 1),
            alert_pending_to_rca_seconds=round((finished - min(starts)).total_seconds(), 1)
            if starts
            else None,
            cost_usd=result.totals.get("cost_usd"),
        )
        if still_firing is False:
            owner = (
                self._owner_updates(incident_id, result.rca.root_cause_service, "investigation")
                if result.rca
                else {}
            )
            return self._move(
                incident_id,
                State.INVESTIGATING,
                State.SELF_RESOLVED,
                rca=result.rca,
                investigation=info,
                escalation_reason=None,
                **owner,
            )
        if result.status != "rca" or result.rca is None:
            return self.escalate(
                incident_id,
                State.INVESTIGATING,
                f"investigation escalated: {result.escalation_reason}",
                investigation=info,
            )
        rca = result.rca
        owner = self._owner_updates(incident_id, rca.root_cause_service, "investigation")
        action = rca.recommended_action
        if action.kind == "escalate":
            return self.escalate(
                incident_id,
                State.INVESTIGATING,
                f"agent recommends escalation: {action.reason}",
                rca=rca,
                investigation=info,
                **owner,
            )
        proposal = ProposedAction(
            kind=action.kind,
            service=action.service,
            flag=action.flag,
            flag_value=action.flag_value,
            reason=action.reason,
            proposed_by="agent",
        )
        return self.propose(
            incident_id, proposal, State.INVESTIGATING, rca=rca, investigation=info, **owner
        )

    def propose(
        self, incident_id: str, proposal: ProposedAction, expected: State = State.OPEN, **updates
    ) -> Incident:
        """Resolve the proposal into an exact action (the dry run). Escalates if it can't."""
        if proposal.proposed_by == "operator" and proposal.service and "product" not in updates:
            updates = {**updates, **self._owner_updates(incident_id, proposal.service, "operator")}
        try:
            resolved = self.actions.resolve(proposal)
        except ActionError as exc:
            return self.escalate(
                incident_id,
                expected,
                f"cannot resolve proposed action: {exc}",
                proposal=proposal,
                **updates,
            )
        self.audit(incident_id).record(
            "action_proposed",
            proposal=proposal.model_dump(),
            dry_run=resolved.dry_run,
            fingerprint=resolved.fingerprint,
        )
        return self._move(
            incident_id, expected, State.PROPOSED, proposal=proposal, action=resolved, **updates
        )

    def approve(self, incident_id: str, approver: str) -> Incident:
        incident = self.store.get(incident_id)
        if incident.action is None:
            raise ActionError("nothing to approve")
        approval = Approval(
            approver=approver, at=datetime.now(UTC), fingerprint=incident.action.fingerprint
        )
        self.audit(incident_id).record("approved", **approval.model_dump(mode="json"))
        return self._move(incident_id, State.PROPOSED, State.APPROVED, approval=approval)

    def reject(self, incident_id: str, approver: str, reason: str) -> Incident:
        self.audit(incident_id).record("rejected", approver=approver, reason=reason)
        return self.escalate(incident_id, State.PROPOSED, f"rejected by {approver}: {reason}")

    def execute(self, incident_id: str) -> Incident:
        """Run the approved action, verify it, and roll it back if verification fails."""
        incident = self._move(incident_id, State.APPROVED, State.EXECUTING)
        audit = self.audit(incident_id)
        assert incident.action and incident.proposal and incident.approval
        if incident.approval.fingerprint != incident.action.fingerprint:
            return self.escalate(incident_id, State.EXECUTING, "approval does not match the action")
        try:
            inverse = self.actions.execute(incident.action, incident.proposal)
        except Exception as exc:  # noqa: BLE001 - recorded and escalated, never swallowed
            audit.record("action_failed", error=repr(exc))
            return self.escalate(incident_id, State.EXECUTING, f"action failed: {exc}")
        audit.record(
            "action_executed",
            action=incident.action.kind,
            target=incident.action.target,
            inverse=mask_secrets(inverse.model_dump()),
        )
        incident = self._move(incident_id, State.EXECUTING, State.VERIFYING, inverse=inverse)

        def on_round(results: list[CheckResult]) -> None:
            audit.record("verification_round", checks=[r.model_dump() for r in results])

        try:
            passed, results = self.verifier.verify(incident, on_round)
        except Exception as exc:  # noqa: BLE001 - a broken check must not leave the action live
            audit.record("verification_error", error=repr(exc))
            passed = False
            results = [CheckResult(name="verification", ok=False, detail=f"could not run: {exc!r}")]
        if passed:
            return self._move(incident_id, State.VERIFYING, State.RESOLVED, verification=results)
        failed = [f"{r.name}: {r.detail}" for r in results if not r.ok]
        try:
            undone = self.actions.undo(inverse)
        except Exception as exc:  # noqa: BLE001 - the incident escalates with the undo failure
            audit.record("rollback_failed", error=repr(exc))
            return self.escalate(
                incident_id,
                State.VERIFYING,
                f"verification failed ({'; '.join(failed)}) and auto-rollback failed: {exc}",
                verification=results,
            )
        audit.record("auto_rollback", detail=undone)
        return self.escalate(
            incident_id,
            State.VERIFYING,
            f"verification failed ({'; '.join(failed)}); auto-rollback: {undone}",
            verification=results,
            rolled_back=True,
        )

    def escalate(self, incident_id: str, expected: State, reason: str, **updates) -> Incident:
        return self._move(
            incident_id, expected, State.ESCALATED, escalation_reason=reason, **updates
        )

    def _move(self, incident_id: str, expected: State, new: State, **updates) -> Incident:
        incident = self.store.transition(incident_id, expected, new, **updates)
        self.audit(incident_id).record("state_change", **{"from": expected.value, "to": new.value})
        return incident


def _now() -> datetime:
    return datetime.now(UTC)


def _plain(value):
    return value.isoformat() if isinstance(value, datetime) else value
