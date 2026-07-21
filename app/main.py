"""FastAPI app: health, metrics, and the lifespan-managed Beacon worker."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import structlog
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import settings
from app.metrics_registry import ACTIVE_FINDINGS
from app.managed import ManagedWorker
from app.pipeline import Deps
from app.scheduler import Scheduler
from app.store import Store

structlog.configure(processors=[structlog.processors.TimeStamper(fmt="iso"), structlog.processors.JSONRenderer()])
log = structlog.get_logger()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    store = Store(Path(settings.data_dir) / "seo.db")
    await store.connect()
    client = httpx.AsyncClient(timeout=settings.crawl_timeout_s, headers={"User-Agent": settings.user_agent})
    deps = Deps(settings=settings, store=store, client=client)
    app.state.deps = deps
    scheduler = Scheduler(deps)
    managed: ManagedWorker | None = None
    if settings.beacon_managed_mode:
        if not settings.beacon_configured:
            raise RuntimeError("Managed mode requires BEACON_CONTROL_PLANE_URL and BEACON_WORKER_TOKEN")
        managed = ManagedWorker(settings=settings, store=store, http=client)
        managed.start()
    elif settings.scheduler_enabled:
        scheduler.start()
    app.state.managed = managed
    try:
        yield
    finally:
        if managed is not None:
            await managed.stop()
        await scheduler.stop()
        await client.aclose()
        await store.close()


app = FastAPI(title="Hyrule Beacon Worker", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    deps: Deps = app.state.deps
    managed: ManagedWorker | None = app.state.managed
    if managed is not None:
        by_severity = dict(managed.finding_counts)
        last_runs: list[dict[str, Any]] = []
    else:
        findings = await deps.store.active_findings()
        by_severity = {}
        for finding in findings:
            by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1
        last_runs = await deps.store.last_runs(5)
    for severity in ("error", "warning", "info"):
        ACTIVE_FINDINGS.labels(severity=severity).set(by_severity.get(severity, 0))
    return {
        "status": "ok",
        "environment": settings.environment,
        "scheduler_enabled": settings.scheduler_enabled,
        "beacon_managed_mode": settings.beacon_managed_mode,
        "beacon_configured": settings.beacon_configured,
        "beacon_current_run": (app.state.managed.current_run_id if app.state.managed is not None else None),
        "beacon_last_error": (app.state.managed.last_error if app.state.managed is not None else None),
        "active_findings": by_severity,
        "last_runs": last_runs,
    }


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest().decode("utf-8"), media_type=CONTENT_TYPE_LATEST)


def main() -> None:  # pragma: no cover - thin uvicorn shim
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
