"""collect_psi: score + lab samples per path/strategy pair, mobile-only CWV
findings at the LCP/CLS thresholds, ([], []) on API failure."""

from __future__ import annotations

from typing import Any

import httpx
import respx

from app.collectors.psi import collect_psi
from app.models import Finding, MetricSample

BASE = "https://hyrule.host"
ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


def _payload(*, seo: float = 0.95, lcp: float = 1200.0, cls: float = 0.02) -> dict[str, Any]:
    return {
        "lighthouseResult": {
            "categories": {
                "performance": {"score": 0.91},
                "accessibility": {"score": 0.88},
                "seo": {"score": seo, "auditRefs": [{"id": "meta-description"}, {"id": "document-title"}]},
            },
            "audits": {
                "meta-description": {"score": 0},
                "document-title": {"score": 1},
                "largest-contentful-paint": {"numericValue": lcp},
                "cumulative-layout-shift": {"numericValue": cls},
                "total-blocking-time": {"numericValue": 150.0},
            },
        }
    }


async def _run(
    response: dict[str, Any] | int, paths: list[str]
) -> tuple[list[MetricSample], list[Finding], respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        if isinstance(response, int):
            route = router.get(ENDPOINT).respond(response)
        else:
            route = router.get(ENDPOINT).respond(200, json=response)
        async with httpx.AsyncClient() as client:
            samples, findings = await collect_psi(client, base_url=BASE, api_key="key", paths=paths)
    return samples, findings, route


async def test_score_and_lab_samples_emitted() -> None:
    samples, _, route = await _run(_payload(seo=0.8, lcp=3000.0), ["/"])

    assert route.call_count == 2  # mobile + desktop
    params = route.calls[0].request.url.params
    assert params["url"] == f"{BASE}/"
    assert params["strategy"] == "mobile"
    assert params["key"] == "key"
    assert params.get_list("category") == ["performance", "seo", "accessibility"]

    assert all(sample.source == "psi" for sample in samples)
    values = {(sample.metric, sample.key): sample.value for sample in samples}
    assert values[("performance_score", "/|mobile")] == 0.91
    assert values[("seo_score", "/|desktop")] == 0.8
    assert values[("accessibility_score", "/|mobile")] == 0.88
    assert values[("lcp_ms", "/|mobile")] == 3000.0
    assert values[("cls", "/|desktop")] == 0.02
    assert values[("tbt_ms", "/|mobile")] == 150.0  # no INP audit → TBT fallback
    assert len(samples) == 12  # (3 scores + 3 lab) per strategy


async def test_seo_score_and_lcp_warning_findings() -> None:
    _, findings, _ = await _run(_payload(seo=0.8, lcp=3000.0), ["/"])

    seo_findings = [finding for finding in findings if finding.check == "psi_seo_score"]
    assert len(seo_findings) == 2  # one per strategy
    assert all(f.severity == "warning" and f.source == "psi" and f.url == f"{BASE}/" for f in seo_findings)
    assert seo_findings[0].evidence == {"strategy": "mobile", "score": 0.8, "failing": ["meta-description"]}
    assert "0.80" in seo_findings[0].message
    assert "meta-description" in seo_findings[0].message

    lcp_findings = [finding for finding in findings if finding.check == "cwv_lcp"]
    assert len(lcp_findings) == 1  # mobile only
    assert lcp_findings[0].severity == "warning"
    assert lcp_findings[0].evidence == {"strategy": "mobile", "lcp_ms": 3000.0}
    assert [finding for finding in findings if finding.check == "cwv_cls"] == []


async def test_lcp_and_cls_error_findings() -> None:
    _, findings, _ = await _run(_payload(seo=0.95, lcp=4500.0, cls=0.3), ["/"])

    assert [finding for finding in findings if finding.check == "psi_seo_score"] == []
    (lcp_finding,) = [finding for finding in findings if finding.check == "cwv_lcp"]
    assert lcp_finding.severity == "error"
    (cls_finding,) = [finding for finding in findings if finding.check == "cwv_cls"]
    assert cls_finding.severity == "error"
    assert cls_finding.evidence == {"strategy": "mobile", "cls": 0.3}


async def test_api_failure_returns_empty() -> None:
    samples, findings, route = await _run(500, ["/", "/services"])

    assert samples == []
    assert findings == []
    assert route.call_count == 4  # every path/strategy pair attempted, none fatal
