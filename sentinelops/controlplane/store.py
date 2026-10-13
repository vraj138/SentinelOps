"""Incident storage. Every state change is a compare-and-set: it only happens if the incident
is still in the expected state, so an approval can't be executed twice and two workers can't
race each other through the state machine."""

from datetime import UTC, datetime
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb

from sentinelops.controlplane.models import TRANSITIONS, Incident, State


class StaleTransition(RuntimeError):
    """The incident was not in the expected state (someone else moved it, or it's invalid)."""


class IncidentStore(Protocol):
    def create(self, incident: Incident) -> Incident: ...
    def get(self, incident_id: str) -> Incident: ...
    def list(self, limit: int = 50) -> list[Incident]: ...
    def transition(
        self, incident_id: str, expected: State, new: State, **updates: Any
    ) -> Incident: ...


def apply_transition(
    incident: Incident, expected: State, new: State, updates: dict[str, Any]
) -> Incident:
    """Validate and apply a transition to an in-memory copy of the incident."""
    if incident.state != expected:
        raise StaleTransition(
            f"incident {incident.id} is {incident.state}, expected {expected} for -> {new}"
        )
    if new not in TRANSITIONS[expected]:
        raise StaleTransition(f"transition {expected} -> {new} is not allowed")
    now = datetime.now(UTC)
    entry = {"at": now.isoformat(), "from": expected, "to": new}
    return incident.model_copy(
        update={
            **updates,
            "state": new,
            "updated_at": now,
            "history": [*incident.history, entry],
        }
    )


class InMemoryStore:
    """For tests."""

    def __init__(self) -> None:
        self.incidents: dict[str, Incident] = {}

    def create(self, incident: Incident) -> Incident:
        self.incidents[incident.id] = incident
        return incident

    def get(self, incident_id: str) -> Incident:
        if incident_id not in self.incidents:
            raise KeyError(f"no incident {incident_id!r}")
        return self.incidents[incident_id]

    def list(self, limit: int = 50) -> list[Incident]:
        return sorted(self.incidents.values(), key=lambda i: i.created_at, reverse=True)[:limit]

    def transition(self, incident_id: str, expected: State, new: State, **updates: Any) -> Incident:
        updated = apply_transition(self.get(incident_id), expected, new, updates)
        self.incidents[incident_id] = updated
        return updated


SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id         TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    data       JSONB NOT NULL
)
"""


class PostgresStore:
    """Incidents in the control plane's own Postgres (not the demo database)."""

    def __init__(self, url: str) -> None:
        self.url = url
        with self._connect() as conn:
            conn.execute(SCHEMA)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.url, connect_timeout=5)

    def create(self, incident: Incident) -> Incident:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO incidents (id, state, created_at, updated_at, data) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    incident.id,
                    incident.state,
                    incident.created_at,
                    incident.updated_at,
                    Jsonb(incident.model_dump(mode="json")),
                ),
            )
        return incident

    def get(self, incident_id: str) -> Incident:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM incidents WHERE id = %s", (incident_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"no incident {incident_id!r}")
        return Incident.model_validate(row[0])

    def list(self, limit: int = 50) -> list[Incident]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM incidents ORDER BY created_at DESC LIMIT %s", (limit,)
            ).fetchall()
        return [Incident.model_validate(r[0]) for r in rows]

    def transition(self, incident_id: str, expected: State, new: State, **updates: Any) -> Incident:
        updated = apply_transition(self.get(incident_id), expected, new, updates)
        with self._connect() as conn:
            row = conn.execute(
                "UPDATE incidents SET state = %s, updated_at = %s, data = %s "
                "WHERE id = %s AND state = %s RETURNING id",
                (
                    new,
                    updated.updated_at,
                    Jsonb(updated.model_dump(mode="json")),
                    incident_id,
                    expected,
                ),
            ).fetchone()
        if row is None:
            raise StaleTransition(f"incident {incident_id} changed state concurrently")
        return updated
