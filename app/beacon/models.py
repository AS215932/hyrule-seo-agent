"""Typed wire models for ``/api/v1/beacon``.

The control plane is TypeScript and returns camelCase JSON. Explicit aliases
keep that public contract visible while Python code remains idiomatic.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

BeaconScope = Literal["http", "x402", "distribution"]
BeaconMode = Literal["measure", "optimize"]
ActionRisk = Literal["automatic", "approval_required", "manual"]
ActionStatus = Literal[
    "proposed", "approved", "rejected", "executing", "succeeded", "failed", "manual_required"
]
EventType = Literal[
    "node_started",
    "node_completed",
    "observation",
    "finding",
    "action_proposed",
    "action_result",
    "awaiting_approval",
    "log",
]


class WireModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class ExistingEvent(WireModel):
    id: str
    sequence: int
    type: EventType


class RunAction(WireModel):
    id: str
    type: str
    risk: ActionRisk
    status: ActionStatus
    proposal_hash: str = Field(alias="proposalHash")
    payload_json: str = Field(alias="payloadJson")
    validation_json: str = Field(alias="validationJson")
    idempotency_key: str = Field(alias="idempotencyKey")

    @property
    def payload(self) -> dict[str, Any]:
        value = json.loads(self.payload_json)
        return value if isinstance(value, dict) else {}


class BeaconRun(WireModel):
    id: str
    project_id: str = Field(alias="projectId")
    thread_id: str = Field(alias="threadId")
    mode: BeaconMode
    scopes: list[BeaconScope]
    status: str
    attempt: int = 0
    events: list[ExistingEvent] = Field(default_factory=list)
    actions: list[RunAction] = Field(default_factory=list)


class BeaconLease(WireModel):
    run: BeaconRun
    lease_token: str = Field(alias="leaseToken")
    lease_expires_at: datetime = Field(alias="leaseExpiresAt")


class WorkerEvent(WireModel):
    id: str
    sequence: int
    type: EventType
    node: str | None = None
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class ObservationPayload(WireModel):
    channel_key: str = Field(alias="channelKey")
    intent_id: str | None = Field(default=None, alias="intentId")
    target_key: str = Field(alias="targetKey")
    present: bool
    position: int | None = None
    score: float | None = None
    result_name: str | None = Field(default=None, alias="resultName")
    result_url: str | None = Field(default=None, alias="resultUrl")
    evidence_r2_key: str | None = Field(default=None, alias="evidenceR2Key")
    observed_at: datetime = Field(alias="observedAt")


class FindingPayload(WireModel):
    surface_id: str | None = Field(default=None, alias="surfaceId")
    channel_key: str | None = Field(default=None, alias="channelKey")
    severity: Literal["info", "warning", "error"]
    code: str
    title: str
    message: str
    evidence_r2_key: str | None = Field(default=None, alias="evidenceR2Key")


class ActionProposal(WireModel):
    channel_key: str | None = Field(default=None, alias="channelKey")
    action_type: str = Field(alias="actionType")
    risk: ActionRisk
    payload: dict[str, Any]
    validation: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(alias="idempotencyKey")
