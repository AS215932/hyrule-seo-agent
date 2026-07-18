from __future__ import annotations

import httpx
import pytest
import respx

from app.beacon.client import BeaconClient, BeaconProtocolError
from app.beacon.models import WorkerEvent


@respx.mock
async def test_client_leases_and_posts_ordered_events() -> None:
    lease_route = respx.post("https://beacon.example/api/v1/beacon/worker/lease").mock(
        return_value=httpx.Response(
            200,
            json={
                "run": {
                    "id": "run-1",
                    "projectId": "project-1",
                    "threadId": "thread-1",
                    "mode": "measure",
                    "scopes": ["http"],
                    "status": "leased",
                    "events": [],
                    "actions": [],
                },
                "leaseToken": "lease-secret",
                "leaseExpiresAt": "2026-07-18T12:00:00Z",
            },
        )
    )
    event_route = respx.post(
        "https://beacon.example/api/v1/beacon/worker/runs/run-1/events"
    ).mock(return_value=httpx.Response(202, json={"accepted": 1}))
    async with httpx.AsyncClient() as http:
        client = BeaconClient(http, base_url="https://beacon.example/", token="worker-secret")
        lease = await client.lease()
        assert lease is not None
        await client.post_events(
            lease.run.id,
            lease.lease_token,
            [
                WorkerEvent(
                    id="34bd3cf8-e110-49fe-9f6c-3bb7b3e62ff4",
                    sequence=0,
                    type="log",
                    message="hello",
                )
            ],
        )
    assert lease_route.calls[0].request.headers["authorization"] == "Bearer worker-secret"
    assert event_route.calls[0].request.headers["x-beacon-lease-token"] == "lease-secret"
    assert b'"sequence":0' in event_route.calls[0].request.content


@respx.mock
async def test_client_treats_no_work_as_none_and_redacts_token_from_errors() -> None:
    respx.post("https://beacon.example/api/v1/beacon/worker/lease").mock(
        side_effect=[httpx.Response(204), httpx.Response(401, text="invalid credential")]
    )
    async with httpx.AsyncClient() as http:
        client = BeaconClient(http, base_url="https://beacon.example", token="do-not-leak")
        assert await client.lease() is None
        with pytest.raises(BeaconProtocolError, match="invalid credential") as error:
            await client.lease()
    assert "do-not-leak" not in str(error.value)
