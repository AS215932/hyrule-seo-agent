"""Small outbound-only client for the Beacon worker API."""

from __future__ import annotations

from typing import Any

import httpx

from app.beacon.models import BeaconLease, EvidenceUpload, WorkerEvent


class BeaconProtocolError(RuntimeError):
    """The control plane rejected or returned an invalid worker response."""


class BeaconClient:
    def __init__(self, client: httpx.AsyncClient, *, base_url: str, token: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._token = token

    @property
    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._token}"}

    async def lease(self) -> BeaconLease | None:
        response = await self._client.post(
            f"{self._base_url}/api/v1/beacon/worker/lease",
            headers=self._headers,
            json={"capabilities": ["http", "x402", "distribution", "actions"]},
        )
        if response.status_code == 204:
            return None
        self._raise(response)
        return BeaconLease.model_validate(response.json())

    async def post_events(self, run_id: str, lease_token: str, events: list[WorkerEvent]) -> None:
        if not events:
            return
        response = await self._client.post(
            f"{self._base_url}/api/v1/beacon/worker/runs/{run_id}/events",
            headers={**self._headers, "x-beacon-lease-token": lease_token},
            json={"events": [event.model_dump(by_alias=True, mode="json") for event in events]},
        )
        self._raise(response)

    async def upload_evidence(
        self,
        run_id: str,
        lease_token: str,
        body: bytes,
        *,
        content_type: str,
    ) -> EvidenceUpload:
        response = await self._client.post(
            f"{self._base_url}/api/v1/beacon/worker/runs/{run_id}/evidence",
            headers={
                **self._headers,
                "x-beacon-lease-token": lease_token,
                "content-type": content_type,
            },
            content=body,
        )
        self._raise(response)
        return EvidenceUpload.model_validate(response.json())

    async def complete(
        self,
        run_id: str,
        lease_token: str,
        *,
        status: str,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        response = await self._client.post(
            f"{self._base_url}/api/v1/beacon/worker/runs/{run_id}/complete",
            headers={**self._headers, "x-beacon-lease-token": lease_token},
            json={"status": status, "errorMessage": error_message},
        )
        self._raise(response)
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    async def heartbeat(self) -> None:
        response = await self._client.post(f"{self._base_url}/api/v1/beacon/worker/heartbeat", headers=self._headers)
        self._raise(response)

    async def renew_lease(self, run_id: str, lease_token: str) -> None:
        response = await self._client.post(
            f"{self._base_url}/api/v1/beacon/worker/runs/{run_id}/heartbeat",
            headers={**self._headers, "x-beacon-lease-token": lease_token},
        )
        self._raise(response)

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text[:1_000]
            raise BeaconProtocolError(f"Beacon returned {response.status_code}: {detail}") from exc
