"""A clean slate before each benchmark run.

`make reset` restores settings but not evidence. Left alone, the next run would see the
previous run's deploys, error logs, container events and metric history, and an agent could
"solve" it from leftovers. Before every run this archives and clears all of that.
"""

import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from sentinelops.stack.deploy_repo import DeployRepo
from sentinelops.stack.flags import BASE_FLAGS, FlagFile

DEMO_SERVICES = ("payments", "orders", "gateway")
INFRA_SERVICES = ("postgres",)


class Runtime(Protocol):
    def recreate(self, service: str, force: bool = False) -> None: ...
    def start(self, service: str) -> None: ...


class Alerts(Protocol):
    def alerts(self) -> list[dict]: ...


class CleanSlate:
    def __init__(
        self,
        repo: DeployRepo,
        flags: FlagFile,
        reset_provider: Callable[[], None],
        runtime: Runtime,
        events_path: Path,
        alerts: Alerts,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.repo = repo
        self.flags = flags
        self.reset_provider = reset_provider
        self.runtime = runtime
        self.events_path = events_path
        self.alerts = alerts
        self.sleep = sleep
        self.clock = clock

    def prepare(self, archive_dir: Path, warmup_seconds: float = 60, timeout: float = 300) -> None:
        """Archive the previous run's evidence, rebuild a fresh baseline, and wait until it
        is quiet and Prometheus has enough fresh data for rate() windows."""
        archive_dir.mkdir(parents=True, exist_ok=True)
        if self.repo.path.exists():
            shutil.move(str(self.repo.path), str(archive_dir / "deploy-repo"))
        self.repo.init()
        self.flags.write(dict(BASE_FLAGS), updated_by="init")
        self.reset_provider()
        for service in INFRA_SERVICES:  # a crashed earlier run may have left the database down
            self.runtime.start(service)
        for service in DEMO_SERVICES:
            self.runtime.recreate(service, force=True)  # fresh logs and counters
        # Prometheus last, so its fresh history never sees the restarts above.
        self.runtime.recreate("prometheus", force=True)
        self.wait_until_quiet(warmup_seconds, timeout)
        # Only now: the restarts above would otherwise show up as this run's container events.
        if self.events_path.exists():
            shutil.copy(self.events_path, archive_dir / "container-events.jsonl")
            self.events_path.write_text("")  # truncate; the recorder keeps appending

    def wait_until_quiet(self, warmup_seconds: float, timeout: float) -> None:
        start = self.clock()
        self.sleep(warmup_seconds)
        last = "no check made"
        while True:
            try:
                active = self.alerts.alerts()
                if not active:
                    return
                last = "alerts still active: " + ", ".join(
                    str(a.get("labels", {}).get("alertname")) for a in active
                )
            except Exception as exc:  # noqa: BLE001 - Prometheus may still be starting
                last = f"could not read alerts: {exc!r}"
            if self.clock() - start > timeout:
                raise TimeoutError(f"stack did not become quiet within {timeout:.0f}s ({last})")
            self.sleep(5)
