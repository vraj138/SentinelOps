"""The control plane: moves incidents through the state machine and records every step.

OPEN -> INVESTIGATING -> PROPOSED -> APPROVED -> EXECUTING -> VERIFYING -> RESOLVED
                                                                      \\-> ESCALATED
Approval exists only here (web page / CLI), never as an agent tool.
"""

from datetime import UTC, datetime
from pathlib import Path

from mcp_servers.common import mask_secrets
from sentinelops.agent.audit import AuditLog
from sentinelops.agent.investigator import InvestigationResult
from sentinelops.controlplane.actions import ActionBackend, ActionError
from sentinelops.controlplane.models import (
    Alert,
    Approval,
    CheckResult,
    Incident,
    ProposedAction,
    State,
)
from sentinelops.controlplane.store import IncidentStore
from sentinelops.controlplane.verify import Verifier


def new_incident_id() -> str:
    return datetime.now(UTC).strftime("INC-%Y%m%dT%H%M%SZ")


class ControlPlane:
    def __init__(
        self, store: IncidentStore, actions: ActionBackend, verifier: Verifier, runs_dir: Path
    ) -> None:
        self.store = store
        self.actions = actions
        self.verifier = verifier
        self.runs_dir = runs_dir

    def audit(self, incident_id: str) -> AuditLog:
        """The incident's audit log; the investigation writes to the same file."""
        return AuditLog(self.runs_dir / incident_id / "audit.jsonl")

    def open(
        self, alert_context: str, alerts: list[Alert], incident_id: str | None = None
    ) -> Incident:
        incident = Incident(
            id=incident_id or new_incident_id(), alert_context=alert_context, alerts=alerts
        )
        self.store.create(incident)
        self.audit(incident.id).record(
            "incident_opened", incident=incident.id, alerts=[a.model_dump() for a in alerts]
        )
        return incident

    def start_investigation(self, incident_id: str) -> Incident:
        return self._move(incident_id, State.OPEN, State.INVESTIGATING)

    def record_investigation(self, incident_id: str, result: InvestigationResult) -> Incident:
        """Attach the agent's result; propose its action, or escalate."""
        if result.status != "rca" or result.rca is None:
            return self.escalate(
                incident_id,
                State.INVESTIGATING,
                f"investigation escalated: {result.escalation_reason}",
            )
        rca = result.rca
        action = rca.recommended_action
        if action.kind == "escalate":
            return self.escalate(
                incident_id,
                State.INVESTIGATING,
                f"agent recommends escalation: {action.reason}",
                rca=rca,
            )
        proposal = ProposedAction(
            kind=action.kind,
            service=action.service,
            flag=action.flag,
            flag_value=action.flag_value,
            reason=action.reason,
            proposed_by="agent",
        )
        return self.propose(incident_id, proposal, State.INVESTIGATING, rca=rca)

    def propose(
        self, incident_id: str, proposal: ProposedAction, expected: State = State.OPEN, **updates
    ) -> Incident:
        """Resolve the proposal into an exact action (the dry run). Escalates if it can't."""
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
