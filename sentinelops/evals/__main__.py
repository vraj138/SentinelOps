"""Benchmark CLI: `python -m sentinelops.evals <run|v1|extract|judge|report|replay>`.

Paid runs need `--confirm-spend`; `--dry-run` uses a scripted model and costs nothing.
"""

import argparse
import asyncio
import json
import sys
from functools import partial
from pathlib import Path

import anthropic

from faults import registry
from faults.environment import Environment
from faults.provider import ProviderFile
from mcp_servers.metrics import PrometheusBackend
from sentinelops.agent.audit import AuditLog
from sentinelops.agent.investigator import Budget, Investigator
from sentinelops.agent.policy import PolicyGate
from sentinelops.catalog import load as load_catalog
from sentinelops.config import ROOT, Settings
from sentinelops.controlplane.actions import ActionBackend
from sentinelops.controlplane.verify import Verifier
from sentinelops.evals import citation_check, heuristic, report, routing_score, v1_baseline
from sentinelops.evals.cleanslate import CleanSlate
from sentinelops.evals.extract import CALIBRATION as EXTRACT_CALIBRATION
from sentinelops.evals.extract import Extractor
from sentinelops.evals.extract import correct as extracted_correct
from sentinelops.evals.judge import Judge, calibrate
from sentinelops.evals.judge_cases import CASES
from sentinelops.evals.naive_agent import NAIVE_SYSTEM_PROMPT, NaiveExecutor
from sentinelops.evals.replay import ReplayHub
from sentinelops.evals.runner import BenchmarkRunner, NaiveAgent, run_v1_from_recordings
from sentinelops.evals.runner import answer_key as runner_answer_key
from sentinelops.evals.scripted import ScriptedLLM
from sentinelops.investigate import build_llm, has_anthropic_credentials, investigate
from sentinelops.stack.deploy_repo import DeployRepo
from sentinelops.stack.flags import FlagFile
from sentinelops.stack.runtime import ComposeRuntime

RESULTS = ROOT / "evals" / "results" / "results.jsonl"
RECORDINGS = ROOT / "evals" / "recordings"
V1_DIR = ROOT / "storage" / "v1-baseline"


def cmd_run(args: argparse.Namespace, settings: Settings) -> None:
    if not args.dry_run:
        if not args.confirm_spend:
            sys.exit("paid run: add --confirm-spend (or use --dry-run for a free harness test)")
        if not has_anthropic_credentials(settings):
            sys.exit("error: no Anthropic credentials; set ANTHROPIC_API_KEY in .env")
    scenarios = (
        [registry.get(s) for s in args.scenarios.split(",")]
        if args.scenarios
        else registry.ALL_SCENARIOS
    )
    repo, flags, provider = (
        DeployRepo(settings.deploy_repo_path),
        FlagFile(settings.flags_path),
        ProviderFile(),
    )
    runtime = ComposeRuntime()
    environment = Environment(repo, flags, provider, runtime)
    clean = CleanSlate(
        repo,
        flags,
        lambda: provider.set_latency(0),
        runtime,
        settings.container_events_path,
        PrometheusBackend(settings.prometheus_url),
    )
    naive = None
    if not args.no_naive:
        actions = ActionBackend(repo, flags, runtime)
        naive = NaiveAgent(
            investigate=partial(investigate, system_prompt=NAIVE_SYSTEM_PROMPT, strict=False),
            executor=NaiveExecutor(actions, Verifier(PrometheusBackend(settings.prometheus_url))),
        )
    runner = BenchmarkRunner(
        settings,
        environment,
        clean,
        investigate,
        results_path=Path(args.results),
        recordings_dir=Path(args.recordings),
        llm_factory=(lambda audit: ScriptedLLM()) if args.dry_run else None,
        naive=naive,
    )
    runner.run(scenarios, args.repeats)
    print(report.markdown(report.load(Path(args.results))))


def cmd_v1(args: argparse.Namespace, settings: Settings) -> None:
    """Run the v1 baseline on every recorded SentinelOps run's alert text."""
    if settings.openai_api_key is None:
        sys.exit("error: v1 needs OPENAI_API_KEY in .env (gpt-4o-mini and OpenAI embeddings)")
    if not args.confirm_spend:
        sys.exit("paid run: add --confirm-spend")
    variants = v1_baseline.VARIANTS if args.variant == "both" else (args.variant,)
    snapshot = v1_baseline.export(variants[0], V1_DIR)
    provenance = {
        "incidentpilot_commit": v1_baseline.legacy_sha(),
        "working_copy_sha256": v1_baseline.snapshot_hash(snapshot),
        "note": "LLM orchestrator is uncommitted in IncidentPilot; snapshot of the working copy",
    }
    (Path(args.results).parent / "v1_snapshot.json").write_text(json.dumps(provenance, indent=2))
    key = settings.openai_api_key.get_secret_value()
    run_v1_from_recordings(
        Path(args.recordings),
        Path(args.results),
        variants,
        lambda variant, query: v1_baseline.run(variant, V1_DIR, query, key),
    )
    print(report.markdown(report.load(Path(args.results))))


def calibration_set() -> list[tuple[str, str, str]]:
    """(answer key, candidate, expected verdict) for one scenario of each fault type."""
    first_of_type: dict[str, str] = {}
    for scenario in registry.ALL_SCENARIOS:
        first_of_type.setdefault(scenario.fault_type.value, scenario.ground_truth.root_cause)
    return [
        (first_of_type[fault], candidate, expected)
        for fault, cases in CASES.items()
        for candidate, expected in cases
    ]


def cmd_judge(args: argparse.Namespace, settings: Settings) -> None:
    """Grade root-cause text of every run that has none yet, after calibrating the judge."""
    if settings.anthropic_api_key is None:
        sys.exit("error: the judge needs ANTHROPIC_API_KEY in .env")
    if not args.confirm_spend:
        sys.exit("paid run: add --confirm-spend")
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())
    judge = Judge(settings.anthropic_model, client=client)
    graded, failures = calibrate(judge, calibration_set())
    path = Path(args.results)
    (path.parent / "judge_calibration.json").write_text(json.dumps(graded, indent=2))
    print(f"judge calibration: {len(graded) - len(failures)}/{len(graded)} cases as expected")
    if failures:
        sys.exit("judge failed calibration; not grading:\n" + "\n".join(failures))
    records = report.load(path)
    for r in records:
        if r.get("manifested") and r.get("root_cause_text") and not r.get("judge"):
            truth = registry.get(r["scenario_id"]).ground_truth.root_cause
            r["judge"] = judge.grade(truth, r["root_cause_text"]).model_dump()
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    print(report.markdown(records))


def cmd_extract(args: argparse.Namespace, settings: Settings) -> None:
    """Extract the blamed service from every answer (all systems), after calibration."""
    if settings.anthropic_api_key is None:
        sys.exit("error: extraction needs ANTHROPIC_API_KEY in .env")
    if not args.confirm_spend:
        sys.exit("paid run: add --confirm-spend")
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())
    extractor = Extractor(client, settings.anthropic_model)
    graded, failures = [], []
    for text, blamed, hedged in EXTRACT_CALIBRATION:
        got = extractor.extract(text)
        graded.append({"text": text, "expected": [blamed, hedged], "got": [got.blamed, got.hedged]})
        if (got.blamed, got.hedged) != (blamed, hedged):
            failures.append(f"expected {blamed}/{hedged}, got {got.blamed}/{got.hedged}: {text!r}")
    path = Path(args.results)
    (path.parent / "extract_calibration.json").write_text(json.dumps(graded, indent=2))
    print(f"extractor calibration: {len(graded) - len(failures)}/{len(graded)} cases as expected")
    if failures:
        sys.exit("extractor failed calibration; not scoring:\n" + "\n".join(failures))
    records = report.load(path)
    for r in records:
        if r.get("manifested") and r.get("root_cause_text") and "extracted" not in r:
            key = runner_answer_key(registry.get(r["scenario_id"]))
            got = extractor.extract(r["root_cause_text"])
            r["extracted"] = got.model_dump()
            accepted = key.accepted_root_causes or [key.root_cause_service]
            r["extracted_correct"] = extracted_correct(got, accepted)
            r["extracted_correct_lenient"] = extracted_correct(got, accepted, allow_hedge=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    print(report.markdown(records))


def cmd_baseline(args: argparse.Namespace, settings: Settings) -> None:
    """Score the naive rule (blame the most-downstream alerting service). Free."""
    heuristic.run(Path(args.results))
    print(report.markdown(report.load(Path(args.results))))


def cmd_routing(args: argparse.Namespace, settings: Settings) -> None:
    """Score 'correct team on the first page' for every system. Free."""
    routing_score.score(Path(args.results), Path(args.recordings), load_catalog())
    print(report.markdown(report.load(Path(args.results))))


def cmd_citations(args: argparse.Namespace, settings: Settings) -> None:
    """Re-run SentinelOps' evidence check on every recorded answer. Free."""
    citation_check.score(Path(args.results), Path(args.recordings))
    print(report.markdown(report.load(Path(args.results))))


def cmd_report(args: argparse.Namespace, settings: Settings) -> None:
    text = report.markdown(report.load(Path(args.results)))
    (Path(args.results).parent / "summary.md").write_text(text + "\n")
    print(text)


def cmd_replay(args: argparse.Namespace, settings: Settings) -> None:
    """Re-run the agent on a recorded run's tool results (needs Anthropic credentials)."""
    if not has_anthropic_credentials(settings):
        sys.exit("error: replay calls the model; set ANTHROPIC_API_KEY in .env")
    recording = RECORDINGS / args.run_id
    audit = AuditLog(recording / f"replay-{len(list(recording.glob('replay-*')))}.jsonl")
    context = json.loads((recording / "audit.jsonl").read_text().splitlines()[0])["alert_context"]

    async def run() -> None:
        async with ReplayHub(recording) as hub:
            investigator = Investigator(
                build_llm(settings, audit), hub, audit, PolicyGate(), Budget()
            )  # type: ignore[arg-type]
            result = await investigator.run(context)
        print(result.model_dump_json(indent=2))

    asyncio.run(run())


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m sentinelops.evals")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the benchmark")
    run.add_argument("--scenarios", help="comma-separated ids, e.g. bad_deploy/c (default: all)")
    run.add_argument("--repeats", type=int, default=3)
    run.add_argument("--dry-run", action="store_true", help="scripted model, no API calls")
    run.add_argument("--confirm-spend", action="store_true", help="required for paid runs")
    run.add_argument("--recordings", default=str(RECORDINGS), help="where run recordings go")
    run.add_argument("--no-naive", action="store_true", help="skip the naive baseline agent")
    v1 = sub.add_parser("v1", help="run the v1 baseline on recorded runs (no stack needed)")
    v1.add_argument("--variant", choices=["shipped", "our-runbooks", "both"], default="both")
    v1.add_argument("--recordings", default=str(RECORDINGS))
    judge = sub.add_parser("judge", help="calibrate the judge, then grade root-cause text")
    extract = sub.add_parser(
        "extract", help="same-metric scoring: which service each answer blames"
    )
    for p in (v1, judge, extract):
        p.add_argument("--confirm-spend", action="store_true", help="required (paid API calls)")
    baseline = sub.add_parser("baseline", help="score the naive rule baseline (free)")
    routing = sub.add_parser("routing", help="score correct team on first page (free)")
    routing.add_argument("--recordings", default=str(RECORDINGS))
    citations = sub.add_parser("citations", help="re-check every answer's citations (free)")
    citations.add_argument("--recordings", default=str(RECORDINGS))
    for p in (run, v1, judge, extract, baseline, routing, citations, sub.add_parser("report")):
        p.add_argument("--results", default=str(RESULTS))
    sub.add_parser("replay").add_argument("run_id")
    args = parser.parse_args()
    handlers = {
        "run": cmd_run,
        "v1": cmd_v1,
        "judge": cmd_judge,
        "extract": cmd_extract,
        "baseline": cmd_baseline,
        "routing": cmd_routing,
        "citations": cmd_citations,
        "report": cmd_report,
        "replay": cmd_replay,
    }
    handlers[args.command](args, Settings())


if __name__ == "__main__":
    main()
