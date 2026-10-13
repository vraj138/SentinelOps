"""Tests for faults.deploy_repo, using a real git repo in a temp directory."""

import subprocess
from pathlib import Path

import pytest

from sentinelops.stack.deploy_repo import BASE_RELEASES, DeployRepo, DeployRepoError, bump_patch


def git_log(repo: DeployRepo) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo.path), "log", "--format=%an|%s"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


@pytest.fixture
def repo(tmp_path: Path) -> DeployRepo:
    repo = DeployRepo(tmp_path / "deploy-repo")
    repo.init()
    return repo


def test_init_writes_base_releases_and_one_commit(repo: DeployRepo) -> None:
    for service, settings in BASE_RELEASES.items():
        assert repo.read(service) == settings
    assert git_log(repo) == ["release-bot|Initial release of gateway, orders and payments"]


def test_init_is_idempotent(repo: DeployRepo) -> None:
    assert repo.init() is False
    assert len(git_log(repo)) == 1


def test_deploy_changes_settings_bumps_version_and_commits(repo: DeployRepo) -> None:
    sha = repo.deploy(
        "payments",
        {"PAYMENTS_FRAUD_CHECK_RATE": "0.3", "PAYMENTS_API_KEY": None},
        "Enable fraud checks",
        author=("dana", "dana@example.com"),
    )

    settings = repo.read("payments")
    assert settings["PAYMENTS_FRAUD_CHECK_RATE"] == "0.3"
    assert "PAYMENTS_API_KEY" not in settings
    assert settings["APP_VERSION"] == "1.9.1"
    assert sha == repo.head()
    assert git_log(repo)[0] == "dana|payments 1.9.1: Enable fraud checks"


def test_revert_restores_previous_release(repo: DeployRepo) -> None:
    before = repo.read("orders")
    sha = repo.deploy("orders", {"ORDERS_RECEIPT_CACHE_MAX_ITEMS": "0"}, "Cache all receipts")

    repo.revert(sha)

    assert repo.read("orders") == before
    assert git_log(repo)[0].startswith('release-bot|Revert "orders 2.1.1')


def test_deploy_after_revert_never_reuses_a_version(repo: DeployRepo) -> None:
    bad = repo.deploy("payments", {"PAYMENTS_FRAUD_CHECK_RATE": "1.0"}, "Bad release")
    repo.revert(bad)
    assert repo.read("payments")["APP_VERSION"] == "1.9.0"

    repo.deploy("payments", {}, "Next release")

    assert repo.read("payments")["APP_VERSION"] == "1.9.2"


def test_git_errors_are_raised_not_swallowed(repo: DeployRepo) -> None:
    with pytest.raises(DeployRepoError, match="revert"):
        repo.revert("0000000")


def test_bump_patch() -> None:
    assert bump_patch("1.9.0") == "1.9.1"
    assert bump_patch("2.10.9") == "2.10.10"
