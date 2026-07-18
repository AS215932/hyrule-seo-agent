from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from app.beacon.models import BeaconLease, ExistingEvent, RunAction, WorkerEvent
from app.config import Settings
from app.graph import run_graph
from app.store import Store


class RecordingBeacon:
    def __init__(self) -> None:
        self.events: list[WorkerEvent] = []

    async def post_events(self, run_id: str, lease_token: str, events: list[WorkerEvent]) -> None:
        self.events.extend(events)


def _lease(
    *, actions: list[RunAction] | None = None, events: list[ExistingEvent] | None = None
) -> BeaconLease:
    return BeaconLease.model_validate(
        {
            "run": {
                "id": "run-graph",
                "projectId": "project-1",
                "threadId": "thread-graph",
                "mode": "optimize",
                "scopes": ["x402"],
                "status": "leased",
                "events": [event.model_dump(mode="json") for event in events or []],
                "actions": [action.model_dump(by_alias=True, mode="json") for action in actions or []],
            },
            "leaseToken": "lease-token",
            "leaseExpiresAt": datetime.now(UTC) + timedelta(minutes=5),
        }
    )


async def test_graph_checkpoints_at_approval_and_resumes_same_thread(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200,
                json={
                    "paths": {
                        "/openapi-only": {
                            "get": {
                                "description": "A paid operation present only in the OpenAPI contract.",
                                "x-payment-info": {"x402Version": 2},
                            }
                        }
                    }
                },
            )
        if request.url.path == "/.well-known/x402.json":
            return httpx.Response(
                200,
                json={
                    "x402Version": 2,
                    "resources": [{"method": "GET", "path": "/manifest-only"}],
                },
            )
        return httpx.Response(200, json={"status": "ok"})

    settings = Settings(
        data_dir=str(tmp_path),
        x402_base_url="https://cloud.example",
        beacon_execute_automatic_actions=False,
    )
    store = Store(tmp_path / "seo.db")
    await store.connect()
    recorder = RecordingBeacon()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        first = await run_graph(
            lease=_lease(), beacon=recorder, settings=settings, store=store, http=http
        )
        assert first.awaiting_approval is True
        proposal_event = next(event for event in recorder.events if event.type == "action_proposed")
        assert recorder.events[-1].type == "awaiting_approval"

        action = RunAction.model_validate(
            {
                "id": "action-1",
                "type": proposal_event.data["actionType"],
                "risk": "approval_required",
                "status": "approved",
                "proposalHash": "a" * 64,
                "payloadJson": json.dumps(proposal_event.data["payload"]),
                "validationJson": json.dumps(proposal_event.data["validation"]),
                "idempotencyKey": proposal_event.data["idempotencyKey"],
            }
        )
        prior_events = [
            ExistingEvent(id=event.id, sequence=event.sequence, type=event.type)
            for event in recorder.events
        ]
        resumed = await run_graph(
            lease=_lease(actions=[action], events=prior_events),
            beacon=recorder,
            settings=settings,
            store=store,
            http=http,
        )
    await store.close()
    assert resumed.awaiting_approval is False
    result = next(event for event in recorder.events if event.type == "action_result")
    assert result.data["status"] == "manual_required"
    assert resumed.state["summary"]["actions"] == 1
