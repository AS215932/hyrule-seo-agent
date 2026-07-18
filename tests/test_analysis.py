from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pydantic_ai
import pytest

import app.analysis as analysis
from app.analysis import Priority, VisibilityAnalysis, analyze_with_model
from app.config import Settings


def test_model_id_reads_policy_and_falls_back(tmp_path: Path) -> None:
    policy = tmp_path / "models.toml"
    policy.write_text('[model]\nprimary = "openrouter:test/model"\n')
    assert analysis._model_id(Settings(model_policy_path=str(policy))) == "openrouter:test/model"
    assert analysis._model_id(Settings(model_policy_path=str(tmp_path / "missing"))).startswith(
        "openrouter:"
    )


def test_model_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unsupported model"):
        analysis._model("other:model", "secret")


async def test_analysis_is_optional() -> None:
    assert await analyze_with_model(Settings(), [{"code": "x"}], []) is None
    assert await analyze_with_model(Settings(openrouter_api_key="key"), [], []) is None


async def test_model_analysis_returns_only_structured_advice(monkeypatch) -> None:
    output = VisibilityAnalysis(
        summary="Repair public metadata first.",
        priorities=[Priority(title="Metadata", rationale="Live drift", finding_codes=["x402.drift"])],
    )

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, prompt):
            assert "x402.drift" in prompt
            return SimpleNamespace(output=output)

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(analysis, "_model", lambda model_id, key: object())
    result = await analyze_with_model(
        Settings(openrouter_api_key="key", model_policy_path="/missing"),
        [{"code": "x402.drift", "severity": "error", "title": "Drift"}],
        [{"channelKey": "x402_list", "present": False}],
    )
    assert result is not None
    assert result["basis"] == "model_assisted_live_evidence"
    assert result["priorities"][0]["finding_codes"] == ["x402.drift"]


async def test_model_failure_falls_back_without_blocking(monkeypatch) -> None:
    class BrokenAgent:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("provider down")

    monkeypatch.setattr(pydantic_ai, "Agent", BrokenAgent)
    monkeypatch.setattr(analysis, "_model", lambda model_id, key: object())
    result = await analyze_with_model(
        Settings(openrouter_api_key="key", model_policy_path="/missing"),
        [{"code": "x"}],
        [],
    )
    assert result is None
