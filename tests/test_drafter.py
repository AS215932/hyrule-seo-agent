"""Drafter: model policy, context gathering, prompt fencing, TestModel run."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.actions import drafter
from app.config import Settings
from app.models import DraftProposal, Finding


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=str(tmp_path), model_policy_path=str(tmp_path / "policy.toml"))


def _finding(url: str | None = "https://hyrule.host/services") -> Finding:
    return Finding(check="meta_description", severity="warning", message="too short", url=url)


def test_load_model_id_reads_policy_and_defaults(settings: Settings, tmp_path: Path) -> None:
    assert drafter.load_model_id(settings) == "openrouter:z-ai/glm-5.2"
    (tmp_path / "policy.toml").write_text('[model]\nprimary = "openrouter:custom/model"\n')
    assert drafter.load_model_id(settings) == "openrouter:custom/model"


def test_build_model_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unsupported model id"):
        drafter._build_model("gemini:pro", "key")


def test_gather_context_files_respects_allowlist_and_cap(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "hyrule_web" / "templates").mkdir(parents=True)
    (ws / "hyrule_web" / "templates" / "base.html").write_text("<title>x</title>")
    (ws / "hyrule_web" / "templates" / "services.html").write_text("svc")
    (ws / "hyrule_web" / "seo.py").write_text("SEO")
    (ws / "hyrule_web" / "app.py").write_text("FORBIDDEN")
    settings = Settings(data_dir=str(tmp_path))

    files = drafter.gather_context_files(ws, settings, [_finding()])
    assert set(files) == {
        "hyrule_web/templates/base.html",
        "hyrule_web/templates/services.html",
        "hyrule_web/seo.py",
    }

    # Findings with no URL or unknown templates degrade gracefully.
    files = drafter.gather_context_files(ws, settings, [_finding(url=None)])
    assert "hyrule_web/templates/base.html" in files

    # Size cap: a giant base.html squeezes later files out.
    (ws / "hyrule_web" / "templates" / "base.html").write_text("x" * 60_000)
    files = drafter.gather_context_files(ws, settings, [_finding()])
    assert "hyrule_web/templates/base.html" not in files
    assert "hyrule_web/templates/services.html" in files


def test_render_user_prompt_fences_untrusted_data() -> None:
    prompt = drafter.render_user_prompt([_finding()], {"hyrule_web/seo.py": "SITE = 1"})
    assert "UNTRUSTED DATA" in prompt
    assert "### Findings (ranked)" in prompt
    assert "### File: hyrule_web/seo.py" in prompt
    assert "meta_description" in prompt


async def test_draft_proposal_disabled_without_key(settings: Settings) -> None:
    proposal, cost = await drafter.draft_proposal(settings, [_finding()], {"f": "x"})
    assert proposal is None
    assert cost == {}


async def test_draft_proposal_empty_inputs(settings: Settings) -> None:
    settings2 = Settings(data_dir=settings.data_dir, openrouter_api_key="k")
    assert (await drafter.draft_proposal(settings2, [], {"f": "x"}))[0] is None
    assert (await drafter.draft_proposal(settings2, [_finding()], {}))[0] is None


async def test_draft_proposal_runs_with_test_model(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic_ai.models.test import TestModel

    monkeypatch.setattr(drafter, "_build_model", lambda model_id, api_key: TestModel())
    live = Settings(data_dir=settings.data_dir, openrouter_api_key="k")
    files = {"hyrule_web/templates/base.html": "<title>t</title>"}
    proposal, cost = await drafter.draft_proposal(live, [_finding()], files)
    assert isinstance(proposal, DraftProposal)
    # Edits naming files outside the provided set are dropped pre-validation.
    assert all(e.path in files for e in proposal.edits)
    assert cost["provider"] == "openrouter"
    assert "model" in cost
