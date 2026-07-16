"""Pipeline phases: run rows, traces, and the draft guardrail branches."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

import app.pipeline as pipeline
from app.actions import github, indexnow, workspace
from app.config import Settings
from app.models import CrawlResult, DraftEdit, DraftProposal, Finding, MetricSample
from app.store import Store


@pytest.fixture
async def deps(tmp_path: Path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    store = Store(tmp_path / "data" / "seo.db")
    await store.connect()
    async with httpx.AsyncClient() as client:
        yield pipeline.Deps(settings=settings, store=store, client=client)
    await store.close()


def _finding(check: str = "meta_description", severity: str = "warning") -> Finding:
    return Finding(check=check, severity=severity, message="m", url="https://hyrule.host/", source="audit")  # type: ignore[arg-type]


async def test_run_audit_persists_findings_and_run_row(deps, monkeypatch) -> None:
    crawl = CrawlResult(base_url="https://hyrule.host", robots_txt_ok=True, sitemap_ok=True)

    async def fake_crawl(client, base_url, **kwargs):
        return crawl

    monkeypatch.setattr(pipeline, "crawl_site", fake_crawl)
    monkeypatch.setattr(pipeline, "audit_crawl", lambda c, site_base_url: [_finding()])
    outcome = await pipeline.run_audit(deps)
    assert outcome.ok
    assert outcome.stats["findings"] == 1
    assert outcome.stats["new"] == 1
    assert len(await deps.store.active_findings()) == 1
    assert (await deps.store.last_runs(1))[0]["kind"] == "audit"

    # Second identical run: seen, none new; a vanished finding resolves.
    outcome = await pipeline.run_audit(deps)
    assert outcome.stats["seen"] == 1
    monkeypatch.setattr(pipeline, "audit_crawl", lambda c, site_base_url: [])
    outcome = await pipeline.run_audit(deps)
    assert outcome.stats["resolved"] == 1


async def test_run_audit_failure_is_recorded(deps, monkeypatch) -> None:
    async def boom(client, base_url, **kwargs):
        raise RuntimeError("crawler exploded")

    monkeypatch.setattr(pipeline, "crawl_site", boom)
    outcome = await pipeline.run_audit(deps)
    assert not outcome.ok
    assert "crawler exploded" in outcome.summary


async def test_run_metrics_with_everything_disabled(deps) -> None:
    outcome = await pipeline.run_metrics(deps)
    assert outcome.ok
    assert outcome.stats["samples"] == 0
    assert outcome.stats["sources"] == []


async def test_run_metrics_collects_and_derives_findings(deps, monkeypatch, tmp_path) -> None:
    creds = tmp_path / "wif.json"
    creds.write_text("{}")
    deps.settings = Settings(
        data_dir=deps.settings.data_dir,
        google_credentials_path=str(creds),
        psi_api_key="psi-key",
        umami_base_url="https://analytics.hyrule.host",
        umami_api_token="tok",
        umami_website_id="site-1",
    )

    class FakeGSC:
        def __init__(self, **kwargs):
            pass

        enabled = True

        async def collect(self, client):
            return [
                MetricSample(source="gsc", metric="clicks", key="28d", value=10),
                MetricSample(source="gsc", metric="clicks", key="28d_prev", value=100),
                MetricSample(source="gsc", metric="impressions", key="28d", value=1000),
                MetricSample(source="gsc", metric="impressions", key="28d_prev", value=1100),
            ]

    async def fake_psi(client, *, base_url, api_key, paths):
        return (
            [MetricSample(source="psi", metric="seo_score", key="/|mobile", value=0.85)],
            [Finding(check="psi_seo_score", severity="warning", message="0.85", source="psi")],
        )

    class FakeUmami:
        def __init__(self, **kwargs):
            pass

        enabled = True

        async def collect(self, client):
            return [MetricSample(source="umami", metric="pageviews", key="7d", value=42)]

    monkeypatch.setattr(pipeline, "GSCCollector", FakeGSC)
    monkeypatch.setattr(pipeline, "collect_psi", fake_psi)
    monkeypatch.setattr(pipeline, "UmamiCollector", FakeUmami)

    outcome = await pipeline.run_metrics(deps)
    assert outcome.ok
    assert outcome.stats["sources"] == ["gsc", "psi", "umami"]
    assert outcome.stats["samples"] == 6
    checks = {f.check for f in await deps.store.active_findings()}
    assert "gsc_clicks_drop" in checks  # 10 vs 100 → drop fires
    assert "psi_seo_score" in checks
    assert await deps.store.latest_metric("umami", "pageviews", "7d") == 42


async def test_run_indexnow_delegates(deps, monkeypatch) -> None:
    async def fake_ping(client, store, settings):
        return True

    monkeypatch.setattr(indexnow, "ping_if_changed", fake_ping)
    outcome = await pipeline.run_indexnow(deps)
    assert outcome.ok
    assert outcome.stats["pinged"] is True


async def test_run_draft_stays_silent_without_findings(deps) -> None:
    outcome = await pipeline.run_draft(deps)
    assert outcome.ok
    assert outcome.stats["decision"] == "stay_silent"


@pytest.fixture
def fake_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "hyrule_web" / "templates").mkdir(parents=True)
    (ws / "hyrule_web" / "templates" / "base.html").write_text("<title>old</title>")
    return ws


async def test_run_draft_dry_run_happy_path(deps, monkeypatch, fake_ws) -> None:
    await deps.store.upsert_findings([_finding()])
    deps.settings = Settings(
        data_dir=deps.settings.data_dir, workspace_validate_cmd="echo ok", dry_run=True
    )

    async def fake_ensure(settings):
        return fake_ws

    async def fake_draft(settings, findings, files):
        return (
            DraftProposal(
                title="tighten title",
                rationale="finding says so",
                body="Fixes the title length.",
                edits=[DraftEdit(path="hyrule_web/templates/base.html", find="old", replace="new")],
            ),
            {"model": "openrouter:test"},
        )

    monkeypatch.setattr(workspace, "ensure_workspace", fake_ensure)
    monkeypatch.setattr(pipeline, "draft_proposal", fake_draft)
    outcome = await pipeline.run_draft(deps)
    assert outcome.ok
    assert outcome.stats["decision"] == "draft"
    assert outcome.stats["dry_run"] is True
    assert outcome.stats["files"] == ["hyrule_web/templates/base.html"]
    # Dry run never records a PR.
    assert await deps.store.last_pr_opened_at() is None


async def test_run_draft_stays_silent_on_empty_proposal(deps, monkeypatch, fake_ws) -> None:
    await deps.store.upsert_findings([_finding()])

    async def fake_ensure(settings):
        return fake_ws

    async def fake_draft(settings, findings, files):
        return DraftProposal(title="t", rationale="nothing safe to change", body="b", edits=[]), {}

    monkeypatch.setattr(workspace, "ensure_workspace", fake_ensure)
    monkeypatch.setattr(pipeline, "draft_proposal", fake_draft)
    outcome = await pipeline.run_draft(deps)
    assert outcome.ok
    assert outcome.stats["decision"] == "stay_silent"


async def test_run_draft_guardrail_rejects_disallowed_path(deps, monkeypatch, fake_ws) -> None:
    await deps.store.upsert_findings([_finding()])
    (fake_ws / "hyrule_web" / "app.py").write_text("x")

    async def fake_ensure(settings):
        return fake_ws

    async def fake_draft(settings, findings, files):
        return (
            DraftProposal(
                title="t", rationale="r", body="b",
                edits=[DraftEdit(path="hyrule_web/app.py", find="x", replace="y")],
            ),
            {},
        )

    monkeypatch.setattr(workspace, "ensure_workspace", fake_ensure)
    monkeypatch.setattr(pipeline, "draft_proposal", fake_draft)
    outcome = await pipeline.run_draft(deps)
    assert not outcome.ok
    assert "guardrail" in outcome.summary


async def test_run_draft_aborts_when_validation_fails(deps, monkeypatch, fake_ws) -> None:
    await deps.store.upsert_findings([_finding()])
    deps.settings = Settings(data_dir=deps.settings.data_dir, workspace_validate_cmd="exit 5")

    async def fake_ensure(settings):
        return fake_ws

    async def fake_draft(settings, findings, files):
        return (
            DraftProposal(
                title="t", rationale="r", body="b",
                edits=[DraftEdit(path="hyrule_web/templates/base.html", find="old", replace="new")],
            ),
            {},
        )

    monkeypatch.setattr(workspace, "ensure_workspace", fake_ensure)
    monkeypatch.setattr(pipeline, "draft_proposal", fake_draft)
    outcome = await pipeline.run_draft(deps)
    assert not outcome.ok
    assert outcome.stats["decision"] == "aborted"


async def test_run_draft_honours_ledger_interval(deps) -> None:
    await deps.store.upsert_findings([_finding()])
    await deps.store.record_pr("seo-agent/prev", "https://x/pull/1")
    outcome = await pipeline.run_draft(deps)
    assert outcome.ok
    assert "ledger" in outcome.summary


async def test_run_draft_honours_live_pr_cap(deps, monkeypatch, tmp_path) -> None:
    await deps.store.upsert_findings([_finding()])
    key = tmp_path / "app.pem"
    key.write_text("k")
    deps.settings = Settings(
        data_dir=deps.settings.data_dir,
        github_app_id="1",
        github_installation_id="2",
        github_private_key_path=str(key),
        max_open_prs=2,
    )

    async def fake_token(client, settings):
        return "tok"

    async def fake_state(client, settings, token):
        return 2, None

    monkeypatch.setattr(github, "installation_token", fake_token)
    monkeypatch.setattr(github, "open_agent_pr_state", fake_state)
    outcome = await pipeline.run_draft(deps)
    assert outcome.ok
    assert "already open" in outcome.summary


async def test_run_draft_opens_real_pr_when_not_dry_run(deps, monkeypatch, fake_ws, tmp_path) -> None:
    await deps.store.upsert_findings([_finding()])
    key = tmp_path / "app.pem"
    key.write_text("k")
    deps.settings = Settings(
        data_dir=deps.settings.data_dir,
        dry_run=False,
        workspace_validate_cmd="echo ok",
        github_app_id="1",
        github_installation_id="2",
        github_private_key_path=str(key),
    )

    async def fake_token(client, settings):
        return "tok"

    async def fake_state(client, settings, token):
        return 0, None

    async def fake_ensure(settings):
        return fake_ws

    async def fake_draft(settings, findings, files):
        return (
            DraftProposal(
                title="t", rationale="r", body="b",
                edits=[DraftEdit(path="hyrule_web/templates/base.html", find="old", replace="new")],
            ),
            {},
        )

    pushed: dict[str, str] = {}

    async def fake_push(ws, settings, token, *, branch, title):
        pushed["branch"] = branch

    async def fake_open(client, settings, token, *, branch, title, body):
        return "https://github.com/AS215932/hyrule-web/pull/9"

    monkeypatch.setattr(github, "installation_token", fake_token)
    monkeypatch.setattr(github, "open_agent_pr_state", fake_state)
    monkeypatch.setattr(workspace, "ensure_workspace", fake_ensure)
    monkeypatch.setattr(workspace, "commit_and_push", fake_push)
    monkeypatch.setattr(github, "open_draft_pr", fake_open)
    monkeypatch.setattr(pipeline, "draft_proposal", fake_draft)

    outcome = await pipeline.run_draft(deps)
    assert outcome.ok
    assert outcome.stats["pr_url"].endswith("/pull/9")
    assert pushed["branch"].startswith("seo-agent/")
    assert await deps.store.last_pr_opened_at() is not None


async def test_run_report_unconfigured(deps) -> None:
    outcome = await pipeline.run_report(deps)
    assert outcome.ok
    assert outcome.stats["sent"] is False


async def test_run_report_sends(deps, monkeypatch) -> None:
    deps.settings = Settings(
        data_dir=deps.settings.data_dir, discord_webhook_url="https://discord.test/hook"
    )
    with respx.mock() as router:
        router.post("https://discord.test/hook").respond(204)
        outcome = await pipeline.run_report(deps)
    assert outcome.stats["sent"] is True
