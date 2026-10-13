"""Post-action verification: did the symptoms that opened the incident go away?

Checks every service that alerted (plus the service acted on), not just the one that was
changed: rolling back the wrong service leaves that service healthy while the real failure
continues upstream. Uses short-window PromQL instead of alert state, which clears 60-90s late.
"""

import time
from collections.abc import Callable
from typing import Protocol

from mcp_servers.common import ToolError
from sentinelops.controlplane.models import CheckResult, Incident

ERROR_RATIO_MAX = 0.05
P95_MAX_SECONDS = 1.0
MEMORY_MAX_BYTES = 100 * 1024 * 1024
WINDOW = "30s"


class Metrics(Protocol):
    def query(self, promql: str) -> list[dict]: ...


def scope(incident: Incident) -> list[str]:
    """Services whose health decides whether the action worked."""
    services = {a.service for a in incident.alerts if a.service}
    if incident.action and incident.action.kind in ("rollback", "restart"):
        services.add(incident.action.target)
    return sorted(services) or ["gateway", "orders", "payments"]


class Verifier:
    def __init__(
        self,
        metrics: Metrics,
        settle_seconds: float = 30,
        deadline_seconds: float = 180,
        interval_seconds: float = 10,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.metrics = metrics
        self.settle_seconds = settle_seconds
        self.deadline_seconds = deadline_seconds
        self.interval_seconds = interval_seconds
        self.sleep = sleep
        self.clock = clock

    def verify(
        self, incident: Incident, on_round: Callable[[list[CheckResult]], None]
    ) -> tuple[bool, list[CheckResult]]:
        """Poll checks until one round fully passes or the deadline is reached."""
        start = self.clock()
        self.sleep(self.settle_seconds)
        results: list[CheckResult] = []
        while True:
            results = self.round(incident)
            on_round(results)
            if all(r.ok for r in results):
                return True, results
            if self.clock() - start + self.interval_seconds > self.deadline_seconds:
                return False, results
            self.sleep(self.interval_seconds)

    def round(self, incident: Incident) -> list[CheckResult]:
        alert_names = {a.name for a in incident.alerts}
        results = []
        for service in scope(incident):
            results.append(self._up(service))
            results.append(self._error_ratio(service))
            if "HighLatencyP95" in alert_names:
                results.append(self._p95(service))
            if "HighMemoryUsage" in alert_names:
                results.append(self._memory(service))
        return results

    def _value(self, promql: str) -> float | None:
        result = self.metrics.query(promql)
        return float(result[0]["value"][1]) if result else None

    def _check(
        self,
        name: str,
        promql: str,
        ok: Callable[[float | None], bool],
        fmt: Callable[[float | None], str],
    ) -> CheckResult:
        try:
            value = self._value(promql)
        except ToolError as exc:
            return CheckResult(name=name, ok=False, detail=f"could not check: {exc}")
        return CheckResult(name=name, ok=ok(value), detail=fmt(value))

    def _up(self, service: str) -> CheckResult:
        return self._check(
            f"{service} up",
            f'up{{job="demo",service="{service}"}}',
            lambda v: v == 1,
            lambda v: "scrape ok" if v == 1 else "not reachable",
        )

    def _error_ratio(self, service: str) -> CheckResult:
        # A freshly started process has no 5xx series until its first error, so a missing
        # numerator means zero errors (`or vector(0)`). Only an empty denominator means no
        # traffic.
        promql = (
            f'(sum(rate(http_requests_total{{service="{service}",status=~"5.."}}[{WINDOW}])) '
            "or vector(0)) "
            f'/ sum(rate(http_requests_total{{service="{service}",route!="/health"}}[{WINDOW}]))'
        )
        return self._check(
            f"{service} 5xx ratio < {ERROR_RATIO_MAX:.0%}",
            promql,
            lambda v: v is not None and v < ERROR_RATIO_MAX,
            lambda v: "no traffic" if v is None else f"{v:.1%}",
        )

    def _p95(self, service: str) -> CheckResult:
        promql = (
            "histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket"
            f'{{service="{service}",route!="/health"}}[{WINDOW}])))'
        )
        return self._check(
            f"{service} p95 < {P95_MAX_SECONDS:.0f}s",
            promql,
            lambda v: v is not None and v < P95_MAX_SECONDS,
            lambda v: "no traffic" if v is None else f"{v:.2f}s",
        )

    def _memory(self, service: str) -> CheckResult:
        return self._check(
            f"{service} memory < 100 MiB",
            f'process_resident_memory_bytes{{service="{service}"}}',
            lambda v: v is not None and v < MEMORY_MAX_BYTES,
            lambda v: "unknown" if v is None else f"{v / 1048576:.0f} MiB",
        )
