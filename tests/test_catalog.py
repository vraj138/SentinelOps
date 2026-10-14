"""Tests for the service catalog, routing, and that hard-coded service lists match it."""

import pytest
from pydantic import ValidationError

from sentinelops import routing
from sentinelops.catalog import Catalog, load


def test_catalog_loads_and_orders_downstream_first() -> None:
    catalog = load()
    assert catalog.downstream_first() == ["payments", "postgres", "orders", "gateway"]
    assert catalog.owner_of("postgres").team == "data-platform-team"  # type: ignore[union-attr]
    assert "postgres" not in catalog.app_services
    assert catalog.owner_of("gateway").team == "edge-team"  # type: ignore[union-attr]
    assert catalog.owner_of("external").id == "payments"  # type: ignore[union-attr]
    assert catalog.owner_of("database") is None


def test_every_hard_coded_service_list_matches_the_catalog() -> None:
    from mcp_servers.common import SERVICES as TOOL_SERVICES
    from sentinelops.agent.models import SERVICES as AGENT_SERVICES
    from sentinelops.controlplane.actions import SERVICES as ACTION_SERVICES
    from sentinelops.evals.cleanslate import DEMO_SERVICES as CLEAN_SERVICES
    from sentinelops.stack.runtime import DEMO_SERVICES as RUNTIME_SERVICES

    catalog = load()
    # Read-only tools may inspect every catalog service, including shared infrastructure.
    assert set(TOOL_SERVICES) == set(catalog.services)
    # Remediation, the agent's action targets and the demo runtime cover app services only.
    for name, services in {
        "agent.models": AGENT_SERVICES,
        "controlplane.actions": ACTION_SERVICES,
        "evals.cleanslate": CLEAN_SERVICES,
        "stack.runtime": RUNTIME_SERVICES,
    }.items():
        assert set(services) == set(catalog.app_services), name


def test_catalog_rejects_a_service_in_two_products() -> None:
    with pytest.raises(ValidationError, match="is in"):
        Catalog.model_validate(
            {
                "products": [
                    {"id": "a", "name": "A", "team": "t", "services": ["x"]},
                    {"id": "b", "name": "B", "team": "t", "services": ["x"]},
                ]
            }
        )


def test_catalog_rejects_unknown_dependency() -> None:
    with pytest.raises(ValidationError, match="unknown service"):
        Catalog.model_validate(
            {
                "products": [
                    {"id": "a", "name": "A", "team": "t", "services": ["x"], "depends_on": ["y"]},
                ]
            }
        )


def test_routing_from_alerts_and_from_root_cause() -> None:
    catalog = load()
    alerts = ["gateway", "orders"]
    assert routing.impacted(catalog, alerts) == ["edge", "orders"]
    assert routing.owner_from_alerts(catalog, alerts).id == "orders"  # type: ignore[union-attr]
    assert routing.owner_from_root_cause(catalog, "external").id == "payments"  # type: ignore[union-attr]
    assert routing.owner_from_root_cause(catalog, "unknown") is None
