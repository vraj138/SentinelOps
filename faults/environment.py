"""Applies scenario steps to the running demo stack and undoes them.

Every applied change is saved to `storage/faults/active.json` right after it happens, so the
active scenario is known even to a separate process. `reset` restores the baseline directly.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from faults.provider import ProviderFile
from faults.scenario import (
    INFRA_SERVICES,
    Deploy,
    FlipFlag,
    Scenario,
    SetProviderLatency,
    Step,
    StopService,
)
from sentinelops.stack.deploy_repo import BASE_RELEASES, ROOT, DeployRepo
from sentinelops.stack.flags import BASE_FLAGS, FlagFile
from sentinelops.stack.runtime import ComposeRuntime, Runtime

ACTIVE_FILE = ROOT / "storage" / "faults" / "active.json"


class AppliedChange(BaseModel):
    """What one step changed, with enough detail to undo it."""

    kind: Literal["deploy", "flag", "provider", "stop"]
    applied_at: datetime
    red_herring: bool
    service: str | None = None
    commit: str | None = None
    flag: str | None = None
    previous_value: bool | None = None
    previous_latency_ms: int | None = None


class ActiveFault(BaseModel):
    """The scenario currently injected, saved to disk."""

    scenario_id: str
    started_at: datetime
    changes: list[AppliedChange] = Field(default_factory=list)


class FaultAlreadyActive(RuntimeError):
    """Raised when injecting while another scenario is still active."""


def now() -> datetime:
    return datetime.now(UTC)


class Environment:
    """The demo stack as seen by the fault injector."""

    def __init__(
        self,
        repo: DeployRepo | None = None,
        flags: FlagFile | None = None,
        provider: ProviderFile | None = None,
        runtime: Runtime | None = None,
        active_file: Path = ACTIVE_FILE,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.repo = repo or DeployRepo()
        self.flags = flags or FlagFile()
        self.provider = provider or ProviderFile()
        self.runtime = runtime or ComposeRuntime()
        self.active_file = active_file
        self.sleep = sleep

    def active(self) -> ActiveFault | None:
        """The currently injected scenario, if any."""
        if not self.active_file.exists():
            return None
        return ActiveFault.model_validate_json(self.active_file.read_text())

    def inject(self, scenario: Scenario, log: Callable[[str], None] = print) -> ActiveFault:
        """Apply every step of `scenario` in order, saving progress after each one."""
        current = self.active()
        if current is not None:
            raise FaultAlreadyActive(f"{current.scenario_id} is still active; run reset first")
        record = ActiveFault(scenario_id=scenario.id, started_at=now())
        self._save(record)
        for step in scenario.steps:
            if step.wait_before_seconds:
                log(f"waiting {step.wait_before_seconds:.0f}s")
                self.sleep(step.wait_before_seconds)
            record.changes.append(self._apply(step))
            self._save(record)
            log(f"applied {describe(record.changes[-1])}")
        return record

    def reset(self, log: Callable[[str], None] = print) -> ActiveFault | None:
        """Return the stack to its baseline, whatever changed since (faults, remediations,
        manual edits). Returns the scenario that was active, if any.

        Restores state directly instead of replaying history, so it works even after a
        remediation already rolled something back.
        """
        record = self.active()
        changed = []
        for service, settings in BASE_RELEASES.items():
            if self.repo.write_release(service, dict(settings), "Restore baseline release"):
                changed.append(service)
        for service in changed:
            self.runtime.recreate(service)
            log(f"restored baseline release of {service}")
        if self.flags.read() != BASE_FLAGS:
            self.flags.write(dict(BASE_FLAGS), updated_by="oncall")
            log("restored baseline feature flags")
        if self.provider.latency_ms() != 0:
            self.provider.set_latency(0)
            log("restored payment provider to normal")
        for service in INFRA_SERVICES:  # idempotent: starting a running container is a no-op
            self.runtime.start(service)
        if self.active_file.exists():
            self.active_file.unlink()
        return record

    def _apply(self, step: Step) -> AppliedChange:
        if isinstance(step, Deploy):
            author = (step.author, f"{step.author}@sentinelops.local")
            sha = self.repo.deploy(step.service, step.changes, step.message, author)
            self.runtime.recreate(step.service)
            return AppliedChange(
                kind="deploy",
                applied_at=now(),
                red_herring=step.red_herring,
                service=step.service,
                commit=sha,
            )
        if isinstance(step, StopService):
            self.runtime.stop(step.service)
            return AppliedChange(
                kind="stop", applied_at=now(), red_herring=step.red_herring, service=step.service
            )
        if isinstance(step, SetProviderLatency):
            previous_latency = self.provider.latency_ms()
            self.provider.set_latency(step.latency_ms)
            return AppliedChange(
                kind="provider",
                applied_at=now(),
                red_herring=step.red_herring,
                previous_latency_ms=previous_latency,
            )
        assert isinstance(step, FlipFlag)
        previous = self.flags.read().get(step.name, False)
        self.flags.set(step.name, step.value, step.updated_by)
        return AppliedChange(
            kind="flag",
            applied_at=now(),
            red_herring=step.red_herring,
            flag=step.name,
            previous_value=previous,
        )

    def _save(self, record: ActiveFault) -> None:
        self.active_file.parent.mkdir(parents=True, exist_ok=True)
        self.active_file.write_text(record.model_dump_json(indent=2))


def describe(change: AppliedChange) -> str:
    """One-line description of an applied change."""
    tag = " (red herring)" if change.red_herring else ""
    if change.kind == "deploy":
        return f"deploy of {change.service} at {change.commit and change.commit[:7]}{tag}"
    if change.kind == "provider":
        return f"payment provider latency (was {change.previous_latency_ms}ms){tag}"
    if change.kind == "stop":
        return f"stopped {change.service}{tag}"
    return f"flag {change.flag} (was {change.previous_value}){tag}"
