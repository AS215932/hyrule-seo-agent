"""Weekly Discord summary (house pattern: webhook embeds, read-only)."""

from __future__ import annotations

import httpx
import structlog

from app.config import Settings
from app.models import severity_rank
from app.store import Store

log = structlog.get_logger()


async def build_summary(store: Store) -> dict[str, object]:
    findings = await store.active_findings()
    by_severity = {"error": 0, "warning": 0, "info": 0}
    for f in findings:
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
    worst = sorted(findings, key=lambda f: (severity_rank(f.severity), f.check))[:5]
    return {
        "findings_total": len(findings),
        "by_severity": by_severity,
        "worst": [f"[{f.severity}] {f.check}: {f.message}" for f in worst],
        "gsc_clicks_28d": await store.latest_metric("gsc", "clicks", "28d"),
        "gsc_impressions_28d": await store.latest_metric("gsc", "impressions", "28d"),
        "psi_seo_score": await store.latest_metric("psi", "seo_score", "/|mobile"),
        "umami_pageviews_7d": await store.latest_metric("umami", "pageviews", "7d"),
        "runs": await store.last_runs(5),
    }


def render_discord_embed(summary: dict[str, object]) -> dict[str, object]:
    by_sev_raw = summary.get("by_severity")
    by_sev: dict[str, int] = by_sev_raw if isinstance(by_sev_raw, dict) else {}
    fields = [
        {
            "name": "Open findings",
            "value": f"{summary.get('findings_total', 0)} "
            f"(err {by_sev.get('error', 0)} / warn {by_sev.get('warning', 0)})",
            "inline": True,
        },
    ]
    metric_labels = [
        ("gsc_clicks_28d", "GSC clicks (28d)"),
        ("gsc_impressions_28d", "GSC impressions (28d)"),
        ("psi_seo_score", "PSI SEO score (/, mobile)"),
        ("umami_pageviews_7d", "Pageviews (7d)"),
    ]
    for key, label in metric_labels:
        value = summary.get(key)
        if value is not None:
            fields.append({"name": label, "value": f"{value:g}", "inline": True})
    worst_raw = summary.get("worst")
    worst: list[str] = [str(line) for line in worst_raw] if isinstance(worst_raw, list) else []
    description = "\n".join(f"- {line}" for line in worst) or "No open findings."
    return {
        "embeds": [
            {
                "title": "seo-agent weekly summary — hyrule.host",
                "description": description[:3500],
                "fields": fields,
            }
        ]
    }


async def send_weekly_report(client: httpx.AsyncClient, store: Store, settings: Settings) -> bool:
    """Post the summary; False when the webhook is unconfigured or rejected."""
    if not settings.discord_webhook_url:
        return False
    payload = render_discord_embed(await build_summary(store))
    try:
        resp = await client.post(settings.discord_webhook_url, json=payload)
        if resp.status_code >= 400:
            log.warning("discord_report_rejected", status=resp.status_code)
            return False
        return True
    except httpx.HTTPError as exc:
        log.warning("discord_report_failed", error=str(exc))
        return False
