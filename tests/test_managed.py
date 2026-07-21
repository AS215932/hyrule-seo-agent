from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

import app.managed as managed_module
from app.beacon.models import BeaconLease
from app.config import Settings
from app.graph import GraphOutcome
from app.managed import ManagedWorker, run_one_managed_lease
from app.store import Store


def _lease() -> BeaconLease:
    return BeaconLease.model_validate(
        {
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
            "leaseToken": "lease-1",
            "leaseExpiresAt": datetime.now(UTC) + timedelta(minutes=5),
        }
    )


class FakeClient:
    def __init__(self, lease: BeaconLease | None) -> None:
        self.next_lease = lease
        self.completions: list[tuple[str, str | None]] = []
        self.renewals = 0
        self.completion_error: Exception | None = None

    async def lease(self):
        value, self.next_lease = self.next_lease, None
        return value

    async def complete(self, run_id, lease_token, *, status, error_message=None):
        if self.completion_error is not None:
            raise self.completion_error
        self.completions.append((status, error_message))
        return {}

    async def renew_lease(self, run_id, lease_token):
        self.renewals += 1


@pytest.fixture
async def worker_deps(tmp_path: Path):
    settings = Settings(
        data_dir=str(tmp_path),
        beacon_control_plane_url="https://beacon.example",
        beacon_worker_token="token",
        beacon_poll_interval_s=0,
    )
    store = Store(tmp_path / "seo.db")
    await store.connect()
    async with httpx.AsyncClient() as http:
        yield settings, store, http
    await store.close()


async def test_run_once_handles_no_work(worker_deps) -> None:
    settings, store, http = worker_deps
    worker = ManagedWorker(settings=settings, store=store, http=http)
    worker._client = FakeClient(None)
    assert await worker.run_once() is False


async def test_run_once_completes_success_and_pauses_without_completion(worker_deps, monkeypatch) -> None:
    settings, store, http = worker_deps
    worker = ManagedWorker(settings=settings, store=store, http=http)
    fake = FakeClient(_lease())
    worker._client = fake

    async def success(**kwargs):
        return GraphOutcome(state={"summary": {}}, awaiting_approval=False)

    monkeypatch.setattr(managed_module, "run_graph", success)
    assert await worker.run_once() is True
    assert fake.completions == [("succeeded", None)]
    assert worker.current_run_id is None
    assert worker.last_error is None

    fake.next_lease = _lease()

    async def paused(**kwargs):
        return GraphOutcome(state={}, awaiting_approval=True)

    monkeypatch.setattr(managed_module, "run_graph", paused)
    assert await worker.run_once() is True
    assert fake.completions == [("succeeded", None)]


async def test_run_once_records_failure_and_reports_it(worker_deps, monkeypatch) -> None:
    settings, store, http = worker_deps
    worker = ManagedWorker(settings=settings, store=store, http=http)
    fake = FakeClient(_lease())
    worker._client = fake

    async def broken(**kwargs):
        raise ValueError("bad graph")

    monkeypatch.setattr(managed_module, "run_graph", broken)
    assert await worker.run_once() is True
    assert worker.last_error == "ValueError: bad graph"
    assert fake.completions[0][0] == "failed"
    assert "bad graph" in (fake.completions[0][1] or "")


async def test_failed_execution_completes_the_run_as_failed(worker_deps, monkeypatch) -> None:
    settings, store, http = worker_deps
    worker = ManagedWorker(settings=settings, store=store, http=http)
    fake = FakeClient(_lease())
    worker._client = fake

    async def action_failed(**kwargs):
        return GraphOutcome(
            state={
                "findings": [{"severity": "warning"}],
                "executions": [{"idempotencyKey": "indexnow", "status": "failed"}],
            },
            awaiting_approval=False,
        )

    monkeypatch.setattr(managed_module, "run_graph", action_failed)

    assert await worker.run_once() is True
    assert fake.completions == [("failed", "1 automatic action execution(s) failed")]
    assert worker.finding_counts == {"warning": 1}


async def test_completion_failure_retains_the_terminal_checkpoint(worker_deps, monkeypatch) -> None:
    settings, store, http = worker_deps
    worker = ManagedWorker(settings=settings, store=store, http=http)
    fake = FakeClient(_lease())
    fake.completion_error = httpx.ConnectError("control plane unavailable")
    worker._client = fake
    deleted: list[str] = []

    async def success(**kwargs):
        return GraphOutcome(state={"findings": [], "executions": []}, awaiting_approval=False)

    async def record_delete(settings, thread_id):
        deleted.append(thread_id)

    monkeypatch.setattr(managed_module, "run_graph", success)
    monkeypatch.setattr(managed_module, "delete_graph_checkpoint", record_delete)

    assert await worker.run_once() is True
    assert deleted == []
    assert "control plane unavailable" in (worker.last_error or "")


async def test_renewal_failure_cancels_the_active_graph(worker_deps, monkeypatch) -> None:
    settings, store, http = worker_deps
    worker = ManagedWorker(settings=settings, store=store, http=http)
    fake = FakeClient(_lease())
    worker._client = fake
    cancelled = False

    async def renewal_failed(run_id, lease_token):
        raise httpx.ConnectError("heartbeat failed")

    async def slow_graph(**kwargs):
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    monkeypatch.setattr(worker, "_renew_lease", renewal_failed)
    monkeypatch.setattr(managed_module, "run_graph", slow_graph)

    assert await worker.run_once() is True
    assert cancelled is True
    assert fake.completions[0][0] == "failed"
    assert "heartbeat failed" in (fake.completions[0][1] or "")


async def test_start_and_stop_are_idempotent(worker_deps) -> None:
    settings, store, http = worker_deps
    worker = ManagedWorker(settings=settings, store=store, http=http)
    worker._client = FakeClient(None)
    worker.start()
    task = worker._task
    worker.start()
    assert worker._task is task
    await worker.stop()
    await worker.stop()
    assert worker._task is None


async def test_one_lease_requires_managed_credentials(tmp_path: Path) -> None:
    settings = Settings(data_dir=str(tmp_path))
    store = Store(tmp_path / "seo.db")
    await store.connect()
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="BEACON_CONTROL_PLANE_URL"):
            await run_one_managed_lease(settings, store, http)
    await store.close()
