"""The service catalog: products, their owning teams, services and dependencies.

Loaded from `catalog.toml` and validated, so a typo can't silently route incidents to nobody.
"""

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from sentinelops.config import ROOT

DEFAULT_PATH = ROOT / "catalog.toml"


class Product(BaseModel):
    id: str
    name: str
    team: str
    description: str = ""
    services: list[str]
    depends_on: list[str] = Field(default_factory=list)  # services this product calls
    external_dependencies: list[str] = Field(default_factory=list)
    infrastructure: bool = (
        False  # shared infrastructure: visible to tools, never a remediation target
    )


class Catalog(BaseModel):
    products: list[Product]

    @model_validator(mode="after")
    def _check(self) -> "Catalog":
        owners: dict[str, str] = {}
        for product in self.products:
            for service in product.services:
                if service in owners:
                    raise ValueError(
                        f"service {service!r} is in {owners[service]!r} and {product.id!r}"
                    )
                owners[service] = product.id
        for product in self.products:
            for dep in product.depends_on:
                if dep not in owners:
                    raise ValueError(f"{product.id!r} depends on unknown service {dep!r}")
        ids = [p.id for p in self.products]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate product ids")
        return self

    def product(self, product_id: str) -> Product:
        return next(p for p in self.products if p.id == product_id)

    def owner_of(self, service: str) -> Product | None:
        """The product a service belongs to. 'external' maps to the product that declares an
        external dependency (it owns that relationship); unknown services map to None."""
        if service == "external":
            return next((p for p in self.products if p.external_dependencies), None)
        return next((p for p in self.products if service in p.services), None)

    @property
    def services(self) -> list[str]:
        return [s for p in self.products for s in p.services]

    @property
    def app_services(self) -> list[str]:
        """Services the typed remediation actions may target (not shared infrastructure)."""
        return [s for p in self.products if not p.infrastructure for s in p.services]

    def dependency_group(self, service: str) -> frozenset[str]:
        """Every service connected to `service` through dependencies, in either direction.

        Alerts on services in one group can be symptoms of one failure, so the alert intake
        groups them into one incident. A service the catalog doesn't know is its own group.
        """
        links: dict[str, set[str]] = {s: set() for s in self.services}
        for product in self.products:
            for s in product.services:
                for other in [*product.services, *product.depends_on]:
                    if other != s and other in links:
                        links[s].add(other)
                        links[other].add(s)
        if service not in links:
            return frozenset({service})
        group, frontier = {service}, [service]
        while frontier:
            for neighbour in links[frontier.pop()] - group:
                group.add(neighbour)
                frontier.append(neighbour)
        return frozenset(group)

    def downstream_first(self) -> list[str]:
        """Services ordered so each comes before the services that depend on it."""
        ordered: list[str] = []
        remaining = set(self.services)
        while remaining:
            leaves = sorted(s for s in remaining if not (self._deps(s) & remaining))
            if not leaves:
                raise ValueError("dependency cycle in catalog")
            ordered += leaves
            remaining -= set(leaves)
        return ordered

    def _deps(self, service: str) -> set[str]:
        product = self.owner_of(service)
        return set(product.depends_on) if product else set()


def load(path: Path = DEFAULT_PATH) -> Catalog:
    return Catalog.model_validate(tomllib.loads(path.read_text()))
