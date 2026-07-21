"""Standalone evidence phases retained for local and operational spot checks.

Managed visibility work runs through ``app.graph``. These phases keep the
existing crawl, GSC, PSI, Umami, IndexNow, and reporting utilities available
without any Git workspace or draft-PR path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from app import agent_core_trace
from app.actions import indexnow
from app.audit.checks import audit_crawl
from app.audit.scoring import findings_from_metrics
from app.collectors.crawler import crawl_site
from app.collectors.gsc import GSCCollector
from app.collectors.psi import collect_psi
from app.collectors.umami import UmamiCollector
from app.config import Settings
from app.metrics_registry import LAST_RUN_TS, RUNS_TOTAL
from app.models import Finding, MetricSample
from app.report import send_weekly_report
from app.store import Store

log = structlog.get_logger()


@dataclass
class Deps:
    settings: Settings
    store: Store
    client: httpx.AsyncClient


@dataclass
class PhaseOutcome:
    kind: str
    ok: bool
    summary: str
    stats: dict[str, Any]


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _finish(
    deps: Deps,
    kind: str,
    *,
    started: str,
    ok: bool,
    summary: str,
    stats: dict[str, Any],
) -> PhaseOutcome:
    await deps.store.record_run(
        kind,
        ok=ok,
        summary=summary,
        started_at=started,
        finished_at=_now(),
    )
    agent_core_trace.emit_run_trace(
        kind,
        run_id=f"{kind}-{uuid.uuid4().hex[:10]}",
        ok=ok,
        summary=summary,
        stats=stats,
    )
    RUNS_TOTAL.labels(kind=kind, ok=str(ok).lower()).inc()
    LAST_RUN_TS.labels(kind=kind).set(datetime.now(UTC).timestamp())
    log.info("phase_finished", kind=kind, ok=ok, summary=summary, **stats)
    return PhaseOutcome(kind=kind, ok=ok, summary=summary, stats=stats)


async def run_audit(deps: Deps) -> PhaseOutcome:
    """Crawl the live site, run deterministic checks, and persist findings."""

    started = _now()
    settings = deps.settings
    try:
        crawl = await crawl_site(
            deps.client,
            settings.site_base_url,
            max_pages=settings.crawl_max_pages,
            max_depth=settings.crawl_max_depth,
            user_agent=settings.user_agent,
        )
        findings = audit_crawl(crawl, site_base_url=settings.site_base_url)
        new, seen = await deps.store.upsert_findings(findings)
        resolved = await deps.store.resolve_stale("audit", {finding.fingerprint for finding in findings})
        stats = {
            "pages": len(crawl.pages),
            "findings": len(findings),
            "new": new,
            "seen": seen,
            "resolved": resolved,
        }
        return await _finish(
            deps,
            "audit",
            started=started,
            ok=True,
            summary=f"{len(crawl.pages)} pages, {len(findings)} findings ({new} new)",
            stats=stats,
        )
    except Exception as exc:
        log.exception("audit_failed")
        return await _finish(
            deps,
            "audit",
            started=started,
            ok=False,
            summary=str(exc)[:200],
            stats={},
        )


async def run_metrics(deps: Deps) -> PhaseOutcome:
    """Pull GSC, PSI, and Umami samples and persist derived findings."""

    started = _now()
    settings = deps.settings
    try:
        samples: list[MetricSample] = []
        findings: list[Finding] = []
        sources: list[str] = []

        gsc = GSCCollector(
            site_url=settings.gsc_site_url,
            credentials_path=settings.google_credentials_path,
        )
        if gsc.enabled:
            gsc_samples = await gsc.collect(deps.client)
            samples.extend(gsc_samples)
            if gsc_samples:
                sources.append("gsc")
            previous = {
                ("gsc", "clicks", "28d_prev"): await deps.store.latest_metric("gsc", "clicks", "28d_prev"),
                ("gsc", "impressions", "28d_prev"): await deps.store.latest_metric("gsc", "impressions", "28d_prev"),
            }
            findings.extend(
                findings_from_metrics(
                    gsc_samples,
                    lambda source, metric, key: previous.get((source, metric, key)),
                )
            )

        if settings.psi_api_key:
            psi_samples, psi_findings = await collect_psi(
                deps.client,
                base_url=settings.site_base_url,
                api_key=settings.psi_api_key,
                paths=settings.psi_path_list,
            )
            samples.extend(psi_samples)
            findings.extend(psi_findings)
            if psi_samples:
                sources.append("psi")

        umami = UmamiCollector(
            base_url=settings.umami_base_url,
            api_token=settings.umami_api_token,
            website_id=settings.umami_website_id,
        )
        if umami.enabled:
            umami_samples = await umami.collect(deps.client)
            samples.extend(umami_samples)
            if umami_samples:
                sources.append("umami")

        stored = await deps.store.add_metrics(samples)
        if findings:
            await deps.store.upsert_findings(findings)
        for source in ("gsc", "psi"):
            if source in sources:
                await deps.store.resolve_stale(
                    source,
                    {finding.fingerprint for finding in findings if finding.source == source},
                )
        stats = {
            "samples": stored,
            "metric_findings": len(findings),
            "sources": sources,
        }
        return await _finish(
            deps,
            "metrics",
            started=started,
            ok=True,
            summary=f"{stored} samples from {', '.join(sources) or 'no sources'}",
            stats=stats,
        )
    except Exception as exc:
        log.exception("metrics_failed")
        return await _finish(
            deps,
            "metrics",
            started=started,
            ok=False,
            summary=str(exc)[:200],
            stats={},
        )


async def run_indexnow(deps: Deps) -> PhaseOutcome:
    started = _now()
    result = await indexnow.ping_if_changed(deps.client, deps.store, deps.settings)
    return await _finish(
        deps,
        "indexnow",
        started=started,
        ok=result.status != "failed",
        summary=result.reason or result.status,
        stats={"pinged": result.pinged, "status": result.status},
    )


async def run_report(deps: Deps) -> PhaseOutcome:
    started = _now()
    sent = await send_weekly_report(deps.client, deps.store, deps.settings)
    return await _finish(
        deps,
        "report",
        started=started,
        ok=True,
        summary="sent" if sent else "webhook unconfigured or rejected",
        stats={"sent": sent},
    )
