"""The external payment provider's conditions file (mounted read-only into payments).

This simulates a dependency we don't control. It is deliberately not in the deploy repo,
so a provider slowdown leaves no trace in our change history.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "storage" / "provider" / "conditions.json"


class ProviderFile:
    """Read and write the provider conditions file."""

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = path

    def init(self) -> bool:
        """Create the file with a healthy provider if missing. Returns True if created."""
        if self.path.exists():
            return False
        self.set_latency(0)
        return True

    def latency_ms(self) -> int:
        """The provider's current extra latency in milliseconds."""
        return int(json.loads(self.path.read_text())["latency_ms"])

    def set_latency(self, latency_ms: int) -> None:
        """Set the provider's latency. Writes a temp file first so readers never see half."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"latency_ms": latency_ms}) + "\n")
        tmp.chmod(0o644)
        tmp.replace(self.path)
