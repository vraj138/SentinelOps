"""Investigations: started automatically when an alert opens an incident.

An investigation is read-only (the agent's tools can't change anything), so it doesn't wait
for a person; only remediation does. What keeps automatic runs cheap and predictable:

- One investigation per incident. The alert intake groups related alerts into one incident,
  and an incident that has been queued, run or finished is never started again automatically.
  A person can still re-investigate.
- At most `concurrency` run at once; the rest wait in a queue.
- A daily budget for automatic runs. Each run reserves part of what is left and is capped
  there by the agent's own cost budget, so in-flight runs can't push the day past the budget
  (beyond the one model call that crosses a run's cap). When the budget is used up, incidents
  are still opened but wait for a manual start, and say why.
- No automatic repeat for alerts an investigation already looked at in the last 24 hours and
  could not fix (escalated or self-resolved): a known, unfixed alert would otherwise buy a new
  investigation every time it reopens.
- If the alerts stop firing before the analysis finishes, the incident is SELF_RESOLVED and
  nothing is proposed.
"""

import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sentinelops.agent.audit import AuditLog
from sentinelops.agent.investigator import InvestigationResult
from sentinelops.controlplane.models import Incident, State
from sentinelops.controlplane.service import ControlPlane
from sentinelops.controlplane.store import StaleTransition

logger = logging.getLogger(__name__)
MIN_RUN_BUDGET_USD = 0.05  # below this an investigation can't do useful work


class InvestigationConflict(RuntimeError):
    """The incident can't be (re-)investigated right now."""


class SpendLedger:
    """What investigations cost, one JSON line per run, so the daily total survives restarts.

    Automatic runs reserve budget before they start; the reservation is replaced by the real
    cost when they finish.
    """

    def __init__(self, path: Path, now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.path = path
        self.now = now
        self.lock = threading.Lock()
        self.reserved: dict[str, float] = {}

    def spent_today(self, trigger: str = "auto") -> float:
        if not self.path.exists():
            return 0.0
        today = self.now().date().isoformat()
        rows = [json.loads(line) for line in self.path.read_text().splitlines() if line]
        return sum(r["cost_usd"] for r in rows if r["at"][:10] == today and r["trigger"] == trigger)

    def remaining(self, budget: float) -> float:
        return budget - self.spent_today() - sum(self.reserved.values())

    def reserve(self, incident_id: str, budget: float, per_run: float) -> float | None:
        """Reserve up to `per_run` of today's remaining budget, or None if too little is left."""
        with self.lock:
            amount = min(per_run, self.remaining(budget))
            if amount < MIN_RUN_BUDGET_USD:
                return None
            self.reserved[incident_id] = amount
            return amount

    def record(self, incident_id: str, trigger: str, cost_usd: float) -> None:
        with self.lock:
            self.reserved.pop(incident_id, None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "at": self.now().isoformat(),
                "incident": incident_id,
                "trigger": trigger,
                "cost_usd": round(cost_usd, 6),
            }
            with self.path.open("a") as f:
                f.write(json.dumps(entry) + "\n")

    def release(self, incident_id: str) -> None:
        with self.lock:
            self.reserved.pop(incident_id, None)


def run_cost(runs_dir: Path, incident_id: str) -> float:
    """Model spend of the latest run, read from the audit log (written as each call happens,
    so a run that crashed half-way is still counted)."""
    path = runs_dir / incident_id / "audit.jsonl"
    if not path.exists():
        return 0.0
    records = AuditLog.load(path).records
    starts = [i for i, r in enumerate(records) if r["kind"] == "investigation_started"]
    since = starts[-1] if starts else 0
    return sum(r.get("cost_usd") or 0 for r in records[since:] if r["kind"] == "llm_call")


RunFn = Callable[[Incident, float | None], InvestigationResult]


class Investigations:
    def __init__(
        self,
        plane: ControlPlane,
        run: RunFn,
        still_firing: Callable[[Incident], bool | None],
        ledger: SpendLedger,
        auto: bool = True,
        concurrency: int = 2,
        daily_budget_usd: float = 2.0,
        max_run_cost_usd: float = 3.0,
        repeat_window: timedelta = timedelta(hours=24),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        executor: Executor | None = None,
    ) -> None:
        """`run(incident, cost_limit)` runs the agent; `cost_limit` is None for manual runs.
        `executor` defaults to a pool of `concurrency` threads (tests pass their own)."""
        self.plane = plane
        self.run = run
        self.still_firing = still_firing
        self.ledger = ledger
        self.auto = auto
        self.concurrency = max(1, concurrency)
        self.daily_budget_usd = daily_budget_usd
        self.max_run_cost_usd = max_run_cost_usd
        self.repeat_window = repeat_window
        self.now = now
        self.executor = executor or ThreadPoolExecutor(
            self.concurrency, thread_name_prefix="investigation"
        )
        self.lock = threading.Lock()
        self.active: set[str] = set()  # queued or running in this process
        self.running: set[str] = set()

    # Starting

    def on_opened(self, incident: Incident) -> None:
        """Called by the alert intake for each new incident: start it, or say why not."""
        current = self.plane.store.get(incident.id)
        info = current.investigation
        if current.state != State.OPEN or info.status is not None or info.runs:
            logger.info("%s already has an investigation; not starting another", incident.id)
            return
        reason = self._why_not_auto(current)
        if reason:
            self.plane.set_investigation(
                incident.id, State.OPEN, "investigation_waiting", status="waiting", note=reason
            )
            logger.info("%s waits for a manual start: %s", incident.id, reason)
            return
        self.plane.set_investigation(
            incident.id,
            State.OPEN,
            "investigation_queued",
            status="queued",
            trigger="auto",
            queued_at=self.now(),
        )
        self._enqueue(incident.id, "auto")

    def start_manual(self, incident_id: str) -> Incident:
        """A person asked for an investigation: start a waiting incident, or re-investigate."""
        incident = self.plane.store.get(incident_id)
        with self.lock:
            busy = incident_id in self.active
        if busy or incident.investigation.status in ("queued", "running"):
            raise InvestigationConflict("an investigation is already queued or running")
        try:
            if incident.state == State.OPEN:
                updated = self.plane.set_investigation(
                    incident_id,
                    State.OPEN,
                    "investigation_queued",
                    status="queued",
                    trigger="manual",
                    note=None,
                    queued_at=self.now(),
                )
            else:
                updated = self.plane.reopen_for_investigation(incident_id, "manual")
        except StaleTransition as exc:
            raise InvestigationConflict(str(exc)) from exc
        self._enqueue(incident_id, "manual")
        return updated

    def recover(self) -> None:
        """After a restart: re-queue what was queued; runs that were cut off are escalated
        (re-running them would spend money nobody asked for twice)."""
        for incident in self.plane.store.list(limit=500):
            if incident.state == State.INVESTIGATING:
                self._fail(incident.id, "investigation interrupted: the control plane restarted")
            elif incident.state == State.OPEN and incident.investigation.status == "queued":
                self._enqueue(incident.id, incident.investigation.trigger or "manual")

    def _why_not_auto(self, incident: Incident) -> str | None:
        if not self.auto:
            return "Automatic investigation is off (AUTO_INVESTIGATE=false)."
        earlier = self._recent_unfixed(incident)
        if earlier is not None:
            outcome = "self-resolved" if earlier.state == State.SELF_RESOLVED else "escalated"
            return (
                f"The same alerts were investigated in {earlier.id}, which {outcome}; they are "
                f"not re-investigated automatically within {self._hours()} hours."
            )
        if self.ledger.remaining(self.daily_budget_usd) < MIN_RUN_BUDGET_USD:
            return self._budget_note()
        return None

    def _recent_unfixed(self, incident: Incident) -> Incident | None:
        keys = {a.key for a in incident.alerts}
        for other in self.plane.store.list(limit=500):
            info = other.investigation
            if (
                other.id != incident.id
                and other.state in (State.ESCALATED, State.SELF_RESOLVED)
                and info.finished_at is not None
                and self.now() - info.finished_at < self.repeat_window
                and keys & {a.key for a in other.alerts}
            ):
                return other
        return None

    def _budget_note(self) -> str:
        spent = self.ledger.spent_today()
        return (
            f"Today's budget for automatic investigations (${self.daily_budget_usd:.2f}) is "
            f"used up (${spent:.2f} spent, AUTO_INVESTIGATE_DAILY_BUDGET_USD)."
        )

    def _hours(self) -> int:
        return int(self.repeat_window.total_seconds() // 3600)

    def _enqueue(self, incident_id: str, trigger: str) -> None:
        with self.lock:
            if incident_id in self.active:
                return
            self.active.add(incident_id)
        self.executor.submit(self._run, incident_id, trigger)

    # Running

    def _run(self, incident_id: str, trigger: str) -> None:
        try:
            self._run_once(incident_id, trigger)
        except Exception:  # noqa: BLE001 - logged; the incident shows what failed
            logger.exception("investigation of %s failed outside the agent", incident_id)
        finally:
            self.ledger.release(incident_id)
            with self.lock:
                self.active.discard(incident_id)
                self.running.discard(incident_id)

    def _run_once(self, incident_id: str, trigger: str) -> None:
        limit = None
        if trigger == "auto":
            per_run = min(self.max_run_cost_usd, self.daily_budget_usd / self.concurrency)
            limit = self.ledger.reserve(incident_id, self.daily_budget_usd, per_run)
            if limit is None:
                self.plane.set_investigation(
                    incident_id,
                    State.OPEN,
                    "investigation_waiting",
                    status="waiting",
                    note=self._budget_note(),
                )
                return
        try:
            incident = self.plane.start_investigation(incident_id, trigger)
        except StaleTransition as exc:
            self._skip(incident_id, f"not started: {exc}")
            return
        with self.lock:
            self.running.add(incident_id)
        try:
            result = self.run(incident, limit)
        except Exception as exc:  # noqa: BLE001 - the incident escalates with the reason
            self.ledger.record(incident_id, trigger, run_cost(self.plane.runs_dir, incident_id))
            self._fail(incident_id, f"investigation failed: {exc!r}")
            return
        self.ledger.record(incident_id, trigger, run_cost(self.plane.runs_dir, incident_id))
        firing = self.still_firing(self.plane.store.get(incident_id))  # includes attached alerts
        self.plane.record_investigation(incident_id, result, still_firing=firing)

    def _fail(self, incident_id: str, reason: str) -> None:
        incident = self.plane.store.get(incident_id)
        info = incident.investigation.model_copy(
            update={"status": "failed", "note": reason, "finished_at": self.now()}
        )
        self.plane.escalate(incident_id, State.INVESTIGATING, reason, investigation=info)

    def _skip(self, incident_id: str, note: str) -> None:
        """The incident moved on before its turn came (someone acted on it); drop the run."""
        state = self.plane.store.get(incident_id).state
        try:
            self.plane.set_investigation(
                incident_id, state, "investigation_skipped", status=None, note=note
            )
        except StaleTransition:
            logger.warning("%s changed again while skipping its investigation", incident_id)

    # Reporting

    def status(self) -> dict[str, Any]:
        with self.lock:
            running, active = len(self.running), len(self.active)
        return {
            "auto": self.auto,
            "concurrency": self.concurrency,
            "running": running,
            "queued": active - running,
            "budget_usd": self.daily_budget_usd,
            "spent_today_usd": self.ledger.spent_today(),
        }

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
