"""Root-cause service extraction: the same accuracy metric for every system.

v1 answers in free text, SentinelOps in structured JSON. To compare them with one metric, an LLM
only *extracts* what each answer's root-cause text claims: which single service it blames, and
whether it hedges between alternative causes. Scoring is then deterministic and identical for
all systems. SentinelOps' text is extracted too, as a cross-check against its structured answer.
"""

import json
from typing import Any, Literal

from pydantic import BaseModel

Blamed = Literal["gateway", "orders", "payments", "external", "database", "unknown"]

EXTRACT_SYSTEM = """\
You read one incident root-cause statement and report what it claims. Do not judge whether it
is right.
blamed: the single service it identifies as the source of the problem.
- gateway, orders or payments: that service (its code, its config, its deploy, or its feature
  flag).
- external: a third-party dependency outside the system (for example a payment provider).
- database: the database itself (connections, queries, the DB server) without naming which
  service caused it.
- unknown: no cause, only symptoms, or only a vague guess.
hedged: true if it offers two or more alternative causes ("X or Y", "either", "possibly X or
possibly Y"). A single cause with supporting detail is not hedged.
If it hedges, set blamed to the first cause named."""

EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["blamed", "hedged"],
    "properties": {
        "blamed": {
            "type": "string",
            "enum": ["gateway", "orders", "payments", "external", "database", "unknown"],
        },
        "hedged": {"type": "boolean"},
    },
}


class Extraction(BaseModel):
    blamed: Blamed
    hedged: bool


class Extractor:
    def __init__(self, client: Any, model: str = "claude-opus-5-5") -> None:
        self.client = client
        self.model = model

    def extract(self, root_cause_text: str) -> Extraction:
        message = self.client.messages.create(
            model=self.model,
            max_tokens=1000,
            system=EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": f"Root-cause statement:\n{root_cause_text}"}],
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": EXTRACT_SCHEMA},
            },
        )
        text = "".join(b.text for b in message.content if b.type == "text")
        return Extraction.model_validate(json.loads(text))


def correct(extraction: Extraction, accepted: list[str], allow_hedge: bool = False) -> bool:
    """The same rule as SentinelOps' structured score: an accepted root cause, single cause.

    "database" counts as `postgres`. `allow_hedge` is the lenient variant: a hedged answer
    counts if the first cause it names is accepted. Both are reported so a strict rule can't
    hide a near miss.
    """
    if extraction.hedged and not allow_hedge:
        return False
    blamed = "postgres" if extraction.blamed == "database" else extraction.blamed
    return blamed in accepted


# Hand-labelled cases the extractor must get right before its output is used.
CALIBRATION: list[tuple[str, Blamed, bool]] = [
    (
        "A payments release enabled a fraud check that crashes on a missing field.",
        "payments",
        False,
    ),
    (
        "The orders release removed the receipt cache limit, so orders leaks memory.",
        "orders",
        False,
    ),
    (
        "Turning on the checkout_v2_pricing flag broke the gateway's price calculation.",
        "gateway",
        False,
    ),
    (
        "The external payment provider slowed down; nothing we deployed caused it.",
        "external",
        False,
    ),
    ("Exhausted database connections leading to 5xx errors.", "database", False),
    (
        "Potential database connection issues or a recent deploy causing high error rates.",
        "database",
        True,
    ),
    (
        "Potential memory leak in the orders service or DB connections not being released.",
        "orders",
        True,
    ),
    ("High error rates on gateway, orders and payments.", "unknown", False),
    (
        "An orders release leaks one Postgres connection per request until the DB is full.",
        "orders",
        False,
    ),
    ("Either the gateway timeout is too short or payments is overloaded.", "gateway", True),
]
