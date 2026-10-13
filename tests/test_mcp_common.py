"""Tests for mcp_servers.common: redaction, size caps, service allowlist."""

import pytest

from mcp_servers.common import ToolError, cap, check_service, redact, to_result


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("PAYMENTS_API_KEY=demo-provider-key", "PAYMENTS_API_KEY=[redacted]"),
        ("-PAYMENTS_API_KEY=abc", "-PAYMENTS_API_KEY=[redacted]"),
        ("db_password: hunter2 ok", "db_password: [redacted] ok"),
        ("SECRET_TOKEN = xyz", "SECRET_TOKEN = [redacted]"),
        (
            "postgresql://orders_app:orders_app@postgres:5432/db",
            "postgresql://orders_app:[redacted]@postgres:5432/db",
        ),
        ("APP_VERSION=1.9.0", "APP_VERSION=1.9.0"),
        ("PAYMENTS_API_KEY is not set", "PAYMENTS_API_KEY is not set"),
    ],
)
def test_redact(text: str, expected: str) -> None:
    assert redact(text) == expected


def test_to_result_redacts_inside_json() -> None:
    assert "hunter2" not in to_result({"line": "PASSWORD=hunter2"})


def test_cap_says_how_much_was_cut() -> None:
    assert cap("x" * 10, limit=4) == "xxxx\n... [truncated 6 characters]"
    assert cap("short") == "short"


def test_check_service_rejects_unknown() -> None:
    assert check_service("orders") == "orders"
    with pytest.raises(ToolError, match="unknown service 'postgres'"):
        check_service("postgres")
