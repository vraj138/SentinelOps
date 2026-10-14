"""Memory leak: an orders release removes the receipt cache limit, so orders is OOM-killed."""

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
        fault_type=FaultType.MEMORY_LEAK,
        variant="a",
        description="orders release caches every receipt; memory grows until OOM kill",
        steps=[
            Deploy(
                service="orders",
                changes={"ORDERS_RECEIPT_CACHE_MAX_ITEMS": "0"},
                message="Keep all receipts in memory for faster re-downloads",
                author="orders-team",
            )
        ],
        ground_truth=GroundTruth(
            root_cause_service="orders",
            root_cause=(
                "An orders release set ORDERS_RECEIPT_CACHE_MAX_ITEMS=0, which means unlimited. "
                "Every order adds a 128 KB receipt to memory, so orders grows until it hits its "
                "128 MB limit, is OOM-killed, restarts, and leaks again."
            ),
            key_evidence=[
                "orders process_resident_memory_bytes climbs steadily after the deploy",
                "orders receipt_cache_items keeps growing instead of staying at 50",
                "Docker events for orders: oom, then die with exit code 137, then start; "
                "RestartCount goes up",
                "deploy repo: orders commit sets ORDERS_RECEIPT_CACHE_MAX_ITEMS=0",
            ],
            expected_action=ExpectedAction(kind="rollback", service="orders"),
            acceptable_actions=[ExpectedAction(kind="restart", service="orders")],
            # FrequentRestarts(orders) should also fire once orders has started 3+ times in 15 min.
            expected_alerts=[ExpectedAlert(name="HighMemoryUsage", service="orders")],
        ),
    ),
]

SCENARIOS.append(
    Scenario(
        fault_type=FaultType.MEMORY_LEAK,
        variant="b",
        description=(
            "hard: orders release with a terse message, then a payments deploy whose message "
            "sounds like a memory change"
        ),
        steps=[
            Deploy(
                service="orders",
                changes={"ORDERS_RECEIPT_CACHE_MAX_ITEMS": "0"},
                message="update orders config",
                author="release-bot",
            ),
            Deploy(
                service="payments",
                message="Increase worker memory buffers",
                author="payments-team",
                red_herring=True,
                wait_before_seconds=30,
            ),
        ],
        ground_truth=SCENARIOS[0].ground_truth,
    )
)
