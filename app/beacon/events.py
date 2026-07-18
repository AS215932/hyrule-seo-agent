"""Ordered, immediately durable worker-event emission."""

from __future__ import annotations

import uuid
from typing import Any

from app.beacon.client import BeaconClient
from app.beacon.models import BeaconLease, EventType, WorkerEvent


class EventEmitter:
    def __init__(self, client: BeaconClient | None, lease: BeaconLease) -> None:
        self._client = client
        self._lease = lease
        self._sequence = max((event.sequence for event in lease.run.events), default=-1) + 1
        self.events: list[WorkerEvent] = []

    async def emit(
        self,
        event_type: EventType,
        message: str,
        *,
        node: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> WorkerEvent:
        event = WorkerEvent(
            id=str(uuid.uuid4()),
            sequence=self._sequence,
            type=event_type,
            node=node,
            message=message,
            data=data or {},
        )
        self._sequence += 1
        self.events.append(event)
        if self._client is not None:
            await self._client.post_events(
                self._lease.run.id, self._lease.lease_token, [event]
            )
        return event
