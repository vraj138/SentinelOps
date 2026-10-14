"""Turns the results file into the summary table that goes in the README.

Only manifested runs (the fault showed up as an alert) are scored. Every number comes with its
run count; nothing is extrapolated.
"""

import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


def load(results_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in results_path.read_text().splitlines() if line]


def pct(part: int, whole: int) -> str:
    return "n/a" if whole == 0 else f"{100 * part / whole:.0f}% ({part}/{whole})"


def summarize(records: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Per-system metrics over manifested runs."""
    by_system: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_system[r["system"]].append(r)
    summary = {}
    for system, runs in sorted(by_system.items()):
        scored = [r for r in runs if r["manifested"] and r["status"] not in ("error",)]
        row = {
            "runs": str(len(runs)),
            "scored": str(len(scored)),
            "not manifested": str(
                sum(1 for r in runs if not r["manifested"] and r["status"] != "error")
            ),
            "errors": str(sum(1 for r in runs if r["status"] == "error")),
        }
        extracted = [r for r in scored if "extracted_correct" in r]
        if extracted:
            row["root cause (same metric, all systems)"] = pct(
                sum(1 for r in extracted if r["extracted_correct"]), len(extracted)
            )
            row["root cause, hedging allowed"] = pct(
                sum(1 for r in extracted if r.get("extracted_correct_lenient")), len(extracted)
            )
            row["hedged between causes"] = pct(
                sum(1 for r in extracted if r["extracted"]["hedged"]), len(extracted)
            )
        judged = [r for r in scored if r.get("judge")]
        if judged:
            correct = sum(1 for r in judged if r["judge"]["verdict"] == "correct")
            row["root cause (judge)"] = pct(correct, len(judged))
        with_score = [r for r in scored if r.get("score")]
        if with_score:
            s = [r["score"] for r in with_score]
            n = len(s)
            row["root cause service"] = pct(sum(x["root_cause_correct"] for x in s), n)
            row["correct action"] = pct(sum(x["action"] == "full" for x in s), n)
            row["correct or partial"] = pct(sum(x["action"] in ("full", "partial") for x in s), n)
            row["harmful proposal"] = pct(sum(x["harmful"] for x in s), n)
            row["escalation correct"] = pct(sum(x["escalation_correct"] for x in s), n)
        times = [r["seconds_to_answer"] for r in scored if r.get("seconds_to_answer") is not None]
        if times:
            row["median time to answer"] = f"{statistics.median(times):.0f}s"
        costs = [
            r["totals"]["cost_usd"]
            for r in scored
            if r.get("totals", {}).get("cost_usd") is not None
        ]
        if costs:
            row["mean cost"] = f"${statistics.mean(costs):.3f}"
        calls = [
            r["totals"]["tool_calls"]
            for r in scored
            if r.get("totals", {}).get("tool_calls") is not None
        ]
        if calls:
            row["mean tool calls"] = f"{statistics.mean(calls):.1f}"
        summary[system] = row
    return summary


def per_scenario(records: list[dict[str, Any]], system: str = "sentinelops") -> list[str]:
    """Markdown rows: scenario -> action grades across repeats."""
    grades: dict[str, list[str]] = defaultdict(list)
    for r in records:
        if r["system"] == system:
            grades[r["scenario_id"]].append(r["score"]["action"] if r.get("score") else r["status"])
    return [f"| {sid} | {', '.join(g)} |" for sid, g in sorted(grades.items())]


def markdown(records: list[dict[str, Any]]) -> str:
    summary = summarize(records)
    if not summary:
        return "No results yet."
    systems = list(summary)
    metrics: list[str] = []
    for row in summary.values():
        metrics += [m for m in row if m not in metrics]
    lines = [
        "| metric | " + " | ".join(systems) + " |",
        "|---|" + "---|" * len(systems),
        *(
            f"| {m} | " + " | ".join(summary[s].get(m, "-") for s in systems) + " |"
            for m in metrics
        ),
        "",
        "| scenario (SentinelOps) | proposed-action grade per run |",
        "|---|---|",
        *per_scenario(records),
    ]
    return "\n".join(lines)
