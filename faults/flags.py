"""The feature-flags file shared by the demo services (mounted read-only into containers)."""

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "storage" / "flags" / "flags.json"
BASE_FLAGS: dict[str, bool] = {"checkout_v2_pricing": False}


class FlagFile:
    """Read and write the flags file. Services reload it automatically when it changes."""

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = path

    def init(self) -> bool:
        """Create the file with the base flags if missing. Returns True if created."""
        if self.path.exists():
            return False
        self.write(BASE_FLAGS, updated_by="init")
        return True

    def read(self) -> dict[str, bool]:
        """Current flag values."""
        return dict(json.loads(self.path.read_text())["flags"])

    def set(self, name: str, value: bool, updated_by: str) -> None:
        """Set one flag, keeping the others."""
        flags = self.read()
        flags[name] = value
        self.write(flags, updated_by)

    def write(self, flags: dict[str, bool], updated_by: str) -> None:
        """Replace all flags. Writes to a temp file first so readers never see a partial file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "flags": flags,
            "updated_by": updated_by,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.chmod(0o644)
        tmp.replace(self.path)
