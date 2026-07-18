"""Deterministic audits over collected public evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


def _finding(
    code: str,
    title: str,
    message: str,
    *,
    severity: str = "warning",
    channel_key: str | None = None,
) -> dict[str, Any]:
    return {
        "surfaceId": None,
        "channelKey": channel_key,
        "severity": severity,
        "code": code,
        "title": title,
        "message": message,
        "evidenceR2Key": None,
    }


def _json(resource: dict[str, Any] | None) -> dict[str, Any] | None:
    if not resource or resource.get("status") != 200:
        return None
    try:
        value = json.loads(resource.get("text", ""))
    except json.JSONDecodeError, TypeError:
        return None
    return value if isinstance(value, dict) else None


def _audit_http(surfaces: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    required = {
        "http:home": "homepage",
        "http:robots": "robots.txt",
        "http:sitemap": "sitemap.xml",
        "http:llms": "llms.txt",
    }
    for key, label in required.items():
        resource = surfaces.get(key)
        if not resource or resource.get("status") != 200:
            findings.append(
                _finding(
                    f"http.{key.removeprefix('http:')}.unavailable",
                    f"{label} is not publicly available",
                    f"Expected a 200 response for {label}; observed "
                    f"{resource.get('status') if resource else 'no response'}.",
                    severity="error" if key == "http:home" else "warning",
                )
            )
    home = surfaces.get("http:home", {})
    home_text = str(home.get("text", "")).lower()
    if home.get("status") == 200 and "application/ld+json" not in home_text:
        findings.append(
            _finding(
                "http.structured_data.missing",
                "Homepage has no JSON-LD",
                "Expose Organization, WebSite, and relevant SoftwareApplication or Service schema.",
            )
        )
    tools = surfaces.get("http:tools")
    if tools and tools.get("status") == 404:
        findings.append(
            _finding(
                "http.tools_index.missing",
                "Curated tool index is missing",
                "Publish a curated /tools index with unique intent-led pages for the strongest Hyrule capabilities.",
            )
        )
    return findings


def _manifest_operations(manifest: dict[str, Any]) -> set[tuple[str, str]]:
    candidates = manifest.get("resources") or manifest.get("endpoints") or []
    operations: set[tuple[str, str]] = set()
    if isinstance(candidates, list):
        for item in candidates:
            if not isinstance(item, dict):
                continue
            method = item.get("method")
            path = item.get("path") or item.get("resource") or item.get("url")
            if isinstance(method, str) and isinstance(path, str):
                operations.add((method.upper(), path))
    return operations


def _paid_openapi_operations(openapi: dict[str, Any]) -> set[tuple[str, str]]:
    methods = {"get", "post", "put", "delete", "patch", "head", "options", "trace"}
    operations: set[tuple[str, str]] = set()
    for path, path_item in openapi.get("paths", {}).items():
        if not isinstance(path, str) or not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if (
                method.lower() in methods
                and isinstance(operation, dict)
                and isinstance(operation.get("x-payment-info"), dict)
            ):
                operations.add((method.upper(), path))
    return operations


def _audit_x402(surfaces: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    openapi = _json(surfaces.get("x402:openapi"))
    manifest = _json(surfaces.get("x402:manifest"))
    if openapi is None:
        findings.append(
            _finding(
                "x402.openapi.invalid",
                "x402 OpenAPI document is unavailable",
                "The canonical /openapi.json must return a valid JSON object.",
                severity="error",
            )
        )
    if manifest is None:
        findings.append(
            _finding(
                "x402.manifest.invalid",
                "x402 discovery manifest is unavailable",
                "The canonical /.well-known/x402.json must return a valid JSON object.",
                severity="error",
            )
        )
    if openapi is None or manifest is None:
        return findings

    openapi_operations = _paid_openapi_operations(openapi)
    manifest_operations = _manifest_operations(manifest)
    if manifest_operations:
        missing_from_openapi = sorted(manifest_operations - openapi_operations)
        missing_from_manifest = sorted(openapi_operations - manifest_operations)
        if missing_from_openapi or missing_from_manifest:
            only_manifest = [f"{method} {path}" for method, path in missing_from_openapi]
            only_openapi = [f"{method} {path}" for method, path in missing_from_manifest]
            findings.append(
                _finding(
                    "x402.catalog.drift",
                    "Manifest and OpenAPI catalogs have drifted",
                    f"Only in manifest: {only_manifest[:10]}; only in OpenAPI: {only_openapi[:10]}.",
                    severity="error",
                )
            )

    version = manifest.get("x402Version") or manifest.get("version")
    if str(version) not in {"2", "2.0", "v2"}:
        findings.append(
            _finding(
                "x402.version.not_v2",
                "Discovery metadata does not identify x402 v2",
                f"Observed version {version!r}; publish current x402 v2 terminology and contracts.",
            )
        )

    weak_descriptions = 0
    for path_item in openapi.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for operation in path_item.values():
            if (
                isinstance(operation, dict)
                and isinstance(operation.get("x-payment-info"), dict)
                and len(str(operation.get("description", "")).strip()) < 40
            ):
                weak_descriptions += 1
    if weak_descriptions:
        findings.append(
            _finding(
                "x402.intent_descriptions.weak",
                "x402 operations lack intent-rich descriptions",
                f"{weak_descriptions} operations have fewer than 40 description characters.",
            )
        )
    return findings


def _audit_distribution(evidence: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    channel_results = evidence.get("channels", {})
    markers = tuple(str(value).lower() for value in evidence.get("markers", []))
    observed_at = datetime.now(UTC).isoformat()
    for spec in evidence.get("channel_specs", []):
        key = spec["key"]
        measurement = spec["measurement"]
        if measurement in {"manual", "not_applicable"}:
            continue
        resource = channel_results.get(key, {})
        body = str(resource.get("text", "")).lower()
        present = resource.get("status") == 200 and any(marker in body for marker in markers)
        if resource.get("status") == 0 or resource.get("status", 500) >= 400:
            findings.append(
                _finding(
                    "distribution.measurement.unavailable",
                    f"Could not measure {spec['name']}",
                    f"Public measurement returned {resource.get('status', 0)}; "
                    "this remains unknown and is not counted as absent.",
                    severity="info",
                    channel_key=key,
                )
            )
            continue
        observations.append(
            {
                "channelKey": key,
                "intentId": None,
                "targetKey": "hyrule-cloud",
                "present": present,
                "position": None,
                "score": None,
                "resultName": "Hyrule Cloud" if present else None,
                "resultUrl": resource.get("url") if present else None,
                "evidenceR2Key": None,
                "observedAt": resource.get("observed_at", observed_at),
            }
        )
        if not present:
            findings.append(
                _finding(
                    "distribution.listing.absent",
                    f"Hyrule was not found on {spec['name']}",
                    "No Hyrule marker was present in the public response. Confirm the channel's "
                    "search behavior before preparing a submission.",
                    severity="warning" if spec["priority"] in {"existing", "high"} else "info",
                    channel_key=key,
                )
            )
    return findings, observations


def audit_evidence(evidence: dict[str, Any], scopes: list[str]) -> dict[str, list[dict[str, Any]]]:
    surfaces = evidence.get("surfaces", {})
    findings: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    if "http" in scopes:
        findings.extend(_audit_http(surfaces))
    if "x402" in scopes:
        findings.extend(_audit_x402(surfaces))
    if "distribution" in scopes:
        distribution_findings, observations = _audit_distribution(evidence)
        findings.extend(distribution_findings)
    return {"findings": findings, "observations": observations}
