from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.beacon.events import EventEmitter
from app.beacon.models import BeaconLease, ExistingEvent, WorkerEvent


class RecordingClient:
    def __init__(self) -> None:
        self.events: list[WorkerEvent] = []

    async def post_events(self, run_id: str, lease_token: str, events: list[WorkerEvent]) -> None:
        self.events.extend(events)


def _lease(events: list[ExistingEvent] | None = None) -> BeaconLease:
    return BeaconLease.model_validate(
        {
            "run": {
                "id": "run-1",
                "projectId": "project-1",
                "threadId": "thread-1",
                "mode": "measure",
                "scopes": ["http"],
                "status": "leased",
                "events": [event.model_dump(mode="json") for event in events or []],
                "actions": [],
            },
            "leaseToken": "lease-token",
            "leaseExpiresAt": datetime.now(UTC) + timedelta(minutes=5),
        }
    )


async def test_retried_logical_event_reuses_id_and_is_not_reposted() -> None:
    first_client = RecordingClient()
    first = EventEmitter(first_client, _lease())
    accepted = await first.emit(
        "finding",
        "Catalog drift",
        node="audit",
        data={"code": "x402.catalog.drift", "severity": "error"},
    )

    retry_client = RecordingClient()
    retry = EventEmitter(
        retry_client,
        _lease(
            [
                ExistingEvent(
                    id=accepted.id,
                    sequence=accepted.sequence,
                    type=accepted.type,
                )
            ]
        ),
    )
    retried = await retry.emit(
        "finding",
        "Catalog drift",
        node="audit",
        data={"severity": "error", "code": "x402.catalog.drift"},
    )
    next_event = await retry.emit("node_completed", "Audit complete", node="audit")

    assert retried.id == accepted.id
    assert retried.sequence == accepted.sequence
    assert retry_client.events == [next_event]
    assert next_event.sequence == accepted.sequence + 1
