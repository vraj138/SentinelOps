"""Tests for faults.flags, and that the services' flag reader understands its file format."""

import json
from pathlib import Path

from demo_app.common import FeatureFlags
from faults.flags import BASE_FLAGS, FlagFile


def test_init_creates_base_flags_once(tmp_path: Path) -> None:
    flags = FlagFile(tmp_path / "flags" / "flags.json")

    assert flags.init() is True
    assert flags.init() is False
    assert flags.read() == BASE_FLAGS


def test_set_changes_one_flag_and_records_who(tmp_path: Path) -> None:
    flags = FlagFile(tmp_path / "flags.json")
    flags.write({"a": False, "b": False}, updated_by="init")

    flags.set("a", True, updated_by="fault:bad_flag")

    data = json.loads(flags.path.read_text())
    assert data["flags"] == {"a": True, "b": False}
    assert data["updated_by"] == "fault:bad_flag"
    assert "updated_at" in data


def test_services_read_what_flag_file_writes(tmp_path: Path) -> None:
    path = tmp_path / "flags.json"
    FlagFile(path).write({"checkout_v2_pricing": True}, updated_by="test")

    assert FeatureFlags(path, "gateway").enabled("checkout_v2_pricing") is True
