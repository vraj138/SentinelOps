"""A naive rule as a baseline: blame the most-downstream service that alerted, roll it back.

It needs no model and no investigation, so it shows how much of the score comes from the
alert pattern alone. It's scored with exactly the same code as SentinelOps.
"""

import json
import re
from pathlib import Path

from faults import registry
from sentinelops.agent.investigator import InvestigationResult
from sentinelops.agent.models import Claim, RecommendedAction, RootCauseAnalysis
from sentinelops.evals.runner import RunRecord, answer_key, append, completed_runs
from sentinelops.evals.scoring import score

DOWNSTREAM_FIRST = ("payments", "orders", "gateway")
SYSTEM = "naive-rule"


def guess(alerts_fired: list[str]) -> str:
    """The most-downstream service among `HighErrorRate(orders)`-style alert labels."""
    services = {m.group(1) for a in alerts_fired if (m := re.search(r"\((\w+)\)", a))}
    return next(s for s in DOWNSTREAM_FIRST if s in services)


def run(results_path: Path) -> list[RunRecord]:
    """Score the rule on every manifested SentinelOps run (same alerts). Free; no API calls."""
    done = completed_runs(results_path, SYSTEM)
    records = []
    for line in results_path.read_text().splitlines():
        base = json.loads(line)
        if base["system"] != "sentinelops" or not base["manifested"] or base["run_id"] in done:
            continue
        service = guess(base["alerts_fired"])
        rca = RootCauseAnalysis(
            summary="naive rule",
            root_cause_service=service,  # type: ignore[arg-type]
            root_cause=f"the most-downstream alerting service, {service}",
            claims=[Claim(statement="it alerted furthest downstream", evidence=["alerts"])],
            confidence="low",
            recommended_action=RecommendedAction(
                kind="rollback",
                service=service,
                reason="roll back the guessed service",  # type: ignore[arg-type]
            ),
            data_gaps=["no investigation"],
        )
        result = InvestigationResult(status="rca", rca=rca, totals={})
        record = RunRecord(
            run_id=base["run_id"],
            scenario_id=base["scenario_id"],
            repeat=base["repeat"],
            system=SYSTEM,
            started_at=base["started_at"],
            manifested=True,
            alerts_fired=base["alerts_fired"],
            status="rca",
            score=score(answer_key(registry.get(base["scenario_id"])), result),
            root_cause_text=rca.root_cause,
            recommended_action=rca.recommended_action.model_dump(),
        )
        append(results_path, record)
        records.append(record)
    return records
