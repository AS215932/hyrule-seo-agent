"""Report: summary assembly, embed rendering, webhook delivery."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from app.config import Settings
from app.models import Finding, MetricSample
from app.report import build_summary, render_discord_embed, send_weekly_report
from app.store import Store


@pytest.fixture
async def store(tmp_path: Path):
    s = Store(tmp_path / "seo.db")
    await s.connect()
    yield s
    await s.close()


async def _seed(store: Store) -> None:
    await store.upsert_findings(
        [
            Finding(check="broken_link", severity="error", message="404 /x", url="https://h/"),
            Finding(check="meta_description", severity="warning", message="short", url="https://h/"),
        ]
    )
    await store.add_metrics(
        [
            MetricSample(source="gsc", metric="clicks", key="28d", value=120),
            MetricSample(source="umami", metric="pageviews", key="7d", value=999),
        ]
    )
    await store.record_run("audit", ok=True, summary="s", started_at="a", finished_at="b")


async def test_build_summary_shape(store: Store) -> None:
    await _seed(store)
    summary = await build_summary(store)
    assert summary["findings_total"] == 2
    assert summary["by_severity"] == {"error": 1, "warning": 1, "info": 0}
    assert summary["gsc_clicks_28d"] == 120
    assert summary["umami_pageviews_7d"] == 999
    assert summary["psi_seo_score"] is None
    worst = summary["worst"]
    assert isinstance(worst, list) and worst[0].startswith("[error]")


async def test_render_embed_includes_metrics_and_worst(store: Store) -> None:
    await _seed(store)
    payload = render_discord_embed(await build_summary(store))
    embed = payload["embeds"][0]  # type: ignore[index]
    assert "seo-agent weekly summary" in embed["title"]
    assert "[error] broken_link" in embed["description"]
    names = [f["name"] for f in embed["fields"]]
    assert "Open findings" in names
    assert "GSC clicks (28d)" in names
    assert "PSI SEO score (/, mobile)" not in names  # None metrics omitted


def test_render_embed_empty_summary() -> None:
    payload = render_discord_embed(
        {"findings_total": 0, "by_severity": {}, "worst": [], "runs": []}
    )
    assert payload["embeds"][0]["description"] == "No open findings."  # type: ignore[index]


async def test_send_weekly_report_paths(store: Store, tmp_path: Path) -> None:
    async with httpx.AsyncClient() as client:
        # Unconfigured → False, no HTTP.
        assert not await send_weekly_report(client, store, Settings(data_dir=str(tmp_path)))
        configured = Settings(data_dir=str(tmp_path), discord_webhook_url="https://d.test/hook")
        with respx.mock() as router:
            router.post("https://d.test/hook").respond(204)
            assert await send_weekly_report(client, store, configured)
        with respx.mock() as router:
            router.post("https://d.test/hook").respond(400)
            assert not await send_weekly_report(client, store, configured)
        with respx.mock() as router:
            router.post("https://d.test/hook").mock(side_effect=httpx.ConnectError("down"))
            assert not await send_weekly_report(client, store, configured)
