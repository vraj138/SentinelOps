"""Command line for the demo environment and fault injection: `python -m faults <command>`."""

import argparse
import sys

from faults import registry
from faults.deploy_repo import DeployRepo
from faults.environment import Environment, FaultAlreadyActive
from faults.flags import FlagFile
from faults.provider import ProviderFile


def init() -> None:
    """Create the deploy repo, flags file and provider file if they don't exist yet."""
    repo, flags, provider = DeployRepo(), FlagFile(), ProviderFile()
    print(f"deploy repo:   {'created' if repo.init() else 'exists'} at {repo.path}")
    print(f"flags file:    {'created' if flags.init() else 'exists'} at {flags.path}")
    print(f"provider file: {'created' if provider.init() else 'exists'} at {provider.path}")


def list_scenarios() -> None:
    for scenario in registry.ALL_SCENARIOS:
        print(f"{scenario.id:<28} {scenario.description}")


def inject(scenario_id: str) -> None:
    scenario = registry.get(scenario_id)
    print(f"injecting {scenario.id}: {scenario.description}")
    Environment().inject(scenario)
    alerts = ", ".join(f"{a.name}({a.service})" for a in scenario.ground_truth.expected_alerts)
    print(f"done. expect alerts: {alerts}. undo with: make reset")


def reset() -> None:
    undone = Environment().reset()
    print(f"reset {undone.scenario_id}" if undone else "nothing to reset")


def status() -> None:
    active = Environment().active()
    if active is None:
        print("no fault active")
        return
    print(f"{active.scenario_id} active since {active.started_at:%H:%M:%S} UTC")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m faults")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create the deploy repo and flags file")
    commands.add_parser("list", help="list fault scenarios")
    inject_cmd = commands.add_parser("inject", help="inject a scenario, e.g. bad_deploy/a")
    inject_cmd.add_argument("scenario_id")
    commands.add_parser("reset", help="undo the active scenario")
    commands.add_parser("status", help="show the active scenario")
    args = parser.parse_args()

    try:
        if args.command == "init":
            init()
        elif args.command == "list":
            list_scenarios()
        elif args.command == "inject":
            inject(args.scenario_id)
        elif args.command == "reset":
            reset()
        elif args.command == "status":
            status()
    except (KeyError, FaultAlreadyActive) as exc:
        sys.exit(f"error: {exc.args[0]}")


if __name__ == "__main__":
    main()
