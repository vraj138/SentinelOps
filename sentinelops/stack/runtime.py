"""Restarts demo services with docker compose (argument lists only, never a shell string)."""

import os
import subprocess
from pathlib import Path
from typing import Protocol

from sentinelops.config import ROOT

COMPOSE_FILE = ROOT / "infra" / "docker-compose.yml"


class Runtime(Protocol):
    """Restarts demo services."""

    def recreate(self, service: str) -> None:
        """Replace the container so it picks up new release settings."""
        ...

    def restart(self, service: str) -> None:
        """Restart the existing container with the same settings."""
        ...


class ContainerRuntimeError(RuntimeError):
    """A container runtime command failed."""


def tag_from_image(image: str) -> str:
    """The tag part of an image reference: `sentinelops-demo:4078549` -> `4078549`."""
    name, sep, tag = image.rpartition(":")
    if not sep or "/" in tag:
        raise ContainerRuntimeError(f"image {image!r} has no tag")
    return tag


class ComposeRuntime:
    """Runtime backed by `docker compose`. Commands are argument lists, never a shell string.

    A recreated container keeps the image tag it was already running, so it runs the same code
    as the rest of the stack even if the main repo has new commits since `make up`.
    """

    def __init__(self, compose_file: Path = COMPOSE_FILE) -> None:
        self.compose_file = compose_file

    def recreate(self, service: str) -> None:
        env = {**os.environ, "IMAGE_TAG": self.running_image_tag(service)}
        args = [*self._compose(), "up", "-d", "--no-deps", service]
        result = subprocess.run(args, capture_output=True, text=True, env=env)
        if result.returncode != 0:
            raise ContainerRuntimeError(f"recreating {service} failed: {result.stderr.strip()}")

    def restart(self, service: str) -> None:
        self._run([*self._compose(), "restart", service])

    def running_image_tag(self, service: str) -> str:
        """Tag of the image the service's container is using (even if it is crash-looping)."""
        container_id = self._run([*self._compose(), "ps", "-a", "-q", service]).strip()
        if not container_id:
            raise ContainerRuntimeError(f"{service} has no container; run `make up` first")
        image = self._run(["docker", "inspect", container_id, "--format", "{{.Config.Image}}"])
        return tag_from_image(image.strip())

    def _compose(self) -> list[str]:
        return ["docker", "compose", "-f", str(self.compose_file)]

    def _run(self, args: list[str]) -> str:
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode != 0:
            raise ContainerRuntimeError(f"{' '.join(args[:3])} failed: {result.stderr.strip()}")
        return result.stdout
