"""Alert intake: opens incidents from firing Prometheus alerts, one incident per problem.

Polls Prometheus instead of receiving an Alertmanager webhook: no extra container, and no
need to expose the (unauthenticated) control plane beyond localhost. Each new incident is
handed to `on_open`, which starts its investigation (see `investigations.py`).

One failure usually fires several alerts: a broken payments release makes payments, orders and
gateway all alert. Alerts are therefore grouped:

- An alert already part of an open incident is ignored.
- An alert on a service related to an open incident (connected through the catalog's
  dependencies) joins it if it started within the grouping window of that incident's first
  alert, or if the incident already covers that service.
- An alert related to an incident closed less than COOLDOWN ago is ignored: alerts linger for
  a minute or two after a fix.
- The remaining alerts are grouped the same way among themselves, one incident per group.
"""

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sentinelops.controlplane.alerts import describe
from sentinelops.controlplane.models import TERMINAL, Alert, Incident
from sentinelops.controlplane.service import ControlPlane

logger = logging.getLogger(__name__)
COOLDOWN = timedelta(minutes=10)  # alerts linger 1-2 min after a fix; don't reopen at once
GROUP_WINDOW = timedelta(minutes=5)


class AlertIntake:
    def __init__(
        self,
        plane: ControlPlane,
        fetch: Callable[[], tuple[str, list[Alert]]],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        on_open: Callable[[Incident], None] | None = None,
        window: timedelta = GROUP_WINDOW,
    ) -> None:
        self.plane = plane
        self.fetch = fetch
        self.now = now
        self.on_open = on_open
        self.window = window

    def tick(self) -> list[str]:
        """One poll. Returns the ids of newly opened incidents."""
        _, alerts = self.fetch()
        if not alerts:
            return []
        now = self.now()
        incidents = self.plane.store.list(limit=200)
        live = [i for i in incidents if i.state not in TERMINAL]
        closed = [i for i in incidents if i.state in TERMINAL and now - i.updated_at < COOLDOWN]
        joins: dict[str, list[Alert]] = {}
        fresh: list[Alert] = []
        for alert in alerts:
            if any(alert.key in {a.key for a in i.alerts} for i in live):
                continue
            if any(self.related(alert, i.alerts) for i in closed):
                continue
            target = next((i for i in live if self.joins(alert, i, now)), None)
            if target is not None:
                joins.setdefault(target.id, []).append(alert)
            else:
                fresh.append(alert)
        for incident_id, extra in joins.items():
            self.plane.attach_alerts(incident_id, extra)
            logger.info("attached %s to %s", [a.key for a in extra], incident_id)
        opened = []
        for group in self.group(fresh, now):
            incident = self.plane.open(describe(group), group)
            logger.info("opened %s for %s", incident.id, [a.key for a in group])
            opened.append(incident.id)
            if self.on_open is not None:
                self.on_open(incident)
        return opened

    def related(self, alert: Alert, others: list[Alert]) -> bool:
        """Same alert, or a service in the same dependency group as one of `others`."""
        if alert.service is None:
            return any(alert.key == o.key for o in others)
        group = self.plane.catalog.dependency_group(alert.service)
        return any(o.service in group for o in others)

    def joins(self, alert: Alert, incident: Incident, now: datetime) -> bool:
        if not self.related(alert, incident.alerts):
            return False
        if any(a.service == alert.service for a in incident.alerts):
            return True  # the incident already covers this service: same ongoing problem
        starts = [a.since for a in incident.alerts if a.since] or [incident.created_at]
        return abs((alert.since or now) - min(starts)) <= self.window

    def group(self, alerts: list[Alert], now: datetime) -> list[list[Alert]]:
        """Related alerts that started within the window of each other, oldest first."""
        groups: list[list[Alert]] = []
        for alert in sorted(alerts, key=lambda a: a.since or now):
            start = alert.since or now
            for group in groups:
                first = group[0].since or now
                if self.related(alert, group) and abs(start - first) <= self.window:
                    group.append(alert)
                    break
            else:
                groups.append([alert])
        return groups

    def run_forever(self, stop: threading.Event, interval: float = 15) -> None:
        while not stop.wait(interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - logged with traceback; the next poll retries
                logger.exception("alert intake poll failed")
