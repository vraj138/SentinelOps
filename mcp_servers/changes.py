"""Changes tool server: deploy history from the deploy repo, and current feature flags.

Scoped to the deploy repo only. The main SentinelOps repo is never readable through this
server: it contains the fault injectors and their answer keys.
"""

import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from mcp_servers.common import ToolError, cap, redact, to_result
from sentinelops.config import Settings

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
SHA = re.compile(r"^[0-9a-f]{7,40}$")
FIELD_SEP = "\x1f"


class ChangesBackend:
    """Reads the deploy repo with git (argument lists only) and the flags file."""

    def __init__(self, repo_path: Path, flags_path: Path) -> None:
        self.repo_path = repo_path
        self.flags_path = flags_path

    def deploys(self, since_minutes: int, limit: int) -> list[dict[str, Any]]:
        since = (datetime.now(UTC) - timedelta(minutes=since_minutes)).isoformat()
        fmt = FIELD_SEP.join(["%h", "%aI", "%an", "%s"])
        out = self._git(
            "log", f"--since={since}", f"--max-count={limit}", f"--format={fmt}", "--name-only"
        )
        deploys: list[dict[str, Any]] = []
        for line in out.splitlines():
            if FIELD_SEP in line:
                sha, when, author, subject = line.split(FIELD_SEP)
                deploys.append(
                    {"sha": sha, "time": when, "author": author, "message": subject, "files": []}
                )
            elif line.strip() and deploys:
                deploys[-1]["files"].append(line.strip())
        return deploys

    def show(self, sha: str) -> str:
        if not SHA.match(sha):
            raise ToolError(f"invalid commit sha {sha!r}: expected 7-40 hex characters")
        return self._git("show", "--format=%h %aI %an%n%s%n", sha, "--")

    def flags(self) -> dict[str, Any]:
        try:
            return json.loads(self.flags_path.read_text())
        except FileNotFoundError as exc:
            raise ToolError(f"feature flags file not found at {self.flags_path}") from exc

    def _git(self, *args: str) -> str:
        if not (self.repo_path / ".git").exists():
            raise ToolError(f"deploy repo not found at {self.repo_path}")
        result = subprocess.run(
            ["git", "-C", str(self.repo_path), *args], capture_output=True, text=True, timeout=20
        )
        if result.returncode != 0:
            raise ToolError(f"git {args[0]} failed: {result.stderr.strip()[:500]}")
        return result.stdout


def build_server(backend: ChangesBackend) -> MCPServer:
    """The changes MCP server, backed by `backend`."""
    server = MCPServer(
        "changes",
        instructions=(
            "Change history for the demo services. A deploy is a commit to the deploy repo that "
            "changes services/<service>.env (release settings, including APP_VERSION); the "
            "service is restarted with the new settings. Secret values are redacted."
        ),
    )

    @server.tool(
        description=(
            "Deploys (newest first) in the last `since_minutes` (max 1440), up to `limit` "
            "(max 50): sha, time, author, message, files changed."
        ),
        annotations=READ_ONLY,
    )
    def list_deploys(since_minutes: int = 120, limit: int = 20) -> str:
        if not 1 <= since_minutes <= 1440 or not 1 <= limit <= 50:
            raise ToolError("since_minutes must be 1-1440 and limit 1-50")
        deploys = backend.deploys(since_minutes, limit)
        return to_result({"since_minutes": since_minutes, "deploys": deploys})

    @server.tool(description="Full diff of one deploy, by sha.", annotations=READ_ONLY)
    def show_deploy(sha: str) -> str:
        return cap(redact(backend.show(sha)))

    @server.tool(
        description="Current feature flag values, with who last changed them and when.",
        annotations=READ_ONLY,
    )
    def get_feature_flags() -> str:
        return to_result(backend.flags())

    return server


if __name__ == "__main__":
    settings = Settings()
    build_server(ChangesBackend(settings.deploy_repo_path, settings.flags_path)).run()
