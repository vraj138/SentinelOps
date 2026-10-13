"""Tests for the changes and runbooks tool servers."""

import json
from pathlib import Path

import pytest

from mcp_servers.changes import ChangesBackend
from mcp_servers.changes import build_server as build_changes
from mcp_servers.common import ToolError
from mcp_servers.runbooks import RunbookLibrary
from mcp_servers.runbooks import build_server as build_runbooks
from sentinelops.config import Settings
from sentinelops.stack.deploy_repo import DeployRepo
from sentinelops.stack.flags import FlagFile
from tests.conftest import CallTool, text_of


@pytest.fixture
def changes(tmp_path: Path) -> ChangesBackend:
    repo = DeployRepo(tmp_path / "repo")
    repo.init()
    repo.deploy("payments", {"PAYMENTS_API_KEY": "new-secret"}, "Rotate key", ("dana", "d@x"))
    FlagFile(tmp_path / "flags.json").write({"checkout_v2_pricing": True}, updated_by="growth")
    return ChangesBackend(repo.path, tmp_path / "flags.json")


def test_list_deploys_newest_first(changes: ChangesBackend, call_tool: CallTool) -> None:
    data = json.loads(text_of(call_tool(build_changes(changes), "list_deploys", {})))

    first = data["deploys"][0]
    assert first["message"] == "payments 1.9.1: Rotate key"
    assert first["author"] == "dana"
    assert first["files"] == ["services/payments.env"]
    assert len(data["deploys"]) == 2


def test_show_deploy_redacts_secrets(changes: ChangesBackend, call_tool: CallTool) -> None:
    sha = changes.deploys(60, 1)[0]["sha"]

    text = text_of(call_tool(build_changes(changes), "show_deploy", {"sha": sha}))

    assert "PAYMENTS_API_KEY=[redacted]" in text
    assert "new-secret" not in text and "demo-provider-key" not in text


@pytest.mark.parametrize("sha", ["HEAD", "--all", "abc; rm -rf /", "../../etc"])
def test_show_deploy_accepts_only_hex_shas(
    changes: ChangesBackend, call_tool: CallTool, sha: str
) -> None:
    result = call_tool(build_changes(changes), "show_deploy", {"sha": sha})

    assert result.is_error
    assert "invalid commit sha" in text_of(result)


def test_feature_flags(changes: ChangesBackend, call_tool: CallTool) -> None:
    data = json.loads(text_of(call_tool(build_changes(changes), "get_feature_flags", {})))

    assert data["flags"] == {"checkout_v2_pricing": True}
    assert data["updated_by"] == "growth"


def test_missing_deploy_repo_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="deploy repo not found"):
        ChangesBackend(tmp_path / "nope", tmp_path / "flags.json").deploys(60, 5)


def test_changes_server_cannot_see_main_repo() -> None:
    assert Settings().deploy_repo_path.parts[-2:] == ("storage", "deploy-repo")


@pytest.mark.parametrize(
    ("query", "best"),
    [
        ("HighMemoryUsage OOM restart", "memory-growth"),
        ("p95 latency slow dependency", "high-latency"),
        ("too many clients connection slots", "database-connections"),
        ("ServiceDown restarting crash", "crash-loop"),
        ("5xx HighErrorRate after deploy", "high-error-rate"),
        ("flag changed", "feature-flags"),
    ],
)
def test_runbook_search_finds_the_right_runbook(query: str, best: str) -> None:
    assert RunbookLibrary(Settings().runbooks_dir).search(query)[0]["name"] == best


def test_runbooks_do_not_mention_faults_or_scenarios() -> None:
    for name, text in RunbookLibrary(Settings().runbooks_dir).all().items():
        for word in ("fault", "inject", "scenario", "red herring", "ground truth"):
            assert word not in text.lower(), f"{name} mentions {word!r}"


def test_read_unknown_runbook_lists_available(call_tool: CallTool) -> None:
    server = build_runbooks(RunbookLibrary(Settings().runbooks_dir))

    result = call_tool(server, "read_runbook", {"name": "nope"})

    assert result.is_error
    assert "memory-growth" in text_of(result)


def test_empty_search_is_an_error() -> None:
    with pytest.raises(ToolError, match="no searchable words"):
        RunbookLibrary(Settings().runbooks_dir).search("the and of")
