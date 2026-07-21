from __future__ import annotations

import json

import httpx

import app.discovery.collect as collect_module
from app.discovery.audit import audit_evidence
from app.discovery.collect import _fetch


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


def test_non_200_channel_response_is_unknown_not_absent() -> None:
    for status in (204, 302):
        evidence = {
            "surfaces": {},
            "channels": {"catalog": _resource(status, "")},
            "channel_specs": [
                {
                    "key": "catalog",
                    "name": "Catalog",
                    "priority": "high",
                    "measurement": "presence",
                    "result_kind": "html",
                }
            ],
            "markers": ["hyrule"],
        }

        result = audit_evidence(evidence, ["distribution"])

        assert result["observations"] == []
        assert [finding["code"] for finding in result["findings"]] == ["distribution.measurement.unavailable"]


def test_malformed_or_truncated_json_channel_is_unknown() -> None:
    for resource in (
        _resource(200, "<html>proxy challenge</html>"),
        {**_resource(200, '{"results": []}'), "truncated": True},
    ):
        evidence = {
            "surfaces": {},
            "channels": {"mcp_registry": resource},
            "channel_specs": [
                {
                    "key": "mcp_registry",
                    "name": "MCP Registry",
                    "priority": "high",
                    "measurement": "presence",
                    "result_kind": "json",
                }
            ],
            "markers": ["hyrule"],
        }

        result = audit_evidence(evidence, ["distribution"])

        assert result["observations"] == []
        assert result["findings"][0]["code"] == "distribution.measurement.unavailable"


def test_deep_json_channel_is_traversed_without_recursion_failure() -> None:
    body = "[" * 1_200 + '{"name":"Hyrule","url":"https://cloud.hyrule.host"}' + "]" * 1_200
    evidence = {
        "surfaces": {},
        "channels": {"catalog": _resource(200, body)},
        "channel_specs": [
            {
                "key": "catalog",
                "name": "Catalog",
                "priority": "high",
                "measurement": "presence",
                "result_kind": "json",
            }
        ],
        "markers": ["hyrule"],
    }

    result = audit_evidence(evidence, ["distribution"])

    assert result["findings"] == []
    assert result["observations"][0]["present"] is True


def test_every_truncated_channel_kind_is_unknown() -> None:
    for result_kind in ("html", "direct", "document"):
        evidence = {
            "surfaces": {},
            "channels": {
                "catalog": {
                    **_resource(200, "unrelated result before the cutoff"),
                    "truncated": True,
                }
            },
            "channel_specs": [
                {
                    "key": "catalog",
                    "name": "Catalog",
                    "priority": "high",
                    "measurement": "presence",
                    "result_kind": result_kind,
                }
            ],
            "markers": ["hyrule"],
        }

        result = audit_evidence(evidence, ["distribution"])

        assert result["observations"] == []
        assert [finding["code"] for finding in result["findings"]] == ["distribution.measurement.unavailable"]


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
                "result_kind": "direct",
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


def test_empty_manifest_is_catalog_drift_when_openapi_has_paid_operations() -> None:
    evidence = {
        "surfaces": {
            "x402:openapi": _resource(
                200,
                '{"paths":{"/v1/dns/lookup":{"post":{'
                '"description":"Resolve public DNS records through Hyrule Cloud.",'
                '"x-payment-info":{}}}}}',
            ),
            "x402:manifest": _resource(200, '{"x402Version":2,"resources":[]}'),
        }
    }

    result = audit_evidence(evidence, ["x402"])

    assert "x402.catalog.drift" in {finding["code"] for finding in result["findings"]}


def test_json_ld_requires_a_valid_script_element() -> None:
    base = {
        "http:robots": _resource(200),
        "http:sitemap": _resource(200),
        "http:llms": _resource(200),
    }
    false_positive = audit_evidence(
        {
            "surfaces": {
                **base,
                "http:home": _resource(
                    200, "<!-- application/ld+json --> <script>const example='application/ld+json'</script>"
                ),
            }
        },
        ["http"],
    )
    valid = audit_evidence(
        {
            "surfaces": {
                **base,
                "http:home": _resource(
                    200,
                    '<script type="application/ld+json">'
                    '{"@context":"https://schema.org","@type":"Organization"}'
                    "</script>",
                ),
            }
        },
        ["http"],
    )

    assert "http.structured_data.missing" in {finding["code"] for finding in false_positive["findings"]}
    assert "http.structured_data.missing" not in {finding["code"] for finding in valid["findings"]}


class _CountingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.yielded = 0

    async def __aiter__(self):
        for chunk in (b"1234", b"5678", b"must-not-be-read"):
            self.yielded += 1
            yield chunk


async def test_evidence_collection_stops_streaming_at_the_cap(monkeypatch) -> None:
    stream = _CountingStream()
    monkeypatch.setattr(collect_module, "MAX_EVIDENCE_BYTES", 5)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resource = await _fetch(client, "surface", "https://example.test/large")

    assert resource["text"] == "12345"
    assert resource["truncated"] is True
    assert stream.yielded == 2


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


def test_malformed_openapi_paths_is_reported_instead_of_crashing() -> None:
    for paths in (None, []):
        evidence = {
            "surfaces": {
                "x402:openapi": _resource(200, f'{{"paths":{json.dumps(paths)}}}'),
                "x402:manifest": _resource(200, '{"x402Version":2,"resources":[]}'),
            }
        }

        result = audit_evidence(evidence, ["x402"])

        assert result["findings"][0]["code"] == "x402.openapi.invalid"


def test_manifest_absolute_urls_compare_as_normalized_paths() -> None:
    evidence = {
        "surfaces": {
            "x402:openapi": _resource(
                200,
                '{"paths":{"/v1/dns/lookup/":{"post":{'
                '"description":"Resolve public DNS records using a paid Hyrule endpoint.",'
                '"x-payment-info":{}}}}}',
            ),
            "x402:manifest": _resource(
                200,
                '{"x402Version":2,"resources":[{"method":"POST",'
                '"url":"https://cloud.hyrule.host/v1/dns/lookup?source=manifest"}]}',
            ),
        }
    }

    result = audit_evidence(evidence, ["x402"])

    assert "x402.catalog.drift" not in {finding["code"] for finding in result["findings"]}


def test_search_query_echo_is_not_listing_evidence() -> None:
    evidence = {
        "surfaces": {},
        "channels": {
            "x402_list": _resource(
                200,
                '<form><a href="/?q=Hyrule">Search again</a></form>',
                url="https://x402-list.com/?q=Hyrule",
            )
        },
        "channel_specs": [
            {
                "key": "x402_list",
                "name": "x402-list",
                "priority": "high",
                "measurement": "presence",
                "result_kind": "html",
            }
        ],
        "markers": ["hyrule"],
    }

    result = audit_evidence(evidence, ["distribution"])

    assert result["observations"][0]["present"] is False


def test_html_listing_records_the_actual_result_url() -> None:
    evidence = {
        "surfaces": {},
        "channels": {
            "x402_list": _resource(
                200,
                '<a href="/services/hyrule-cloud">Hyrule Cloud</a>',
                url="https://x402-list.com/?q=Hyrule",
            )
        },
        "channel_specs": [
            {
                "key": "x402_list",
                "name": "x402-list",
                "priority": "high",
                "measurement": "presence",
                "result_kind": "html",
            }
        ],
        "markers": ["hyrule"],
    }

    result = audit_evidence(evidence, ["distribution"])

    observation = result["observations"][0]
    assert observation["present"] is True
    assert observation["resultUrl"] == "https://x402-list.com/services/hyrule-cloud"
