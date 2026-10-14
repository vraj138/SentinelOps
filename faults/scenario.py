"""Fault scenarios: the steps that break the demo stack, and the answer key used for scoring.

A scenario is declarative. Its steps say *what* changes (a deploy, a flag flip), and
`faults.environment` knows *how* to apply and undo them. Steps marked `red_herring` are
harmless changes made around the same time to test whether the agent follows evidence.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

SERVICES = ("gateway", "orders", "payments")
Service = Literal["gateway", "orders", "payments"]
INFRA_SERVICES = ("postgres",)
InfraService = Literal["postgres"]


class FaultType(StrEnum):
    BAD_DEPLOY = "bad_deploy"
    MEMORY_LEAK = "memory_leak"
    DB_CONNECTION_EXHAUSTION = "db_connection_exhaustion"
    SLOW_DEPENDENCY = "slow_dependency"
    BAD_FEATURE_FLAG = "bad_feature_flag"
    MISSING_SECRET = "missing_secret"
    DB_OUTAGE = "db_outage"


class Deploy(BaseModel):
    """Commit a new release of one service to the deploy repo, then recreate its container."""

    kind: Literal["deploy"] = "deploy"
    service: Service
    changes: dict[str, str | None] = Field(default_factory=dict)  # None removes a setting
    message: str
    author: str  # team or bot name shown in the deploy repo history
    red_herring: bool = False
    wait_before_seconds: float = 0


class FlipFlag(BaseModel):
    """Set one feature flag. Services pick up the change within a few seconds."""

    kind: Literal["flag"] = "flag"
    name: str
    value: bool
    updated_by: str
    red_herring: bool = False
    wait_before_seconds: float = 0


class SetProviderLatency(BaseModel):
    """Change how slowly the external payment provider answers. Not a deploy: it leaves no trace
    in our change history, like a real third-party slowdown."""

    kind: Literal["provider"] = "provider"
    latency_ms: int = Field(ge=0)
    red_herring: bool = False
    wait_before_seconds: float = 0


class StopService(BaseModel):
    """Stop an infrastructure container (the demo database). Reset starts it again."""

    kind: Literal["stop"] = "stop"
    service: InfraService
    red_herring: bool = False
    wait_before_seconds: float = 0


Step = Annotated[Deploy | FlipFlag | SetProviderLatency | StopService, Field(discriminator="kind")]


class ExpectedAction(BaseModel):
    """The remediation a correct agent should propose."""

    kind: Literal["rollback", "restart", "scale", "set_flag", "escalate"]
    service: Service | None = None
    flag: str | None = None
    flag_value: bool | None = None


class ExpectedAlert(BaseModel):
    """An alert that should fire while the fault is active."""

    name: str
    service: Service


class GroundTruth(BaseModel):
    """The answer key. Never shown to the agent; used only for scoring.

    `expected_action` is the full fix and earns full credit. `acceptable_actions` are safe
    stopgaps that earn partial credit (for example, a restart that clears a leak until it
    builds up again). Any other action is scored as wrong.
    """

    root_cause_service: Service | InfraService
    # Root-cause answers that count as correct. Defaults to just `root_cause_service`; for an
    # external dependency, naming the external provider or the service that calls it both count.
    accepted_root_causes: list[str] = Field(default_factory=list)
    root_cause: str
    key_evidence: list[str]
    expected_action: ExpectedAction
    acceptable_actions: list[ExpectedAction] = Field(default_factory=list)
    expected_alerts: list[ExpectedAlert]

    @property
    def accepted(self) -> list[str]:
        return self.accepted_root_causes or [self.root_cause_service]


class Scenario(BaseModel):
    """One variant of one fault type."""

    fault_type: FaultType
    variant: str
    description: str
    steps: list[Step]
    ground_truth: GroundTruth

    @property
    def id(self) -> str:
        return f"{self.fault_type}/{self.variant}"

    @property
    def red_herrings(self) -> list[Step]:
        return [step for step in self.steps if step.red_herring]

    @model_validator(mode="after")
    def _check_consistency(self) -> "Scenario":
        if not any(not step.red_herring for step in self.steps):
            raise ValueError(f"{self.id}: needs at least one step that is not a red herring")
        for step in self.red_herrings:
            touches_cause = (
                isinstance(step, Deploy) and step.service == self.ground_truth.root_cause_service
            )
            # A decoy on the faulty service is allowed only as a version bump with no setting
            # changes, so the answer key stays unambiguous: its diff provably can't be the cause.
            if touches_cause and step.changes:
                raise ValueError(
                    f"{self.id}: a red herring on the faulty service may not change settings"
                )
        return self
