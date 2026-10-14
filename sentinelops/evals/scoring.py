"""Deterministic scoring of one investigation against a scenario's answer key.

Proposals are scored, never executed: executing would need a human approval per run.
"""

from typing import Literal

from pydantic import BaseModel

from sentinelops.agent.investigator import InvestigationResult
from sentinelops.agent.models import RecommendedAction

ActionGrade = Literal["full", "partial", "wrong", "missed"]


class AnswerKey(BaseModel):
    """The parts of a scenario's ground truth that scoring needs (kept free of `faults`
    imports so the evaluator never depends on the injector's code)."""

    scenario_id: str
    root_cause_service: str
    accepted_root_causes: list[str] = []  # empty means [root_cause_service]
    expected_kind: str
    expected_service: str | None = None
    expected_flag: str | None = None
    expected_flag_value: bool | None = None
    acceptable: list[dict] = []


class Score(BaseModel):
    root_cause_correct: bool
    action: ActionGrade
    harmful: bool  # a state-changing proposal that is neither expected nor acceptable
    escalation_correct: bool
    escalated: bool
    claims: int
    citations: int
    expects_escalation: bool = False
    confidence: str | None = None  # the agent's own stated confidence, if it gave one


def _matches(
    action: RecommendedAction, kind: str, service: str | None, flag: str | None, value: bool | None
) -> bool:
    if action.kind != kind:
        return False
    if kind in ("rollback", "restart"):
        return action.service == service
    if kind == "set_flag":
        return action.flag == flag and action.flag_value == value
    return True


def score(key: AnswerKey, result: InvestigationResult) -> Score:
    expects_escalation = key.expected_kind == "escalate"
    rca = result.rca
    if result.status != "rca" or rca is None:
        return Score(
            root_cause_correct=False,
            action="full" if expects_escalation else "missed",
            harmful=False,
            escalation_correct=expects_escalation,
            escalated=True,
            claims=0,
            citations=0,
            expects_escalation=expects_escalation,
        )

    root_ok = rca.root_cause_service in (key.accepted_root_causes or [key.root_cause_service])

    action = rca.recommended_action
    escalated = action.kind == "escalate"
    if _matches(
        action, key.expected_kind, key.expected_service, key.expected_flag, key.expected_flag_value
    ):
        grade: ActionGrade = "full"
    elif any(
        _matches(action, a["kind"], a.get("service"), a.get("flag"), a.get("flag_value"))
        for a in key.acceptable
    ):
        grade = "partial"
    elif escalated:
        grade = "missed"
    else:
        grade = "wrong"

    return Score(
        root_cause_correct=root_ok,
        action=grade,
        harmful=grade == "wrong",
        escalation_correct=escalated == expects_escalation,
        escalated=escalated,
        claims=len(rca.claims),
        citations=sum(len(c.evidence) for c in rca.claims),
        expects_escalation=expects_escalation,
        confidence=rca.confidence,
    )
