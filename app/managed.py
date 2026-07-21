"""Long-running Beacon lease loop and run-level lease renewal."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import json
import httpx
import structlog

from app.beacon.client import BeaconClient
from app.beacon.models import BeaconLease
from app.config import Settings
from app.graph import GraphOutcome, delete_graph_checkpoint, run_graph
from app.store import Store

log = structlog.get_logger()
_PENDING_CLEANUP_KEY = "beacon_pending_checkpoint_cleanup"


@dataclass(frozen=True, slots=True)
class ManagedRunResult:
    leased: bool
    ok: bool
    awaiting_approval: bool = False
    error: str | None = None


class ManagedWorker:
    def __init__(self, *, settings: Settings, store: Store, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._store = store
        self._http = http
        self._client = BeaconClient(
            http,
            base_url=settings.beacon_control_plane_url,
            token=settings.beacon_worker_token,
        )
        self._task: asyncio.Task[None] | None = None
        self.current_run_id: str | None = None
        self.last_error: str | None = None
        self.finding_counts: dict[str, int] = {}

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="beacon-managed-worker")
        log.info("beacon_worker_started", control_plane=self._settings.beacon_control_plane_url)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def run_once(self) -> ManagedRunResult:
        if not await self._retry_pending_cleanups():
            return ManagedRunResult(leased=False, ok=False, error=self.last_error)
        lease = await self._client.lease()
        if lease is None:
            return ManagedRunResult(leased=False, ok=True)
        self.current_run_id = lease.run.id
        renewal = asyncio.create_task(
            self._renew_lease(lease.run.id, lease.lease_token),
            name=f"beacon-lease-renewal-{lease.run.id}",
        )
        try:
            try:
                outcome = await self._run_with_lease_guard(lease, renewal)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._report_graph_failure(lease, exc)
                return ManagedRunResult(leased=True, ok=False, error=self.last_error)

            self.finding_counts = self._count_findings(outcome)
            if outcome.awaiting_approval:
                self.last_error = None
                return ManagedRunResult(leased=True, ok=True, awaiting_approval=True)

            failed = [
                execution
                for execution in outcome.state.get("executions", [])
                if isinstance(execution, dict) and execution.get("status") == "failed"
            ]
            status = "failed" if failed else "succeeded"
            error_message = f"{len(failed)} automatic action execution(s) failed" if failed else None
            try:
                await self._client.complete(
                    lease.run.id,
                    lease.lease_token,
                    status=status,
                    error_message=error_message,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
                log.exception(
                    "beacon_completion_not_acknowledged",
                    run_id=lease.run.id,
                    completion_status=status,
                )
                return ManagedRunResult(leased=True, ok=False, error=self.last_error)
            checkpoint_deleted = await self._delete_acknowledged_checkpoint(lease)
            if checkpoint_deleted:
                self.last_error = error_message
            return ManagedRunResult(
                leased=True,
                ok=not failed and checkpoint_deleted,
                error=self.last_error,
            )
        finally:
            renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await renewal
            self.current_run_id = None

    async def _run_with_lease_guard(
        self,
        lease: BeaconLease,
        renewal: asyncio.Task[None],
    ) -> GraphOutcome:
        graph = asyncio.create_task(
            run_graph(
                lease=lease,
                beacon=self._client,
                settings=self._settings,
                store=self._store,
                http=self._http,
            ),
            name=f"beacon-graph-{lease.run.id}",
        )
        try:
            done, _ = await asyncio.wait({graph, renewal}, return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                await renewal
                raise RuntimeError("lease renewal task stopped unexpectedly")
            return await graph
        finally:
            if not graph.done():
                graph.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await graph

    async def _report_graph_failure(self, lease: BeaconLease, exc: Exception) -> None:
        self.last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
        log.exception("beacon_run_failed", run_id=lease.run.id)
        try:
            await self._client.complete(
                lease.run.id,
                lease.lease_token,
                status="failed",
                error_message=self.last_error,
            )
        except Exception:
            log.exception("beacon_failure_not_acknowledged", run_id=lease.run.id)
            return
        await self._delete_acknowledged_checkpoint(lease)

    async def _delete_acknowledged_checkpoint(self, lease: BeaconLease) -> bool:
        try:
            await delete_graph_checkpoint(self._settings, lease.run.thread_id)
        except Exception as exc:
            await self._remember_pending_cleanup(lease.run.thread_id)
            self.last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
            log.exception("beacon_checkpoint_cleanup_failed", run_id=lease.run.id)
            return False
        await self._forget_pending_cleanup(lease.run.thread_id)
        return True

    async def _pending_cleanups(self) -> list[str]:
        raw = await self._store.get_kv(_PENDING_CLEANUP_KEY)
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except json.JSONDecodeError, TypeError:
            return []
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    async def _remember_pending_cleanup(self, thread_id: str) -> None:
        pending = set(await self._pending_cleanups())
        pending.add(thread_id)
        await self._store.set_kv(_PENDING_CLEANUP_KEY, json.dumps(sorted(pending)))

    async def _forget_pending_cleanup(self, thread_id: str) -> None:
        pending = set(await self._pending_cleanups())
        pending.discard(thread_id)
        await self._store.set_kv(_PENDING_CLEANUP_KEY, json.dumps(sorted(pending)))

    async def _retry_pending_cleanups(self) -> bool:
        for thread_id in await self._pending_cleanups():
            try:
                await delete_graph_checkpoint(self._settings, thread_id)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
                log.exception("beacon_checkpoint_cleanup_retry_failed", thread_id=thread_id)
                return False
            await self._forget_pending_cleanup(thread_id)
        return True

    @staticmethod
    def _count_findings(outcome: GraphOutcome) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in outcome.state.get("findings", []):
            if not isinstance(finding, dict):
                continue
            severity = finding.get("severity")
            if isinstance(severity, str):
                counts[severity] = counts.get(severity, 0) + 1
        return counts

    async def _renew_lease(self, run_id: str, lease_token: str) -> None:
        while True:
            await asyncio.sleep(45)
            await self._client.renew_lease(run_id, lease_token)

    async def _loop(self) -> None:
        while True:
            try:
                result = await self.run_once()
                if not result.leased or not result.ok:
                    await asyncio.sleep(self._settings.beacon_poll_interval_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
                log.exception("beacon_poll_failed")
                await asyncio.sleep(self._settings.beacon_poll_interval_s)


async def run_one_managed_lease(settings: Settings, store: Store, http: httpx.AsyncClient) -> ManagedRunResult:
    """CLI/test seam: lease and execute at most one control-plane run."""

    if not settings.beacon_configured:
        raise RuntimeError("BEACON_CONTROL_PLANE_URL and BEACON_WORKER_TOKEN are required")
    return await ManagedWorker(settings=settings, store=store, http=http).run_once()
