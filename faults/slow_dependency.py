"""Slow dependency: the external payment provider slows down. Nothing we deployed is at fault."""

from faults.scenario import (
    Deploy,
    ExpectedAction,
    ExpectedAlert,
    FaultType,
    GroundTruth,
    Scenario,
    SetProviderLatency,
)

# 1.5s per charge: over the 1s p95 alert threshold, under the 3s client timeouts,
# so the chain gets slow but does not fail.
_SLOWDOWN = SetProviderLatency(latency_ms=1500)

_GROUND_TRUTH = GroundTruth(
    root_cause_service="payments",
    root_cause=(
        "The external payment provider slowed down to about 1.5s per charge. Payments, orders "
        "and gateway all wait on it, so p95 latency rises across the chain. No deploy or flag "
        "change caused it, and nothing we control can fix it: escalate to the provider."
    ),
    key_evidence=[
        "p95 latency rises to the same 1.5-2s histogram bucket on payments, orders and gateway "
        "at the same moment: each waits on the next, so they look identical",
        "payments logs: 'payment provider slow: charge for order ... took 1.51s', which "
        "accounts for almost all of the chain's latency",
        "no recent payments deploy or flag change lines up with the start of the slowdown",
        "error rates stay low: requests are slow, not failing",
    ],
    expected_action=ExpectedAction(kind="escalate"),
    expected_alerts=[
        ExpectedAlert(name="HighLatencyP95", service="payments"),
        ExpectedAlert(name="HighLatencyP95", service="orders"),
        ExpectedAlert(name="HighLatencyP95", service="gateway"),
    ],
)

SCENARIOS = [
    Scenario(
        fault_type=FaultType.SLOW_DEPENDENCY,
        variant="a",
        description="payment provider slows to 1.5s per charge",
        steps=[_SLOWDOWN],
        ground_truth=_GROUND_TRUTH,
    ),
    Scenario(
        fault_type=FaultType.SLOW_DEPENDENCY,
        variant="b",
        description="an unrelated orders deploy, then the payment provider slows down 30s later",
        steps=[
            Deploy(
                service="orders",
                message="Add order source to logs",
                author="orders-team",
                red_herring=True,
            ),
            _SLOWDOWN.model_copy(update={"wait_before_seconds": 30}),
        ],
        ground_truth=_GROUND_TRUTH,
    ),
]
