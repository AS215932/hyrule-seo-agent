"""GitHub App action: token mint, PR state, draft opening, body rendering."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from app.actions import github
from app.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    key = tmp_path / "app.pem"
    key.write_text("dummy")
    return Settings(
        data_dir=str(tmp_path),
        github_app_id="42",
        github_installation_id="1337",
        github_private_key_path=str(key),
    )


def test_app_configured(settings: Settings, tmp_path: Path) -> None:
    assert github.app_configured(settings)
    assert not github.app_configured(Settings(data_dir=str(tmp_path)))
    missing = Settings(
        data_dir=str(tmp_path),
        github_app_id="42",
        github_installation_id="1337",
        github_private_key_path=str(tmp_path / "nope.pem"),
    )
    assert not github.app_configured(missing)


async def test_installation_token(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "_app_jwt", lambda s: "jwt-token")
    with respx.mock(assert_all_called=True) as router:
        route = router.post(
            "https://api.github.com/app/installations/1337/access_tokens"
        ).respond(201, json={"token": "ghs_abc"})
        async with httpx.AsyncClient() as client:
            token = await github.installation_token(client, settings)
    assert token == "ghs_abc"
    assert route.calls[0].request.headers["Authorization"] == "Bearer jwt-token"


async def test_open_agent_pr_state_filters_our_branches(settings: Settings) -> None:
    prs = [
        {"head": {"ref": "seo-agent/20260701-aaa"}, "created_at": "2026-07-01T10:00:00Z"},
        {"head": {"ref": "seo-agent/20260710-bbb"}, "created_at": "2026-07-10T10:00:00Z"},
        {"head": {"ref": "feat/unrelated"}, "created_at": "2026-07-12T10:00:00Z"},
    ]
    with respx.mock() as router:
        router.get("https://api.github.com/repos/AS215932/hyrule-web/pulls").respond(200, json=prs)
        async with httpx.AsyncClient() as client:
            count, latest = await github.open_agent_pr_state(client, settings, "tok")
    assert count == 2
    assert latest is not None and latest.day == 10


async def test_open_draft_pr_labels_and_url(settings: Settings) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.post("https://api.github.com/repos/AS215932/hyrule-web/pulls").respond(
            201, json={"number": 7, "html_url": "https://github.com/AS215932/hyrule-web/pull/7"}
        )
        label_route = router.post(
            "https://api.github.com/repos/AS215932/hyrule-web/issues/7/labels"
        ).respond(200, json=[])
        async with httpx.AsyncClient() as client:
            url = await github.open_draft_pr(
                client, settings, "tok", branch="seo-agent/x", title="t", body="b"
            )
    assert url.endswith("/pull/7")
    import json as _json

    sent = _json.loads(label_route.calls[0].request.content)
    assert sent == {"labels": ["seo-agent", "ai-generated", "draft"]}


async def test_open_draft_pr_survives_label_failure(settings: Settings) -> None:
    with respx.mock() as router:
        router.post("https://api.github.com/repos/AS215932/hyrule-web/pulls").respond(
            201, json={"number": 8, "html_url": "https://x/pull/8"}
        )
        router.post("https://api.github.com/repos/AS215932/hyrule-web/issues/8/labels").respond(422)
        async with httpx.AsyncClient() as client:
            url = await github.open_draft_pr(
                client, settings, "tok", branch="seo-agent/y", title="t", body="b"
            )
    assert url == "https://x/pull/8"


def test_render_pr_body_carries_policy_and_evidence() -> None:
    body = github.render_pr_body(
        intent="Fix metas",
        changed_files=["hyrule_web/templates/base.html"],
        evidence=["[warning] meta_description: too short"],
        model_transparency="Drafted by seo-agent",
    )
    assert "Do not merge without human review." in body
    assert "Block G" in body
    assert "`hyrule_web/templates/base.html`" in body
    assert "[warning] meta_description" in body
    empty = github.render_pr_body(intent="i", changed_files=[], evidence=[], model_transparency="m")
    assert "_No files listed yet_" in empty
