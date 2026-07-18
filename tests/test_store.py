"""Store: findings lifecycle, metrics, key/value state, and runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models import Finding, MetricSample
from app.store import Store


@pytest.fixture
async def store(tmp_path: Path):
    s = Store(tmp_path / "seo.db")
    await s.connect()
    yield s
    await s.close()


def _finding(check: str = "title", url: str = "https://h/", severity: str = "error") -> Finding:
    return Finding(check=check, severity=severity, message=f"{check} missing", url=url)  # type: ignore[arg-type]


async def test_connect_is_required(tmp_path: Path) -> None:
    s = Store(tmp_path / "x.db")
    with pytest.raises(RuntimeError):
        _ = s.db


async def test_upsert_findings_dedupes_and_reopens(store: Store) -> None:
    f = _finding()
    new, seen = await store.upsert_findings([f])
    assert (new, seen) == (1, 0)
    new, seen = await store.upsert_findings([f])
    assert (new, seen) == (0, 1)
    assert len(await store.active_findings()) == 1

    # Disappears from a run → resolved; reappears → reopened.
    resolved = await store.resolve_stale("audit", set())
    assert resolved == 1
    assert await store.active_findings() == []
    await store.upsert_findings([f])
    active = await store.active_findings()
    assert len(active) == 1
    assert active[0].fingerprint == f.fingerprint
    assert active[0].evidence == {}


async def test_resolve_stale_only_touches_named_source(store: Store) -> None:
    audit = _finding("title")
    gsc = Finding(check="gsc_clicks_drop", severity="warning", message="drop", source="gsc")
    await store.upsert_findings([audit, gsc])
    resolved = await store.resolve_stale("audit", set())
    assert resolved == 1
    remaining = await store.active_findings()
    assert [f.check for f in remaining] == ["gsc_clicks_drop"]


async def test_metrics_roundtrip_latest_wins(store: Store) -> None:
    await store.add_metrics([MetricSample(source="gsc", metric="clicks", key="28d", value=10)])
    await store.add_metrics([MetricSample(source="gsc", metric="clicks", key="28d", value=12)])
    assert await store.latest_metric("gsc", "clicks", "28d") == 12
    assert await store.latest_metric("gsc", "clicks", "missing") is None


async def test_kv_upsert(store: Store) -> None:
    assert await store.get_kv("sitemap") is None
    await store.set_kv("sitemap", "abc")
    await store.set_kv("sitemap", "def")
    assert await store.get_kv("sitemap") == "def"


async def test_runs_log(store: Store) -> None:
    rid = await store.record_run("audit", ok=True, summary="s", started_at="a", finished_at="b")
    assert rid > 0
    runs = await store.last_runs(5)
    assert runs[0]["kind"] == "audit"
    assert runs[0]["ok"] == 1
