"""Metrics tool server: read-only PromQL queries and firing alerts from Prometheus."""

import time
from typing import Any, Protocol

import httpx
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from mcp_servers.common import ToolError, to_result
from sentinelops.config import Settings

MAX_SERIES = 20
MAX_RANGE_MINUTES = 60
MAX_POINTS_PER_SERIES = 30
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


class MetricsBackend(Protocol):
    def query(self, promql: str) -> list[dict[str, Any]]: ...
    def query_range(
        self, promql: str, start: float, end: float, step: float
    ) -> list[dict[str, Any]]: ...
    def alerts(self) -> list[dict[str, Any]]: ...


class PrometheusBackend:
    """Talks to the Prometheus HTTP API. Any failure raises ToolError with the reason."""

    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(base_url=base_url, timeout=10.0)

    def query(self, promql: str) -> list[dict[str, Any]]:
        return self._get("/api/v1/query", {"query": promql})["result"]

    def query_range(
        self, promql: str, start: float, end: float, step: float
    ) -> list[dict[str, Any]]:
        params = {"query": promql, "start": start, "end": end, "step": step}
        return self._get("/api/v1/query_range", params)["result"]

    def alerts(self) -> list[dict[str, Any]]:
        return self._get("/api/v1/alerts", {})["alerts"]

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ToolError(f"Prometheus is unreachable: {exc!r}") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise ToolError(f"Prometheus returned non-JSON ({response.status_code})") from exc
        if body.get("status") != "success":
            raise ToolError(f"Prometheus error: {body.get('errorType')}: {body.get('error')}")
        return body["data"]


def _series(result: list[dict[str, Any]]) -> dict[str, Any]:
    """Trim a PromQL result to a bounded number of series and points."""
    series = []
    for item in result[:MAX_SERIES]:
        entry: dict[str, Any] = {"labels": item.get("metric", {})}
        if "value" in item:
            entry["value"] = item["value"][1]
            entry["at"] = item["value"][0]
        else:
            values = item.get("values", [])
            stride = max(1, -(-len(values) // MAX_POINTS_PER_SERIES))
            entry["points"] = [[t, v] for t, v in values[::stride]]
        series.append(entry)
    return {"series": series, "series_total": len(result), "series_shown": len(series)}


def build_server(backend: MetricsBackend) -> MCPServer:
    """The metrics MCP server, backed by `backend`."""
    server = MCPServer(
        "metrics",
        instructions=(
            "Read-only Prometheus access. Services expose http_requests_total{service,route,"
            "status}, http_request_duration_seconds_bucket, app_build_info{service,version}, "
            "app_start_time_seconds, process_resident_memory_bytes; orders also exposes "
            'db_connections_open and receipt_cache_items. `up{job="demo"}` shows scrape health.'
        ),
    )

    @server.tool(
        description="Run an instant PromQL query. Returns up to 20 series.", annotations=READ_ONLY
    )
    def query_metrics(promql: str) -> str:
        return to_result({"query": promql, **_series(backend.query(promql))})

    @server.tool(
        description=(
            "Run a PromQL range query over the last `minutes` (max 60), sampled every "
            "`step_seconds` (min 5). Returns up to 20 series of up to 30 points each."
        ),
        annotations=READ_ONLY,
    )
    def query_metrics_range(promql: str, minutes: int = 15, step_seconds: int = 30) -> str:
        if not 1 <= minutes <= MAX_RANGE_MINUTES:
            raise ToolError(f"minutes must be between 1 and {MAX_RANGE_MINUTES}")
        end = time.time()
        result = backend.query_range(promql, end - minutes * 60, end, max(step_seconds, 5))
        return to_result({"query": promql, "minutes": minutes, **_series(result)})

    @server.tool(
        description="List current Prometheus alerts (pending and firing).", annotations=READ_ONLY
    )
    def list_alerts() -> str:
        alerts = [
            {
                "name": a["labels"].get("alertname"),
                "service": a["labels"].get("service"),
                "state": a.get("state"),
                "active_since": a.get("activeAt"),
                "summary": a.get("annotations", {}).get("summary"),
                "severity": a["labels"].get("severity"),
            }
            for a in backend.alerts()
        ]
        return to_result({"alerts": alerts, "count": len(alerts)})

    return server


if __name__ == "__main__":
    build_server(PrometheusBackend(Settings().prometheus_url)).run()
