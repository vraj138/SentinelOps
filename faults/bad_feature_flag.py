"""Bad feature flag: turning on v2 pricing in gateway zeroes every price."""

from faults.scenario import (
    Deploy,
    ExpectedAction,
    ExpectedAlert,
    FaultType,
    FlipFlag,
    GroundTruth,
    Scenario,
)

FLAG = "checkout_v2_pricing"

_FLIP = FlipFlag(name=FLAG, value=True, updated_by="growth-team")

_GROUND_TRUTH = GroundTruth(
    root_cause_service="gateway",
    root_cause=(
        f"The {FLAG} flag was turned on. Its pricing code applies the discount as a whole "
        "number instead of a percentage, so every price becomes 0, orders rejects the order "
        "with 422, and gateway returns 502."
    ),
    key_evidence=[
        f"gateway logs: feature flag {FLAG} changed False -> True (updated_by=growth-team)",
        "gateway 5xx rate rises right after the flag change, with no gateway deploy",
        "gateway logs: orders returned 422 'Input should be greater than 0' for amount_cents",
        "orders returns 4xx, not 5xx; payments is healthy",
    ],
    expected_action=ExpectedAction(kind="set_flag", flag=FLAG, flag_value=False),
    expected_alerts=[ExpectedAlert(name="HighErrorRate", service="gateway")],
)

SCENARIOS = [
    Scenario(
        fault_type=FaultType.BAD_FEATURE_FLAG,
        variant="a",
        description=f"{FLAG} turned on in gateway",
        steps=[_FLIP],
        ground_truth=_GROUND_TRUTH,
    ),
    Scenario(
        fault_type=FaultType.BAD_FEATURE_FLAG,
        variant="b",
        description=f"an unrelated orders deploy, then {FLAG} turned on 30s later",
        steps=[
            Deploy(
                service="orders",
                message="Tune receipt rendering",
                author="orders-team",
                red_herring=True,
            ),
            _FLIP.model_copy(update={"wait_before_seconds": 30}),
        ],
        ground_truth=_GROUND_TRUTH,
    ),
]
