"""Deterministic audits over collected public evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit


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


def _normalized_path(value: str) -> str:
    parsed = urlsplit(value)
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    return path.rstrip("/") or "/"


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
                operations.add((method.upper(), _normalized_path(path)))
    return operations


def _paid_openapi_operations(openapi: dict[str, Any]) -> set[tuple[str, str]]:
    methods = {"get", "post", "put", "delete", "patch", "head", "options", "trace"}
    operations: set[tuple[str, str]] = set()
    paths = openapi.get("paths")
    if not isinstance(paths, dict):
        return operations
    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if (
                method.lower() in methods
                and isinstance(operation, dict)
                and isinstance(operation.get("x-payment-info"), dict)
            ):
                operations.add((method.upper(), _normalized_path(path)))
    return operations


def _audit_x402(surfaces: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    openapi = _json(surfaces.get("x402:openapi"))
    manifest = _json(surfaces.get("x402:manifest"))
    if openapi is None or not isinstance(openapi.get("paths"), dict):
        findings.append(
            _finding(
                "x402.openapi.invalid",
                "x402 OpenAPI document is unavailable",
                "The canonical /openapi.json must return a valid JSON object.",
                severity="error",
            )
        )
        openapi = None
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
    for path_item in openapi["paths"].values():
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


def _contains_marker(value: Any, markers: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        lowered = value.lower()
        return any(marker in lowered for marker in markers)
    if isinstance(value, dict):
        return any(_contains_marker(item, markers) for item in value.values())
    if isinstance(value, list):
        return any(_contains_marker(item, markers) for item in value)
    return False


def _record_url(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("url", "homepage", "website", "endpoint", "repository", "href"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith(("https://", "http://")):
                return candidate
        for candidate in value.values():
            found = _record_url(candidate)
            if found:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _record_url(candidate)
            if found:
                return found
    return None


def _json_listing(text: str, markers: tuple[str, ...]) -> tuple[bool, str | None]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError, TypeError:
        return False, None

    def records(value: Any) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    found.append(item)
                else:
                    found.extend(records(item))
        elif isinstance(value, dict):
            for item in value.values():
                found.extend(records(item))
        return found

    for record in records(payload):
        if _contains_marker(record, markers):
            return True, _record_url(record)
    return False, None


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        self._href = next((value for key, value in attrs if key == "href" and value), "")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.anchors.append((self._href, " ".join(self._text)))
            self._href = None
            self._text = []


def _html_listing(text: str, base_url: str, markers: tuple[str, ...]) -> tuple[bool, str | None]:
    parser = _AnchorParser()
    parser.feed(text)
    for href, label in parser.anchors:
        resolved = urljoin(base_url, href)
        parsed = urlsplit(resolved)
        # Search forms commonly echo the query in `?q=...`; only visible link
        # text or the destination itself is evidence of an actual result.
        destination = unquote(f"{parsed.netloc}{parsed.path}{parsed.fragment}").lower()
        if _contains_marker(label, markers) or any(marker in destination for marker in markers):
            return True, resolved
    return False, None


def _listing_result(
    resource: dict[str, Any], spec: dict[str, Any], markers: tuple[str, ...]
) -> tuple[bool, str | None]:
    text = str(resource.get("text", ""))
    url = str(resource.get("url", ""))
    result_kind = spec.get("result_kind", "html")
    if result_kind == "json":
        present, result_url = _json_listing(text, markers)
        return present, result_url or (url if present else None)
    if result_kind == "direct":
        present = _contains_marker(text, markers)
        return present, url if present else None
    if result_kind == "document":
        present = any(_contains_marker(line, markers) for line in text.splitlines())
        return present, url if present else None
    return _html_listing(text, url, markers)


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
        present, result_url = _listing_result(resource, spec, markers)
        observations.append(
            {
                "channelKey": key,
                "intentId": None,
                "targetKey": "hyrule-cloud",
                "present": present,
                "position": None,
                "score": None,
                "resultName": "Hyrule Cloud" if present else None,
                "resultUrl": result_url,
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
