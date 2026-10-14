"""Alert intake: opens incidents from firing Prometheus alerts, without spending anything.

Polls Prometheus instead of receiving an Alertmanager webhook: no extra container, and no
need to expose the (unauthenticated) control plane beyond localhost. It only opens and
de-duplicates incidents; an investigation costs money, so it starts only when a person clicks
Investigate.
"""

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sentinelops import routing
from sentinelops.controlplane.models import TERMINAL, Alert
from sentinelops.controlplane.service import ControlPlane

logger = logging.getLogger(__name__)
COOLDOWN = timedelta(minutes=10)  # alerts linger 1-2 min after a fix; don't reopen at once


class AlertIntake:
    def __init__(
        self,
        plane: ControlPlane,
        fetch: Callable[[], tuple[str, list[Alert]]],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.plane = plane
        self.fetch = fetch
        self.now = now

    def tick(self) -> str | None:
        """One poll. Returns the id of a newly opened incident, if any."""
        context, alerts = self.fetch()
        services = [a.service for a in alerts if a.service]
        products = set(routing.impacted(self.plane.catalog, services))
        if not products:
            return None
        for incident in self.plane.store.list(limit=200):
            overlaps = products & set(incident.impacted_products)
            recent = self.now() - incident.updated_at < COOLDOWN
            if overlaps and (incident.state not in TERMINAL or recent):
                return None  # already handled, or just handled
        incident = self.plane.open(context, alerts)
        logger.info("opened %s for %s", incident.id, sorted(products))
        return incident.id

    def run_forever(self, stop: threading.Event, interval: float = 15) -> None:
        while not stop.wait(interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - logged with traceback; the next poll retries
                logger.exception("alert intake poll failed")
