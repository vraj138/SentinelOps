"""Read-only access to the demo containers through the docker CLI and the event log.

Commands are argument lists (never a shell string) and only read: `inspect` and `logs`.
Lifecycle events come from the file written by the event-recorder container.
`inspect` uses a format template that selects specific fields, so container environment
variables (which hold secrets) are never read at all.
"""

import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp_servers.common import ToolError, check_service

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]
INSPECT_FORMAT = (
    '{"state":{{json .State}},"image":{{json .Config.Image}},'
    '"restart_count":{{.RestartCount}},"created":{{json .Created}}}'
)
EVENT_ACTIONS = {"create", "start", "restart", "die", "oom", "kill", "stop", "destroy"}
EVENT_ATTRIBUTES = ("exitCode", "image", "signal")


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=20)


class DockerBackend:
    """Reads container state, logs and events for the demo services."""

    def __init__(
        self, docker_bin: str, name_template: str, events_path: Path, run: Runner = run_command
    ) -> None:
        self.docker_bin = docker_bin
        self.name_template = name_template
        self.events_path = events_path
        self.run = run

    def container(self, service: str) -> str:
        return self.name_template.format(service=check_service(service))

    def status(self, service: str) -> dict[str, Any]:
        raw = json.loads(
            self._docker(["inspect", self.container(service), "--format", INSPECT_FORMAT])
        )
        state = raw["state"]
        return {
            "service": service,
            "status": state.get("Status"),
            "running": state.get("Running"),
            "restarting": state.get("Restarting"),
            "exit_code": state.get("ExitCode"),
            "oom_killed_last_exit": state.get("OOMKilled"),
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
            "health": (state.get("Health") or {}).get("Status"),
            "restart_count": raw["restart_count"],
            "image": raw["image"],
            "created": raw["created"],
        }

    def logs(self, service: str, since_minutes: int, tail: int) -> str:
        args = ["logs", "--timestamps", "--since", f"{since_minutes}m", "--tail", str(tail)]
        result = self._run([self.docker_bin, *args, self.container(service)])
        return result.stdout + result.stderr

    def events(self, service: str, since_minutes: int) -> list[dict[str, Any]]:
        """Lifecycle events from the recorder's log (Docker's own buffer is too short)."""
        try:
            lines = self.events_path.read_text().splitlines()
        except FileNotFoundError as exc:
            raise ToolError(
                f"container event log not found at {self.events_path}; is event-recorder running?"
            ) from exc
        name = self.container(service)
        cutoff = datetime.now(UTC).timestamp() - since_minutes * 60
        events = []
        for line in lines:
            event = json.loads(line)
            attributes = event.get("Actor", {}).get("Attributes", {})
            action = event.get("Action", "").split(":")[0]
            if attributes.get("name") != name or event["time"] < cutoff:
                continue
            if action not in EVENT_ACTIONS:
                continue
            events.append(
                {
                    "time": datetime.fromtimestamp(event["time"], UTC).isoformat(),
                    "action": action,
                    **{k: attributes[k] for k in EVENT_ATTRIBUTES if k in attributes},
                }
            )
        return events

    def _docker(self, args: list[str]) -> str:
        return self._run([self.docker_bin, *args]).stdout

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            result = self.run(args)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolError(f"docker command failed: {exc!r}") from exc
        if result.returncode != 0:
            raise ToolError(f"docker {args[1]} failed: {result.stderr.strip()[:500]}")
        return result
