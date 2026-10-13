"""Command line: investigate the currently firing alerts. `python -m sentinelops.investigate`.

Writes everything to storage/runs/<run id>/: `audit.jsonl` (every LLM and tool call) and
`result.json` (the RCA or the escalation).
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import anthropic
import openai

from mcp_servers.common import ToolError
from mcp_servers.metrics import PrometheusBackend
from sentinelops.agent.anthropic_llm import AnthropicLLM
from sentinelops.agent.audit import AuditLog
from sentinelops.agent.investigator import Budget, InvestigationResult, Investigator
from sentinelops.agent.llm import FallbackLLM, LLMUnavailable
from sentinelops.agent.openai_llm import OpenAILLM
from sentinelops.agent.policy import PolicyGate
from sentinelops.agent.toolhub import ToolHub, stdio_servers
from sentinelops.config import Settings


def alert_context(settings: Settings) -> str:
    """Describe the firing alerts. If Prometheus can't be reached, say so plainly."""
    try:
        alerts = PrometheusBackend(settings.prometheus_url).alerts()
    except ToolError as exc:
        return f"Could not fetch alerts: {exc}. Investigate with the tools that work."
    firing = [a for a in alerts if a.get("state") == "firing"]
    if not firing:
        return "No alerts are firing right now. Check whether anything is wrong."
    lines = [
        f"- {a['labels'].get('alertname')} on {a['labels'].get('service')} since "
        f"{a.get('activeAt')}: {a.get('annotations', {}).get('summary', '')}"
        for a in firing
    ]
    return "Firing alerts:\n" + "\n".join(lines)


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


async def investigate(settings: Settings, context: str, run_id: str) -> InvestigationResult:
    run_dir = settings.runs_dir / run_id
    audit = AuditLog(run_dir / "audit.jsonl")
    budget = Budget(
        max_tool_calls=settings.agent_max_tool_calls,
        max_tokens=settings.agent_max_tokens,
        max_cost_usd=settings.agent_max_cost_usd,
        max_seconds=settings.agent_max_seconds,
    )
    async with ToolHub(stdio_servers()) as hub:
        investigator = Investigator(build_llm(settings, audit), hub, audit, PolicyGate(), budget)
        result = await investigator.run(context)
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
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    context = args.alert or alert_context(settings)
    print(context)
    try:
        result = asyncio.run(investigate(settings, context, run_id))
    except anthropic.AuthenticationError:
        sys.exit(
            "error: no valid Anthropic credentials. Set ANTHROPIC_API_KEY in .env "
            "(see .env.example) or run `ant auth login`."
        )
    except LLMUnavailable as exc:
        sys.exit(f"error: no model provider available: {exc}")
    print_result(result, str(settings.runs_dir / run_id))
    print(json.dumps({"run_id": run_id, "status": result.status}))


if __name__ == "__main__":
    main()
