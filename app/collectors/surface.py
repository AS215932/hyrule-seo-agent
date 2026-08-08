"""Fetch-only sweep of the agent-discovery surface.

Unlike the crawler this is not a BFS: it GETs a fixed set of well-known
paths on the managed site and (when configured) the paid-API origin, plus
the external Bazaar discovery endpoint. Redirects are followed — hyrule.host
serves its x402 manifest as a redirect to the API origin, and "an agent can
obtain the manifest from the brand origin" is exactly the property we audit.
``fetch_surface`` never raises: a failing fetch degrades to a
``status_code=0`` doc, an unconfigured origin to ``None``.
"""

from __future__ import annotations

import json

import httpx
import structlog

from app.models import FetchedDoc, SurfaceSnapshot

log = structlog.get_logger()

# Bound stored text so one huge response can't bloat findings evidence or the
# trace stream; JSON is parsed from the full body before truncation.
_TEXT_CAP = 512 * 1024

_WEB_PATHS = {
    "web_llms_txt": "/llms.txt",
    "web_robots_txt": "/robots.txt",
    "web_x402": "/.well-known/x402.json",
    "web_agent_card": "/.well-known/agent-card.json",
    "web_indexnow": "/indexnow.txt",
}
_API_PATHS = {
    "api_x402": "/.well-known/x402.json",
    "api_agent_card": "/.well-known/agent-card.json",
    "api_openapi": "/openapi.json",
    "api_robots_txt": "/robots.txt",
    "api_llms_txt": "/llms.txt",
}


async def _fetch_doc(client: httpx.AsyncClient, url: str, user_agent: str) -> FetchedDoc:
    try:
        response = await client.get(url, headers={"User-Agent": user_agent}, follow_redirects=True)
    except Exception:
        log.warning("surface_fetch_failed", url=url, exc_info=True)
        return FetchedDoc(url=url)
    json_body = None
    if response.status_code < 400:
        try:
            json_body = json.loads(response.text)
        except ValueError:
            json_body = None
    return FetchedDoc(
        url=url,
        status_code=response.status_code,
        content_type=response.headers.get("content-type", ""),
        text=response.text[:_TEXT_CAP],
        json_body=json_body,
    )


async def fetch_surface(
    client: httpx.AsyncClient,
    *,
    site_base_url: str,
    api_base_url: str,
    user_agent: str,
    bazaar_url: str = "",
) -> SurfaceSnapshot:
    """Sequential polite GETs over the discovery surface. Never raises."""
    snapshot = SurfaceSnapshot()
    try:
        site = site_base_url.rstrip("/")
        for field, path in _WEB_PATHS.items():
            setattr(snapshot, field, await _fetch_doc(client, site + path, user_agent))
        if api_base_url:
            api = api_base_url.rstrip("/")
            for field, path in _API_PATHS.items():
                setattr(snapshot, field, await _fetch_doc(client, api + path, user_agent))
        if bazaar_url:
            snapshot.bazaar = await _fetch_doc(client, bazaar_url, user_agent)
    except Exception:  # defense in depth; _fetch_doc already degrades
        log.warning("surface_sweep_failed", exc_info=True)
    return snapshot
