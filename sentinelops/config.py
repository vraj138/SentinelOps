"""Application settings loaded from environment variables and `.env`."""

from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration for SentinelOps.

    API keys are optional so the package imports cleanly without them; code that
    calls an LLM provider must check for its key and raise a clear error if missing.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    prometheus_url: str = "http://localhost:9090"
    # The control plane's own database (not the demo app's Postgres, which faults overload).
    controlplane_database_url: str = (
        "postgresql://sentinelops:sentinelops@localhost:5433/sentinelops"
    )
    controlplane_url: str = "http://localhost:8080"
    controlplane_poll_alerts: bool = True  # open incidents from firing alerts
    incident_group_window_minutes: float = 5  # related alerts this close together: one incident

    # Automatic investigation of incidents opened from alerts. Read-only, but it costs money.
    auto_investigate: bool = True
    investigation_concurrency: int = 2  # more wait in a queue
    auto_investigate_daily_budget_usd: float = 2.0  # then incidents wait for a manual start
    auto_investigate_repeat_hours: float = 24  # no automatic rerun for alerts it couldn't fix

    # Tool servers. The agent's tools may only see these locations.
    deploy_repo_path: Path = ROOT / "storage" / "deploy-repo"
    flags_path: Path = ROOT / "storage" / "flags" / "flags.json"
    container_events_path: Path = ROOT / "storage" / "events" / "containers.jsonl"
    runbooks_dir: Path = ROOT / "docs" / "runbooks"
    docker_bin: str = "docker"
    container_name_template: str = "sentinelops-{service}-1"

    # Agent
    anthropic_model: str = "claude-opus-5-5"
    agent_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    openai_model: str | None = None  # fallback is enabled only when this and the key are set
    runs_dir: Path = ROOT / "storage" / "runs"
    agent_max_tool_calls: int = 30
    agent_max_tokens: int = 1_000_000
    agent_max_cost_usd: float = 3.0
    agent_max_seconds: float = 600
