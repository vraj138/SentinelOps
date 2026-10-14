"""Missing secret: a payments release drops its API key, so payments crash-loops on startup."""

from faults.scenario import (
    Deploy,
    ExpectedAction,
    ExpectedAlert,
    FaultType,
    GroundTruth,
    Scenario,
)

SCENARIOS = [
    Scenario(
        fault_type=FaultType.MISSING_SECRET,
        variant="a",
        description="payments release removes PAYMENTS_API_KEY; payments crash-loops",
        steps=[
            Deploy(
                service="payments",
                changes={"PAYMENTS_API_KEY": None},
                message="Move provider credentials to the secrets manager",
                author="payments-team",
            )
        ],
        ground_truth=GroundTruth(
            root_cause_service="payments",
            root_cause=(
                "A payments release removed PAYMENTS_API_KEY (the commit says it moved to a "
                "secrets manager, but nothing provides it). Payments refuses to start, so it "
                "crash-loops, and every order fails at the payment step."
            ),
            key_evidence=[
                "payments target is down (up == 0) and the container keeps restarting",
                "payments logs: 'PAYMENTS_API_KEY is not set' at startup",
                "deploy repo: payments commit removes PAYMENTS_API_KEY",
                "orders logs: payments connection refused",
            ],
            expected_action=ExpectedAction(kind="rollback", service="payments"),
            expected_alerts=[
                ExpectedAlert(name="ServiceDown", service="payments"),
                ExpectedAlert(name="HighErrorRate", service="orders"),
                ExpectedAlert(name="HighErrorRate", service="gateway"),
            ],
        ),
    ),
]

SCENARIOS.append(
    Scenario(
        fault_type=FaultType.MISSING_SECRET,
        variant="b",
        description=(
            "hard: the release that drops PAYMENTS_API_KEY is labelled 'Routine dependency bumps'"
        ),
        steps=[
            Deploy(
                service="payments",
                changes={"PAYMENTS_API_KEY": None},
                message="Routine dependency bumps",
                author="release-bot",
            )
        ],
        ground_truth=SCENARIOS[0].ground_truth,
    )
)
