"""Command line for incidents: `python -m sentinelops.incidents <command>`.

`open --propose` lets an operator propose an action without the agent, which goes through
exactly the same approval, execution and verification path as the agent's proposals.
"""

import argparse
import sys

from sentinelops.config import Settings
from sentinelops.controlplane import build_control_plane
from sentinelops.controlplane.actions import ActionError
from sentinelops.controlplane.alerts import firing_alerts
from sentinelops.controlplane.models import Incident, ProposedAction
from sentinelops.controlplane.store import StaleTransition


def parse_proposal(text: str, reason: str) -> ProposedAction:
    """`rollback:payments`, `restart:orders`, or `set_flag:checkout_v2_pricing=false`."""
    kind, _, target = text.partition(":")
    if kind in ("rollback", "restart"):
        return ProposedAction(kind=kind, service=target, reason=reason, proposed_by="operator")
    if kind == "set_flag":
        flag, _, value = target.partition("=")
        if value.lower() not in ("true", "false"):
            raise ValueError("set_flag needs a value: set_flag:<flag>=true|false")
        return ProposedAction(
            kind="set_flag",
            flag=flag,
            flag_value=value.lower() == "true",
            reason=reason,
            proposed_by="operator",
        )
    raise ValueError(f"unknown action {kind!r}: use rollback, restart or set_flag")


def show(incident: Incident) -> None:
    print(f"{incident.id}  {incident.state}")
    print(f"  alerts: {', '.join(f'{a.name}({a.service})' for a in incident.alerts) or 'none'}")
    if incident.rca:
        print(f"  root cause: {incident.rca.root_cause}")
    if incident.action:
        print("  action (dry run):")
        for line in incident.action.dry_run.splitlines():
            print(f"    {line}")
    if incident.approval:
        print(f"  approved by {incident.approval.approver} at {incident.approval.at:%H:%M:%S}")
    for check in incident.verification:
        print(f"  {'PASS' if check.ok else 'FAIL'} {check.name}: {check.detail}")
    if incident.escalation_reason:
        print(f"  escalated: {incident.escalation_reason}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m sentinelops.incidents")
    commands = parser.add_subparsers(dest="command", required=True)
    open_cmd = commands.add_parser("open", help="open an incident from the firing alerts")
    open_cmd.add_argument(
        "--propose", help="rollback:<svc> | restart:<svc> | set_flag:<flag>=<bool>"
    )
    open_cmd.add_argument("--reason", default="proposed by operator")
    commands.add_parser("list", help="recent incidents")
    for name in ("show", "execute"):
        commands.add_parser(name).add_argument("incident_id")
    approve = commands.add_parser("approve")
    approve.add_argument("incident_id")
    approve.add_argument("--by", required=True, help="who is approving")
    reject = commands.add_parser("reject")
    reject.add_argument("incident_id")
    reject.add_argument("--by", required=True)
    reject.add_argument("--reason", required=True)
    args = parser.parse_args()

    settings = Settings()
    plane = build_control_plane(settings)
    try:
        if args.command == "open":
            context, alerts = firing_alerts(settings.prometheus_url)
            incident = plane.open(context, alerts)
            if args.propose:
                incident = plane.propose(incident.id, parse_proposal(args.propose, args.reason))
            show(incident)
        elif args.command == "list":
            for incident in plane.store.list():
                print(
                    f"{incident.id}  {incident.state:<11} "
                    f"{incident.alert_context.splitlines()[-1][:70]}"
                )
        elif args.command == "show":
            show(plane.store.get(args.incident_id))
        elif args.command == "approve":
            show(plane.approve(args.incident_id, args.by))
        elif args.command == "reject":
            show(plane.reject(args.incident_id, args.by, args.reason))
        elif args.command == "execute":
            print("executing and verifying (up to ~3.5 minutes)...")
            show(plane.execute(args.incident_id))
    except (ActionError, StaleTransition, KeyError, ValueError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
