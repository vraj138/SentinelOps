"""An LLM judge for root-cause text, used identically for SentinelOps and v1.

v1 answers in free text, so its root cause can't be scored deterministically. To keep the
comparison fair, both systems' root-cause text goes through the same judge, which is not told
which system wrote it. Before trusting it, `calibrate` checks it on hand-labelled cases.
"""

import json
from typing import Any, Literal

import anthropic
from pydantic import BaseModel

Verdict = Literal["correct", "partial", "wrong"]

JUDGE_SYSTEM = """\
You grade incident root-cause analyses against an answer key.
- correct: names the same faulty component AND the same triggering change or mechanism as the
  answer key, at the level an on-call engineer would act on. It does not need the answer key's
  level of detail (exact setting names, numbers, or code lines).
- partial: names the right faulty component, but a different or vague mechanism; or names the
  right mechanism while also blaming something unrelated. A right component with a different
  mechanism is always partial, never wrong.
- wrong: blames a different component, only describes symptoms, or gives no cause.
Grade only the root cause. Ignore writing quality, length, remediation advice and confidence."""

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "reason"],
    "properties": {
        "verdict": {"type": "string", "enum": ["correct", "partial", "wrong"]},
        "reason": {"type": "string"},
    },
}


class Judgement(BaseModel):
    verdict: Verdict
    reason: str


class Judge:
    def __init__(self, model: str = "claude-opus-5-5", client: Any | None = None) -> None:
        self.model = model
        self.client = client or anthropic.Anthropic()

    def grade(self, answer_key: str, candidate: str) -> Judgement:
        message = self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=JUDGE_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"Answer key:\n{answer_key}\n\nCandidate root cause:\n{candidate}",
                }
            ],
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": JUDGE_SCHEMA},
            },
        )
        text = "".join(b.text for b in message.content if b.type == "text")
        return Judgement.model_validate(json.loads(text))


def calibrate(
    judge: Judge, cases: list[tuple[str, str, Verdict]]
) -> tuple[list[dict[str, str]], list[str]]:
    """Run (answer key, candidate, expected verdict) cases.

    Returns every case with its verdict (saved next to the results) and the failures.
    """
    graded, failures = [], []
    for key, candidate, expected in cases:
        got = judge.grade(key, candidate).verdict
        graded.append({"candidate": candidate, "expected": expected, "got": got})
        if got != expected:
            failures.append(f"expected {expected}, got {got}: {candidate[:80]!r}")
    return graded, failures
