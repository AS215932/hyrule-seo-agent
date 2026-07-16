"""PageSpeed Insights v5 collector.

Every configured path is measured for both strategies; per-URL failures are
logged and skipped so one bad run never loses the batch — ``collect_psi``
never raises. Core Web Vitals findings are emitted for the mobile strategy
only so each threshold breach yields a single fingerprint per URL.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.models import Finding, MetricSample, Severity

log = structlog.get_logger()

_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
_CATEGORIES = ("performance", "seo", "accessibility")
_STRATEGIES = ("mobile", "desktop")
_SEO_SCORE_FLOOR = 0.9
_LCP_ERROR_MS = 4000.0
_LCP_WARNING_MS = 2500.0
_CLS_ERROR = 0.25
_CLS_WARNING = 0.1


async def collect_psi(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    paths: list[str],
) -> tuple[list[MetricSample], list[Finding]]:
    """Measure every path for both strategies (mobile, desktop). Never raises."""
    samples: list[MetricSample] = []
    findings: list[Finding] = []
    base = base_url.rstrip("/")
    for path in paths:
        page_url = base + (path if path.startswith("/") else f"/{path}")
        for strategy in _STRATEGIES:
            try:
                response = await client.get(
                    _ENDPOINT,
                    params={"url": page_url, "strategy": strategy, "key": api_key, "category": list(_CATEGORIES)},
                )
                response.raise_for_status()
                lighthouse = response.json().get("lighthouseResult", {})
            except Exception:
                log.warning("psi_run_failed", url=page_url, strategy=strategy, exc_info=True)
                continue
            _extract(lighthouse, path=path, page_url=page_url, strategy=strategy, samples=samples, findings=findings)
    return samples, findings


def _extract(
    lighthouse: dict[str, Any],
    *,
    path: str,
    page_url: str,
    strategy: str,
    samples: list[MetricSample],
    findings: list[Finding],
) -> None:
    key = f"{path}|{strategy}"
    categories: dict[str, Any] = lighthouse.get("categories", {})
    audits: dict[str, Any] = lighthouse.get("audits", {})

    for category in _CATEGORIES:
        score = (categories.get(category) or {}).get("score")
        if score is not None:
            samples.append(MetricSample(source="psi", metric=f"{category}_score", key=key, value=float(score)))

    lab: list[tuple[str, str]] = [("largest-contentful-paint", "lcp_ms"), ("cumulative-layout-shift", "cls")]
    if (audits.get("interaction-to-next-paint") or {}).get("numericValue") is not None:
        lab.append(("interaction-to-next-paint", "inp_ms"))
    else:
        lab.append(("total-blocking-time", "tbt_ms"))
    for audit_id, metric in lab:
        value = (audits.get(audit_id) or {}).get("numericValue")
        if value is not None:
            samples.append(MetricSample(source="psi", metric=metric, key=key, value=float(value)))

    seo_score = (categories.get("seo") or {}).get("score")
    if seo_score is not None and seo_score < _SEO_SCORE_FLOOR:
        failing: list[str] = []
        for ref in (categories.get("seo") or {}).get("auditRefs") or []:
            audit_score = (audits.get(ref.get("id", "")) or {}).get("score")
            if audit_score is not None and audit_score < 1:
                failing.append(str(ref["id"]))
        findings.append(
            Finding(
                check="psi_seo_score",
                severity="warning",
                message=(
                    f"Lighthouse SEO score {seo_score:.2f} ({strategy}); "
                    f"failing audits: {', '.join(failing) or 'none'}"
                ),
                url=page_url,
                source="psi",
                evidence={"strategy": strategy, "score": seo_score, "failing": failing},
            )
        )

    if strategy != "mobile":
        return
    lcp = (audits.get("largest-contentful-paint") or {}).get("numericValue")
    if lcp is not None and lcp > _LCP_WARNING_MS:
        lcp_severity: Severity = "error" if lcp > _LCP_ERROR_MS else "warning"
        findings.append(
            Finding(
                check="cwv_lcp",
                severity=lcp_severity,
                message=f"Mobile LCP {lcp:.0f}ms (warning >{_LCP_WARNING_MS:.0f}ms, error >{_LCP_ERROR_MS:.0f}ms)",
                url=page_url,
                source="psi",
                evidence={"strategy": strategy, "lcp_ms": lcp},
            )
        )
    cls_value = (audits.get("cumulative-layout-shift") or {}).get("numericValue")
    if cls_value is not None and cls_value > _CLS_WARNING:
        cls_severity: Severity = "error" if cls_value > _CLS_ERROR else "warning"
        findings.append(
            Finding(
                check="cwv_cls",
                severity=cls_severity,
                message=f"Mobile CLS {cls_value:.2f} (warning >{_CLS_WARNING}, error >{_CLS_ERROR})",
                url=page_url,
                source="psi",
                evidence={"strategy": strategy, "cls": cls_value},
            )
        )
