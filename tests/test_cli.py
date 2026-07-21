"""seoctl: one-shot phases and status output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.cli as cli
from app.config import settings
from app.managed import ManagedRunResult
from app.pipeline import PhaseOutcome


@pytest.fixture(autouse=True)
def _data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))


async def test_status_prints_summary(capsys: pytest.CaptureFixture[str]) -> None:
    assert await cli._with_deps("status") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["findings_total"] == 0


async def test_phase_exit_codes(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    async def ok_phase(deps):
        return PhaseOutcome(kind="audit", ok=True, summary="fine", stats={"pages": 1})

    async def bad_phase(deps):
        return PhaseOutcome(kind="audit", ok=False, summary="broke", stats={})

    monkeypatch.setitem(cli._PHASES, "run-audit", ok_phase)
    assert await cli._with_deps("run-audit") == 0
    assert json.loads(capsys.readouterr().out)["summary"] == "fine"

    monkeypatch.setitem(cli._PHASES, "run-audit", bad_phase)
    assert await cli._with_deps("run-audit") == 1


async def test_beacon_once_returns_nonzero_for_a_failed_lease(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    async def failed_lease(settings, store, client):
        return ManagedRunResult(leased=True, ok=False, error="graph failed")

    monkeypatch.setattr(cli, "run_one_managed_lease", failed_lease)

    assert await cli._with_deps("beacon-once") == 1
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "leased": True,
        "ok": False,
        "awaitingApproval": False,
        "error": "graph failed",
    }


def test_main_parses_argv(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr("sys.argv", ["seoctl", "status"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 0

    monkeypatch.setattr("sys.argv", ["seoctl", "not-a-command"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 2
