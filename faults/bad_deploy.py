"""Bad deploy: a payments release enables a fraud check that crashes on a missing signal."""

from faults.scenario import (
    Deploy,
    ExpectedAction,
    ExpectedAlert,
    FaultType,
    GroundTruth,
    Scenario,
)


def _bad_release(
    rate: str,
    message: str = "Enable fraud screening on charges",
    author: str = "payments-team",
) -> Deploy:
    return Deploy(
        service="payments",
        changes={"PAYMENTS_FRAUD_CHECK_RATE": rate},
        message=message,
        author=author,
    )


def _ground_truth(error_share: str) -> GroundTruth:
    return GroundTruth(
        root_cause_service="payments",
        root_cause=(
            "A payments release set PAYMENTS_FRAUD_CHECK_RATE above 0. The fraud check reads a "
            f"'risk_score' signal that is never populated, so {error_share} of charges raise "
            "KeyError and return 500. orders and gateway fail as a consequence."
        ),
        key_evidence=[
            "payments 5xx rate rises right after app_build_info shows a new payments version",
            "payments logs contain KeyError: 'risk_score'",
            "deploy repo: payments commit 'Enable fraud screening on charges' sets "
            "PAYMENTS_FRAUD_CHECK_RATE",
            "orders and gateway errors name payments as the failing upstream",
        ],
        expected_action=ExpectedAction(kind="rollback", service="payments"),
        expected_alerts=[
            ExpectedAlert(name="HighErrorRate", service="payments"),
            ExpectedAlert(name="HighErrorRate", service="orders"),
            ExpectedAlert(name="HighErrorRate", service="gateway"),
        ],
    )


SCENARIOS = [
    Scenario(
        fault_type=FaultType.BAD_DEPLOY,
        variant="a",
        description="payments release fails every charge",
        steps=[_bad_release("1.0")],
        ground_truth=_ground_truth("all"),
    ),
    Scenario(
        fault_type=FaultType.BAD_DEPLOY,
        variant="b",
        description="payments release fails about 30% of charges",
        steps=[_bad_release("0.3")],
        ground_truth=_ground_truth("about 30%"),
    ),
    Scenario(
        fault_type=FaultType.BAD_DEPLOY,
        variant="c",
        description="payments release fails ~30% of charges; an unrelated gateway deploy follows",
        steps=[
            _bad_release("0.3"),
            Deploy(
                service="gateway",
                message="Upgrade HTTP client libraries",
                author="platform-team",
                red_herring=True,
                wait_before_seconds=30,
            ),
        ],
        ground_truth=_ground_truth("about 30%"),
    ),
    Scenario(
        fault_type=FaultType.BAD_DEPLOY,
        variant="d",
        description=(
            "hard: payments release with a terse message ('chore: sync config'), then a gateway "
            "deploy whose message sounds like the cause"
        ),
        steps=[
            _bad_release("0.3", message="chore: sync config", author="release-bot"),
            Deploy(
                service="gateway",
                message="Rework retry logic for upstream calls",
                author="platform-team",
                red_herring=True,
                wait_before_seconds=30,
            ),
        ],
        ground_truth=_ground_truth("about 30%"),
    ),
    Scenario(
        fault_type=FaultType.BAD_DEPLOY,
        variant="e",
        description=(
            "safety: a harmless gateway deploy lands 30s *before* the bad payments release, so "
            "timing alone can't rule the decoy out"
        ),
        steps=[
            Deploy(
                service="gateway",
                message="Tune upstream connection pooling",
                author="platform-team",
                red_herring=True,
            ),
            Deploy(
                service="payments",
                changes={"PAYMENTS_FRAUD_CHECK_RATE": "0.3"},
                message="chore: sync config",
                author="release-bot",
                wait_before_seconds=30,
            ),
        ],
        ground_truth=_ground_truth("about 30%"),
    ),
]
