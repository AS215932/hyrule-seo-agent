"""FastAPI app: /health + /metrics, lifespan-managed store/client/scheduler."""

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
from app.pipeline import Deps
from app.scheduler import Scheduler
from app.store import Store

structlog.configure(
    processors=[structlog.processors.TimeStamper(fmt="iso"), structlog.processors.JSONRenderer()]
)
log = structlog.get_logger()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    store = Store(Path(settings.data_dir) / "seo.db")
    await store.connect()
    client = httpx.AsyncClient(
        timeout=settings.crawl_timeout_s, headers={"User-Agent": settings.user_agent}
    )
    deps = Deps(settings=settings, store=store, client=client)
    app.state.deps = deps
    scheduler = Scheduler(deps)
    if settings.scheduler_enabled:
        scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()
        await client.aclose()
        await store.close()


app = FastAPI(title="seo-agent", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    deps: Deps = app.state.deps
    findings = await deps.store.active_findings()
    by_severity: dict[str, int] = {}
    for f in findings:
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
    for severity in ("error", "warning", "info"):
        ACTIVE_FINDINGS.labels(severity=severity).set(by_severity.get(severity, 0))
    return {
        "status": "ok",
        "environment": settings.environment,
        "dry_run": settings.dry_run,
        "scheduler_enabled": settings.scheduler_enabled,
        "active_findings": by_severity,
        "last_runs": await deps.store.last_runs(5),
    }


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest().decode("utf-8"), media_type=CONTENT_TYPE_LATEST)


def main() -> None:  # pragma: no cover - thin uvicorn shim
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
