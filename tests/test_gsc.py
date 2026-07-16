"""GSCCollector: disabled without credentials, windowed totals + page rows on
the happy path (token monkeypatched), [] on API failure."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import httpx
import pytest
import respx

from app.collectors.gsc import GSCCollector

SITE = "sc-domain:hyrule.host"
ENDPOINT = "https://searchconsole.googleapis.com/webmasters/v3/sites/sc-domain%3Ahyrule.host/searchAnalytics/query"


async def _fake_token(self: GSCCollector) -> str:
    return "tok"


def _collector(tmp_path: Path) -> GSCCollector:
    credentials = tmp_path / "sa.json"
    credentials.write_text("{}")
    return GSCCollector(site_url=SITE, credentials_path=str(credentials))


async def test_disabled_without_credentials_path() -> None:
    collector = GSCCollector(site_url=SITE, credentials_path="")
    assert collector.enabled is False
    async with httpx.AsyncClient() as client:
        assert await collector.collect(client) == []


def test_disabled_when_credentials_file_missing(tmp_path: Path) -> None:
    assert GSCCollector(site_url=SITE, credentials_path=str(tmp_path / "nope.json")).enabled is False


async def test_collect_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GSCCollector, "_token", _fake_token)
    collector = _collector(tmp_path)
    assert collector.enabled is True

    current_end = datetime.date.today() - datetime.timedelta(days=3)
    current_start = current_end - datetime.timedelta(days=27)

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("dimensions") == ["page"]:
            rows = [
                {"keys": ["https://hyrule.host/"], "clicks": 30},
                {"keys": ["https://hyrule.host/domains"], "clicks": 12},
            ]
        elif body["startDate"] == current_start.isoformat():
            rows = [{"clicks": 100, "impressions": 2000, "ctr": 0.05, "position": 7.5}]
        else:
            rows = [{"clicks": 80, "impressions": 1600, "ctr": 0.04, "position": 9.0}]
        return httpx.Response(200, json={"rows": rows})

    with respx.mock(assert_all_called=False) as router:
        route = router.post(ENDPOINT).mock(side_effect=respond)
        async with httpx.AsyncClient() as client:
            samples = await collector.collect(client)

    assert route.call_count == 3
    for call in route.calls:
        assert call.request.headers["authorization"] == "Bearer tok"
        assert json.loads(call.request.content)["dataState"] == "final"
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert bodies[0]["startDate"] == current_start.isoformat()
    assert bodies[0]["endDate"] == current_end.isoformat()
    assert bodies[1]["endDate"] == (current_start - datetime.timedelta(days=1)).isoformat()
    assert bodies[2]["dimensions"] == ["page"]
    assert bodies[2]["rowLimit"] == 10

    assert all(sample.source == "gsc" for sample in samples)
    values = {(sample.metric, sample.key): sample.value for sample in samples}
    assert values[("clicks", "28d")] == 100
    assert values[("impressions", "28d")] == 2000
    assert values[("ctr", "28d")] == 0.05
    assert values[("position", "28d")] == 7.5
    assert values[("clicks", "28d_prev")] == 80
    assert values[("ctr", "28d_prev")] == 0.04
    assert values[("position", "28d_prev")] == 9.0
    assert values[("clicks", "https://hyrule.host/")] == 30
    assert values[("clicks", "https://hyrule.host/domains")] == 12
    assert len(samples) == 10


async def test_collect_returns_empty_on_http_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GSCCollector, "_token", _fake_token)
    collector = _collector(tmp_path)

    with respx.mock(assert_all_called=False) as router:
        router.post(ENDPOINT).respond(500)
        async with httpx.AsyncClient() as client:
            assert await collector.collect(client) == []
