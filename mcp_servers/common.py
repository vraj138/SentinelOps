"""Helpers shared by the tool servers: service allowlist, secret redaction, size caps.

Every tool result passes through `redact` and a size cap before the model sees it. Tool
failures are raised as `ToolError`, which MCP returns to the model as `is_error=True` with the
message, so a broken backend is reported as broken instead of looking like "no data".
"""

import json
import re
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

# Containers the read-only tools may inspect: the app services plus the shared database.
SERVICES = ("gateway", "orders", "payments", "postgres")
MAX_RESULT_CHARS = 8000

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:KEY|SECRET|PASSWORD|PASSWD|TOKEN)[A-Z0-9_]*)(\s*[=:]\s*)(\S+)"
)
_URL_CREDENTIALS = re.compile(r"([a-z][a-z0-9+.-]*://[^:/\s@]+):[^@\s]+@")


NOT_SECRETS = ("error", "exception")  # `KeyError: 'risk_score'` is evidence, not a secret


def _mask(match: re.Match[str]) -> str:
    name, separator, _ = match.groups()
    if name.lower().endswith(NOT_SECRETS):
        return match.group(0)
    return f"{name}{separator}[redacted]"


def redact(text: str) -> str:
    """Replace secret values (`*KEY*=...`, `*PASSWORD*: ...`, `scheme://user:pass@`).

    Exception names such as `KeyError` contain KEY but are kept: their message is evidence.
    """
    text = _SECRET_ASSIGNMENT.sub(_mask, text)
    return _URL_CREDENTIALS.sub(r"\1:[redacted]@", text)


_SECRET_NAME = re.compile(r"(?i)(KEY|SECRET|PASSWORD|PASSWD|TOKEN)")


def mask_secrets(data: Any) -> Any:
    """Copy of `data` with the values of secret-looking keys (`*KEY*`, `*TOKEN*`...) masked.

    Use for structured data; `redact` handles free text like logs and diffs.
    """
    if isinstance(data, dict):
        return {
            k: "[redacted]"
            if _SECRET_NAME.search(str(k)) and not isinstance(v, (dict, list))
            else mask_secrets(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [mask_secrets(v) for v in data]
    return data


def cap(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    """Truncate `text` to `limit` characters, saying how much was cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} characters]"


def to_result(data: Any) -> str:
    """Serialize a tool result as compact JSON, redacted and size-capped."""
    return cap(redact(json.dumps(data, default=str, separators=(",", ":"))))


def check_service(service: str) -> str:
    """Reject services outside the demo stack so tools can't be pointed elsewhere."""
    if service not in SERVICES:
        raise ToolError(f"unknown service {service!r}; choose one of {list(SERVICES)}")
    return service


__all__ = ["SERVICES", "ToolError", "cap", "check_service", "mask_secrets", "redact", "to_result"]
