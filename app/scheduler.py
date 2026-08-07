"""In-process asyncio scheduler (NOC proactive-loop pattern).

One task per cadence; a failed cycle logs and waits for the next tick — the
pipeline functions already guarantee they don't raise, this is belt and
braces so the process outlives anything unexpected.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog

from app.pipeline import (
    Deps,
    PhaseOutcome,
    run_audit,
    run_draft,
    run_indexnow,
    run_metrics,
    run_report,
    run_surface,
)

log = structlog.get_logger()

Phase = Callable[[Deps], Awaitable[PhaseOutcome]]


class Scheduler:
    def __init__(self, deps: Deps) -> None:
        self._deps = deps
        self._tasks: list[asyncio.Task[None]] = []

    def jobs(self) -> list[tuple[str, Phase, int, int]]:
        """(name, phase, interval_s, initial_delay_s) — staggered starts so a
        boot doesn't fire every collector at once."""
        s = self._deps.settings
        return [
            ("audit", run_audit, s.audit_interval_s, min(15, s.audit_interval_s)),
            ("surface", run_surface, s.surface_interval_s, min(30, s.surface_interval_s)),
            ("metrics", run_metrics, s.metrics_interval_s, min(60, s.metrics_interval_s)),
            ("indexnow", run_indexnow, s.indexnow_interval_s, min(120, s.indexnow_interval_s)),
            ("draft", run_draft, s.draft_interval_s, min(300, s.draft_interval_s)),
            ("report", run_report, s.report_interval_s, min(600, s.report_interval_s)),
        ]

    def start(self) -> None:
        for name, phase, interval, delay in self.jobs():
            self._tasks.append(asyncio.create_task(self._loop(name, phase, interval, delay)))
        log.info("scheduler_started", jobs=[j[0] for j in self.jobs()])

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    async def _loop(self, name: str, phase: Phase, interval: int, delay: int) -> None:
        await asyncio.sleep(delay)
        while True:
            try:
                await phase(self._deps)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("job_cycle_failed", job=name)
            await asyncio.sleep(interval)
