"""Applies scenario steps to the running demo stack and undoes them.

Every applied change is saved to `storage/faults/active.json` right after it happens, so
`reset` can undo a scenario from a separate process, even one that failed halfway through.
"""

import os
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from faults.deploy_repo import ROOT, DeployRepo
from faults.flags import FlagFile
from faults.provider import ProviderFile
from faults.scenario import Deploy, FlipFlag, Scenario, SetProviderLatency, Step

COMPOSE_FILE = ROOT / "infra" / "docker-compose.yml"
ACTIVE_FILE = ROOT / "storage" / "faults" / "active.json"


class Runtime(Protocol):
    """Restarts a service so it picks up its new release settings."""

    def recreate(self, service: str) -> None: ...


class ContainerRuntimeError(RuntimeError):
    """A container runtime command failed."""


def tag_from_image(image: str) -> str:
    """The tag part of an image reference: `sentinelops-demo:4078549` -> `4078549`."""
    name, sep, tag = image.rpartition(":")
    if not sep or "/" in tag:
        raise ContainerRuntimeError(f"image {image!r} has no tag")
    return tag


class ComposeRuntime:
    """Runtime backed by `docker compose`. Commands are argument lists, never a shell string.

    A recreated container keeps the image tag it was already running, so it runs the same code
    as the rest of the stack even if the main repo has new commits since `make up`.
    """

    def __init__(self, compose_file: Path = COMPOSE_FILE) -> None:
        self.compose_file = compose_file

    def recreate(self, service: str) -> None:
        env = {**os.environ, "IMAGE_TAG": self.running_image_tag(service)}
        args = [*self._compose(), "up", "-d", "--no-deps", service]
        result = subprocess.run(args, capture_output=True, text=True, env=env)
        if result.returncode != 0:
            raise ContainerRuntimeError(f"recreating {service} failed: {result.stderr.strip()}")

    def running_image_tag(self, service: str) -> str:
        """Tag of the image the service's container is running now."""
        container_id = self._run([*self._compose(), "ps", "-q", service]).strip()
        if not container_id:
            raise ContainerRuntimeError(f"{service} has no container; run `make up` first")
        image = self._run(["docker", "inspect", container_id, "--format", "{{.Config.Image}}"])
        return tag_from_image(image.strip())

    def _compose(self) -> list[str]:
        return ["docker", "compose", "-f", str(self.compose_file)]

    def _run(self, args: list[str]) -> str:
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode != 0:
            raise ContainerRuntimeError(f"{' '.join(args[:3])} failed: {result.stderr.strip()}")
        return result.stdout


class AppliedChange(BaseModel):
    """What one step changed, with enough detail to undo it."""

    kind: Literal["deploy", "flag", "provider"]
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
        """Undo the active scenario's changes in reverse order. Returns what was undone."""
        record = self.active()
        if record is None:
            return None
        while record.changes:
            change = record.changes[-1]
            self._undo(change)
            record.changes.pop()
            self._save(record)
            log(f"undid {describe(change)}")
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

    def _undo(self, change: AppliedChange) -> None:
        if change.kind == "deploy" and change.commit and change.service:
            self.repo.revert(change.commit)
            self.runtime.recreate(change.service)
        elif change.kind == "flag" and change.flag and change.previous_value is not None:
            self.flags.set(change.flag, change.previous_value, "oncall")
        elif change.kind == "provider" and change.previous_latency_ms is not None:
            self.provider.set_latency(change.previous_latency_ms)
        else:
            raise ValueError(f"cannot undo incomplete change record: {change}")

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
    return f"flag {change.flag} (was {change.previous_value}){tag}"
