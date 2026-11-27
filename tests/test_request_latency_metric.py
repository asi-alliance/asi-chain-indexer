from aiohttp.test_utils import TestClient, TestServer
from prometheus_client import REGISTRY
import pytest

from src.monitoring import MonitoringServer

METRIC = "indexer_http_request_duration_seconds"


def observed(endpoint, method, status):
    value = REGISTRY.get_sample_value(
        f"{METRIC}_count",
        {"endpoint": endpoint, "method": method, "status": status},
    )
    return value or 0.0


@pytest.mark.asyncio
async def test_requests_are_observed_under_their_route_template():
    before = {
        "health": observed("/health", "GET", "200"),
        "block": observed("/api/blocks/{block_number}", "GET", "400"),
        "unmatched": observed("unmatched", "GET", "404"),
        "method": observed("unmatched", "OTHER", "405"),
    }

    async with TestClient(TestServer(MonitoringServer(None).app)) as client:
        assert (await client.get("/health")).status == 200
        assert (await client.get("/health")).status == 200
        for value in ("not-a-number", "also-not-a-number"):
            assert (await client.get(f"/api/blocks/{value}")).status == 400
        assert (await client.get("/no/such/route/123")).status == 404
        assert (await client.request("PROPFIND", "/health")).status == 405

    assert observed("/health", "GET", "200") == before["health"] + 2
    # Two raw paths share one series: the label is the template, not the path.
    assert observed("/api/blocks/{block_number}", "GET", "400") == before["block"] + 2
    assert observed("unmatched", "GET", "404") == before["unmatched"] + 1
    assert observed("unmatched", "OTHER", "405") == before["method"] + 1
    assert REGISTRY.get_sample_value(
        f"{METRIC}_count",
        {"endpoint": "/api/blocks/not-a-number", "method": "GET", "status": "400"},
    ) is None


@pytest.mark.asyncio
async def test_unhandled_handler_errors_are_observed_as_500():
    monitor = MonitoringServer(None)

    async def broken(request):
        raise RuntimeError("boom")

    monitor.app.router.add_get("/test/broken", broken)
    before = observed("/test/broken", "GET", "500")
    async with TestClient(TestServer(monitor.app)) as client:
        assert (await client.get("/test/broken")).status == 500
    assert observed("/test/broken", "GET", "500") == before + 1


@pytest.mark.asyncio
async def test_metric_is_exposed_on_the_metrics_endpoint(monkeypatch):
    async def last_indexed():
        return 0

    monkeypatch.setattr("src.monitoring.db.get_last_indexed_block", last_indexed)
    async with TestClient(TestServer(MonitoringServer(None).app)) as client:
        await client.get("/health")
        body = await (await client.get("/metrics")).text()
    assert f'{METRIC}_count{{endpoint="/health",method="GET",status="200"}}' in body
