"""The GitOps deploy repo: each demo service's release settings, versioned in a local git repo.

A deploy is a commit that changes a service's `.env` file (and bumps its `APP_VERSION`),
followed by recreating that container. A rollback reverts the commit. The repo lives in
`storage/deploy-repo/`, is never pushed, and gives the agent a real change history to inspect.
"""

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "storage" / "deploy-repo"

BASE_RELEASES: dict[str, dict[str, str]] = {
    "gateway": {"APP_VERSION": "1.4.0"},
    "orders": {
        "APP_VERSION": "2.1.0",
        "ORDERS_DB_POOL_MODE": "per_request",
        "ORDERS_RECEIPT_CACHE_MAX_ITEMS": "50",
    },
    "payments": {
        "APP_VERSION": "1.9.0",
        "PAYMENTS_API_KEY": "demo-provider-key",
        "PAYMENTS_FRAUD_CHECK_RATE": "0.0",
    },
}
DEFAULT_AUTHOR = ("release-bot", "release-bot@sentinelops.local")


class DeployRepoError(RuntimeError):
    """A git command in the deploy repo failed."""


class DeployRepo:
    """Read and change the release settings stored in the deploy repo."""

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = path

    def env_file(self, service: str) -> Path:
        """Path of the release settings file for `service`."""
        return self.path / "services" / f"{service}.env"

    def init(self) -> bool:
        """Create the repo with the base releases if it doesn't exist. Returns True if created."""
        if (self.path / ".git").exists():
            return False
        self.path.mkdir(parents=True, exist_ok=True)
        self._git("init", "--quiet", "--initial-branch=main")
        for service, settings in BASE_RELEASES.items():
            self._write(service, settings)
        self._git("add", "--all")
        self._commit("Initial release of gateway, orders and payments", DEFAULT_AUTHOR)
        return True

    def read(self, service: str) -> dict[str, str]:
        """Current release settings for `service`."""
        settings: dict[str, str] = {}
        for line in self.env_file(service).read_text().splitlines():
            if line and not line.startswith("#"):
                key, _, value = line.partition("=")
                settings[key] = value
        return settings

    def deploy(
        self,
        service: str,
        changes: dict[str, str | None],
        message: str,
        author: tuple[str, str] = DEFAULT_AUTHOR,
    ) -> str:
        """Commit a new release of `service`. A value of None removes that setting.

        The new version is one patch above the highest version this service has ever had, so a
        release after a revert never reuses a version number. Returns the new commit SHA.
        """
        settings = self.read(service)
        for key, value in changes.items():
            if value is None:
                settings.pop(key, None)
            else:
                settings[key] = value
        settings["APP_VERSION"] = bump_patch(self.highest_version(service))
        self._write(service, settings)
        self._git("add", str(self.env_file(service)))
        self._commit(f"{service} {settings['APP_VERSION']}: {message}", author)
        return self.head()

    def revert(self, sha: str, author: tuple[str, str] = DEFAULT_AUTHOR) -> str:
        """Revert one commit with a new commit. Returns the new commit SHA."""
        name, email = author
        self._git(
            "-c", f"user.name={name}", "-c", f"user.email={email}", "revert", "--no-edit", sha
        )
        return self.head()

    def highest_version(self, service: str) -> str:
        """The highest `APP_VERSION` this service has had, including reverted releases."""
        subjects = self._git("log", "--format=%s").splitlines()
        pattern = re.compile(rf"^{re.escape(service)} (\d+\.\d+\.\d+):")
        versions = [m.group(1) for s in subjects if (m := pattern.match(s))]
        versions.append(self.read(service)["APP_VERSION"])
        return max(versions, key=lambda v: tuple(int(part) for part in v.split(".")))

    def head(self) -> str:
        """SHA of the latest commit."""
        return self._git("rev-parse", "HEAD").strip()

    def _write(self, service: str, settings: dict[str, str]) -> None:
        path = self.env_file(service)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# Release settings for {service}. Change only through a deploy commit."]
        lines += [f"{key}={settings[key]}" for key in sorted(settings)]
        path.write_text("\n".join(lines) + "\n")

    def _commit(self, message: str, author: tuple[str, str]) -> None:
        name, email = author
        self._git("-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-qm", message)

    def _git(self, *args: str) -> str:
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
        result = subprocess.run(
            ["git", "-C", str(self.path), *args], capture_output=True, text=True, env=env
        )
        if result.returncode != 0:
            raise DeployRepoError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout


def bump_patch(version: str) -> str:
    """Increment the last part of a dotted version: 1.9.0 -> 1.9.1."""
    major_minor, _, patch = version.rpartition(".")
    return f"{major_minor}.{int(patch) + 1}"
