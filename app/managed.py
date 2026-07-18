"""Long-running Beacon lease loop and run-level lease renewal."""

from __future__ import annotations

import asyncio
import contextlib
import httpx
import structlog

from app.beacon.client import BeaconClient, BeaconProtocolError
from app.config import Settings
from app.graph import run_graph
from app.store import Store

log = structlog.get_logger()


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

    async def run_once(self) -> bool:
        lease = await self._client.lease()
        if lease is None:
            return False
        self.current_run_id = lease.run.id
        renewal = asyncio.create_task(
            self._renew_lease(lease.run.id, lease.lease_token),
            name=f"beacon-lease-renewal-{lease.run.id}",
        )
        try:
            outcome = await run_graph(
                lease=lease,
                beacon=self._client,
                settings=self._settings,
                store=self._store,
                http=self._http,
            )
            if not outcome.awaiting_approval:
                await self._client.complete(
                    lease.run.id, lease.lease_token, status="succeeded"
                )
            self.last_error = None
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
            log.exception("beacon_run_failed", run_id=lease.run.id)
            with contextlib.suppress(httpx.HTTPError, BeaconProtocolError):
                await self._client.complete(
                    lease.run.id,
                    lease.lease_token,
                    status="failed",
                    error_message=self.last_error,
                )
            return True
        finally:
            renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError, BeaconProtocolError, httpx.HTTPError):
                await renewal
            self.current_run_id = None

    async def _renew_lease(self, run_id: str, lease_token: str) -> None:
        while True:
            await asyncio.sleep(45)
            await self._client.renew_lease(run_id, lease_token)

    async def _loop(self) -> None:
        while True:
            try:
                worked = await self.run_once()
                if not worked:
                    await asyncio.sleep(self._settings.beacon_poll_interval_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
                log.exception("beacon_poll_failed")
                await asyncio.sleep(self._settings.beacon_poll_interval_s)


async def run_one_managed_lease(
    settings: Settings, store: Store, http: httpx.AsyncClient
) -> bool:
    """CLI/test seam: lease and execute at most one control-plane run."""

    if not settings.beacon_configured:
        raise RuntimeError("BEACON_CONTROL_PLANE_URL and BEACON_WORKER_TOKEN are required")
    return await ManagedWorker(settings=settings, store=store, http=http).run_once()
