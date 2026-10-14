"""Incident records and the state machine they move through."""

import secrets
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from sentinelops.agent.models import RootCauseAnalysis


class State(StrEnum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"


TERMINAL = {State.RESOLVED, State.ESCALATED}

# Allowed transitions. Anything not listed is refused by the store.
TRANSITIONS: dict[State, set[State]] = {
    State.OPEN: {State.INVESTIGATING, State.PROPOSED, State.ESCALATED},
    State.INVESTIGATING: {State.PROPOSED, State.ESCALATED},
    State.PROPOSED: {State.APPROVED, State.ESCALATED},
    State.APPROVED: {State.EXECUTING, State.ESCALATED},
    State.EXECUTING: {State.VERIFYING, State.ESCALATED},
    State.VERIFYING: {State.RESOLVED, State.ESCALATED},
    State.RESOLVED: set(),
    State.ESCALATED: set(),
}


class Alert(BaseModel):
    name: str
    service: str | None


class ProposedAction(BaseModel):
    """What someone (the agent or an operator) wants done, before it is resolved."""

    kind: Literal["rollback", "restart", "set_flag"]
    service: str | None = None
    flag: str | None = None
    flag_value: bool | None = None
    reason: str
    proposed_by: str


class ResolvedAction(BaseModel):
    """A proposal turned into one exact change: what a human reviews and approves."""

    kind: Literal["rollback", "restart", "set_flag"]
    target: str  # service name or flag name
    payload: dict[str, Any]  # everything needed to execute, including what was observed
    dry_run: str  # human-readable description of the change (secrets redacted)
    fingerprint: str  # sha256 of the payload; approval is bound to it


class Approval(BaseModel):
    approver: str
    at: datetime
    fingerprint: str


class Inverse(BaseModel):
    """How to undo an executed action, recorded at execution time."""

    kind: Literal["restore_release", "set_flag", "none"]
    target: str
    data: dict[str, Any] = Field(default_factory=dict)


class CheckResult(BaseModel):
    name: str
    ok: bool
    detail: str


class Incident(BaseModel):
    id: str
    state: State = State.OPEN
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    alert_context: str
    alerts: list[Alert] = Field(default_factory=list)
    rca: RootCauseAnalysis | None = None
    proposal: ProposedAction | None = None
    action: ResolvedAction | None = None
    approval: Approval | None = None
    inverse: Inverse | None = None
    verification: list[CheckResult] = Field(default_factory=list)
    rolled_back: bool = False
    escalation_reason: str | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)
    # Routing. Optional so incidents stored before the catalog existed still load.
    product: str | None = None  # owning product id: the team that gets paged
    owner_source: Literal["alerts", "investigation", "operator"] | None = None
    impacted_products: list[str] = Field(default_factory=list)
    investigate_token: str = Field(default_factory=lambda: secrets.token_hex(16))
