from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from app.beacon.models import BeaconLease, EvidenceUpload, ExistingEvent, RunAction, WorkerEvent
from app.config import Settings
from app.graph import delete_graph_checkpoint, run_graph
from app.store import Store


class RecordingBeacon:
    def __init__(self) -> None:
        self.events: list[WorkerEvent] = []
        self.evidence: list[bytes] = []

    async def post_events(self, run_id: str, lease_token: str, events: list[WorkerEvent]) -> None:
        self.events.extend(events)

    async def upload_evidence(
        self,
        run_id: str,
        lease_token: str,
        body: bytes,
        *,
        content_type: str,
    ) -> EvidenceUpload:
        self.evidence.append(body)
        number = len(self.evidence)
        return EvidenceUpload(key=f"evidence/{number}", sha256=f"{number:064x}", sizeBytes=len(body))


def _lease(
    *,
    actions: list[RunAction] | None = None,
    events: list[ExistingEvent] | None = None,
    scopes: list[str] | None = None,
) -> BeaconLease:
    return BeaconLease.model_validate(
        {
            "run": {
                "id": "run-graph",
                "projectId": "project-1",
                "threadId": "thread-graph",
                "mode": "optimize",
                "scopes": scopes or ["x402"],
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
        first = await run_graph(lease=_lease(), beacon=recorder, settings=settings, store=store, http=http)
        assert first.awaiting_approval is True
        assert recorder.evidence
        audited_events = [event for event in recorder.events if event.type in {"finding", "observation"}]
        assert audited_events
        assert all(event.data["evidenceR2Key"].startswith("evidence/") for event in audited_events)
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
            ExistingEvent(id=event.id, sequence=event.sequence, type=event.type) for event in recorder.events
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
    with sqlite3.connect(tmp_path / "beacon-checkpoints.sqlite") as checkpoint_db:
        assert (
            checkpoint_db.execute("SELECT count(*) FROM checkpoints WHERE thread_id = ?", ("thread-graph",)).fetchone()[
                0
            ]
            > 0
        )

    await delete_graph_checkpoint(settings, "thread-graph")

    with sqlite3.connect(tmp_path / "beacon-checkpoints.sqlite") as checkpoint_db:
        assert checkpoint_db.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = ?", ("thread-graph",)
        ).fetchone() == (0,)
        assert checkpoint_db.execute(
            "SELECT count(*) FROM writes WHERE thread_id = ?", ("thread-graph",)
        ).fetchone() == (0,)


async def test_graph_executes_automatic_actions_before_approval_interrupt(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.indexnow.org":
            return httpx.Response(200)
        if request.url.path == "/sitemap.xml":
            return httpx.Response(
                200,
                text=(
                    '<?xml version="1.0"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    "<url><loc>https://site.example/</loc></url>"
                    "</urlset>"
                ),
            )
        if request.url.path == "/":
            return httpx.Response(200, text="<html><title>Hyrule</title></html>")
        return httpx.Response(200, text="ok")

    settings = Settings(
        data_dir=str(tmp_path),
        site_base_url="https://site.example",
        indexnow_key="k" * 32,
        beacon_execute_automatic_actions=True,
    )
    store = Store(tmp_path / "seo.db")
    await store.connect()
    await store.set_kv("sitemap_sha256", "previous-sitemap")
    recorder = RecordingBeacon()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        outcome = await run_graph(
            lease=_lease(scopes=["http"]),
            beacon=recorder,
            settings=settings,
            store=store,
            http=http,
        )
    await store.close()

    assert outcome.awaiting_approval is True
    automatic = next(
        event for event in recorder.events if event.type == "action_proposed" and event.data["risk"] == "automatic"
    )
    result_index = next(
        index
        for index, event in enumerate(recorder.events)
        if event.type == "action_result" and event.data["idempotencyKey"] == automatic.data["idempotencyKey"]
    )
    approval_index = next(index for index, event in enumerate(recorder.events) if event.type == "awaiting_approval")
    assert result_index < approval_index
    assert recorder.events[result_index].data["status"] == "succeeded"
