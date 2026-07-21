"""Standalone collector phases persist evidence without a Git workflow."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

import app.pipeline as pipeline
from app.actions import indexnow
from app.config import Settings
from app.models import CrawlResult, Finding, MetricSample
from app.store import Store


@pytest.fixture
async def deps(tmp_path: Path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    store = Store(tmp_path / "data" / "seo.db")
    await store.connect()
    async with httpx.AsyncClient() as client:
        yield pipeline.Deps(settings=settings, store=store, client=client)
    await store.close()


def _finding() -> Finding:
    return Finding(
        check="meta_description",
        severity="warning",
        message="m",
        url="https://hyrule.host/",
        source="audit",
    )


async def test_run_audit_persists_and_resolves_findings(deps, monkeypatch) -> None:
    crawl = CrawlResult(base_url="https://hyrule.host", robots_txt_ok=True, sitemap_ok=True)

    async def fake_crawl(client, base_url, **kwargs):
        return crawl

    monkeypatch.setattr(pipeline, "crawl_site", fake_crawl)
    monkeypatch.setattr(pipeline, "audit_crawl", lambda value, site_base_url: [_finding()])
    first = await pipeline.run_audit(deps)
    assert first.ok
    assert first.stats["new"] == 1
    second = await pipeline.run_audit(deps)
    assert second.stats["seen"] == 1
    monkeypatch.setattr(pipeline, "audit_crawl", lambda value, site_base_url: [])
    third = await pipeline.run_audit(deps)
    assert third.stats["resolved"] == 1


async def test_run_audit_failure_is_recorded(deps, monkeypatch) -> None:
    async def boom(client, base_url, **kwargs):
        raise RuntimeError("crawler exploded")

    monkeypatch.setattr(pipeline, "crawl_site", boom)
    outcome = await pipeline.run_audit(deps)
    assert not outcome.ok
    assert "crawler exploded" in outcome.summary


async def test_run_metrics_with_everything_disabled(deps) -> None:
    outcome = await pipeline.run_metrics(deps)
    assert outcome.ok
    assert outcome.stats["samples"] == 0
    assert outcome.stats["sources"] == []


async def test_run_metrics_collects_and_derives_findings(deps, monkeypatch, tmp_path) -> None:
    credentials = tmp_path / "wif.json"
    credentials.write_text("{}")
    deps.settings = Settings(
        data_dir=deps.settings.data_dir,
        google_credentials_path=str(credentials),
        psi_api_key="psi-key",
        umami_base_url="https://analytics.hyrule.host",
        umami_api_token="token",
        umami_website_id="site-1",
    )

    class FakeGSC:
        def __init__(self, **kwargs):
            pass

        enabled = True

        async def collect(self, client):
            return [
                MetricSample(source="gsc", metric="clicks", key="28d", value=10),
                MetricSample(source="gsc", metric="clicks", key="28d_prev", value=100),
                MetricSample(source="gsc", metric="impressions", key="28d", value=1000),
                MetricSample(source="gsc", metric="impressions", key="28d_prev", value=1100),
            ]

    async def fake_psi(client, *, base_url, api_key, paths):
        return (
            [MetricSample(source="psi", metric="seo_score", key="/|mobile", value=0.85)],
            [
                Finding(
                    check="psi_seo_score",
                    severity="warning",
                    message="0.85",
                    source="psi",
                )
            ],
        )

    class FakeUmami:
        def __init__(self, **kwargs):
            pass

        enabled = True

        async def collect(self, client):
            return [MetricSample(source="umami", metric="pageviews", key="7d", value=42)]

    monkeypatch.setattr(pipeline, "GSCCollector", FakeGSC)
    monkeypatch.setattr(pipeline, "collect_psi", fake_psi)
    monkeypatch.setattr(pipeline, "UmamiCollector", FakeUmami)
    outcome = await pipeline.run_metrics(deps)
    assert outcome.ok
    assert outcome.stats["sources"] == ["gsc", "psi", "umami"]
    assert outcome.stats["samples"] == 6
    checks = {finding.check for finding in await deps.store.active_findings()}
    assert {"gsc_clicks_drop", "psi_seo_score"}.issubset(checks)


async def test_run_indexnow_delegates(deps, monkeypatch) -> None:
    async def fake_ping(client, store, settings):
        return indexnow.IndexNowResult("submitted")

    monkeypatch.setattr(indexnow, "ping_if_changed", fake_ping)
    outcome = await pipeline.run_indexnow(deps)
    assert outcome.ok
    assert outcome.stats["pinged"] is True
    assert outcome.stats["status"] == "submitted"
