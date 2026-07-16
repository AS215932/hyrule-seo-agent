"""Metric-delta findings plus ranking/selection of the actionable slice.

Deterministic like ``checks``: the drafter and weekly report only ever see
``top_findings`` output, so ordering here decides what the agent works on.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from app.models import Finding, MetricSample, severity_rank

# (metric, check, floor): a drop only counts when the previous 28-day window
# was above the floor — small absolute counts swing wildly week to week.
_GSC_DROP_RULES: tuple[tuple[str, str, float], ...] = (
    ("clicks", "gsc_clicks_drop", 20.0),
    ("impressions", "gsc_impressions_drop", 200.0),
)
# Fire below 70% of the previous window.
_DROP_RATIO = 0.7


def _batch_value(samples: list[MetricSample], source: str, metric: str, key: str) -> float | None:
    for sample in samples:
        if sample.source == source and sample.metric == metric and sample.key == key:
            return sample.value
    return None


def findings_from_metrics(
    current: list[MetricSample],
    previous_lookup: Callable[[str, str, str], float | None],
) -> list[Finding]:
    """Compare this batch's GSC 28-day windows against the previous windows.

    The comparison value comes from the batch's own ``28d_prev`` sample when
    present; ``previous_lookup`` (the store) is consulted only as a fallback.
    No history at all means no finding — never a false alarm on first run.
    """
    findings: list[Finding] = []
    for metric, check, floor in _GSC_DROP_RULES:
        current_value = _batch_value(current, "gsc", metric, "28d")
        if current_value is None:
            continue
        previous_value = _batch_value(current, "gsc", metric, "28d_prev")
        if previous_value is None:
            previous_value = previous_lookup("gsc", metric, "28d_prev")
        if previous_value is None or previous_value <= floor:
            continue
        if current_value >= previous_value * _DROP_RATIO:
            continue
        drop_pct = (1 - current_value / previous_value) * 100
        findings.append(
            Finding(
                check=check,
                severity="warning",
                message=(
                    f"GSC {metric} dropped from {previous_value:g} to {current_value:g} "
                    f"over 28d (-{drop_pct:.0f}%)"
                ),
                source="gsc",
                evidence={"current": current_value, "previous": previous_value},
            )
        )
    return findings


def rank_findings(
    findings: Sequence[Finding],
    *,
    traffic_by_url: Mapping[str, float] | None = None,
) -> list[Finding]:
    """Dedupe by fingerprint (first occurrence wins) and order for triage:
    errors first, then higher-traffic URLs, then (check, url) as a stable tie
    break. Unknown or absent URLs weigh 0.0."""
    seen: set[str] = set()
    unique: list[Finding] = []
    for finding in findings:
        if finding.fingerprint in seen:
            continue
        seen.add(finding.fingerprint)
        unique.append(finding)

    def traffic_weight(url: str | None) -> float:
        if url is None or traffic_by_url is None:
            return 0.0
        return float(traffic_by_url.get(url, 0.0))

    unique.sort(key=lambda f: (severity_rank(f.severity), -traffic_weight(f.url), f.check, f.url or ""))
    return unique


def top_findings(findings: Sequence[Finding], *, limit: int = 8) -> list[Finding]:
    """The slice the drafter/report acts on: info excluded, ranked, capped."""
    actionable = [finding for finding in findings if finding.severity != "info"]
    return rank_findings(actionable)[:limit]
