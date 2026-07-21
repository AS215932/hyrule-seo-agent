"""IndexNow: hash seeding, change detection, ping payload."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.actions import indexnow
from app.config import Settings
from app.store import Store

_SITEMAP_V1 = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://hyrule.host/</loc></url></urlset>"
)
_SITEMAP_V2 = _SITEMAP_V1.replace("</urlset>", "<url><loc>https://hyrule.host/new</loc></url></urlset>")


@pytest.fixture
async def store(tmp_path: Path):
    s = Store(tmp_path / "seo.db")
    await s.connect()
    yield s
    await s.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=str(tmp_path), indexnow_key="k" * 32)


async def test_disabled_without_key(store: Store, tmp_path: Path) -> None:
    async with httpx.AsyncClient() as client:
        result = await indexnow.ping_if_changed(client, store, Settings(data_dir=str(tmp_path)))
    assert result.status == "manual_required"


async def test_first_observation_seeds_without_ping(store: Store, settings: Settings) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get("https://hyrule.host/sitemap.xml").respond(200, text=_SITEMAP_V1)
        ping = router.post("https://api.indexnow.org/indexnow")
        async with httpx.AsyncClient() as client:
            result = await indexnow.ping_if_changed(client, store, settings)
    assert result.status == "seeded"
    assert not ping.called
    assert await store.get_kv("sitemap_sha256") is not None


async def test_change_triggers_ping_with_key_location(store: Store, settings: Settings) -> None:
    with respx.mock() as router:
        router.get("https://hyrule.host/sitemap.xml").respond(200, text=_SITEMAP_V1)
        async with httpx.AsyncClient() as client:
            await indexnow.ping_if_changed(client, store, settings)  # seed
    with respx.mock(assert_all_called=True) as router:
        router.get("https://hyrule.host/sitemap.xml").respond(200, text=_SITEMAP_V2)
        ping = router.post("https://api.indexnow.org/indexnow").respond(200)
        async with httpx.AsyncClient() as client:
            result = await indexnow.ping_if_changed(client, store, settings)
    assert result.status == "submitted"
    assert result.pinged is True
    payload = json.loads(ping.calls[0].request.content)
    assert payload["host"] == "hyrule.host"
    assert payload["key"] == "k" * 32
    assert payload["keyLocation"] == "https://hyrule.host/indexnow.txt"
    assert "https://hyrule.host/new" in payload["urlList"]


async def test_sitemap_redirect_is_followed(store: Store, settings: Settings) -> None:
    canonical = "https://hyrule.host/sitemaps/current.xml"
    with respx.mock(assert_all_called=True) as router:
        router.get("https://hyrule.host/sitemap.xml").respond(302, headers={"Location": canonical})
        router.get(canonical).respond(200, text=_SITEMAP_V1)
        async with httpx.AsyncClient() as client:
            result = await indexnow.ping_if_changed(client, store, settings)

    assert result.status == "seeded"
    assert await store.get_kv("sitemap_sha256") is not None


async def test_unchanged_sitemap_is_silent(store: Store, settings: Settings) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get("https://hyrule.host/sitemap.xml").respond(200, text=_SITEMAP_V1)
        ping = router.post("https://api.indexnow.org/indexnow")
        async with httpx.AsyncClient() as client:
            await indexnow.ping_if_changed(client, store, settings)
            result = await indexnow.ping_if_changed(client, store, settings)
    assert result.status == "unchanged"
    assert not ping.called


async def test_rejected_ping_keeps_old_hash_for_retry(store: Store, settings: Settings) -> None:
    with respx.mock() as router:
        router.get("https://hyrule.host/sitemap.xml").respond(200, text=_SITEMAP_V1)
        async with httpx.AsyncClient() as client:
            await indexnow.ping_if_changed(client, store, settings)
    seeded = await store.get_kv("sitemap_sha256")
    with respx.mock() as router:
        router.get("https://hyrule.host/sitemap.xml").respond(200, text=_SITEMAP_V2)
        router.post("https://api.indexnow.org/indexnow").respond(429)
        async with httpx.AsyncClient() as client:
            result = await indexnow.ping_if_changed(client, store, settings)
    assert result.status == "failed"
    # Hash not advanced → the change is retried next cycle.
    assert await store.get_kv("sitemap_sha256") == seeded


async def test_sitemap_fetch_failure_is_reported(store: Store, settings: Settings) -> None:
    with respx.mock() as router:
        router.get("https://hyrule.host/sitemap.xml").mock(side_effect=httpx.ConnectError("down"))
        async with httpx.AsyncClient() as client:
            result = await indexnow.ping_if_changed(client, store, settings)
    assert result.status == "failed"


async def test_invalid_sitemap_is_reported(store: Store, settings: Settings) -> None:
    with respx.mock() as router:
        router.get("https://hyrule.host/sitemap.xml").respond(200, text="not xml")
        async with httpx.AsyncClient() as client:
            result = await indexnow.ping_if_changed(client, store, settings)
    assert result.status == "failed"


def test_locs_tolerates_bad_xml() -> None:
    assert indexnow._locs("not xml") == []
    assert indexnow._locs(_SITEMAP_V1) == ["https://hyrule.host/"]
