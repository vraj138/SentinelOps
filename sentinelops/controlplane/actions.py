"""Typed remediation actions: rollback, restart, set_flag. No free-form commands exist.

Each action goes through three steps:
1. resolve: turn a proposal into one exact change (the dry run a human approves). The payload
   records what was observed (deploy repo HEAD, current settings or flag value), and its hash
   is the fingerprint the approval is bound to.
2. execute: re-resolve, refuse if the fingerprint changed (the world moved since approval),
   apply the change, and return how to undo it.
3. undo: used by auto-rollback when verification fails.
"""

import hashlib
import json
from typing import Any

from mcp_servers.common import redact
from sentinelops.controlplane.models import Inverse, ProposedAction, ResolvedAction
from sentinelops.stack.deploy_repo import DeployRepo
from sentinelops.stack.flags import FlagFile
from sentinelops.stack.runtime import Runtime

SERVICES = ("gateway", "orders", "payments")
ACTOR = ("sentinelops", "sentinelops@sentinelops.local")
UNDO_PREFIXES = ("Revert ", "Rollback ", "Restore ")


class ActionError(RuntimeError):
    """The action can't be resolved or executed safely; the incident must escalate."""


def fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def settings_diff(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Readable `-old` / `+new` lines for changed settings, secrets redacted."""
    lines = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            if key in before:
                lines.append(redact(f"- {key}={before[key]}"))
            if key in after:
                lines.append(redact(f"+ {key}={after[key]}"))
    return lines


class ActionBackend:
    def __init__(self, repo: DeployRepo, flags: FlagFile, runtime: Runtime) -> None:
        self.repo = repo
        self.flags = flags
        self.runtime = runtime

    def resolve(self, proposal: ProposedAction) -> ResolvedAction:
        if proposal.kind in ("rollback", "restart") and proposal.service not in SERVICES:
            raise ActionError(f"{proposal.kind} needs one of {list(SERVICES)}")
        if proposal.kind == "rollback":
            return self._resolve_rollback(proposal.service or "")
        if proposal.kind == "restart":
            service = proposal.service or ""
            payload = {"kind": "restart", "service": service, "release": self.repo.read(service)}
            return self._resolved("restart", service, payload, [f"restart the {service} container"])
        if proposal.flag is None or proposal.flag_value is None:
            raise ActionError("set_flag needs a flag and a value")
        current = self.flags.read()
        if proposal.flag not in current:
            raise ActionError(f"unknown feature flag {proposal.flag!r}")
        payload = {
            "kind": "set_flag",
            "flag": proposal.flag,
            "current": current[proposal.flag],
            "new": proposal.flag_value,
        }
        dry_run = [
            f"set feature flag {proposal.flag}: {current[proposal.flag]} -> {proposal.flag_value}"
        ]
        return self._resolved("set_flag", proposal.flag, payload, dry_run)

    def _resolve_rollback(self, service: str) -> ResolvedAction:
        change = self.repo.last_change(service)
        if change is None:
            raise ActionError(f"{service} has no deploy to roll back")
        if change.message.startswith(UNDO_PREFIXES):
            raise ActionError(
                f"the latest change to {service} is already an undo ({change.message!r}); "
                "rolling it back would re-apply an older release"
            )
        current = self.repo.read(service)
        payload = {
            "kind": "rollback",
            "service": service,
            "undo_commit": change.sha,
            "undo_message": change.message,
            "head": self.repo.head(),
            "current": current,
            "restore": change.before,
        }
        dry_run = [
            f'roll back {service}: undo {change.sha[:7]} "{change.message}" '
            f"by {change.author} at {change.time}",
            f"version {current.get('APP_VERSION')} -> {change.before.get('APP_VERSION')}",
            *settings_diff(current, change.before),
            f"then recreate the {service} container",
        ]
        return self._resolved("rollback", service, payload, dry_run)

    def _resolved(
        self, kind: str, target: str, payload: dict[str, Any], dry_run: list[str]
    ) -> ResolvedAction:
        return ResolvedAction(
            kind=kind,  # type: ignore[arg-type]
            target=target,
            payload=payload,
            dry_run="\n".join(dry_run),
            fingerprint=fingerprint(payload),
        )

    def execute(self, approved: ResolvedAction, proposal: ProposedAction) -> Inverse:
        """Apply the approved action if nothing changed since approval. Returns its undo."""
        try:
            now = self.resolve(proposal)
        except ActionError as exc:
            raise ActionError(f"refusing to execute: {exc}") from exc
        if now.fingerprint != approved.fingerprint:
            raise ActionError(
                "refusing to execute: the system changed since approval "
                "(the action would no longer do exactly what was approved)"
            )
        p = approved.payload
        if approved.kind == "rollback":
            service = p["service"]
            version = p["restore"].get("APP_VERSION")
            message = f"Rollback {service} to {version}: undo {p['undo_commit'][:7]}"
            self.repo.write_release(service, p["restore"], message, ACTOR)
            self.runtime.recreate(service)
            return Inverse(kind="restore_release", target=service, data={"settings": p["current"]})
        if approved.kind == "restart":
            self.runtime.restart(p["service"])
            return Inverse(kind="none", target=p["service"])
        self.flags.set(p["flag"], p["new"], updated_by="sentinelops")
        return Inverse(kind="set_flag", target=p["flag"], data={"value": p["current"]})

    def undo(self, inverse: Inverse) -> str:
        """Undo an executed action. Returns a description of what was done."""
        if inverse.kind == "restore_release":
            settings = inverse.data["settings"]
            message = f"Restore {inverse.target} {settings.get('APP_VERSION')}: verification failed"
            self.repo.write_release(inverse.target, settings, message, ACTOR)
            self.runtime.recreate(inverse.target)
            return f"restored the {inverse.target} release that was live before the action"
        if inverse.kind == "set_flag":
            self.flags.set(inverse.target, inverse.data["value"], updated_by="sentinelops")
            return f"set {inverse.target} back to {inverse.data['value']}"
        return f"nothing to undo for a restart of {inverse.target}"
