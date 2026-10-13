"""The control plane: incident state machine, approvals, typed actions, verification."""

from mcp_servers.metrics import PrometheusBackend
from sentinelops.config import Settings
from sentinelops.controlplane.actions import ActionBackend
from sentinelops.controlplane.service import ControlPlane
from sentinelops.controlplane.store import PostgresStore
from sentinelops.controlplane.verify import Verifier
from sentinelops.stack.deploy_repo import DeployRepo
from sentinelops.stack.flags import FlagFile
from sentinelops.stack.runtime import ComposeRuntime


def build_control_plane(settings: Settings) -> ControlPlane:
    """The production control plane: Postgres store, real deploy repo, docker, Prometheus."""
    actions = ActionBackend(
        DeployRepo(settings.deploy_repo_path), FlagFile(settings.flags_path), ComposeRuntime()
    )
    return ControlPlane(
        PostgresStore(settings.controlplane_database_url),
        actions,
        Verifier(PrometheusBackend(settings.prometheus_url)),
        settings.runs_dir,
    )
