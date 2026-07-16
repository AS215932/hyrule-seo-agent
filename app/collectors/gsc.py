"""Google Search Console (Search Analytics) collector.

Disabled unless ``credentials_path`` names an existing service-account file.
Windows end 3 days back because GSC data lags ~2 days; ``dataState: final``
keeps the two 28-day windows comparable across runs. ``collect`` never
raises — any auth or API failure logs a warning and yields ``[]``.
"""

from __future__ import annotations

import asyncio
import datetime
import urllib.parse
from pathlib import Path
from typing import Any

import google.auth
import httpx
import structlog
from google.auth.transport.requests import Request as GoogleAuthRequest

from app.models import MetricSample

log = structlog.get_logger()

_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
_WINDOW_DAYS = 28
_LAG_DAYS = 3
_TOTALS_METRICS = ("clicks", "impressions", "ctr", "position")


class GSCCollector:
    """Totals for the current and previous 28-day window plus top-10 pages."""

    def __init__(self, *, site_url: str, credentials_path: str) -> None:
        self.site_url = site_url
        self.credentials_path = credentials_path
        self._endpoint = (
            "https://searchconsole.googleapis.com/webmasters/v3/sites/"
            f"{urllib.parse.quote(site_url, safe='')}/searchAnalytics/query"
        )

    @property
    def enabled(self) -> bool:
        return bool(self.credentials_path) and Path(self.credentials_path).exists()

    async def _token(self) -> str:
        """Bearer token from the service-account file; the blocking google-auth
        load+refresh runs in a worker thread, never on the event loop."""

        def _load_and_refresh() -> str:
            credentials, _ = google.auth.load_credentials_from_file(self.credentials_path, scopes=[_SCOPE])
            credentials.refresh(GoogleAuthRequest())
            return str(credentials.token)

        return await asyncio.to_thread(_load_and_refresh)

    async def _query(self, client: httpx.AsyncClient, token: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        response = await client.post(self._endpoint, json=body, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        rows = response.json().get("rows", [])
        return rows if isinstance(rows, list) else []

    async def collect(self, client: httpx.AsyncClient) -> list[MetricSample]:
        if not self.enabled:
            return []
        try:
            return await self._collect(client)
        except Exception:
            log.warning("gsc_collect_failed", site_url=self.site_url, exc_info=True)
            return []

    async def _collect(self, client: httpx.AsyncClient) -> list[MetricSample]:
        token = await self._token()
        current_end = datetime.date.today() - datetime.timedelta(days=_LAG_DAYS)
        current_start = current_end - datetime.timedelta(days=_WINDOW_DAYS - 1)
        prev_end = current_start - datetime.timedelta(days=1)
        prev_start = prev_end - datetime.timedelta(days=_WINDOW_DAYS - 1)

        samples: list[MetricSample] = []
        for key, start, end in (("28d", current_start, current_end), ("28d_prev", prev_start, prev_end)):
            body: dict[str, Any] = {
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "dataState": "final",
            }
            for row in await self._query(client, token, body):
                for metric in _TOTALS_METRICS:
                    if metric in row:
                        samples.append(MetricSample(source="gsc", metric=metric, key=key, value=float(row[metric])))

        page_body: dict[str, Any] = {
            "startDate": current_start.isoformat(),
            "endDate": current_end.isoformat(),
            "dataState": "final",
            "dimensions": ["page"],
            "rowLimit": 10,
        }
        for row in await self._query(client, token, page_body):
            keys = row.get("keys") or []
            if keys and "clicks" in row:
                samples.append(
                    MetricSample(source="gsc", metric="clicks", key=str(keys[0]), value=float(row["clicks"]))
                )
        return samples
