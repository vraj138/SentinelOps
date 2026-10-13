"""Tests for the runtime and logs tool servers and their docker backend (docker faked)."""

import json
import subprocess
import time
from pathlib import Path

import pytest

from mcp_servers import logs, runtime
from mcp_servers.common import ToolError
from mcp_servers.docker_backend import DockerBackend
from tests.conftest import CallTool, text_of

STATE = {
    "state": {
        "Status": "running",
        "Running": True,
        "Restarting": False,
        "ExitCode": 0,
        "OOMKilled": False,
        "StartedAt": "2026-10-13T20:00:00Z",
        "FinishedAt": "0001-01-01T00:00:00Z",
        "Health": {"Status": "healthy", "Log": [{"Output": "PASSWORD=leak"}]},
    },
    "image": "sentinelops-demo:abc",
    "restart_count": 2,
    "created": "2026-10-13T19:00:00Z",
}


def fake_runner(stdout: str = "", returncode: int = 0, stderr: str = ""):
    calls: list[list[str]] = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def backend(tmp_path: Path, run=None) -> DockerBackend:
    return DockerBackend(
        "docker", "demo-{service}-1", tmp_path / "events.jsonl", run or fake_runner()
    )


def test_status_returns_only_whitelisted_fields(tmp_path: Path) -> None:
    run = fake_runner(json.dumps(STATE))

    status = backend(tmp_path, run).status("orders")

    assert status["restart_count"] == 2
    assert status["health"] == "healthy"
    assert "leak" not in json.dumps(status)
    assert run.calls[0][:3] == ["docker", "inspect", "demo-orders-1"]  # type: ignore[attr-defined]


def test_docker_failure_raises_tool_error(tmp_path: Path) -> None:
    run = fake_runner(returncode=1, stderr="Cannot connect to the Docker daemon")

    with pytest.raises(ToolError, match="Cannot connect to the Docker daemon"):
        backend(tmp_path, run).status("orders")


def test_events_filter_by_container_time_and_action(tmp_path: Path) -> None:
    now = time.time()

    def event(name: str, action: str, age: float, **attrs: str) -> str:
        actor = {"Attributes": {"name": name, "image": "img", **attrs}}
        return json.dumps({"time": int(now - age), "Action": action, "Actor": actor})

    (tmp_path / "events.jsonl").write_text(
        "\n".join(
            [
                event("demo-orders-1", "oom", 60),
                event("demo-orders-1", "die", 59, exitCode="137"),
                event("demo-orders-1", "start", 58),
                event("demo-orders-1", "start", 7200),
                event("demo-payments-1", "die", 30, exitCode="0"),
                event("demo-orders-1", "exec_start", 10),
            ]
        )
    )

    events = backend(tmp_path).events("orders", since_minutes=30)

    assert [e["action"] for e in events] == ["oom", "die", "start"]
    assert events[1]["exitCode"] == "137"


def test_missing_event_log_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="event-recorder"):
        backend(tmp_path).events("orders", 30)


def test_list_containers_reports_per_service_errors(tmp_path: Path, call_tool: CallTool) -> None:
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if "demo-payments-1" in args:
            return subprocess.CompletedProcess(args, 1, "", "No such container")
        return subprocess.CompletedProcess(args, 0, json.dumps(STATE), "")

    result = call_tool(runtime.build_server(backend(tmp_path, run)), "list_containers", {})

    data = json.loads(text_of(result))
    assert len(data["containers"]) == 2
    assert "No such container" in data["errors"]["payments"]


def test_read_logs_filters_noise_and_redacts(tmp_path: Path, call_tool: CallTool) -> None:
    raw = "\n".join(
        [
            'INFO: 127.0.0.1 - "GET /health HTTP/1.1" 200 OK',
            'INFO: 1.2.3.4 - "GET /metrics HTTP/1.1" 200 OK',
            "ERROR payments call failed: 500",
            "WARNING connecting with PASSWORD=hunter2",
        ]
    )
    server = logs.build_server(backend(tmp_path, fake_runner(stdout=raw)))

    text = text_of(call_tool(server, "read_logs", {"service": "orders"}))

    assert "/health" not in text and "/metrics" not in text
    assert "payments call failed" in text
    assert "hunter2" not in text


def test_read_logs_contains_filter(tmp_path: Path, call_tool: CallTool) -> None:
    server = logs.build_server(backend(tmp_path, fake_runner(stdout="ERROR a\nINFO b\nerror c")))

    text = text_of(call_tool(server, "read_logs", {"service": "orders", "contains": "error"}))

    assert "2 of 2 matching lines" in text
    assert "INFO b" not in text


def test_read_logs_rejects_unknown_service(tmp_path: Path, call_tool: CallTool) -> None:
    result = call_tool(logs.build_server(backend(tmp_path)), "read_logs", {"service": "postgres"})

    assert result.is_error
