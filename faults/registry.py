"""All fault scenarios, looked up by id (`<fault_type>/<variant>`)."""

from faults import (
    bad_deploy,
    bad_feature_flag,
    db_connection_exhaustion,
    memory_leak,
    missing_secret,
    slow_dependency,
)
from faults.scenario import Scenario

ALL_SCENARIOS: list[Scenario] = [
    *bad_deploy.SCENARIOS,
    *memory_leak.SCENARIOS,
    *db_connection_exhaustion.SCENARIOS,
    *slow_dependency.SCENARIOS,
    *bad_feature_flag.SCENARIOS,
    *missing_secret.SCENARIOS,
]
SCENARIOS: dict[str, Scenario] = {scenario.id: scenario for scenario in ALL_SCENARIOS}


def get(scenario_id: str) -> Scenario:
    """Return the scenario with this id, or raise KeyError listing the valid ids."""
    try:
        return SCENARIOS[scenario_id]
    except KeyError:
        raise KeyError(
            f"unknown scenario {scenario_id!r}; choose one of {sorted(SCENARIOS)}"
        ) from None
