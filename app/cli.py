"""seoctl — one-shot Beacon leases and evidence phases for operator checks."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx

from app.config import settings
from app.managed import run_one_managed_lease
from app.pipeline import Deps, run_audit, run_indexnow, run_metrics, run_report
from app.report import build_summary
from app.store import Store

_PHASES = {
    "run-audit": run_audit,
    "run-metrics": run_metrics,
    "indexnow": run_indexnow,
    "report": run_report,
}


async def _with_deps(command: str) -> int:
    store = Store(Path(settings.data_dir) / "seo.db")
    await store.connect()
    async with httpx.AsyncClient(
        timeout=settings.crawl_timeout_s, headers={"User-Agent": settings.user_agent}
    ) as client:
        deps = Deps(settings=settings, store=store, client=client)
        try:
            if command == "status":
                print(json.dumps(await build_summary(store), indent=2, default=str))
                return 0
            if command == "beacon-once":
                managed_result = await run_one_managed_lease(settings, store, client)
                print(
                    json.dumps(
                        {
                            "leased": managed_result.leased,
                            "ok": managed_result.ok,
                            "awaitingApproval": managed_result.awaiting_approval,
                            "error": managed_result.error,
                        },
                        indent=2,
                    )
                )
                return 0 if managed_result.ok else 1
            phase_outcome = await _PHASES[command](deps)
            print(
                json.dumps(
                    {
                        "kind": phase_outcome.kind,
                        "ok": phase_outcome.ok,
                        "summary": phase_outcome.summary,
                        "stats": phase_outcome.stats,
                    },
                    indent=2,
                    default=str,
                )
            )
            return 0 if phase_outcome.ok else 1
        finally:
            await store.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="seoctl", description=__doc__)
    parser.add_argument("command", choices=[*_PHASES.keys(), "beacon-once", "status"])
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_with_deps(args.command)))


if __name__ == "__main__":
    main()
