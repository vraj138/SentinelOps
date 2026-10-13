"""Tests for faults.environment with a real temp deploy repo and a fake container runtime."""

from pathlib import Path

import pytest

from faults import registry
from faults.deploy_repo import BASE_RELEASES, DeployRepo
from faults.environment import (
    ContainerRuntimeError,
    Environment,
    FaultAlreadyActive,
    tag_from_image,
)
from faults.flags import BASE_FLAGS, FlagFile
from faults.provider import ProviderFile


class FakeRuntime:
    def __init__(self, fail_on_call: int | None = None) -> None:
        self.recreated: list[str] = []
        self.fail_on_call = fail_on_call

    def recreate(self, service: str) -> None:
        if self.fail_on_call is not None and len(self.recreated) + 1 == self.fail_on_call:
            raise RuntimeError(f"docker failed for {service}")
        self.recreated.append(service)


def make_env(tmp_path: Path, runtime: FakeRuntime | None = None) -> Environment:
    repo, flags = DeployRepo(tmp_path / "repo"), FlagFile(tmp_path / "flags.json")
    provider = ProviderFile(tmp_path / "provider.json")
    repo.init()
    flags.init()
    provider.init()
    return Environment(
        repo=repo,
        flags=flags,
        provider=provider,
        runtime=runtime or FakeRuntime(),
        active_file=tmp_path / "active.json",
        sleep=lambda seconds: None,
    )


def quiet(_: str) -> None:
    pass


def test_inject_deploys_and_records_every_step(tmp_path: Path) -> None:
    env = make_env(tmp_path)

    record = env.inject(registry.get("bad_deploy/c"), log=quiet)

    assert env.repo.read("payments")["PAYMENTS_FRAUD_CHECK_RATE"] == "0.3"
    assert env.repo.read("gateway")["APP_VERSION"] == "1.4.1"
    assert env.runtime.recreated == ["payments", "gateway"]  # type: ignore[attr-defined]
    assert [c.red_herring for c in record.changes] == [False, True]
    assert env.active() == record


def test_reset_restores_baseline_and_clears_active(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.inject(registry.get("bad_deploy/c"), log=quiet)

    undone = env.reset(log=quiet)

    assert undone is not None and undone.scenario_id == "bad_deploy/c"
    for service, settings in BASE_RELEASES.items():
        assert env.repo.read(service) == settings
    assert env.runtime.recreated == ["payments", "gateway", "gateway", "payments"]  # type: ignore[attr-defined]
    assert env.active() is None


def test_flag_scenario_flips_and_resets_flag(tmp_path: Path) -> None:
    env = make_env(tmp_path)

    env.inject(registry.get("bad_feature_flag/b"), log=quiet)
    assert env.flags.read()["checkout_v2_pricing"] is True

    env.reset(log=quiet)
    assert env.flags.read() == BASE_FLAGS
    assert env.repo.read("orders") == BASE_RELEASES["orders"]


def test_cannot_inject_while_a_fault_is_active(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.inject(registry.get("bad_feature_flag/a"), log=quiet)

    with pytest.raises(FaultAlreadyActive, match="bad_feature_flag/a is still active"):
        env.inject(registry.get("bad_deploy/a"), log=quiet)


def test_half_finished_injection_can_still_be_reset(tmp_path: Path) -> None:
    env = make_env(tmp_path, FakeRuntime(fail_on_call=2))

    with pytest.raises(RuntimeError, match="docker failed for gateway"):
        env.inject(registry.get("bad_deploy/c"), log=quiet)

    active = env.active()
    assert active is not None and len(active.changes) == 1
    env.runtime = FakeRuntime()
    env.reset(log=quiet)
    assert env.repo.read("payments") == BASE_RELEASES["payments"]


def test_reset_with_nothing_active_returns_none(tmp_path: Path) -> None:
    assert make_env(tmp_path).reset(log=quiet) is None


def test_provider_slowdown_changes_no_deploy_history_and_resets(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    head_before = env.repo.head()

    env.inject(registry.get("slow_dependency/a"), log=quiet)
    assert env.provider.latency_ms() == 1500
    assert env.repo.head() == head_before
    assert env.runtime.recreated == []  # type: ignore[attr-defined]

    env.reset(log=quiet)
    assert env.provider.latency_ms() == 0


def test_missing_secret_removes_key_and_reset_restores_it(tmp_path: Path) -> None:
    env = make_env(tmp_path)

    env.inject(registry.get("missing_secret/a"), log=quiet)
    assert "PAYMENTS_API_KEY" not in env.repo.read("payments")

    env.reset(log=quiet)
    assert env.repo.read("payments") == BASE_RELEASES["payments"]


def test_tag_from_image() -> None:
    assert tag_from_image("sentinelops-demo:4078549") == "4078549"
    assert tag_from_image("localhost:5000/sentinelops-demo:abc") == "abc"
    with pytest.raises(ContainerRuntimeError, match="has no tag"):
        tag_from_image("localhost:5000/sentinelops-demo")
