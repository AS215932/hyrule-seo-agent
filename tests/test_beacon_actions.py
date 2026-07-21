from __future__ import annotations

import httpx

from app.actions import indexnow
from app.actions.beacon import execute_action, plan_actions
from app.config import Settings
from app.store import Store


VALID_SITEMAP = "<urlset><url><loc>https://hyrule.host/</loc></url></urlset>"


def test_measure_mode_never_proposes_mutations() -> None:
    actions = plan_actions(
        run_id="run-1",
        mode="measure",
        scopes=["http", "x402", "distribution"],
        findings=[{"code": "http.tools_index.missing"}],
        observations=[{"channelKey": "x402_list", "present": False}],
        evidence={},
    )
    assert actions == []


def test_optimize_policy_separates_automatic_approval_and_manual_work() -> None:
    actions = plan_actions(
        run_id="run-1",
        mode="optimize",
        scopes=["http", "distribution"],
        findings=[{"code": "http.tools_index.missing"}],
        observations=[
            {"channelKey": "x402_list", "present": False},
            {"channelKey": "a2alist", "present": False},
        ],
        evidence={"surfaces": {"http:sitemap": {"status": 200, "sha256": "abc", "text": VALID_SITEMAP}}},
    )
    risks = {(action["actionType"], action["risk"]) for action in actions}
    assert ("indexnow.submit", "automatic") in risks
    assert ("repository.change", "approval_required") in risks
    assert ("registry.create_listing", "approval_required") in risks
    assert ("registry.paid_form", "manual") in risks
    assert all(action["idempotencyKey"].startswith("run-1:") for action in actions)


def test_unavailable_partial_measurements_do_not_propose_repairs() -> None:
    actions = plan_actions(
        run_id="run-unknown",
        mode="optimize",
        scopes=["http"],
        findings=[
            {"code": "http.structured_data.unavailable"},
            {"code": "http.sitemap.validation_unavailable"},
        ],
        observations=[],
        evidence={"surfaces": {}},
    )

    assert actions == []


def test_recurring_manual_handoffs_require_new_evidence() -> None:
    without_evidence = plan_actions(
        run_id="run-1",
        mode="optimize",
        scopes=["distribution"],
        findings=[],
        observations=[],
        evidence={},
    )
    with_evidence = plan_actions(
        run_id="run-2",
        mode="optimize",
        scopes=["distribution"],
        findings=[
            {"code": "distribution.community.outreach_needed"},
            {"code": "distribution.import_verification_needed"},
        ],
        observations=[],
        evidence={},
    )

    assert without_evidence == []
    assert {action["actionType"] for action in with_evidence} == {
        "community.outreach",
        "account.verify_import",
    }


async def test_executor_never_treats_approval_as_new_capability(tmp_path, monkeypatch) -> None:
    store = Store(tmp_path / "seo.db")
    await store.connect()
    async with httpx.AsyncClient() as client:
        base = {"actionType": "repository.change", "risk": "approval_required"}
        assert (await execute_action(base, settings=Settings(), client=client, store=store, approved=False))[
            "status"
        ] == "manual_required"
        assert (await execute_action(base, settings=Settings(), client=client, store=store, approved=True))[
            "status"
        ] == "manual_required"
        manual = {"actionType": "community.outreach", "risk": "manual"}
        assert (await execute_action(manual, settings=Settings(), client=client, store=store, approved=True))[
            "status"
        ] == "manual_required"

        automatic = {"actionType": "indexnow.submit", "risk": "automatic"}
        disabled = await execute_action(
            automatic,
            settings=Settings(beacon_execute_automatic_actions=False),
            client=client,
            store=store,
            approved=True,
        )
        assert disabled["status"] == "manual_required"

        async def fake_ping(client, store, settings):
            return indexnow.IndexNowResult("submitted")

        monkeypatch.setattr(indexnow, "ping_if_changed", fake_ping)
        enabled = await execute_action(
            automatic,
            settings=Settings(beacon_execute_automatic_actions=True),
            client=client,
            store=store,
            approved=True,
        )
        assert enabled == {
            "status": "succeeded",
            "indexnowStatus": "submitted",
            "pinged": True,
        }

        async def fake_failure(client, store, settings):
            return indexnow.IndexNowResult("failed", "IndexNow rejected the request.")

        monkeypatch.setattr(indexnow, "ping_if_changed", fake_failure)
        failed = await execute_action(
            automatic,
            settings=Settings(beacon_execute_automatic_actions=True),
            client=client,
            store=store,
            approved=True,
        )
        assert failed == {
            "status": "failed",
            "reason": "IndexNow rejected the request.",
        }
    await store.close()
