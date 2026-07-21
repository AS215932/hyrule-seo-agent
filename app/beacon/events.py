"""Ordered, immediately durable worker-event emission."""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.beacon.client import BeaconClient
from app.beacon.models import BeaconLease, EventType, WorkerEvent


class EventEmitter:
    def __init__(self, client: BeaconClient | None, lease: BeaconLease) -> None:
        self._client = client
        self._lease = lease
        self._sequence = max((event.sequence for event in lease.run.events), default=-1) + 1
        self._accepted = {event.id: event.sequence for event in lease.run.events}
        self.events: list[WorkerEvent] = []

    def _event_id(
        self,
        event_type: EventType,
        message: str,
        node: str | None,
        data: dict[str, Any],
    ) -> str:
        identity: dict[str, Any] = {
            "runId": self._lease.run.id,
            "type": event_type,
            "node": node,
            "message": message,
            "data": data,
        }
        idempotency_key = data.get("idempotencyKey")
        if event_type == "action_result" and isinstance(idempotency_key, str) and idempotency_key:
            identity = {
                "runId": self._lease.run.id,
                "type": event_type,
                "idempotencyKey": idempotency_key,
            }
        logical_event = json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
        )
        return str(uuid.uuid5(uuid.NAMESPACE_URL, logical_event))

    async def emit(
        self,
        event_type: EventType,
        message: str,
        *,
        node: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> WorkerEvent:
        payload = data or {}
        event_id = self._event_id(event_type, message, node, payload)
        accepted_sequence = self._accepted.get(event_id)
        event = WorkerEvent(
            id=event_id,
            sequence=accepted_sequence if accepted_sequence is not None else self._sequence,
            type=event_type,
            node=node,
            message=message,
            data=payload,
        )
        if accepted_sequence is not None:
            self.events.append(event)
            return event
        self._sequence += 1
        self._accepted[event_id] = event.sequence
        self.events.append(event)
        if self._client is not None:
            await self._client.post_events(self._lease.run.id, self._lease.lease_token, [event])
        return event
