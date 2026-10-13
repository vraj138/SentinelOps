"""Tests for the metrics tool server and its Prometheus backend."""

import json
from typing import Any

import httpx
import pytest

from mcp_servers.common import ToolError
from mcp_servers.metrics import PrometheusBackend, build_server
from tests.conftest import CallTool, text_of


def prometheus(handler: Any) -> PrometheusBackend:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://prom")
    return PrometheusBackend("http://prom", client=client)


def ok(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": data})


def test_backend_unreachable_raises_tool_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ToolError, match="Prometheus is unreachable"):
        prometheus(refuse).query("up")


def test_backend_query_error_is_reported() -> None:
    body = {"status": "error", "errorType": "bad_data", "error": "parse error"}
    backend = prometheus(lambda r: httpx.Response(400, json=body))

    with pytest.raises(ToolError, match="bad_data: parse error"):
        backend.query("rate((")


def test_backend_non_json_is_reported() -> None:
    with pytest.raises(ToolError, match="non-JSON"):
        prometheus(lambda r: httpx.Response(502, text="Bad Gateway")).query("up")


def test_query_caps_series(call_tool: CallTool) -> None:
    result = [{"metric": {"i": str(i)}, "value": [1.0, "1"]} for i in range(30)]
    server = build_server(prometheus(lambda r: ok({"result": result})))

    data = json.loads(text_of(call_tool(server, "query_metrics", {"promql": "up"})))

    assert data["series_total"] == 30
    assert data["series_shown"] == 20


def test_range_query_downsamples_points(call_tool: CallTool) -> None:
    values = [[float(t), "1"] for t in range(120)]
    server = build_server(prometheus(lambda r: ok({"result": [{"metric": {}, "values": values}]})))

    result = call_tool(server, "query_metrics_range", {"promql": "up", "minutes": 10})

    assert len(json.loads(text_of(result))["series"][0]["points"]) <= 30


def test_range_query_rejects_long_windows(call_tool: CallTool) -> None:
    server = build_server(prometheus(lambda r: ok({"result": []})))

    result = call_tool(server, "query_metrics_range", {"promql": "up", "minutes": 600})

    assert result.is_error


def test_list_alerts_summarizes(call_tool: CallTool) -> None:
    alert = {
        "labels": {"alertname": "HighErrorRate", "service": "payments", "severity": "critical"},
        "annotations": {"summary": "payments 5xx rate is 30%"},
        "state": "firing",
        "activeAt": "2026-10-13T20:00:00Z",
    }
    server = build_server(prometheus(lambda r: ok({"alerts": [alert]})))

    data = json.loads(text_of(call_tool(server, "list_alerts", {})))

    assert data["alerts"][0]["name"] == "HighErrorRate"
    assert data["alerts"][0]["service"] == "payments"


def test_prometheus_down_reaches_the_model_as_an_error(call_tool: CallTool) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = call_tool(build_server(prometheus(refuse)), "list_alerts", {})

    assert result.is_error
    assert "Prometheus is unreachable" in text_of(result)
