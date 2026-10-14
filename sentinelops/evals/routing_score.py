"""'Correct team on the first page': which team each system would page, scored against the
answer key's owning team. Free: derived from answers already recorded.

- sentinelops and naive-agent: the product owning the diagnosed root-cause service.
- naive-rule: the owner of the most-downstream alerting service (today's alert-owner routing).
- v1: the product owning the service its answer blames; "database" pages the database's owner,
  "unknown" pages no team.
"""

import json
from pathlib import Path

from faults import registry
from sentinelops.catalog import Catalog


def paged_service(record: dict, recordings_dir: Path) -> str | None:
    """The service whose owner this system would page."""
    system = record["system"]
    if system in ("sentinelops", "naive-agent"):
        suffix = "-naive" if system == "naive-agent" else ""
        result = recordings_dir / f"{record['run_id']}{suffix}" / "result.json"
        rca = json.loads(result.read_text()).get("rca") if result.exists() else None
        return rca["root_cause_service"] if rca else None
    if system == "naive-rule":
        return (record.get("recommended_action") or {}).get("service")
    extracted = record.get("extracted")
    if not extracted:
        return None
    return "postgres" if extracted["blamed"] == "database" else extracted["blamed"]


def score(results_path: Path, recordings_dir: Path, catalog: Catalog) -> None:
    rows = [json.loads(line) for line in results_path.read_text().splitlines() if line]
    for r in rows:
        if not r.get("manifested") or r.get("status") == "error":
            continue
        truth = registry.get(r["scenario_id"]).ground_truth.root_cause_service
        expected = catalog.owner_of(truth)
        service = paged_service(r, recordings_dir)
        paged = catalog.owner_of(service) if service else None
        r["paged_team"] = paged.team if paged else None
        r["routing_correct"] = bool(paged and expected and paged.id == expected.id)
    results_path.write_text("".join(json.dumps(r) + "\n" for r in rows))
