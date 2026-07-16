"""Process-wide Prometheus series (importable by pipeline and main without cycles)."""

from __future__ import annotations

from prometheus_client import Counter, Gauge

RUNS_TOTAL = Counter("seo_agent_runs_total", "Pipeline phase runs", ["kind", "ok"])
ACTIVE_FINDINGS = Gauge("seo_agent_active_findings", "Unresolved findings", ["severity"])
LAST_RUN_TS = Gauge("seo_agent_last_run_timestamp_seconds", "Unix time of last run", ["kind"])
