"""Who gets paged: routing an incident to the owning product's team.

Before any investigation, routing can only use the alerts (the industry default: page the
owner of the service that alerted, here the most-downstream one). After the investigation,
the owner is the product of the root-cause service. Products the failure cascades into are
"impacted" and are told why it isn't theirs.
"""

from sentinelops.catalog import Catalog, Product


def impacted(catalog: Catalog, alerting_services: list[str]) -> list[str]:
    """Product ids whose services are alerting, in catalog order."""
    ids = {p.id for s in alerting_services if (p := catalog.owner_of(s))}
    return [p.id for p in catalog.products if p.id in ids]


def owner_from_alerts(catalog: Catalog, alerting_services: list[str]) -> Product | None:
    """The most-downstream alerting service's product: a guess, not a diagnosis."""
    for service in catalog.downstream_first():
        if service in alerting_services:
            return catalog.owner_of(service)
    return None


def owner_from_root_cause(catalog: Catalog, root_cause_service: str) -> Product | None:
    """The product that owns the diagnosed root cause. 'external' -> the product that owns
    that external dependency; 'unknown' -> None (keep the alert-based owner)."""
    return catalog.owner_of(root_cause_service)
