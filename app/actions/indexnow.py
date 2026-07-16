"""IndexNow ping on real sitemap change.

The sitemap is byte-stable by design (hyrule-web dropped per-request lastmod
stamps for exactly this reason), so a sha256 delta means the URL set actually
changed. The key must match hyrule-web's HYRULE_WEB_INDEXNOW_KEY so the
published key file at /indexnow.txt validates the ping.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

import httpx
import structlog

from app.config import Settings
from app.store import Store

log = structlog.get_logger()

_KV_KEY = "sitemap_sha256"
_ENDPOINT = "https://api.indexnow.org/indexnow"


async def ping_if_changed(client: httpx.AsyncClient, store: Store, settings: Settings) -> bool:
    """Ping IndexNow when the sitemap hash changed; return True if pinged."""
    if not settings.indexnow_key:
        return False
    try:
        resp = await client.get(f"{settings.site_base_url}/sitemap.xml")
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("indexnow_sitemap_fetch_failed", error=str(exc))
        return False
    digest = hashlib.sha256(resp.content).hexdigest()
    previous = await store.get_kv(_KV_KEY)
    if previous == digest:
        return False
    urls = _locs(resp.text)
    if previous is not None and urls:
        try:
            ping = await client.post(
                _ENDPOINT,
                json={
                    "host": urlsplit(settings.site_base_url).netloc,
                    "key": settings.indexnow_key,
                    "keyLocation": f"{settings.site_base_url}/indexnow.txt",
                    "urlList": urls[:100],
                },
            )
            if ping.status_code >= 400:
                log.warning("indexnow_ping_rejected", status=ping.status_code)
                return False
            log.info("indexnow_pinged", urls=len(urls))
        except httpx.HTTPError as exc:
            log.warning("indexnow_ping_failed", error=str(exc))
            return False
    await store.set_kv(_KV_KEY, digest)
    # First observation only seeds the hash; a ping without a known previous
    # state would re-submit an unchanged site on every fresh deployment.
    return previous is not None and bool(urls)


def _locs(xml_text: str) -> list[str]:
    from xml.etree import ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    return [
        el.text.strip()
        for el in root.iter()
        if el.tag.endswith("loc") and el.text and el.text.strip()
    ]
