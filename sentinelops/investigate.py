"""Command line: investigate the currently firing alerts. `python -m sentinelops.investigate`.

Writes everything to storage/runs/<run id>/: `audit.jsonl` (every LLM and tool call) and
`result.json` (the RCA or the escalation).
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

import anthropic
import openai

from sentinelops.agent.anthropic_llm import AnthropicLLM
from sentinelops.agent.audit import AuditLog
from sentinelops.agent.investigator import Budget, InvestigationResult, Investigator
from sentinelops.agent.llm import LLM, FallbackLLM, LLMUnavailable
from sentinelops.agent.openai_llm import OpenAILLM
from sentinelops.agent.policy import PolicyGate
from sentinelops.agent.prompts import SYSTEM_PROMPT
from sentinelops.agent.toolhub import ToolHub, stdio_servers
from sentinelops.config import Settings
from sentinelops.controlplane import build_control_plane
from sentinelops.controlplane.alerts import firing_alerts
from sentinelops.controlplane.models import State

CREDENTIAL_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
)


def has_anthropic_credentials(settings: Settings) -> bool:
    """True if any credential source the Anthropic SDK reads is present."""
    profile_dir = Path.home() / ".config" / "anthropic"
    return (
        settings.anthropic_api_key is not None
        or any(os.environ.get(name) for name in CREDENTIAL_ENV_VARS)
        or profile_dir.is_dir()
    )


def build_llm(settings: Settings, audit: AuditLog) -> FallbackLLM:
    key = settings.anthropic_api_key
    client = anthropic.AsyncAnthropic(api_key=key.get_secret_value()) if key else None
    primary = AnthropicLLM(settings.anthropic_model, settings.agent_effort, client=client)
    secondary = None
    if settings.openai_api_key and settings.openai_model:
        openai_client = openai.AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())
        secondary = OpenAILLM(settings.openai_model, client=openai_client)
    return FallbackLLM(primary, secondary, audit)


async def investigate(
    settings: Settings,
    context: str,
    run_id: str,
    llm_factory: Callable[[AuditLog], LLM] | None = None,
    system_prompt: str = SYSTEM_PROMPT,
    strict: bool = True,
) -> InvestigationResult:
    """Run the agent; everything it does is written to <runs_dir>/<run_id>/audit.jsonl.

    The tool specs are saved next to it (tools.json) so the run can be replayed later.
    """
    run_dir = settings.runs_dir / run_id
    audit = AuditLog(run_dir / "audit.jsonl")
    budget = Budget(
        max_tool_calls=settings.agent_max_tool_calls,
        max_tokens=settings.agent_max_tokens,
        max_cost_usd=settings.agent_max_cost_usd,
        max_seconds=settings.agent_max_seconds,
    )
    llm = llm_factory(audit) if llm_factory else build_llm(settings, audit)
    try:
        async with ToolHub(stdio_servers()) as hub:
            specs = [spec.model_dump() for spec in hub.specs.values()]
            (run_dir / "tools.json").write_text(json.dumps(specs, indent=2))
            investigator = Investigator(
                llm, hub, audit, PolicyGate(), budget, system_prompt=system_prompt, strict=strict
            )
            result = await investigator.run(context)
    finally:
        close = getattr(llm, "aclose", None)
        if close is not None:  # close HTTP clients before asyncio.run closes the event loop
            await close()
    (run_dir / "result.json").write_text(result.model_dump_json(indent=2))
    return result


def print_result(result: InvestigationResult, run_dir: str) -> None:
    totals = result.totals
    cost = f"${totals['cost_usd']:.4f}"
    if totals["unpriced_llm_calls"]:
        cost = f"at least {cost} ({totals['unpriced_llm_calls']} calls on an unpriced model)"
    print(f"\nstatus: {result.status}")
    if result.rca:
        rca = result.rca
        action = rca.recommended_action
        print(
            f"root cause ({rca.confidence} confidence, {rca.root_cause_service}): {rca.root_cause}"
        )
        for claim in rca.claims:
            print(f"  - {claim.statement} [{', '.join(claim.evidence)}]")
        target = action.service or (f"{action.flag}={action.flag_value}" if action.flag else "")
        print(f"recommended: {action.kind} {target} - {action.reason}")
        for gap in rca.data_gaps:
            print(f"  data gap: {gap}")
    else:
        print(f"escalated: {result.escalation_reason}")
    print(
        f"{totals['llm_calls']} LLM calls, {totals['tool_calls']} tool calls, "
        f"{totals['tokens']} tokens, cost {cost}"
    )
    print(f"audit log: {run_dir}/audit.jsonl")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m sentinelops.investigate")
    parser.add_argument("--alert", help="describe the alert yourself instead of asking Prometheus")
    args = parser.parse_args()
    settings = Settings()
    if not has_anthropic_credentials(settings):
        sys.exit(
            "error: no Anthropic credentials found. Set ANTHROPIC_API_KEY in .env "
            "(see .env.example) or run `ant auth login`."
        )
    context, alerts = firing_alerts(settings.prometheus_url)
    context = args.alert or context
    print(context)
    plane = build_control_plane(settings)
    incident = plane.open(context, alerts)
    plane.start_investigation(incident.id)
    try:
        result = asyncio.run(investigate(settings, context, incident.id))
    except anthropic.AuthenticationError:
        plane.escalate(incident.id, State.INVESTIGATING, "no valid Anthropic credentials")
        sys.exit(
            "error: no valid Anthropic credentials. Set ANTHROPIC_API_KEY in .env "
            "(see .env.example) or run `ant auth login`."
        )
    except LLMUnavailable as exc:
        plane.escalate(incident.id, State.INVESTIGATING, f"no model provider available: {exc}")
        sys.exit(f"error: no model provider available: {exc}")
    print_result(result, str(settings.runs_dir / incident.id))
    incident = plane.record_investigation(incident.id, result)
    print(f"\nincident {incident.id}: {incident.state}")
    if incident.state == State.PROPOSED:
        print(f"review and approve: {settings.controlplane_url}/incidents/{incident.id}")
    print(json.dumps({"incident": incident.id, "state": incident.state, "status": result.status}))


if __name__ == "__main__":
    main()
