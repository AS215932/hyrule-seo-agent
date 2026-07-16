"""UmamiCollector: 7-day stats + top URLs, both payload value shapes,
disabled and HTTP failure both yield []."""

from __future__ import annotations

import httpx
import respx

from app.collectors.umami import UmamiCollector

BASE = "https://umami.test"
WEBSITE = "11111111-2222-3333-4444-555555555555"


def _collector() -> UmamiCollector:
    return UmamiCollector(base_url=BASE, api_token="tok", website_id=WEBSITE)


async def test_disabled_when_any_field_empty() -> None:
    collector = UmamiCollector(base_url=BASE, api_token="", website_id=WEBSITE)
    assert collector.enabled is False
    assert UmamiCollector(base_url="", api_token="tok", website_id=WEBSITE).enabled is False
    assert UmamiCollector(base_url=BASE, api_token="tok", website_id="").enabled is False
    async with httpx.AsyncClient() as client:
        assert await collector.collect(client) == []


async def test_collect_stats_and_top_urls() -> None:
    stats = {"pageviews": {"value": 42}, "visitors": 17, "bounces": {"value": 5}}
    top = [{"x": "/", "y": 30}, {"x": "/domains", "y": 12}]

    with respx.mock(assert_all_called=False) as router:
        stats_route = router.get(f"{BASE}/api/websites/{WEBSITE}/stats").respond(200, json=stats)
        metrics_route = router.get(f"{BASE}/api/websites/{WEBSITE}/metrics").respond(200, json=top)
        async with httpx.AsyncClient() as client:
            samples = await _collector().collect(client)

    assert stats_route.called is True
    stats_params = stats_route.calls[0].request.url.params
    assert stats_params["startAt"].isdigit() and stats_params["endAt"].isdigit()
    assert int(stats_params["endAt"]) - int(stats_params["startAt"]) == 7 * 24 * 60 * 60 * 1000
    assert stats_route.calls[0].request.headers["authorization"] == "Bearer tok"

    assert metrics_route.called is True
    metrics_params = metrics_route.calls[0].request.url.params
    assert metrics_params["type"] == "url"
    assert metrics_params["limit"] == "10"

    assert all(sample.source == "umami" for sample in samples)
    values = {(sample.metric, sample.key): sample.value for sample in samples}
    assert values[("pageviews", "7d")] == 42  # {"value": n} shape
    assert values[("visitors", "7d")] == 17  # bare int shape
    assert values[("bounces", "7d")] == 5
    assert values[("pageviews", "/")] == 30
    assert values[("pageviews", "/domains")] == 12
    assert len(samples) == 5


async def test_collect_returns_empty_on_http_error() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE}/api/websites/{WEBSITE}/stats").respond(500)
        router.get(f"{BASE}/api/websites/{WEBSITE}/metrics").respond(200, json=[])
        async with httpx.AsyncClient() as client:
            assert await _collector().collect(client) == []
