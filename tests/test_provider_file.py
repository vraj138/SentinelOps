"""Tests for faults.provider."""

from pathlib import Path

from demo_app.payments import ProviderConditions
from faults.provider import ProviderFile


def test_init_creates_healthy_provider_once(tmp_path: Path) -> None:
    provider = ProviderFile(tmp_path / "provider" / "conditions.json")

    assert provider.init() is True
    assert provider.init() is False
    assert provider.latency_ms() == 0


def test_payments_reads_what_provider_file_writes(tmp_path: Path) -> None:
    provider = ProviderFile(tmp_path / "conditions.json")
    provider.set_latency(1500)

    assert ProviderConditions(provider.path).latency_ms() == 1500
