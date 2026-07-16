"""Best-effort agent-core TraceEvent emission for SEO Agent runs.

Emission is strictly best-effort: a missing ``agent-core`` install, collector
failures, invalid payload shapes, and file/HTTP errors are all swallowed so
the audit/draft pipeline can never be affected by observability delivery.
Mirrors ``hyrule-soc-agent/app/agent_core_trace.py`` with SEO-shaped events.

Contract note (agent-core v0.8.0): ``InsightLoop``/``LoopKind`` have no
``"seo"`` member, so this module emits plain TraceEvents only — the
``seo_pr_decision`` event carries the decision payload that would otherwise
ride a LoopDecisionEnvelope. Upgrade to envelope emission once agent-core
ships a tag whose ``InsightLoop`` includes ``"seo"``.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping
from typing import Any

FLAG_ENV = "HYRULE_SEO_AGENT_CORE_TRACE"
_TRUTHY = {"1", "true", "yes", "on"}
GRAPH_ID = "seo-agent"
AGENT_ROLE = "seo_agent"


def enabled() -> bool:
    return os.environ.get(FLAG_ENV, "").strip().lower() in _TRUTHY


def _environment() -> str:
    return os.environ.get("SEO_AGENT_ENVIRONMENT", "production")


def emit_run_trace(
    kind: str,
    *,
    run_id: str,
    ok: bool,
    summary: str,
    stats: Mapping[str, Any] | None = None,
    cost: Mapping[str, Any] | None = None,
) -> int:
    """Emit one run-summary TraceEvent for a pipeline phase; return delivered count."""
    if not enabled():
        return 0
    try:
        tracing_mod = importlib.import_module("agent_core.contracts.tracing")
        sink_mod = importlib.import_module("agent_core.tracing.sink")
        cost_obj = None
        if cost:
            models_mod = importlib.import_module("agent_core.contracts.models")
            cost_obj = models_mod.CostUsage.model_validate(dict(cost))
        event = tracing_mod.TraceEvent(
            event_type="seo_run_summary",
            graph_id=GRAPH_ID,
            node_id=kind,
            agent_role=AGENT_ROLE,
            environment=_environment(),
            run_id=run_id,
            trace_id=run_id,
            summary=_safe_text(f"{kind}: {summary}"),
            cost=cost_obj,
            payload={
                "kind": kind,
                "ok": ok,
                **_jsonish(dict(stats or {})),
                # Crawl/GSC/referrer-derived text is untrusted site/search data;
                # never re-feed trace payloads to a model raw.
                "untrusted_loop_text": True,
                "model_consumption_allowed": False,
            },
        )
        sink = sink_mod.sink_from_env(FLAG_ENV)
        return 1 if sink.emit(event) else 0
    except Exception:
        return 0


def emit_pr_decision(
    *,
    run_id: str,
    decision: str,
    title: str,
    branch: str = "",
    pr_url: str = "",
    finding_fingerprints: list[str] | None = None,
    rationale: str = "",
    dry_run: bool = True,
) -> int:
    """Emit the draft/stay_silent decision for one draft cycle."""
    if not enabled():
        return 0
    try:
        tracing_mod = importlib.import_module("agent_core.contracts.tracing")
        sink_mod = importlib.import_module("agent_core.tracing.sink")
        links = []
        if pr_url:
            links.append({"kind": "other", "label": "Draft PR", "url": pr_url, "ref_id": branch})
        event = tracing_mod.TraceEvent(
            event_type="seo_pr_decision",
            graph_id=GRAPH_ID,
            node_id="draft",
            agent_role=AGENT_ROLE,
            environment=_environment(),
            run_id=run_id,
            trace_id=run_id,
            repository="AS215932/hyrule-web",
            summary=_safe_text(f"draft decision: {decision} — {title}" if title else f"draft decision: {decision}"),
            links=links,
            payload={
                "decision": decision,
                "branch": branch,
                "pr_url": pr_url,
                "dry_run": dry_run,
                "finding_fingerprints": list(finding_fingerprints or []),
                "rationale": _safe_text(rationale, limit=400),
                "untrusted_loop_text": True,
                "model_consumption_allowed": False,
            },
        )
        sink = sink_mod.sink_from_env(FLAG_ENV)
        return 1 if sink.emit(event) else 0
    except Exception:
        return 0


def _safe_text(value: Any, *, limit: int = 120) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _jsonish(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonish(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonish(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
