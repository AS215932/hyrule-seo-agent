"""Pipeline phases shared by the scheduler and seoctl.

Each phase is a self-contained async function over ``Deps``; phases log a
run row, emit an agent-core trace, and never raise (the scheduler and CLI
rely on that so one bad cycle can't kill the process or a one-shot run).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from app import agent_core_trace
from app.actions import github, indexnow, workspace
from app.metrics_registry import LAST_RUN_TS, RUNS_TOTAL
from app.actions.drafter import draft_proposal, gather_context_files
from app.audit.agent_surface import audit_surface, bazaar_indexed_count
from app.audit.checks import audit_crawl
from app.audit.scoring import findings_from_metrics, rank_findings, top_findings
from app.collectors.crawler import crawl_site
from app.collectors.surface import fetch_surface
from app.collectors.gsc import GSCCollector
from app.collectors.psi import collect_psi
from app.collectors.umami import UmamiCollector
from app.config import Settings
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
    deps: Deps, kind: str, *, started: str, ok: bool, summary: str, stats: dict[str, Any]
) -> PhaseOutcome:
    await deps.store.record_run(kind, ok=ok, summary=summary, started_at=started, finished_at=_now())
    agent_core_trace.emit_run_trace(
        kind, run_id=f"{kind}-{uuid.uuid4().hex[:10]}", ok=ok, summary=summary, stats=stats
    )
    RUNS_TOTAL.labels(kind=kind, ok=str(ok).lower()).inc()
    LAST_RUN_TS.labels(kind=kind).set(datetime.now(UTC).timestamp())
    log.info("phase_finished", kind=kind, ok=ok, summary=summary, **stats)
    return PhaseOutcome(kind=kind, ok=ok, summary=summary, stats=stats)


async def run_audit(deps: Deps) -> PhaseOutcome:
    """Crawl the live site, run deterministic checks, persist findings."""
    started = _now()
    s = deps.settings
    try:
        crawl = await crawl_site(
            deps.client,
            s.site_base_url,
            max_pages=s.crawl_max_pages,
            max_depth=s.crawl_max_depth,
            user_agent=s.user_agent,
        )
        findings = audit_crawl(crawl, site_base_url=s.site_base_url)
        new, seen = await deps.store.upsert_findings(findings)
        resolved = await deps.store.resolve_stale("audit", {f.fingerprint for f in findings})
        stats = {
            "pages": len(crawl.pages),
            "findings": len(findings),
            "new": new,
            "seen": seen,
            "resolved": resolved,
        }
        return await _finish(
            deps, "audit", started=started, ok=True,
            summary=f"{len(crawl.pages)} pages, {len(findings)} findings ({new} new)", stats=stats,
        )
    except Exception as exc:  # defense in depth; collectors already swallow
        log.exception("audit_failed")
        return await _finish(
            deps, "audit", started=started, ok=False, summary=str(exc)[:200], stats={}
        )


async def run_surface(deps: Deps) -> PhaseOutcome:
    """Sweep the agent-discovery surface (llms.txt, x402, agent card, Bazaar)."""
    started = _now()
    s = deps.settings
    try:
        snapshot = await fetch_surface(
            deps.client,
            site_base_url=s.site_base_url,
            api_base_url=s.api_base_url,
            user_agent=s.user_agent,
            bazaar_url=s.bazaar_discovery_url,
        )
        findings = audit_surface(
            snapshot,
            site_base_url=s.site_base_url,
            api_base_url=s.api_base_url,
            indexnow_key=s.indexnow_key,
            agent_bots=s.agent_bot_list,
        )
        new, seen = await deps.store.upsert_findings(findings)
        resolved = await deps.store.resolve_stale("surface", {f.fingerprint for f in findings})
        indexed = bazaar_indexed_count(snapshot, s.api_base_url)
        if indexed is not None:
            await deps.store.add_metrics(
                [MetricSample(source="surface", metric="bazaar_indexed_resources", value=float(indexed))]
            )
        stats = {
            "docs": len(snapshot.docs()),
            "findings": len(findings),
            "new": new,
            "seen": seen,
            "resolved": resolved,
            "bazaar_indexed": indexed,
        }
        return await _finish(
            deps, "surface", started=started, ok=True,
            summary=f"{len(snapshot.docs())} docs, {len(findings)} findings ({new} new)", stats=stats,
        )
    except Exception as exc:  # defense in depth; the collector already degrades
        log.exception("surface_failed")
        return await _finish(
            deps, "surface", started=started, ok=False, summary=str(exc)[:200], stats={}
        )


async def run_metrics(deps: Deps) -> PhaseOutcome:
    """Pull GSC / PSI / Umami, persist samples + metric-derived findings."""
    started = _now()
    s = deps.settings
    try:
        samples: list[MetricSample] = []
        findings: list[Finding] = []
        sources: list[str] = []

        gsc = GSCCollector(site_url=s.gsc_site_url, credentials_path=s.google_credentials_path)
        if gsc.enabled:
            gsc_samples = await gsc.collect(deps.client)
            samples.extend(gsc_samples)
            if gsc_samples:
                sources.append("gsc")

            async def _lookup(source: str, metric: str, key: str) -> float | None:
                return await deps.store.latest_metric(source, metric, key)

            # findings_from_metrics is sync; feed it a pre-resolved lookup.
            prev_clicks = await deps.store.latest_metric("gsc", "clicks", "28d_prev")
            prev_impressions = await deps.store.latest_metric("gsc", "impressions", "28d_prev")
            lookup_table = {("gsc", "clicks", "28d_prev"): prev_clicks,
                            ("gsc", "impressions", "28d_prev"): prev_impressions}
            findings.extend(
                findings_from_metrics(gsc_samples, lambda a, b, c: lookup_table.get((a, b, c)))
            )

        if s.psi_api_key:
            psi_samples, psi_findings = await collect_psi(
                deps.client, base_url=s.site_base_url, api_key=s.psi_api_key, paths=s.psi_path_list
            )
            samples.extend(psi_samples)
            findings.extend(psi_findings)
            if psi_samples:
                sources.append("psi")

        umami = UmamiCollector(
            base_url=s.umami_base_url, api_token=s.umami_api_token, website_id=s.umami_website_id
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
                    source, {f.fingerprint for f in findings if f.source == source}
                )
        stats = {"samples": stored, "metric_findings": len(findings), "sources": sources}
        return await _finish(
            deps, "metrics", started=started, ok=True,
            summary=f"{stored} samples from {', '.join(sources) or 'no sources'}", stats=stats,
        )
    except Exception as exc:
        log.exception("metrics_failed")
        return await _finish(
            deps, "metrics", started=started, ok=False, summary=str(exc)[:200], stats={}
        )


async def run_indexnow(deps: Deps) -> PhaseOutcome:
    started = _now()
    pinged = await indexnow.ping_if_changed(deps.client, deps.store, deps.settings)
    return await _finish(
        deps, "indexnow", started=started, ok=True,
        summary="pinged" if pinged else "no change", stats={"pinged": pinged},
    )


async def run_draft(deps: Deps) -> PhaseOutcome:
    """Findings → LLM draft → guarded apply → validate → draft PR (dry-run default)."""
    started = _now()
    s = deps.settings
    run_id = f"draft-{uuid.uuid4().hex[:10]}"
    try:
        candidates = top_findings(
            rank_findings(await deps.store.active_findings()), limit=8
        )
        actionable = [f for f in candidates if f.severity in ("error", "warning")]
        # Surface findings on the API origin are report-only: the drafter can
        # only edit hyrule-web, so never feed it URLs it cannot fix.
        actionable = [f for f in actionable if not f.url or f.url.startswith(s.site_base_url)]
        if not actionable:
            agent_core_trace.emit_pr_decision(
                run_id=run_id, decision="stay_silent", title="", rationale="no actionable findings",
                dry_run=s.dry_run,
            )
            return await _finish(
                deps, "draft", started=started, ok=True, summary="no actionable findings",
                stats={"decision": "stay_silent"},
            )

        # Guardrail: PR budget — live GitHub state AND local ledger interval.
        token = ""
        if github.app_configured(s):
            token = await github.installation_token(deps.client, s)
            open_count, latest = await github.open_agent_pr_state(deps.client, s, token)
            if open_count >= s.max_open_prs:
                return await _finish(
                    deps, "draft", started=started, ok=True,
                    summary=f"{open_count} seo-agent PRs already open (cap {s.max_open_prs})",
                    stats={"decision": "stay_silent", "open_prs": open_count},
                )
            if latest is not None:
                age_h = (datetime.now(UTC) - latest).total_seconds() / 3600
                if age_h < s.min_pr_interval_hours:
                    return await _finish(
                        deps, "draft", started=started, ok=True,
                        summary=f"last PR opened {age_h:.1f}h ago (min {s.min_pr_interval_hours}h)",
                        stats={"decision": "stay_silent"},
                    )
        last_local = await deps.store.last_pr_opened_at()
        if last_local is not None:
            age_h = (datetime.now(UTC) - last_local).total_seconds() / 3600
            if age_h < s.min_pr_interval_hours:
                return await _finish(
                    deps, "draft", started=started, ok=True,
                    summary=f"ledger: last PR {age_h:.1f}h ago (min {s.min_pr_interval_hours}h)",
                    stats={"decision": "stay_silent"},
                )

        ws = await workspace.ensure_workspace(s)
        files = gather_context_files(ws, s, actionable)
        proposal, cost = await draft_proposal(s, actionable, files)
        if proposal is None or not proposal.edits:
            rationale = proposal.rationale if proposal else "drafter disabled or empty output"
            agent_core_trace.emit_pr_decision(
                run_id=run_id, decision="stay_silent", title="", rationale=rationale, dry_run=s.dry_run,
            )
            return await _finish(
                deps, "draft", started=started, ok=True,
                summary=f"stay_silent: {rationale[:120]}", stats={"decision": "stay_silent"},
            )

        changed = workspace.apply_edits(ws, s, proposal.edits)
        validation = await workspace.validate_workspace(ws, s)
        if not validation.ok:
            agent_core_trace.emit_pr_decision(
                run_id=run_id, decision="stay_silent", title=proposal.title,
                rationale="workspace validation failed", dry_run=s.dry_run,
            )
            return await _finish(
                deps, "draft", started=started, ok=False,
                summary="draft failed hyrule-web's own gate; aborted",
                stats={"decision": "aborted", "validation_tail": validation.stderr[-400:]},
            )

        branch = f"{github.BRANCH_PREFIX}{datetime.now(UTC).strftime('%Y%m%d')}-{run_id[-6:]}"
        body = github.render_pr_body(
            intent=proposal.body,
            changed_files=changed,
            evidence=[f"[{f.severity}] {f.check}: {f.message}" for f in actionable],
            model_transparency=f"Drafted by seo-agent using {cost.get('model', 'n/a')}; "
            "validated with hyrule-web's ruff+pytest gate before opening.",
        )
        if s.dry_run or not token:
            agent_core_trace.emit_pr_decision(
                run_id=run_id, decision="draft", title=proposal.title, branch=branch,
                finding_fingerprints=[f.fingerprint for f in actionable],
                rationale=proposal.rationale, dry_run=True,
            )
            return await _finish(
                deps, "draft", started=started, ok=True,
                summary=f"dry-run: would open '{proposal.title}' touching {len(changed)} files",
                stats={"decision": "draft", "dry_run": True, "files": changed, "cost": cost},
            )

        await workspace.commit_and_push(ws, s, token, branch=branch, title=proposal.title)
        pr_url = await github.open_draft_pr(
            deps.client, s, token, branch=branch, title=f"seo: {proposal.title}", body=body
        )
        await deps.store.record_pr(branch, pr_url)
        agent_core_trace.emit_pr_decision(
            run_id=run_id, decision="draft", title=proposal.title, branch=branch, pr_url=pr_url,
            finding_fingerprints=[f.fingerprint for f in actionable],
            rationale=proposal.rationale, dry_run=False,
        )
        return await _finish(
            deps, "draft", started=started, ok=True, summary=f"opened {pr_url}",
            stats={"decision": "draft", "pr_url": pr_url, "files": changed, "cost": cost},
        )
    except workspace.WorkspaceError as exc:
        agent_core_trace.emit_pr_decision(
            run_id=run_id, decision="stay_silent", title="", rationale=str(exc), dry_run=s.dry_run,
        )
        return await _finish(
            deps, "draft", started=started, ok=False, summary=f"guardrail: {exc}", stats={}
        )
    except Exception as exc:
        log.exception("draft_failed")
        return await _finish(
            deps, "draft", started=started, ok=False, summary=str(exc)[:200], stats={}
        )


async def run_report(deps: Deps) -> PhaseOutcome:
    started = _now()
    sent = await send_weekly_report(deps.client, deps.store, deps.settings)
    return await _finish(
        deps, "report", started=started, ok=True,
        summary="sent" if sent else "webhook unconfigured or rejected", stats={"sent": sent},
    )
