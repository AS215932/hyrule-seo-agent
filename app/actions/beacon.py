"""Deterministic Beacon action planning and capability-limited execution."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.actions import indexnow
from app.beacon.models import ActionProposal, ActionRisk
from app.config import Settings
from app.discovery.audit import has_usable_sitemap
from app.store import Store


def _proposal(
    run_id: str,
    action_type: str,
    risk: ActionRisk,
    payload: dict[str, Any],
    *,
    channel_key: str | None = None,
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    canonical = json.dumps(
        {"actionType": action_type, "channelKey": channel_key, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    suffix = hashlib.sha256(canonical.encode()).hexdigest()[:20]
    return ActionProposal(
        channelKey=channel_key,
        actionType=action_type,
        risk=risk,
        payload=payload,
        validation=validation or {},
        idempotencyKey=f"{run_id}:{suffix}",
    ).model_dump(by_alias=True, mode="json")


def plan_actions(
    *,
    run_id: str,
    mode: str,
    scopes: list[str],
    findings: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    if mode != "optimize":
        return []

    actions: list[dict[str, Any]] = []
    codes = {finding.get("code", "") for finding in findings}
    observed_absent = {
        str(observation["channelKey"])
        for observation in observations
        if observation.get("present") is False and isinstance(observation.get("channelKey"), str)
    }

    non_actionable_http_codes = {
        "http.sitemap.validation_unavailable",
        "http.structured_data.unavailable",
    }
    actionable_http_codes = {
        code for code in codes if code.startswith("http.") and code not in non_actionable_http_codes
    }
    if "http" in scopes and actionable_http_codes:
        sitemap = evidence.get("surfaces", {}).get("http:sitemap", {})
        sitemap_source = str(sitemap.get("requested_url") or sitemap.get("url") or "")
        try:
            parsed_sitemap = urlsplit(sitemap_source)
            sitemap_host = (
                parsed_sitemap.netloc if parsed_sitemap.scheme in {"http", "https"} and parsed_sitemap.hostname else ""
            )
        except ValueError:
            sitemap_host = ""
        if (
            sitemap.get("status") == 200
            and not sitemap.get("truncated")
            and has_usable_sitemap(str(sitemap.get("text", "")))
            and sitemap_host
        ):
            actions.append(
                _proposal(
                    run_id,
                    "indexnow.submit",
                    "automatic",
                    {
                        "host": sitemap_host,
                        "sitemapSha256": sitemap.get("sha256"),
                        "reason": "Public HTTP surface changed or needs recrawling.",
                    },
                    validation={
                        "sitemapReachable": True,
                        "source": "live_http_audit",
                    },
                )
            )
        actions.append(
            _proposal(
                run_id,
                "repository.change",
                "approval_required",
                {
                    "repository": "AS215932/hyrule-web",
                    "surface": "http",
                    "findingCodes": sorted(actionable_http_codes),
                    "workflow": "prepare_reviewable_patch",
                },
                validation={
                    "source": "live_http_audit",
                    "externalMutation": "git",
                },
            )
        )

    actionable_x402_codes = {code for code in codes if code.startswith("x402.") and code != "x402.health.unavailable"}
    if "x402" in scopes and actionable_x402_codes:
        actions.append(
            _proposal(
                run_id,
                "repository.change",
                "approval_required",
                {
                    "repository": "AS215932/hyrule-cloud",
                    "surface": "x402",
                    "findingCodes": sorted(actionable_x402_codes),
                    "workflow": "repair_manifest_openapi_from_canonical_catalog",
                },
                validation={
                    "source": "live_x402_audit",
                    "externalMutation": "git",
                },
            )
        )

    channel_actions: dict[str, tuple[str, ActionRisk, dict[str, Any]]] = {
        "x402_list": (
            "registry.create_listing",
            "approval_required",
            {"channel": "x402-list", "endpoint": "https://cloud.hyrule.host"},
        ),
        "clawhub": (
            "skill.publish",
            "approval_required",
            {"channel": "ClawHub", "skill": "hyrule-cloud", "publishUmbrellaFirst": True},
        ),
        "skills_sh": (
            "repository.distribution_metadata",
            "approval_required",
            {
                "channel": "skills.sh",
                "repository": "AS215932/hyrule-cloud",
                "install": "npx skills add AS215932/hyrule-cloud",
            },
        ),
        "mcp_registry": (
            "mcp_registry.publish",
            "approval_required",
            {"channel": "Official MCP Registry", "server": "hyrule-cloud"},
        ),
        "agent402": (
            "registry.create_listing",
            "approval_required",
            {"channel": "Agent402", "endpoint": "https://cloud.hyrule.host"},
        ),
        "a2alist": (
            "registry.paid_form",
            "manual",
            {"channel": "a2alist", "reason": "Wallet or paid form requires an operator."},
        ),
        "awesome_x402_xpaysh": (
            "repository.listing_change",
            "approval_required",
            {"repository": "xpaysh/awesome-x402", "entry": "Hyrule Cloud"},
        ),
        "awesome_x402_merit": (
            "repository.listing_change",
            "approval_required",
            {"repository": "Merit-Systems/awesome-x402", "entry": "Hyrule Cloud"},
        ),
    }
    for channel_key in sorted(observed_absent):
        definition = channel_actions.get(channel_key)
        if definition is None:
            continue
        action_type, risk, payload = definition
        actions.append(
            _proposal(
                run_id,
                action_type,
                risk,
                payload,
                channel_key=channel_key,
                validation={
                    "source": "public_channel_measurement",
                    "listingFound": False,
                },
            )
        )

    manual_evidence_actions: dict[str, tuple[str, str, dict[str, Any]]] = {
        "distribution.community.outreach_needed": (
            "x402_foundation",
            "community.outreach",
            {
                "channel": "x402 Foundation",
                "deliverable": "Hyrule demo and evidence pack",
            },
        ),
        "distribution.import_verification_needed": (
            "ampersend",
            "account.verify_import",
            {
                "channel": "Ampersend",
                "question": "Was Hyrule imported from CDP Bazaar?",
            },
        ),
    }
    if "distribution" in scopes:
        for code in sorted(codes):
            manual_definition = manual_evidence_actions.get(code)
            if manual_definition is None:
                continue
            channel_key, action_type, payload = manual_definition
            actions.append(
                _proposal(
                    run_id,
                    action_type,
                    "manual",
                    payload,
                    channel_key=channel_key,
                    validation={
                        "source": "new_distribution_evidence",
                        "findingCode": code,
                    },
                )
            )
    return actions


async def execute_action(
    action: dict[str, Any],
    *,
    settings: Settings,
    client: httpx.AsyncClient,
    store: Store,
    approved: bool,
) -> dict[str, Any]:
    """Execute only capabilities implemented and enabled in code.

    Approval is necessary but never expands the executor's capability set.
    Unsupported external writes become explicit manual handoffs.
    """

    risk = action["risk"]
    action_type = action["actionType"]
    if risk == "approval_required" and not approved:
        return {"status": "manual_required", "reason": "Action was not approved."}
    if risk == "manual":
        return {"status": "manual_required", "reason": "Policy requires an operator."}
    if action_type != "indexnow.submit":
        return {
            "status": "manual_required",
            "reason": "No credentialed executor is installed for this external channel.",
        }
    if not settings.beacon_execute_automatic_actions:
        return {
            "status": "manual_required",
            "reason": "Automatic execution is disabled for this worker.",
        }
    outcome = await indexnow.ping_if_changed(client, store, settings)
    if outcome.status == "manual_required":
        return {"status": "manual_required", "reason": outcome.reason}
    if outcome.status == "failed":
        return {"status": "failed", "reason": outcome.reason}
    return {
        "status": "succeeded",
        "indexnowStatus": outcome.status,
        "pinged": outcome.pinged,
    }
