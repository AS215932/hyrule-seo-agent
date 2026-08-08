"""Tests for app.audit.agent_surface over SurfaceSnapshot fixtures."""

from __future__ import annotations

import json
from typing import Any

from app.audit.agent_surface import audit_surface, bazaar_indexed_count
from app.models import FetchedDoc, Finding, SurfaceSnapshot, severity_rank

WEB = "https://hyrule.host"
API = "https://cloud.hyrule.host"

# One payable and one free operation — the smallest catalog that exercises
# both drift directions.
OPENAPI: dict[str, Any] = {
    "openapi": "3.1.0",
    "paths": {
        "/v1/dns/lookup": {
            "post": {
                "x-payment-info": {"minPrice": "$0.001"},
                "responses": {"200": {}, "402": {}},
            }
        },
        "/v1/vm/{vm_id}/status": {"get": {"responses": {"200": {}}}},
    },
}
X402: dict[str, Any] = {
    "x402Version": 2,
    "resources": [{"path": "/v1/dns/lookup", "method": "POST", "minPrice": 0.001, "maxPrice": 0.001}],
}
CARD: dict[str, Any] = {
    "name": "Hyrule Cloud",
    "description": "d",
    "url": API,
    "version": "0.2.0",
    "capabilities": {},
    "defaultInputModes": ["application/json"],
    "defaultOutputModes": ["application/json"],
    "skills": [],
}
LLMS = (
    "# Hyrule Cloud\n\n"
    "Golden path:\n\n"
    "    GET /.well-known/x402.json\n\n"
    "## Enabled x402 operations\n\n"
    "- `POST /v1/dns/lookup` — DNS lookup, $0.001\n"
)
ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"
KEY = "a1b2c3d4e5f6"


def doc(url: str, status: int = 200, *, text: str = "", json_body: Any = None) -> FetchedDoc:
    if json_body is not None and not text:
        text = json.dumps(json_body)
    return FetchedDoc(url=url, status_code=status, text=text, json_body=json_body)


def surface(**overrides: Any) -> SurfaceSnapshot:
    """A fully healthy estate unless overridden."""
    fields: dict[str, Any] = {
        "web_llms_txt": doc(f"{WEB}/llms.txt", text=LLMS),
        "web_robots_txt": doc(f"{WEB}/robots.txt", text=ROBOTS_ALLOW),
        "web_x402": doc(f"{WEB}/.well-known/x402.json", json_body=X402),
        "web_agent_card": doc(f"{WEB}/.well-known/agent-card.json", json_body=CARD),
        "web_indexnow": doc(f"{WEB}/indexnow.txt", text=KEY + "\n"),
        "api_x402": doc(f"{API}/.well-known/x402.json", json_body=X402),
        "api_agent_card": doc(f"{API}/.well-known/agent-card.json", json_body=CARD),
        "api_openapi": doc(f"{API}/openapi.json", json_body=OPENAPI),
        "api_robots_txt": doc(f"{API}/robots.txt", text=ROBOTS_ALLOW),
        "api_llms_txt": doc(f"{API}/llms.txt", text=LLMS),
        "bazaar": None,
    }
    fields.update(overrides)
    return SurfaceSnapshot(**fields)


def run(snapshot: SurfaceSnapshot, **overrides: Any) -> list[Finding]:
    args: dict[str, Any] = {
        "site_base_url": WEB,
        "api_base_url": API,
        "indexnow_key": KEY,
        "agent_bots": ["ClaudeBot", "GPTBot"],
    }
    args.update(overrides)
    return audit_surface(snapshot, **args)


def checks_of(findings: list[Finding]) -> set[tuple[str, str]]:
    return {(finding.check, finding.severity) for finding in findings}


def test_clean_surface_produces_zero_findings() -> None:
    assert run(surface()) == []


def test_unconfigured_api_origin_produces_zero_findings() -> None:
    snapshot = surface(
        api_x402=None, api_agent_card=None, api_openapi=None, api_robots_txt=None, api_llms_txt=None
    )
    assert run(snapshot, api_base_url="") == []


# -- llms.txt ---------------------------------------------------------------


def test_llms_txt_missing_or_empty_is_error() -> None:
    for bad in (doc(f"{WEB}/llms.txt", 404), doc(f"{WEB}/llms.txt", text="  \n")):
        findings = run(surface(web_llms_txt=bad))
        assert checks_of(findings) == {("llms_txt", "error")}


def test_llms_drift_unknown_endpoint() -> None:
    text = LLMS + "- `POST /v1/gone` — removed op\n"
    findings = run(surface(web_llms_txt=doc(f"{WEB}/llms.txt", text=text)))
    assert checks_of(findings) == {("llms_txt_drift", "warning")}
    assert "POST /v1/gone" in findings[0].message
    assert "not in openapi.json" in findings[0].message


def test_llms_drift_payable_op_unmentioned() -> None:
    text = "# Hyrule Cloud\n\nNothing enabled yet.\n"
    findings = run(surface(web_llms_txt=doc(f"{WEB}/llms.txt", text=text)))
    assert checks_of(findings) == {("llms_txt_drift", "warning")}
    assert "POST /v1/dns/lookup" in findings[0].message


def test_llms_templated_paths_compare_equal() -> None:
    text = LLMS + "- `GET /v1/vm/{id}/status` — free status endpoint\n"
    assert run(surface(web_llms_txt=doc(f"{WEB}/llms.txt", text=text))) == []


def test_llms_drift_skipped_when_openapi_unusable() -> None:
    text = LLMS + "- `POST /v1/gone` — removed op\n"
    snapshot = surface(
        web_llms_txt=doc(f"{WEB}/llms.txt", text=text),
        api_openapi=doc(f"{API}/openapi.json", 500),
    )
    assert checks_of(run(snapshot)) == {("openapi", "error")}


# -- x402 manifest ----------------------------------------------------------


def test_x402_manifest_error_variants() -> None:
    cases = [
        doc(f"{API}/.well-known/x402.json", 404),
        doc(f"{API}/.well-known/x402.json", text="not json"),
        doc(f"{API}/.well-known/x402.json", json_body={"resources": []}),
        doc(f"{API}/.well-known/x402.json", json_body={"x402Version": 2, "resources": []}),
    ]
    for bad in cases:
        findings = run(surface(api_x402=bad))
        assert ("well_known_x402", "error") in checks_of(findings)


def test_web_x402_missing_is_error() -> None:
    findings = run(surface(web_x402=doc(f"{WEB}/.well-known/x402.json", 404)))
    assert checks_of(findings) == {("well_known_x402", "error")}


def test_x402_price_sanity() -> None:
    bad = {
        "x402Version": 2,
        "resources": [{"path": "/v1/dns/lookup", "method": "POST", "minPrice": 0, "maxPrice": "abc"}],
    }
    snapshot = surface(
        api_x402=doc(f"{API}/.well-known/x402.json", json_body=bad),
        web_x402=doc(f"{WEB}/.well-known/x402.json", json_body=bad),
    )
    findings = run(snapshot)
    assert ("x402_prices", "warning") in checks_of(findings)
    prices = next(f for f in findings if f.check == "x402_prices")
    assert "minPrice=0" in prices.message and "maxPrice='abc'" in prices.message


def test_x402_resource_absent_from_openapi() -> None:
    drifted = {
        "x402Version": 2,
        "resources": [{"path": "/v1/legacy", "method": "POST", "minPrice": 0.01, "maxPrice": 0.01}],
    }
    snapshot = surface(
        api_x402=doc(f"{API}/.well-known/x402.json", json_body=drifted),
        web_x402=doc(f"{WEB}/.well-known/x402.json", json_body=drifted),
        web_llms_txt=doc(f"{WEB}/llms.txt", text=LLMS),
    )
    findings = run(snapshot)
    consistency = [f for f in findings if f.check == "x402_consistency"]
    assert len(consistency) == 1
    assert "POST /v1/legacy" in consistency[0].message


def test_x402_brand_and_api_manifests_disagree() -> None:
    other = {"x402Version": 2, "resources": [{"path": "/v1/other", "method": "GET", "minPrice": 1}]}
    findings = run(surface(web_x402=doc(f"{WEB}/.well-known/x402.json", json_body=other)))
    assert any(
        f.check == "x402_consistency" and "disagrees" in f.message for f in findings
    )


# -- agent card -------------------------------------------------------------


def test_agent_card_missing_is_error() -> None:
    findings = run(surface(api_agent_card=doc(f"{API}/.well-known/agent-card.json", 404)))
    assert checks_of(findings) == {("agent_card", "error")}


def test_agent_card_invalid_json_is_error() -> None:
    findings = run(surface(web_agent_card=doc(f"{WEB}/.well-known/agent-card.json", text="<html>")))
    assert checks_of(findings) == {("agent_card", "error")}


def test_agent_card_missing_fields_listed() -> None:
    partial = {"name": "Hyrule Cloud", "description": "d"}
    findings = run(surface(api_agent_card=doc(f"{API}/.well-known/agent-card.json", json_body=partial)))
    assert checks_of(findings) == {("agent_card", "warning")}
    for field in ("url", "version", "capabilities", "skills"):
        assert field in findings[0].message
    assert "name" not in findings[0].evidence["missing"]


# -- openapi ----------------------------------------------------------------


def test_openapi_unfetchable_is_error() -> None:
    findings = run(surface(api_openapi=doc(f"{API}/openapi.json", 500)))
    assert ("openapi", "error") in checks_of(findings)


def test_payable_op_without_402_response_documented() -> None:
    spec = json.loads(json.dumps(OPENAPI))
    del spec["paths"]["/v1/dns/lookup"]["post"]["responses"]["402"]
    findings = run(surface(api_openapi=doc(f"{API}/openapi.json", json_body=spec)))
    assert checks_of(findings) == {("openapi_402_docs", "warning")}
    assert "POST /v1/dns/lookup" in findings[0].message


# -- bazaar -----------------------------------------------------------------


def _bazaar_doc(items: list[Any], status: int = 200) -> FetchedDoc:
    return doc("https://bazaar.test/resources", status, json_body={"items": items})


def test_bazaar_unconfigured_or_unhealthy_never_flags() -> None:
    assert run(surface()) == []  # bazaar=None
    assert run(surface(bazaar=_bazaar_doc([], status=500))) == []
    assert run(surface(bazaar=_bazaar_doc([]))) == []  # empty listing → floor


def test_bazaar_listing_without_our_resources_warns() -> None:
    findings = run(surface(bazaar=_bazaar_doc([{"resource": "https://other.example/v1/x"}])))
    assert checks_of(findings) == {("bazaar_listing", "warning")}
    assert API in findings[0].message


def test_bazaar_indexed_count() -> None:
    assert bazaar_indexed_count(surface(), API) is None
    assert bazaar_indexed_count(surface(bazaar=_bazaar_doc([], status=500)), API) is None
    listed = _bazaar_doc([{"resource": f"{API}/v1/dns/lookup"}, {"resource": "https://other.example/"}])
    assert bazaar_indexed_count(surface(bazaar=listed), API) == 1
    assert run(surface(bazaar=listed)) == []


# -- robots + llms on both origins ------------------------------------------


def test_robots_blocking_ai_crawler_is_error() -> None:
    robots = "User-agent: ClaudeBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    findings = run(surface(web_robots_txt=doc(f"{WEB}/robots.txt", text=robots)))
    assert checks_of(findings) == {("ai_robots", "error")}
    assert "ClaudeBot" in findings[0].message and "GPTBot" not in findings[0].message


def test_robots_fetch_failure_is_not_our_finding() -> None:
    # The classic audit owns "robots.txt missing"; we only guard allowances.
    assert run(surface(web_robots_txt=doc(f"{WEB}/robots.txt", 0))) == []


def test_api_origin_missing_robots_and_llms_error() -> None:
    snapshot = surface(
        api_robots_txt=doc(f"{API}/robots.txt", 404),
        api_llms_txt=doc(f"{API}/llms.txt", 404),
    )
    findings = run(snapshot)
    assert checks_of(findings) == {("ai_robots", "error")}
    assert len(findings) == 2


# -- indexnow ---------------------------------------------------------------


def test_indexnow_unset_key_skips() -> None:
    snapshot = surface(web_indexnow=doc(f"{WEB}/indexnow.txt", 404))
    assert run(snapshot, indexnow_key="") == []


def test_indexnow_unpublished_and_mismatch_both_error() -> None:
    unpublished = run(surface(web_indexnow=doc(f"{WEB}/indexnow.txt", 404)))
    assert checks_of(unpublished) == {("indexnow_key", "error")}
    mismatch = run(surface(web_indexnow=doc(f"{WEB}/indexnow.txt", text="different")))
    assert checks_of(mismatch) == {("indexnow_key", "error")}


# -- ordering + provenance --------------------------------------------------


def test_ordering_is_deterministic_and_severity_sorted() -> None:
    snapshot = surface(
        web_llms_txt=doc(f"{WEB}/llms.txt", 404),
        api_agent_card=doc(f"{API}/.well-known/agent-card.json", 404),
        api_robots_txt=doc(f"{API}/robots.txt", 404),
        web_indexnow=doc(f"{WEB}/indexnow.txt", text="different"),
    )
    first = run(snapshot)
    second = run(snapshot)
    assert first == second
    ranks = [severity_rank(f.severity) for f in first]
    assert ranks == sorted(ranks)
    assert all(f.source == "surface" for f in first)


def test_free_ops_need_no_402_and_no_llms_mention() -> None:
    # Live shape: free discovery ops carry x-payment-info {"price": {"mode":
    # "free"}} — they never 402 and llms.txt need not list them.
    spec = json.loads(json.dumps(OPENAPI))
    spec["paths"]["/v1/pricing"] = {
        "get": {"x-payment-info": {"price": {"mode": "free"}}, "responses": {"200": {}}}
    }
    assert run(surface(api_openapi=doc(f"{API}/openapi.json", json_body=spec))) == []


def test_brand_proxy_paths_are_not_drift() -> None:
    # llms.txt documents the brand origin's /api/* proxy namespace (native
    # checkout rails live there) — the cloud openapi is not their truth.
    text = LLMS + "- Native rail: `POST /api/v1/intent/create`\n"
    assert run(surface(web_llms_txt=doc(f"{WEB}/llms.txt", text=text))) == []
