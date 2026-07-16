"""Tests for app.audit.scoring: GSC drop findings, ranking, top slice."""

from __future__ import annotations

from app.audit.scoring import findings_from_metrics, rank_findings, top_findings
from app.models import Finding, MetricSample


def gsc(metric: str, key: str, value: float) -> MetricSample:
    return MetricSample(source="gsc", metric=metric, key=key, value=value)


def forbidden_lookup(source: str, metric: str, key: str) -> float | None:
    raise AssertionError("previous_lookup must not be consulted when the batch carries *_prev")


def test_clicks_drop_fires_below_70_percent() -> None:
    batch = [gsc("clicks", "28d", 10.0), gsc("clicks", "28d_prev", 100.0)]
    findings = findings_from_metrics(batch, forbidden_lookup)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.check == "gsc_clicks_drop"
    assert finding.severity == "warning"
    assert finding.source == "gsc"
    assert finding.evidence == {"current": 10.0, "previous": 100.0}
    assert "100" in finding.message
    assert "10" in finding.message
    assert "90%" in finding.message


def test_clicks_drop_silent_at_75_percent() -> None:
    batch = [gsc("clicks", "28d", 75.0), gsc("clicks", "28d_prev", 100.0)]
    assert findings_from_metrics(batch, forbidden_lookup) == []


def test_clicks_drop_needs_previous_above_20() -> None:
    batch = [gsc("clicks", "28d", 2.0), gsc("clicks", "28d_prev", 20.0)]
    assert findings_from_metrics(batch, forbidden_lookup) == []


def test_impressions_drop_rule() -> None:
    fires = [gsc("impressions", "28d", 100.0), gsc("impressions", "28d_prev", 1000.0)]
    findings = findings_from_metrics(fires, forbidden_lookup)
    assert [finding.check for finding in findings] == ["gsc_impressions_drop"]
    below_floor = [gsc("impressions", "28d", 10.0), gsc("impressions", "28d_prev", 200.0)]
    assert findings_from_metrics(below_floor, forbidden_lookup) == []


def test_previous_lookup_used_only_when_batch_lacks_prev() -> None:
    calls: list[tuple[str, str, str]] = []

    def lookup(source: str, metric: str, key: str) -> float | None:
        calls.append((source, metric, key))
        return 100.0

    # No 28d_prev in the batch: the store is asked (clicks only; impressions
    # has no current sample so its rule never gets that far).
    findings = findings_from_metrics([gsc("clicks", "28d", 10.0)], lookup)
    assert [finding.check for finding in findings] == ["gsc_clicks_drop"]
    assert calls == [("gsc", "clicks", "28d_prev")]

    # 28d_prev in the batch: the store is never asked, batch value wins.
    calls.clear()
    findings = findings_from_metrics([gsc("clicks", "28d", 10.0), gsc("clicks", "28d_prev", 50.0)], lookup)
    assert calls == []
    assert findings[0].evidence == {"current": 10.0, "previous": 50.0}


def test_lookup_none_means_no_finding() -> None:
    def lookup(source: str, metric: str, key: str) -> float | None:
        return None

    assert findings_from_metrics([gsc("clicks", "28d", 10.0)], lookup) == []


def test_rank_findings_dedupes_by_fingerprint_keeping_first() -> None:
    first = Finding(
        check="canonical", severity="warning", message="canonical mismatch",
        url="https://hyrule.host/a", evidence={"seen": "first"},
    )
    dup = Finding(
        check="canonical", severity="warning", message="canonical mismatch",
        url="https://hyrule.host/a", evidence={"seen": "second"},
    )
    assert first.fingerprint == dup.fingerprint
    ranked = rank_findings([first, dup])
    assert len(ranked) == 1
    assert ranked[0].evidence == {"seen": "first"}


def test_rank_orders_errors_first_then_traffic_at_equal_severity() -> None:
    warn_low = Finding(check="aaa", severity="warning", message="w-low", url="https://hyrule.host/low")
    warn_high = Finding(check="zzz", severity="warning", message="w-high", url="https://hyrule.host/high")
    err = Finding(check="zzz", severity="error", message="e", url="https://hyrule.host/low")
    traffic = {"https://hyrule.host/high": 250.0, "https://hyrule.host/low": 3.0}
    ranked = rank_findings([warn_low, warn_high, err], traffic_by_url=traffic)
    assert [finding.message for finding in ranked] == ["e", "w-high", "w-low"]


def test_rank_weighs_unknown_or_missing_urls_as_zero() -> None:
    known = Finding(check="bbb", severity="warning", message="known", url="https://hyrule.host/x")
    unknown = Finding(check="aaa", severity="warning", message="unknown", url=None)
    ranked = rank_findings([known, unknown], traffic_by_url={"https://hyrule.host/x": 1.0})
    assert [finding.message for finding in ranked] == ["known", "unknown"]
    # Without traffic data every URL weighs 0.0 and (check, url) decides.
    ranked = rank_findings([known, unknown])
    assert [finding.message for finding in ranked] == ["unknown", "known"]


def test_top_findings_drops_info_and_respects_limit() -> None:
    info = Finding(check="note", severity="info", message="i")
    warns = [Finding(check=f"w{i}", severity="warning", message=f"w{i}") for i in range(3)]
    err = Finding(check="boom", severity="error", message="e")
    top = top_findings([info, *warns, err], limit=2)
    assert [finding.message for finding in top] == ["e", "w0"]
    everything = top_findings([info, *warns, err])
    assert len(everything) == 4
    assert all(finding.severity != "info" for finding in everything)
