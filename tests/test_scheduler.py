"""Scheduler: job table, cycle execution, resilience, clean stop."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

import app.scheduler as scheduler_mod
from app.config import Settings
from app.pipeline import Deps, PhaseOutcome
from app.scheduler import Scheduler
from app.store import Store


@pytest.fixture
async def deps(tmp_path: Path):
    settings = Settings(
        data_dir=str(tmp_path / "data"),
        audit_interval_s=0,
        metrics_interval_s=3600,
        indexnow_interval_s=3600,
        report_interval_s=3600,
    )
    store = Store(tmp_path / "data" / "seo.db")
    await store.connect()
    async with httpx.AsyncClient() as client:
        yield Deps(settings=settings, store=store, client=client)
    await store.close()


def test_jobs_table_matches_settings(deps: Deps) -> None:
    jobs = Scheduler(deps).jobs()
    assert [j[0] for j in jobs] == ["audit", "metrics", "indexnow", "report"]
    audit = jobs[0]
    assert audit[2] == 0 and audit[3] == 0  # interval 0 → immediate start
    assert jobs[1][3] == 60  # delay bounded by min(base, interval)


async def test_scheduler_runs_cycles_and_survives_failures(deps: Deps, monkeypatch) -> None:
    calls = {"audit": 0}

    async def fake_audit(d: Deps) -> PhaseOutcome:
        calls["audit"] += 1
        if calls["audit"] == 1:
            raise RuntimeError("first cycle explodes")
        return PhaseOutcome(kind="audit", ok=True, summary="", stats={})

    monkeypatch.setattr(scheduler_mod, "run_audit", fake_audit)
    sched = Scheduler(deps)
    sched.start()
    for _ in range(200):
        if calls["audit"] >= 2:
            break
        await asyncio.sleep(0.01)
    await sched.stop()
    # First cycle raised, loop kept going and ran again.
    assert calls["audit"] >= 2


async def test_stop_cancels_pending_tasks(deps: Deps) -> None:
    sched = Scheduler(deps)
    sched.start()
    await asyncio.sleep(0)
    await sched.stop()
    assert sched._tasks == []
