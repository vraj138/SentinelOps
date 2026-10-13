"""Checks that every registered scenario is well formed and only uses real settings."""

import re
from pathlib import Path

import pytest

from demo_app.common import DemoSettings
from faults import registry
from faults.scenario import Deploy, ExpectedAction, FaultType, GroundTruth, Scenario
from sentinelops.stack.deploy_repo import BASE_RELEASES

ALERTS_FILE = Path(__file__).resolve().parent.parent / "infra" / "prometheus" / "alerts.yml"
DEFINED_ALERTS = set(re.findall(r"- alert: (\w+)", ALERTS_FILE.read_text()))

KNOWN_SETTINGS = {name.upper() for name in DemoSettings.model_fields}
KNOWN_FLAGS = {"checkout_v2_pricing"}


def test_scenario_ids_are_unique() -> None:
    assert len(registry.SCENARIOS) == len(registry.ALL_SCENARIOS)


def test_every_fault_type_has_a_scenario() -> None:
    assert {s.fault_type for s in registry.ALL_SCENARIOS} == set(FaultType)


@pytest.mark.parametrize("scenario", registry.ALL_SCENARIOS, ids=lambda s: s.id)
def test_expected_alerts_exist_in_prometheus_rules(scenario: Scenario) -> None:
    for alert in scenario.ground_truth.expected_alerts:
        assert alert.name in DEFINED_ALERTS, f"{scenario.id}: no alert rule named {alert.name}"


@pytest.mark.parametrize("scenario", registry.ALL_SCENARIOS, ids=lambda s: s.id)
def test_deploys_only_change_settings_the_services_read(scenario: Scenario) -> None:
    for step in scenario.steps:
        if isinstance(step, Deploy):
            assert set(step.changes) <= KNOWN_SETTINGS, f"typo in {scenario.id}: {step.changes}"
            assert step.service in BASE_RELEASES


@pytest.mark.parametrize("scenario", registry.ALL_SCENARIOS, ids=lambda s: s.id)
def test_flags_and_expected_actions_are_valid(scenario: Scenario) -> None:
    truth = scenario.ground_truth
    for action in [truth.expected_action, *truth.acceptable_actions]:
        if action.kind == "set_flag":
            assert action.flag in KNOWN_FLAGS
            assert action.flag_value is not None
        elif action.kind != "escalate":
            assert action.service is not None
    assert truth.expected_action not in truth.acceptable_actions
    assert scenario.ground_truth.expected_alerts
    assert scenario.ground_truth.key_evidence


def test_get_unknown_scenario_lists_valid_ids() -> None:
    with pytest.raises(KeyError, match="bad_deploy/a"):
        registry.get("nope/z")


def test_red_herring_cannot_touch_the_faulty_service() -> None:
    with pytest.raises(ValueError, match="red herring must not touch the faulty service"):
        Scenario(
            fault_type=FaultType.BAD_DEPLOY,
            variant="x",
            description="invalid",
            steps=[
                Deploy(service="payments", message="real fault", author="t"),
                Deploy(service="payments", message="herring", author="t", red_herring=True),
            ],
            ground_truth=GroundTruth(
                root_cause_service="payments",
                root_cause="x",
                key_evidence=["x"],
                expected_action=ExpectedAction(kind="rollback", service="payments"),
                expected_alerts=[],
            ),
        )
