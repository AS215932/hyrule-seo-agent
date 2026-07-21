"""Deterministic audits over collected public evidence."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections.abc import Iterator
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
    evidence_r2_key: str | None = None,
) -> dict[str, Any]:
    return {
        "surfaceId": None,
        "channelKey": channel_key,
        "severity": severity,
        "code": code,
        "title": title,
        "message": message,
        "evidenceR2Key": evidence_r2_key,
    }


def _evidence_key(resource: dict[str, Any] | None) -> str | None:
    if not resource:
        return None
    value = resource.get("evidence_r2_key")
    return value if isinstance(value, str) and value else None


def _json(resource: dict[str, Any] | None) -> dict[str, Any] | None:
    if not resource or resource.get("status") != 200 or resource.get("truncated"):
        return None
    try:
        value = json.loads(resource.get("text", ""))
    except json.JSONDecodeError, TypeError, RecursionError:
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
                    evidence_r2_key=_evidence_key(resource),
                )
            )
    sitemap = surfaces.get("http:sitemap", {})
    if sitemap.get("status") == 200:
        if sitemap.get("truncated"):
            findings.append(
                _finding(
                    "http.sitemap.validation_unavailable",
                    "Sitemap validation is unavailable",
                    "The sitemap exceeded the evidence limit, so its XML and URL entries could not be validated.",
                    severity="info",
                    evidence_r2_key=_evidence_key(sitemap),
                )
            )
        elif not has_usable_sitemap(str(sitemap.get("text", ""))):
            findings.append(
                _finding(
                    "http.sitemap.invalid",
                    "Sitemap is not usable",
                    "Publish a valid sitemap XML document with at least one absolute HTTP(S) URL entry.",
                    evidence_r2_key=_evidence_key(sitemap),
                )
            )

    home = surfaces.get("http:home", {})
    home_text = str(home.get("text", ""))
    if home.get("status") == 200:
        if home.get("truncated"):
            findings.append(
                _finding(
                    "http.structured_data.unavailable",
                    "Homepage structured data could not be measured",
                    "The homepage exceeded the evidence limit, so JSON-LD beyond the cutoff remains unknown.",
                    severity="info",
                    evidence_r2_key=_evidence_key(home),
                )
            )
        elif not _has_valid_json_ld(home_text):
            findings.append(
                _finding(
                    "http.structured_data.missing",
                    "Homepage has no JSON-LD",
                    "Expose Organization, WebSite, and relevant SoftwareApplication or Service schema.",
                    evidence_r2_key=_evidence_key(home),
                )
            )
    tools = surfaces.get("http:tools")
    if not tools or tools.get("status") != 200:
        missing = bool(tools and tools.get("status") == 404)
        findings.append(
            _finding(
                "http.tools_index.missing" if missing else "http.tools_index.unavailable",
                "Curated tool index is missing" if missing else "Curated tool index is unavailable",
                (
                    "Publish a curated /tools index with unique intent-led pages for the strongest Hyrule capabilities."
                    if missing
                    else "Expected a 200 response for /tools; restore the public tool-index surface before measuring it."
                ),
                evidence_r2_key=_evidence_key(tools),
            )
        )
    return findings


def _normalized_path(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    return path.rstrip("/") or "/"


def _manifest_catalog(manifest: dict[str, Any]) -> list[Any] | None:
    for key in ("resources", "endpoints"):
        if key not in manifest or manifest[key] is None:
            continue
        return manifest[key] if isinstance(manifest[key], list) else None
    return None


def _manifest_operations(candidates: list[Any]) -> set[tuple[str, str]] | None:
    operations: set[tuple[str, str]] = set()
    for item in candidates:
        if not isinstance(item, dict):
            return None
        method = item.get("method")
        path = item.get("path") or item.get("resource") or item.get("url")
        if not isinstance(method, str) or not method.strip() or not isinstance(path, str):
            return None
        normalized = _normalized_path(path)
        if normalized is None:
            return None
        operations.add((method.upper(), normalized))
    return operations


def _paid_openapi_operations(openapi: dict[str, Any]) -> set[tuple[str, str]] | None:
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
                normalized = _normalized_path(path)
                if normalized is None:
                    return None
                operations.add((method.upper(), normalized))
    return operations


def _audit_x402(surfaces: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    health = surfaces.get("x402:health")
    if not health or health.get("status") != 200:
        findings.append(
            _finding(
                "x402.health.unavailable",
                "x402 service health is unavailable",
                f"Expected a 200 response from /health; observed {health.get('status') if health else 'no response'}.",
                severity="error",
                evidence_r2_key=_evidence_key(health),
            )
        )
    truncated_documents = [
        (key, label)
        for key, label in (("x402:openapi", "OpenAPI"), ("x402:manifest", "manifest"))
        if surfaces.get(key, {}).get("truncated")
    ]
    for key, label in truncated_documents:
        findings.append(
            _finding(
                f"x402.{key.removeprefix('x402:')}.validation_unavailable",
                f"x402 {label} validation is unavailable",
                f"The x402 {label} exceeded the evidence limit and was not classified as invalid.",
                severity="info",
                evidence_r2_key=_evidence_key(surfaces.get(key)),
            )
        )
    if truncated_documents:
        return findings
    openapi = _json(surfaces.get("x402:openapi"))
    manifest = _json(surfaces.get("x402:manifest"))
    if openapi is None or not isinstance(openapi.get("paths"), dict):
        findings.append(
            _finding(
                "x402.openapi.invalid",
                "x402 OpenAPI document is unavailable",
                "The canonical /openapi.json must return a valid JSON object.",
                severity="error",
                evidence_r2_key=_evidence_key(surfaces.get("x402:openapi")),
            )
        )
        openapi = None
    manifest_catalog = _manifest_catalog(manifest) if manifest is not None else None
    if manifest is None or manifest_catalog is None:
        findings.append(
            _finding(
                "x402.manifest.invalid",
                "x402 discovery manifest is unavailable",
                "The canonical /.well-known/x402.json must return a valid JSON object with a resources or endpoints array.",
                severity="error",
                evidence_r2_key=_evidence_key(surfaces.get("x402:manifest")),
            )
        )
        manifest = None
    if openapi is None or manifest is None:
        return findings
    assert manifest_catalog is not None

    openapi_operations = _paid_openapi_operations(openapi)
    manifest_operations = _manifest_operations(manifest_catalog)
    if openapi_operations is None:
        findings.append(
            _finding(
                "x402.openapi.invalid",
                "x402 OpenAPI document is unavailable",
                "The canonical /openapi.json contains a malformed operation path.",
                severity="error",
                evidence_r2_key=_evidence_key(surfaces.get("x402:openapi")),
            )
        )
    if manifest_operations is None:
        findings.append(
            _finding(
                "x402.manifest.invalid",
                "x402 discovery manifest is unavailable",
                "The canonical /.well-known/x402.json contains a malformed resource URL or path.",
                severity="error",
                evidence_r2_key=_evidence_key(surfaces.get("x402:manifest")),
            )
        )
    if openapi_operations is None or manifest_operations is None:
        return findings
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
                evidence_r2_key=_evidence_key(surfaces.get("x402:openapi")),
            )
        )

    version = manifest.get("x402Version") or manifest.get("version")
    if str(version) not in {"2", "2.0", "v2"}:
        findings.append(
            _finding(
                "x402.version.not_v2",
                "Discovery metadata does not identify x402 v2",
                f"Observed version {version!r}; publish current x402 v2 terminology and contracts.",
                evidence_r2_key=_evidence_key(surfaces.get("x402:manifest")),
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
                evidence_r2_key=_evidence_key(surfaces.get("x402:openapi")),
            )
        )
    return findings


def _contains_marker(value: Any, markers: tuple[str, ...]) -> bool:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            lowered = current.lower()
            if any(marker in lowered for marker in markers):
                return True
        elif isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return False


def _record_url(value: Any) -> str | None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            for key in ("url", "homepage", "website", "endpoint", "repository", "href"):
                candidate = current.get(key)
                if isinstance(candidate, str) and candidate.startswith(("https://", "http://")):
                    return candidate
            pending.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            pending.extend(reversed(current))
    return None


def _json_records(value: Any) -> Iterator[dict[str, Any]]:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, list):
            for item in reversed(current):
                if isinstance(item, dict):
                    yield item
                elif isinstance(item, list):
                    pending.append(item)
        elif isinstance(current, dict):
            pending.extend(reversed(list(current.values())))


def _json_listing(text: str, markers: tuple[str, ...]) -> tuple[bool | None, str | None]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError, TypeError, RecursionError:
        return None, None

    if isinstance(payload, list):
        records: Any = payload
    elif isinstance(payload, dict):
        containers = [
            payload.get(key) for key in ("results", "resources", "servers", "items", "data") if key in payload
        ]
        records = next(
            (container for container in containers if isinstance(container, (dict, list))),
            None,
        )
        if records is None:
            return None, None
    else:
        return None, None

    for record in _json_records(records):
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


class _JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._capturing = False
        self._content: list[str] = []
        self.valid_document = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        media_type = next((value for key, value in attrs if key.lower() == "type"), None)
        if isinstance(media_type, str) and media_type.strip().lower() == "application/ld+json":
            self._capturing = True
            self._content = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._content.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "script" or not self._capturing:
            return
        self._capturing = False
        try:
            value = json.loads("".join(self._content))
        except json.JSONDecodeError, TypeError:
            return
        if isinstance(value, (dict, list)):
            self.valid_document = True


def _has_valid_json_ld(text: str) -> bool:
    parser = _JsonLdParser()
    parser.feed(text)
    return parser.valid_document


def has_usable_sitemap(text: str) -> bool:
    """Return whether sitemap XML contains at least one usable URL entry."""

    try:
        root = ET.fromstring(text)
    except ET.ParseError, TypeError, RecursionError:
        return False
    root_name = root.tag.rsplit("}", 1)[-1] if isinstance(root.tag, str) else ""
    entry_name = {"urlset": "url", "sitemapindex": "sitemap"}.get(root_name)
    if entry_name is None:
        return False
    for entry in root:
        if not isinstance(entry.tag, str) or entry.tag.rsplit("}", 1)[-1] != entry_name:
            continue
        for child in entry:
            if not isinstance(child.tag, str) or child.tag.rsplit("}", 1)[-1] != "loc":
                continue
            candidate = (child.text or "").strip()
            try:
                parsed = urlsplit(candidate)
            except ValueError:
                continue
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                return True
    return False


def _html_listing(text: str, base_url: str, markers: tuple[str, ...]) -> tuple[bool, str | None]:
    parser = _AnchorParser()
    parser.feed(text)
    for href, label in parser.anchors:
        try:
            resolved = urljoin(base_url, href)
            parsed = urlsplit(resolved)
        except ValueError:
            continue
        # Search forms commonly echo the query in `?q=...`; only visible link
        # text or the destination itself is evidence of an actual result.
        destination = unquote(f"{parsed.netloc}{parsed.path}{parsed.fragment}").lower()
        if _contains_marker(label, markers) or any(marker in destination for marker in markers):
            return True, resolved
    return False, None


def _listing_result(
    resource: dict[str, Any], spec: dict[str, Any], markers: tuple[str, ...]
) -> tuple[bool | None, str | None]:
    if resource.get("truncated"):
        return None, None
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
    surfaces = evidence.get("surfaces", {})
    skill = surfaces.get("skills:umbrella")
    if not _valid_skill_document(skill):
        findings.append(
            _finding(
                "distribution.skill_source.invalid",
                "Umbrella skill source is unavailable or malformed",
                "The public SKILL.md must return a complete document with name and description frontmatter.",
                severity="error",
                evidence_r2_key=_evidence_key(skill),
            )
        )
    descriptor = surfaces.get("mcp:descriptor")
    if not _valid_mcp_descriptor(descriptor):
        findings.append(
            _finding(
                "distribution.mcp_descriptor.invalid",
                "MCP registry descriptor is unavailable or malformed",
                "The public server.json must identify a versioned server and at least one package or remote.",
                severity="error",
                evidence_r2_key=_evidence_key(descriptor),
            )
        )
    channel_results = evidence.get("channels", {})
    markers = tuple(str(value).lower() for value in evidence.get("markers", []))
    observed_at = datetime.now(UTC).isoformat()
    for spec in evidence.get("channel_specs", []):
        key = spec["key"]
        measurement = spec["measurement"]
        if measurement in {"manual", "not_applicable"}:
            continue
        resource = channel_results.get(key, {})
        if resource.get("status") != 200:
            findings.append(
                _finding(
                    "distribution.measurement.unavailable",
                    f"Could not measure {spec['name']}",
                    f"Public measurement returned {resource.get('status', 0)}; "
                    "this remains unknown and is not counted as absent.",
                    severity="info",
                    channel_key=key,
                    evidence_r2_key=_evidence_key(resource),
                )
            )
            continue
        present, result_url = _listing_result(resource, spec, markers)
        if present is None:
            findings.append(
                _finding(
                    "distribution.measurement.unavailable",
                    f"Could not measure {spec['name']}",
                    "The channel response was malformed or truncated; "
                    "this remains unknown and is not counted as absent.",
                    severity="info",
                    channel_key=key,
                    evidence_r2_key=_evidence_key(resource),
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
                "resultUrl": result_url,
                "evidenceR2Key": _evidence_key(resource),
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
                    evidence_r2_key=_evidence_key(resource),
                )
            )
    return findings, observations


def _valid_skill_document(resource: dict[str, Any] | None) -> bool:
    if not resource or resource.get("status") != 200 or resource.get("truncated"):
        return False
    lines = str(resource.get("text", "")).splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    try:
        closing = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return False
    frontmatter = lines[1:closing]
    return all(
        any(line.strip().startswith(f"{field}:") and line.partition(":")[2].strip() for line in frontmatter)
        for field in ("name", "description")
    )


def _valid_mcp_descriptor(resource: dict[str, Any] | None) -> bool:
    if not resource or resource.get("truncated"):
        return False
    payload = _json(resource)
    if payload is None:
        return False
    distributions = payload.get("packages") or payload.get("remotes")
    return (
        isinstance(payload.get("name"), str)
        and bool(payload["name"].strip())
        and isinstance(payload.get("version"), str)
        and bool(payload["version"].strip())
        and isinstance(distributions, list)
        and any(isinstance(item, dict) for item in distributions)
    )


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
