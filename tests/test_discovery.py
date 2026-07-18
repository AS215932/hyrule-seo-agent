from __future__ import annotations

from app.discovery.audit import audit_evidence


def _resource(status: int, text: str = "", *, url: str = "https://example.test") -> dict:
    return {
        "status": status,
        "text": text,
        "url": url,
        "observed_at": "2026-07-18T12:00:00+00:00",
    }


def test_inaccessible_channel_is_unknown_not_false_observation() -> None:
    evidence = {
        "surfaces": {},
        "channels": {"x402_list": _resource(0)},
        "channel_specs": [
            {
                "key": "x402_list",
                "name": "x402-list",
                "priority": "high",
                "measurement": "presence",
            }
        ],
        "markers": ["hyrule"],
    }
    result = audit_evidence(evidence, ["distribution"])
    assert result["observations"] == []
    assert result["findings"][0]["code"] == "distribution.measurement.unavailable"


def test_public_listing_presence_is_measured_without_inventing_rank() -> None:
    evidence = {
        "surfaces": {},
        "channels": {"skills_sh": _resource(200, "Install AS215932/hyrule-cloud")},
        "channel_specs": [
            {
                "key": "skills_sh",
                "name": "skills.sh",
                "priority": "high",
                "measurement": "presence",
            }
        ],
        "markers": ["as215932/hyrule-cloud"],
    }
    result = audit_evidence(evidence, ["distribution"])
    assert result["observations"][0]["present"] is True
    assert result["observations"][0]["position"] is None
    assert result["findings"] == []


def test_x402_manifest_openapi_drift_is_deterministic() -> None:
    evidence = {
        "surfaces": {
            "x402:openapi": _resource(
                200,
                '{"paths":{"/openapi-only":{"get":{"x-payment-info":{}}}}}',
            ),
            "x402:manifest": _resource(
                200, '{"x402Version":2,"resources":[{"method":"POST","path":"/manifest-only"}]}'
            ),
        },
        "channels": {},
        "channel_specs": [],
    }
    result = audit_evidence(evidence, ["x402"])
    codes = {finding["code"] for finding in result["findings"]}
    assert "x402.catalog.drift" in codes
    assert "x402.intent_descriptions.weak" in codes


def test_full_openapi_free_and_authenticated_routes_are_not_catalog_drift() -> None:
    evidence = {
        "surfaces": {
            "x402:openapi": _resource(
                200,
                '{"paths":{'
                '"/health":{"get":{"responses":{"200":{}}}},'
                '"/v1/auth/login":{"post":{"responses":{"200":{}}}},'
                '"/v1/dns/lookup":{"post":{'
                '"description":"Resolve DNS records for troubleshooting and automation workflows.",'
                '"x-payment-info":{},"responses":{"200":{},"402":{}}}}}}',
            ),
            "x402:manifest": _resource(
                200,
                '{"x402Version":2,"resources":[{"method":"POST","path":"/v1/dns/lookup"}]}',
            ),
        },
        "channels": {},
        "channel_specs": [],
    }

    result = audit_evidence(evidence, ["x402"])

    assert {finding["code"] for finding in result["findings"]} == set()
