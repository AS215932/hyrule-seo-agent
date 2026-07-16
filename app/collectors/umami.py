"""Self-hosted Umami v2 analytics collector.

Enabled only when base URL, API token, and website id are all set. Window:
trailing 7 days as millisecond epochs. Stats fields arrive either as
``{"value": n}`` or bare numbers depending on Umami version — both are
accepted. ``collect`` never raises — any failure logs and yields ``[]``.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from app.models import MetricSample

log = structlog.get_logger()

_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
_STATS_METRICS = ("pageviews", "visitors", "bounces")


def _value(raw: object) -> float | None:
    """Accept ``{"value": n}`` and bare-number shapes; None otherwise."""
    if isinstance(raw, dict):
        raw = raw.get("value")
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


class UmamiCollector:
    """7-day site stats plus top-10 URLs by pageviews."""

    def __init__(self, *, base_url: str, api_token: str, website_id: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.website_id = website_id

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_token and self.website_id)

    async def collect(self, client: httpx.AsyncClient) -> list[MetricSample]:
        if not self.enabled:
            return []
        try:
            return await self._collect(client)
        except Exception:
            log.warning("umami_collect_failed", website_id=self.website_id, exc_info=True)
            return []

    async def _collect(self, client: httpx.AsyncClient) -> list[MetricSample]:
        end_at = int(time.time() * 1000)
        window = {"startAt": end_at - _WINDOW_MS, "endAt": end_at}
        headers = {"Authorization": f"Bearer {self.api_token}"}
        website = f"{self.base_url}/api/websites/{self.website_id}"

        samples: list[MetricSample] = []
        response = await client.get(f"{website}/stats", params=window, headers=headers)
        response.raise_for_status()
        stats: dict[str, Any] = response.json() or {}
        for metric in _STATS_METRICS:
            value = _value(stats.get(metric))
            if value is not None:
                samples.append(MetricSample(source="umami", metric=metric, key="7d", value=value))

        response = await client.get(f"{website}/metrics", params={**window, "type": "url", "limit": 10}, headers=headers)
        response.raise_for_status()
        rows = response.json()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            path = row.get("x")
            count = _value(row.get("y"))
            if path and count is not None:
                samples.append(MetricSample(source="umami", metric="pageviews", key=str(path), value=count))
        return samples
