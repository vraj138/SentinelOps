"""Hand-labelled calibration cases for the judge, one set per fault type.

The judge's grades are only used if it labels all of these as expected. Each set checks that
the judge accepts a correct answer in different wording and in v1's free-text style (so v1
isn't penalized for format), gives partial credit for the right service with the wrong
mechanism, and rejects an unrelated answer.
"""

from sentinelops.evals.judge import Verdict

IRRELEVANT = "High CPU on the database node from a runaway batch job; restart the Kubernetes pods."

# fault type -> list of (candidate root cause, expected verdict)
CASES: dict[str, list[tuple[str, Verdict]]] = {
    "bad_deploy": [
        (
            "The latest payments release turned on fraud screening, and that code looks up a "
            "risk score that doesn't exist, so charges throw KeyError and fail with 500s.",
            "correct",
        ),
        (
            "Likely root cause: a recent change to the payments service enabled a fraud check that "
            "crashes on a missing field. Remediation: roll back the payments deployment.",
            "correct",
        ),
        (
            "The payments service is failing because its database connection pool is exhausted.",
            "partial",
        ),
        (IRRELEVANT, "wrong"),
    ],
    "memory_leak": [
        (
            "Since the last orders release the receipt cache has no size limit, so every order "
            "adds a receipt to memory until orders hits its memory limit and gets OOM-killed.",
            "correct",
        ),
        (
            "Likely root cause: unbounded in-memory caching in the orders service after a config "
            "change, causing out-of-memory restarts. Remediation: revert the orders release.",
            "correct",
        ),
        (
            "The orders service is slow because its Postgres queries are missing an index.",
            "partial",
        ),
        (IRRELEVANT, "wrong"),
    ],
    "db_connection_exhaustion": [
        (
            "An orders release switched the DB pool to a persistent mode that never returns "
            "connections, so orders uses up all Postgres connection slots and new orders fail.",
            "correct",
        ),
        (
            "Likely root cause: connection leak in orders introduced by a pooling change; Postgres "
            "is out of connection slots. Remediation: roll back the orders change.",
            "correct",
        ),
        ("The orders service runs out of memory and is being OOM-killed.", "partial"),
        (IRRELEVANT, "wrong"),
    ],
    "slow_dependency": [
        (
            "The external payment provider is slow, about 1.5 seconds per charge, and every "
            "service upstream waits on it. None of our changes caused it.",
            "correct",
        ),
        (
            "Likely root cause: latency in the third-party payment provider that the payments "
            "service calls. Remediation: escalate to the provider; no rollback needed.",
            "correct",
        ),
        ("A recent orders deploy added slow logging that increased latency.", "wrong"),
        (IRRELEVANT, "wrong"),
    ],
    "bad_feature_flag": [
        (
            "Turning on the checkout_v2_pricing flag broke the gateway's price calculation: prices "
            "become 0, orders rejects them with 422, and gateway returns 502.",
            "correct",
        ),
        (
            "Likely root cause: the new v2 pricing feature flag in the gateway computes invalid "
            "amounts. Remediation: disable the checkout_v2_pricing flag.",
            "correct",
        ),
        ("The gateway is failing because its upstream timeout is too short.", "partial"),
        (IRRELEVANT, "wrong"),
    ],
    "missing_secret": [
        (
            "The latest payments release removed PAYMENTS_API_KEY, so payments refuses to start "
            "and crash-loops; orders then fails on every payment.",
            "correct",
        ),
        (
            "Likely root cause: missing payment provider credentials after the payments deploy "
            "that moved secrets; the service cannot start. Remediation: roll back payments.",
            "correct",
        ),
        ("Payments is crash-looping because it runs out of memory at startup.", "partial"),
        (IRRELEVANT, "wrong"),
    ],
}
