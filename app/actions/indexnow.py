"""IndexNow ping on real sitemap change.

The sitemap is byte-stable by design (hyrule-web dropped per-request lastmod
stamps for exactly this reason), so a sha256 delta means the URL set actually
changed. The key must match hyrule-web's HYRULE_WEB_INDEXNOW_KEY so the
published key file at /indexnow.txt validates the ping.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx
import structlog

from app.config import Settings
from app.store import Store

log = structlog.get_logger()

_KV_KEY = "sitemap_sha256"
_ENDPOINT = "https://api.indexnow.org/indexnow"


@dataclass(frozen=True, slots=True)
class IndexNowResult:
    status: Literal["submitted", "unchanged", "seeded", "failed", "manual_required"]
    reason: str | None = None

    @property
    def pinged(self) -> bool:
        return self.status == "submitted"


async def ping_if_changed(client: httpx.AsyncClient, store: Store, settings: Settings) -> IndexNowResult:
    """Submit a changed sitemap and preserve no-op, config, and failure states."""
    if not settings.indexnow_key:
        return IndexNowResult("manual_required", "IndexNow key is not configured.")
    try:
        resp = await client.get(f"{settings.site_base_url}/sitemap.xml", follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("indexnow_sitemap_fetch_failed", error=str(exc))
        return IndexNowResult("failed", f"Sitemap fetch failed: {exc}")
    urls = _locs(resp.text)
    if not urls:
        return IndexNowResult("failed", "Sitemap XML contained no valid URLs.")
    digest = hashlib.sha256(resp.content).hexdigest()
    previous = await store.get_kv(_KV_KEY)
    if previous == digest:
        return IndexNowResult("unchanged")
    if previous is not None:
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
                return IndexNowResult("failed", f"IndexNow rejected the submission with HTTP {ping.status_code}.")
            log.info("indexnow_pinged", urls=len(urls))
        except httpx.HTTPError as exc:
            log.warning("indexnow_ping_failed", error=str(exc))
            return IndexNowResult("failed", f"IndexNow submission failed: {exc}")
    await store.set_kv(_KV_KEY, digest)
    # First observation only seeds the hash; a ping without a known previous
    # state would re-submit an unchanged site on every fresh deployment.
    return IndexNowResult("submitted" if previous is not None else "seeded")


def _locs(xml_text: str) -> list[str]:
    from xml.etree import ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    return [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text and el.text.strip()]
