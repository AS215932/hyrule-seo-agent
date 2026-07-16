"""agent-core trace emission: flag-gated, JSONL sink, never raises."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import agent_core_trace as act


def test_disabled_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(act.FLAG_ENV, raising=False)
    assert act.emit_run_trace("audit", run_id="r", ok=True, summary="s") == 0
    assert act.emit_pr_decision(run_id="r", decision="draft", title="t") == 0


def _enable_jsonl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    out = tmp_path / "trace.jsonl"
    monkeypatch.setenv(act.FLAG_ENV, "1")
    monkeypatch.setenv(f"{act.FLAG_ENV}_PATH", str(out))
    monkeypatch.setenv("SEO_AGENT_ENVIRONMENT", "test")
    return out


def test_run_trace_emits_valid_trace_event(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = _enable_jsonl(monkeypatch, tmp_path)
    delivered = act.emit_run_trace(
        "audit",
        run_id="audit-abc",
        ok=True,
        summary="12 pages",
        stats={"pages": 12, "weird": object()},
        cost={"model": "openrouter:x", "input_tokens": 10, "output_tokens": 2},
    )
    assert delivered == 1
    event = json.loads(out.read_text().splitlines()[-1])
    assert event["event_type"] == "seo_run_summary"
    assert event["graph_id"] == act.GRAPH_ID == "seo-agent"
    assert event["run_id"] == "audit-abc"
    assert event["environment"] == "test"
    assert event["payload"]["untrusted_loop_text"] is True
    assert event["payload"]["model_consumption_allowed"] is False
    assert event["payload"]["pages"] == 12
    assert isinstance(event["payload"]["weird"], str)  # jsonish coercion
    assert event["cost"]["input_tokens"] == 10

    # Round-trip through the real contract model.
    from agent_core.contracts.tracing import TraceEvent

    TraceEvent.model_validate(event)


def test_pr_decision_event_links_and_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = _enable_jsonl(monkeypatch, tmp_path)
    delivered = act.emit_pr_decision(
        run_id="draft-1",
        decision="draft",
        title="tighten meta",
        branch="seo-agent/20260717-abc",
        pr_url="https://github.com/AS215932/hyrule-web/pull/99",
        finding_fingerprints=["f1"],
        rationale="x" * 999,
        dry_run=False,
    )
    assert delivered == 1
    event = json.loads(out.read_text().splitlines()[-1])
    assert event["event_type"] == "seo_pr_decision"
    assert event["repository"] == "AS215932/hyrule-web"
    assert event["links"][0]["url"].endswith("/pull/99")
    assert event["payload"]["decision"] == "draft"
    assert len(event["payload"]["rationale"]) <= 400


def test_invalid_cost_is_swallowed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _enable_jsonl(monkeypatch, tmp_path)
    # CostUsage forbids extra fields → validation error → swallowed → 0.
    assert (
        act.emit_run_trace("audit", run_id="r", ok=True, summary="s", cost={"nope": 1}) == 0
    )


def test_safe_text_truncates() -> None:
    assert act._safe_text("a  b\n c") == "a b c"
    assert len(act._safe_text("x" * 500)) == 120
