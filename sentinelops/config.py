"""Application settings loaded from environment variables and `.env`."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for SentinelOps.

    API keys are optional so the package imports cleanly without them; code that
    calls an LLM provider must check for its key and raise a clear error if missing.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    prometheus_url: str = "http://localhost:9090"
    database_url: str | None = None
