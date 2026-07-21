"""Bounded, read-only collection from Hyrule and public discovery channels."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote_plus

import httpx

from app.config import Settings

MAX_EVIDENCE_BYTES = 512_000
MAX_REDIRECTS = 10
HYRULE_MARKERS = ("hyrule", "cloud.hyrule.host", "as215932/hyrule-cloud")


class _UnsafeRedirectError(Exception):
    """A public channel attempted to leave its configured origin."""


@dataclass(frozen=True)
class ChannelSpec:
    key: str
    name: str
    url: str | None
    priority: Literal["existing", "high", "secondary", "manual", "not_applicable"]
    measurement: Literal["presence", "manual", "not_applicable"] = "presence"
    result_kind: Literal["html", "json", "direct", "document"] = "html"


def channel_specs(settings: Settings) -> tuple[ChannelSpec, ...]:
    query = quote_plus("Hyrule cloud.hyrule.host")
    return (
        ChannelSpec(
            "cdp_bazaar",
            "CDP Bazaar",
            "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources",
            "existing",
            result_kind="json",
        ),
        ChannelSpec(
            "agentic_market",
            "Agentic.Market",
            f"https://agentic.market/search?q={query}",
            "existing",
        ),
        ChannelSpec(
            "x402scan",
            "x402scan",
            f"https://www.x402scan.com/search?q={query}",
            "existing",
        ),
        ChannelSpec("x402_list", "x402-list", f"https://x402-list.com/?q={query}", "high"),
        ChannelSpec("clawhub", "ClawHub", f"https://clawhub.ai/?q={query}", "high"),
        ChannelSpec(
            "skills_sh",
            "skills.sh",
            "https://skills.sh/AS215932/hyrule-cloud",
            "high",
            result_kind="direct",
        ),
        ChannelSpec(
            "mcp_registry",
            "Official MCP Registry",
            "https://registry.modelcontextprotocol.io/v0.1/servers?search=hyrule",
            "high",
            result_kind="json",
        ),
        ChannelSpec(
            "agent402",
            "Agent402",
            f"https://marketplace.agent402.app/?q={query}",
            "secondary",
        ),
        ChannelSpec("a2alist", "a2alist", f"https://a2alist.ai/?q={query}", "secondary"),
        ChannelSpec(
            "awesome_x402_xpaysh",
            "xpaysh/awesome-x402",
            "https://raw.githubusercontent.com/xpaysh/awesome-x402/main/README.md",
            "secondary",
            result_kind="document",
        ),
        ChannelSpec(
            "awesome_x402_merit",
            "Merit-Systems/awesome-x402",
            "https://raw.githubusercontent.com/Merit-Systems/awesome-x402/main/README.md",
            "secondary",
            result_kind="document",
        ),
        ChannelSpec(
            "x402_foundation",
            "x402 Foundation",
            "https://x402.org/get-involved/",
            "manual",
            "manual",
        ),
        ChannelSpec("ampersend", "Ampersend", None, "manual", "manual"),
        ChannelSpec("paysh", "Pay.sh", None, "not_applicable", "not_applicable"),
    )


def _surface_urls(settings: Settings, scopes: set[str]) -> dict[str, str]:
    urls: dict[str, str] = {}
    if "http" in scopes:
        urls.update(
            {
                "http:home": f"{settings.site_base_url}/",
                "http:robots": f"{settings.site_base_url}/robots.txt",
                "http:sitemap": f"{settings.site_base_url}/sitemap.xml",
                "http:llms": f"{settings.site_base_url}/llms.txt",
                "http:tools": f"{settings.site_base_url}/tools",
            }
        )
    if "x402" in scopes:
        urls.update(
            {
                "x402:openapi": f"{settings.x402_base_url}/openapi.json",
                "x402:manifest": f"{settings.x402_base_url}/.well-known/x402.json",
                "x402:health": f"{settings.x402_base_url}/health",
            }
        )
    if "distribution" in scopes:
        urls.update(
            {
                "skills:umbrella": (
                    "https://raw.githubusercontent.com/AS215932/hyrule-cloud/main/skills/hyrule-cloud/SKILL.md"
                ),
                "mcp:descriptor": (
                    "https://raw.githubusercontent.com/AS215932/hyrule-cloud/main/packages/hyrule-cloud-mcp/server.json"
                ),
            }
        )
    return urls


async def _fetch(
    client: httpx.AsyncClient,
    key: str,
    url: str,
    *,
    restrict_redirects_to_origin: bool = False,
) -> dict[str, Any]:
    observed_at = datetime.now(UTC).isoformat()
    try:
        initial = httpx.URL(url)
        initial_origin = (initial.scheme, initial.host, initial.port)
        current_url = url
        redirect_count = 0
        while True:
            async with client.stream("GET", current_url, follow_redirects=False) as response:
                if response.has_redirect_location:
                    location = response.headers["location"]
                    try:
                        target = response.url.join(location)
                    except (httpx.InvalidURL, ValueError) as exc:
                        raise _UnsafeRedirectError("redirect target is malformed") from exc
                    target_origin = (target.scheme, target.host, target.port)
                    if target.scheme not in {"http", "https"} or not target.host:
                        raise _UnsafeRedirectError("redirect target is not HTTP(S)")
                    if restrict_redirects_to_origin and target_origin != initial_origin:
                        raise _UnsafeRedirectError("redirect target left the configured public origin")
                    redirect_count += 1
                    if redirect_count > MAX_REDIRECTS:
                        raise _UnsafeRedirectError("redirect limit exceeded")
                    current_url = str(target)
                    continue

                buffered = bytearray()
                truncated = False
                async for chunk in response.aiter_bytes():
                    remaining = MAX_EVIDENCE_BYTES + 1 - len(buffered)
                    buffered.extend(chunk[:remaining])
                    if len(buffered) > MAX_EVIDENCE_BYTES:
                        truncated = True
                        break
                body = bytes(buffered[:MAX_EVIDENCE_BYTES])
                content_type = response.headers.get("content-type", "")[:200]
                text = body.decode(response.encoding or "utf-8", errors="replace")
                response_url = str(response.url)
                status_code = response.status_code
                break
        return {
            "key": key,
            "url": response_url,
            "requested_url": url,
            "status": status_code,
            "content_type": content_type,
            "sha256": hashlib.sha256(body).hexdigest(),
            "truncated": truncated,
            "text": text,
            "observed_at": observed_at,
            "error": None,
        }
    except (httpx.HTTPError, UnicodeError, _UnsafeRedirectError) as exc:
        return {
            "key": key,
            "url": url,
            "requested_url": url,
            "status": 0,
            "content_type": "",
            "sha256": "",
            "truncated": False,
            "text": "",
            "observed_at": observed_at,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }


async def collect_evidence(client: httpx.AsyncClient, settings: Settings, scopes: list[str]) -> dict[str, Any]:
    """Collect all requested evidence concurrently under the client's timeout."""

    scope_set = set(scopes)
    surfaces = _surface_urls(settings, scope_set)
    channel_list = channel_specs(settings) if "distribution" in scope_set else ()
    surface_tasks = [_fetch(client, key, url) for key, url in surfaces.items()]
    fetchable_channels = [spec for spec in channel_list if spec.url]
    channel_tasks = [
        _fetch(
            client,
            f"channel:{spec.key}",
            spec.url or "",
            restrict_redirects_to_origin=True,
        )
        for spec in fetchable_channels
    ]
    results = await asyncio.gather(*surface_tasks, *channel_tasks)
    surface_count = len(surface_tasks)
    channel_results = {result["key"].removeprefix("channel:"): result for result in results[surface_count:]}
    return {
        "surfaces": {result["key"]: result for result in results[:surface_count]},
        "channels": channel_results,
        "channel_specs": [asdict(spec) for spec in channel_list],
        "markers": list(HYRULE_MARKERS),
    }
