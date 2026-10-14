"""DB connection exhaustion: an orders release leaks connections until Postgres is full."""

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
        fault_type=FaultType.DB_CONNECTION_EXHAUSTION,
        variant="a",
        description="orders release leaks a Postgres connection per request",
        steps=[
            Deploy(
                service="orders",
                changes={"ORDERS_DB_POOL_MODE": "persistent"},
                message="Reuse database connections to cut connect latency",
                author="orders-team",
            )
        ],
        ground_truth=GroundTruth(
            root_cause_service="orders",
            root_cause=(
                "An orders release set ORDERS_DB_POOL_MODE=persistent. Connections are kept "
                "'for reuse' but never reused or closed, so orders opens one per request until "
                "Postgres refuses new clients. Every new order then fails."
            ),
            key_evidence=[
                "orders db_connections_open climbs after the deploy and stops near the limit",
                "orders logs: psycopg OperationalError 'remaining connection slots are reserved "
                "for roles with the SUPERUSER attribute'",
                "pg_stat_activity: almost every connection belongs to orders_app and is idle",
                "deploy repo: orders commit sets ORDERS_DB_POOL_MODE=persistent",
            ],
            expected_action=ExpectedAction(kind="rollback", service="orders"),
            acceptable_actions=[ExpectedAction(kind="restart", service="orders")],
            expected_alerts=[
                ExpectedAlert(name="HighErrorRate", service="orders"),
                ExpectedAlert(name="HighErrorRate", service="gateway"),
            ],
        ),
    ),
]

SCENARIOS.append(
    Scenario(
        fault_type=FaultType.DB_CONNECTION_EXHAUSTION,
        variant="b",
        description="hard: the connection-leaking orders release is labelled 'perf tuning'",
        steps=[
            Deploy(
                service="orders",
                changes={"ORDERS_DB_POOL_MODE": "persistent"},
                message="perf tuning",
                author="release-bot",
            )
        ],
        ground_truth=SCENARIOS[0].ground_truth,
    )
)
