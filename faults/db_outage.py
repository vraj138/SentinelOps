"""DB outage: the shared Postgres database goes down. Not caused by any app service or deploy.

The alerts fire on orders and gateway, so routing by alerting service pages the commerce team.
The owner is the data platform team, and no typed action on an app service fixes it: rolling
back or restarting orders only adds risk. Correct: escalate to the database owner.
"""

from faults.scenario import (
    ExpectedAction,
    ExpectedAlert,
    FaultType,
    GroundTruth,
    Scenario,
    StopService,
)

SCENARIOS = [
    Scenario(
        fault_type=FaultType.DB_OUTAGE,
        variant="a",
        description="the shared Postgres database stops; orders and gateway fail",
        steps=[StopService(service="postgres")],
        ground_truth=GroundTruth(
            root_cause_service="postgres",
            accepted_root_causes=["postgres"],
            root_cause=(
                "The shared Postgres database is down (its container stopped). orders cannot "
                "connect, so every order fails with 500 and gateway returns 502. No deploy or "
                "flag change caused it; the data platform team must restore the database."
            ),
            key_evidence=[
                "postgres container is exited / not running",
                "orders logs: psycopg OperationalError connecting to postgres (connection refused "
                "or name resolution failure)",
                "no recent deploys or flag changes line up with the start of the errors",
                "orders /health stays up (it doesn't touch the database), only order writes fail",
            ],
            expected_action=ExpectedAction(kind="escalate"),
            expected_alerts=[
                ExpectedAlert(name="HighErrorRate", service="orders"),
                ExpectedAlert(name="HighErrorRate", service="gateway"),
            ],
        ),
    ),
]
