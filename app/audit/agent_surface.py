"""Deterministic checks over one agent-surface sweep (AEO audit).

The classic audit protects how humans find hyrule.host; this module protects
how *agents* do: llms.txt, the x402 manifest, the A2A agent card, the curated
OpenAPI document, Bazaar indexing, and the robots allowances AI crawlers need.
All checks are pure over a ``SurfaceSnapshot`` — the network lives in
``app.collectors.surface``.

Cloud-origin findings (``url`` on the API host) are report-only: the drafter's
``allowed_edit_paths`` covers hyrule-web only, so ``run_draft`` never proposes
edits for them.

Severity policy: every audited surface is live in production (the discovery
workstream deployed 2026-08-08), so absence regresses as an error. New
surfaces that are planned but not yet shipped should enter ``_PENDING`` as
"warning" and be bumped once deployed.
"""

from __future__ import annotations

import json
import re
import urllib.robotparser
from typing import Any, TypeGuard

from app.models import FetchedDoc, Finding, Severity, SurfaceSnapshot, severity_rank

# Severity per surface while it rolls out: "warning" until the asset first
# ships, "error" once live (regressions must page). All current surfaces
# deployed 2026-08-08.
_PENDING: dict[str, Severity] = {
    "web_x402": "error",
    "agent_card": "error",
    "api_robots": "error",
    "api_llms": "error",
    "indexnow_unpublished": "error",
}

# Required top-level A2A agent-card fields (spec minimum we rely on).
_AGENT_CARD_FIELDS = (
    "name",
    "description",
    "url",
    "version",
    "capabilities",
    "defaultInputModes",
    "defaultOutputModes",
    "skills",
)

# ``METHOD /path`` references in llms.txt, backticked or plain, absolute or
# origin-relative.
_ENDPOINT_RE = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH)\s+(?:https?://[^/\s]+)?(/[^\s`\"'<>),]*)")
_TEMPLATE_RE = re.compile(r"\{[^}]*\}")
_OPENAPI_METHODS = ("get", "put", "post", "delete", "patch")

# Meta/document paths an llms.txt legitimately references outside the catalog.
_NON_CATALOG_PATHS = {"/", "/openapi.json", "/llms.txt", "/robots.txt", "/sitemap.xml", "/health", "/indexnow.txt"}

# One aggregated finding stays readable; the full set is in evidence.
_SHOWN = 10

# A per-request price outside (0, ceiling] is a manifest bug, not a product.
_PRICE_CEILING_USD = 1000.0


def _finding(
    check: str,
    severity: Severity,
    message: str,
    *,
    url: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> Finding:
    return Finding(check=check, severity=severity, message=message, url=url, source="surface", evidence=evidence or {})


def _ok(doc: FetchedDoc | None) -> TypeGuard[FetchedDoc]:
    return doc is not None and doc.status_code == 200


def _capped(items: list[str]) -> str:
    message = ", ".join(items[:_SHOWN])
    if len(items) > _SHOWN:
        message += f" and {len(items) - _SHOWN} more"
    return message


def _normalize_path(path: str) -> str:
    """Template segments compare equal: /v1/vm/{vm_id}/status == /v1/vm/{id}/status."""
    return _TEMPLATE_RE.sub("{}", path.rstrip(".,;:"))


def _llms_endpoints(text: str) -> set[tuple[str, str]]:
    return {(method, _normalize_path(path)) for method, path in _ENDPOINT_RE.findall(text)}


def _openapi_ops(doc: FetchedDoc | None) -> dict[tuple[str, str], dict[str, Any]] | None:
    """(METHOD, normalized path) → operation object, or None when the spec
    is unusable (callers must then skip consistency checks, not flag drift)."""
    if not _ok(doc) or not isinstance(doc.json_body, dict):
        return None
    paths = doc.json_body.get("paths")
    if not isinstance(paths, dict):
        return None
    ops: dict[tuple[str, str], dict[str, Any]] = {}
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method in _OPENAPI_METHODS:
            operation = item.get(method)
            if isinstance(operation, dict):
                ops[(method.upper(), _normalize_path(path))] = operation
    return ops


def _payable(operation: dict[str, Any]) -> bool:
    """Ops marked x-payment-info but priced ``{"mode": "free"}`` never 402."""
    info = operation.get("x-payment-info")
    if not isinstance(info, dict):
        return False
    price = info.get("price")
    return not (isinstance(price, dict) and price.get("mode") == "free")


def _price_of(value: Any) -> float | None:
    try:
        return float(str(value).lstrip("$"))
    except ValueError:
        return None


def _x402_resources(doc: FetchedDoc | None) -> list[dict[str, Any]] | None:
    if not _ok(doc) or not isinstance(doc.json_body, dict):
        return None
    resources = doc.json_body.get("resources")
    if not isinstance(resources, list):
        return None
    return [r for r in resources if isinstance(r, dict)]


def _bazaar_items(doc: FetchedDoc | None) -> list[Any] | None:
    if not _ok(doc):
        return None
    body = doc.json_body
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("items", "resources", "data"):
            value = body.get(key)
            if isinstance(value, list):
                return value
    return None


def bazaar_indexed_count(snapshot: SurfaceSnapshot, api_base_url: str) -> int | None:
    """How many Bazaar entries reference our API origin; None when the
    discovery endpoint was unconfigured, unreachable, or shaped unexpectedly."""
    items = _bazaar_items(snapshot.bazaar)
    if items is None or not api_base_url:
        return None
    return sum(1 for item in items if api_base_url in json.dumps(item))


def _llms_txt_findings(snapshot: SurfaceSnapshot) -> list[Finding]:
    out: list[Finding] = []
    doc = snapshot.web_llms_txt
    if doc is not None and (doc.status_code != 200 or not doc.text.strip()):
        out.append(
            _finding(
                "llms_txt",
                "error",
                "llms.txt missing or empty",
                url=doc.url,
                evidence={"status": doc.status_code},
            )
        )

    # Drift needs both documents to be trustworthy; a failed fetch must not
    # flag every endpoint (sitemap_drift discipline).
    ops = _openapi_ops(snapshot.api_openapi)
    if doc is None or not _ok(doc) or not doc.text.strip() or ops is None:
        return out
    mentioned = {
        (method, path)
        for method, path in _llms_endpoints(doc.text)
        if path not in _NON_CATALOG_PATHS and not path.startswith("/.well-known")
    }
    # /api/* is the brand origin's proxy namespace: llms.txt legitimately
    # documents endpoints there (e.g. native checkout rails) that the curated
    # x402 openapi.json never lists — the cloud spec is not their truth.
    unknown = sorted(
        f"{m} {p}" for m, p in mentioned if (m, p) not in ops and not p.startswith("/api/")
    )
    if unknown:
        out.append(
            _finding(
                "llms_txt_drift",
                "warning",
                "llms.txt references endpoints not in openapi.json: " + _capped(unknown),
                url=doc.url,
                evidence={"endpoints": unknown[:_SHOWN]},
            )
        )
    payable = {key for key, operation in ops.items() if _payable(operation)}
    unmentioned = sorted(f"{m} {p}" for m, p in payable if (m, p) not in mentioned)
    if unmentioned:
        out.append(
            _finding(
                "llms_txt_drift",
                "warning",
                "payable operations missing from llms.txt: " + _capped(unmentioned),
                url=doc.url,
                evidence={"endpoints": unmentioned[:_SHOWN]},
            )
        )
    return out


def _x402_findings(snapshot: SurfaceSnapshot) -> list[Finding]:
    out: list[Finding] = []
    api = snapshot.api_x402
    resources = _x402_resources(api)
    if api is not None:
        if api.status_code != 200:
            out.append(
                _finding(
                    "well_known_x402",
                    "error",
                    f"x402 manifest missing (HTTP {api.status_code})",
                    url=api.url,
                    evidence={"status": api.status_code},
                )
            )
        elif not isinstance(api.json_body, dict):
            out.append(_finding("well_known_x402", "error", "x402 manifest is not a JSON object", url=api.url))
        elif "x402Version" not in api.json_body:
            out.append(_finding("well_known_x402", "error", "x402 manifest missing x402Version", url=api.url))
        elif not resources:
            out.append(_finding("well_known_x402", "error", "x402 manifest lists no resources", url=api.url))

    web = snapshot.web_x402
    if web is not None and (web.status_code != 200 or not isinstance(web.json_body, dict)):
        out.append(
            _finding(
                "well_known_x402",
                _PENDING["web_x402"],
                "brand origin does not serve the x402 manifest (directly or via redirect)",
                url=web.url,
                evidence={"status": web.status_code},
            )
        )

    if resources:
        bad_prices: list[str] = []
        for resource in resources:
            path = str(resource.get("path", "?"))
            for field in ("minPrice", "maxPrice"):
                if field not in resource:
                    continue
                price = _price_of(resource[field])
                if price is None or price <= 0 or price > _PRICE_CEILING_USD:
                    bad_prices.append(f"{path} {field}={resource[field]!r}")
        if bad_prices:
            bad_prices.sort()
            out.append(
                _finding(
                    "x402_prices",
                    "warning",
                    "x402 resource prices look wrong: " + _capped(bad_prices),
                    url=api.url if api else None,
                    evidence={"prices": bad_prices[:_SHOWN]},
                )
            )

        ops = _openapi_ops(snapshot.api_openapi)
        if ops is not None:
            orphaned = sorted(
                {
                    f"{str(r.get('method', 'GET')).upper()} {_normalize_path(str(r.get('path', '')))}"
                    for r in resources
                    if (str(r.get("method", "GET")).upper(), _normalize_path(str(r.get("path", "")))) not in ops
                }
            )
            if orphaned:
                out.append(
                    _finding(
                        "x402_consistency",
                        "warning",
                        "x402 resources absent from openapi.json: " + _capped(orphaned),
                        url=api.url if api else None,
                        evidence={"resources": orphaned[:_SHOWN]},
                    )
                )
        web_resources = _x402_resources(web)
        if web_resources is not None:
            def paths_of(rs: list[dict[str, Any]]) -> set[str]:
                return {str(r.get("path", "")) for r in rs}

            if paths_of(web_resources) != paths_of(resources):
                out.append(
                    _finding(
                        "x402_consistency",
                        "warning",
                        "brand-origin x402 manifest disagrees with the API origin's",
                        url=web.url if web else None,
                        evidence={"web": len(web_resources), "api": len(resources)},
                    )
                )
    return out


def _agent_card_findings(snapshot: SurfaceSnapshot) -> list[Finding]:
    out: list[Finding] = []
    for doc in (snapshot.api_agent_card, snapshot.web_agent_card):
        if doc is None:
            continue
        if doc.status_code != 200:
            out.append(
                _finding(
                    "agent_card",
                    _PENDING["agent_card"],
                    f"agent card missing (HTTP {doc.status_code})",
                    url=doc.url,
                    evidence={"status": doc.status_code},
                )
            )
        elif not isinstance(doc.json_body, dict):
            # Present but unparseable is worse than absent: agents that do
            # look will choke on it.
            out.append(_finding("agent_card", "error", "agent card is not a JSON object", url=doc.url))
        else:
            missing = [field for field in _AGENT_CARD_FIELDS if field not in doc.json_body]
            if missing:
                out.append(
                    _finding(
                        "agent_card",
                        "warning",
                        "agent card missing required fields: " + ", ".join(missing),
                        url=doc.url,
                        evidence={"missing": missing},
                    )
                )
    return out


def _openapi_findings(snapshot: SurfaceSnapshot) -> list[Finding]:
    out: list[Finding] = []
    doc = snapshot.api_openapi
    if doc is None:
        return out
    ops = _openapi_ops(doc)
    if ops is None:
        out.append(
            _finding(
                "openapi",
                "error",
                "openapi.json missing or invalid",
                url=doc.url,
                evidence={"status": doc.status_code},
            )
        )
        return out
    undocumented = sorted(
        f"{method} {path}"
        for (method, path), operation in ops.items()
        if _payable(operation) and "402" not in (operation.get("responses") or {})
    )
    if undocumented:
        out.append(
            _finding(
                "openapi_402_docs",
                "warning",
                "payable operations without a documented 402 response: " + _capped(undocumented),
                url=doc.url,
                evidence={"operations": undocumented[:_SHOWN]},
            )
        )
    return out


def _bazaar_findings(snapshot: SurfaceSnapshot, api_base_url: str) -> list[Finding]:
    # Warning-only, and only on a healthy non-empty listing: an external
    # index being down or empty must never page anyone (scoring.py floors).
    items = _bazaar_items(snapshot.bazaar)
    if not items or not api_base_url:
        return []
    if bazaar_indexed_count(snapshot, api_base_url) == 0:
        assert snapshot.bazaar is not None
        return [
            _finding(
                "bazaar_listing",
                "warning",
                f"no {api_base_url} resources in the Bazaar discovery listing",
                url=snapshot.bazaar.url,
                evidence={"listed_total": len(items)},
            )
        ]
    return []


def _robots_findings(snapshot: SurfaceSnapshot, site_base_url: str, agent_bots: list[str]) -> list[Finding]:
    out: list[Finding] = []
    web = snapshot.web_robots_txt
    # A missing web robots.txt is the classic audit's finding; ours is the
    # regression guard for the AI-crawler allowances inside a served one.
    if web is not None and _ok(web) and web.text.strip():
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(web.text.splitlines())
        blocked = sorted(bot for bot in agent_bots if not parser.can_fetch(bot, site_base_url + "/"))
        if blocked:
            out.append(
                _finding(
                    "ai_robots",
                    "error",
                    "robots.txt blocks AI crawlers: " + ", ".join(blocked),
                    url=web.url,
                    evidence={"blocked": blocked},
                )
            )
    if snapshot.api_robots_txt is not None and not _ok(snapshot.api_robots_txt):
        out.append(
            _finding(
                "ai_robots",
                _PENDING["api_robots"],
                "API origin serves no robots.txt",
                url=snapshot.api_robots_txt.url,
            )
        )
    if snapshot.api_llms_txt is not None and not _ok(snapshot.api_llms_txt):
        out.append(
            _finding(
                "ai_robots",
                _PENDING["api_llms"],
                "API origin serves no llms.txt",
                url=snapshot.api_llms_txt.url,
            )
        )
    return out


def _indexnow_findings(snapshot: SurfaceSnapshot, indexnow_key: str) -> list[Finding]:
    doc = snapshot.web_indexnow
    if not indexnow_key or doc is None:
        return []
    if doc.status_code != 200:
        return [
            _finding(
                "indexnow_key",
                _PENDING["indexnow_unpublished"],
                "IndexNow key configured but /indexnow.txt is not published",
                url=doc.url,
                evidence={"status": doc.status_code},
            )
        ]
    if doc.text.strip() != indexnow_key:
        # A mismatched key file silently invalidates every ping we send.
        return [_finding("indexnow_key", "error", "/indexnow.txt does not match the configured IndexNow key", url=doc.url)]
    return []


def audit_surface(
    snapshot: SurfaceSnapshot,
    *,
    site_base_url: str,
    api_base_url: str,
    indexnow_key: str,
    agent_bots: list[str],
) -> list[Finding]:
    """Run every agent-surface check over one sweep.

    Output order is stable — (severity, check, url) — matching ``audit_crawl``
    so identical sweeps produce byte-identical finding lists.
    """
    findings: list[Finding] = []
    findings.extend(_llms_txt_findings(snapshot))
    findings.extend(_x402_findings(snapshot))
    findings.extend(_agent_card_findings(snapshot))
    findings.extend(_openapi_findings(snapshot))
    findings.extend(_bazaar_findings(snapshot, api_base_url))
    findings.extend(_robots_findings(snapshot, site_base_url, agent_bots))
    findings.extend(_indexnow_findings(snapshot, indexnow_key))
    findings.sort(key=lambda f: (severity_rank(f.severity), f.check, f.url or ""))
    return findings
