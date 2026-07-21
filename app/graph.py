"""The resumable LangGraph shared by scheduled and API-triggered Beacon runs."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.analysis import analyze_with_model
from app.actions.beacon import execute_action, plan_actions
from app.beacon.client import BeaconClient
from app.beacon.events import EventEmitter
from app.beacon.models import BeaconLease
from app.config import Settings
from app.discovery import audit_evidence, collect_evidence
from app.store import Store


class BeaconState(TypedDict):
    run_id: str
    project_id: str
    mode: Literal["measure", "optimize"]
    scopes: list[str]
    evidence: dict[str, Any]
    findings: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    analysis: dict[str, Any]
    actions: list[dict[str, Any]]
    decisions: dict[str, Any]
    executions: list[dict[str, Any]]
    summary: dict[str, Any]


@dataclass
class GraphDeps:
    settings: Settings
    store: Store
    http: Any
    emitter: EventEmitter
    beacon: BeaconClient | None
    lease_token: str


@dataclass
class GraphOutcome:
    state: dict[str, Any]
    awaiting_approval: bool


async def delete_graph_checkpoint(settings: Settings, thread_id: str) -> None:
    """Delete a terminal graph only after Beacon acknowledges completion."""

    checkpoint_path = Path(settings.data_dir) / "beacon-checkpoints.sqlite"
    if not checkpoint_path.exists():
        return
    async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
        await checkpointer.setup()
        await checkpointer.adelete_thread(thread_id)


def initial_state(lease: BeaconLease) -> BeaconState:
    run = lease.run
    return {
        "run_id": run.id,
        "project_id": run.project_id,
        "mode": run.mode,
        "scopes": list(run.scopes),
        "evidence": {},
        "findings": [],
        "observations": [],
        "analysis": {},
        "actions": [],
        "decisions": {},
        "executions": [],
        "summary": {},
    }


def _build_graph(checkpointer: AsyncSqliteSaver, deps: GraphDeps):
    async def collect(state: BeaconState) -> dict[str, Any]:
        await deps.emitter.emit("node_started", "Collecting owned surfaces and public channels.", node="collect")
        evidence = await collect_evidence(deps.http, deps.settings, state["scopes"])
        beacon = deps.beacon
        if beacon is not None:
            resources = [
                resource
                for group in ("surfaces", "channels")
                for resource in evidence.get(group, {}).values()
                if isinstance(resource, dict)
            ]

            async def upload(resource: dict[str, Any]) -> tuple[dict[str, Any], str]:
                document = json.dumps(
                    {"evidenceFormat": "hyrule-beacon-resource-v1", **resource},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
                uploaded = await beacon.upload_evidence(
                    state["run_id"],
                    deps.lease_token,
                    document,
                    content_type="application/json",
                )
                return resource, uploaded.key

            for resource, evidence_key in await asyncio.gather(*(upload(resource) for resource in resources)):
                resource["evidence_r2_key"] = evidence_key
        await deps.emitter.emit(
            "node_completed",
            "Public evidence collection completed.",
            node="collect",
            data={
                "surfaceResources": len(evidence.get("surfaces", {})),
                "channelResources": len(evidence.get("channels", {})),
            },
        )
        return {"evidence": evidence}

    async def audit(state: BeaconState) -> dict[str, Any]:
        await deps.emitter.emit("node_started", "Running deterministic visibility audits.", node="audit")
        result = audit_evidence(state["evidence"], state["scopes"])
        observations = result["observations"]
        findings = result["findings"]
        for observation in observations:
            await deps.emitter.emit(
                "observation",
                f"Measured {observation['channelKey']} for Hyrule.",
                node="audit",
                data=observation,
            )
        for finding in findings:
            await deps.emitter.emit("finding", finding["title"], node="audit", data=finding)
        await deps.emitter.emit(
            "node_completed",
            f"Audit produced {len(findings)} findings and {len(observations)} observations.",
            node="audit",
        )
        return {"findings": findings, "observations": observations}

    async def analyze(state: BeaconState) -> dict[str, Any]:
        await deps.emitter.emit("node_started", "Prioritizing evidence-backed improvements.", node="analyze")
        severity_counts = {
            severity: sum(1 for finding in state["findings"] if finding.get("severity") == severity)
            for severity in ("error", "warning", "info")
        }
        missing_channels = sorted(
            str(observation["channelKey"])
            for observation in state["observations"]
            if observation.get("present") is False
        )
        analysis = {
            "severityCounts": severity_counts,
            "missingChannels": missing_channels,
            "basis": "deterministic_live_evidence",
        }
        model_analysis = await analyze_with_model(deps.settings, state["findings"], state["observations"])
        if model_analysis is not None:
            analysis["modelAnalysis"] = model_analysis
        await deps.emitter.emit(
            "node_completed",
            "Evidence priorities are ready.",
            node="analyze",
            data=analysis,
        )
        return {"analysis": analysis}

    async def plan(state: BeaconState) -> dict[str, Any]:
        await deps.emitter.emit("node_started", "Building policy-scoped action proposals.", node="plan")
        actions = plan_actions(
            run_id=state["run_id"],
            mode=state["mode"],
            scopes=state["scopes"],
            findings=state["findings"],
            observations=state["observations"],
            evidence=state["evidence"],
        )
        for action in actions:
            await deps.emitter.emit(
                "action_proposed",
                f"Proposed {action['actionType']} ({action['risk']}).",
                node="plan",
                data=action,
            )
        await deps.emitter.emit(
            "node_completed",
            f"Prepared {len(actions)} actions; policy is enforced by Beacon again.",
            node="plan",
        )
        return {"actions": actions}

    def route_after_automatic(state: BeaconState) -> str:
        if any(action["risk"] == "approval_required" for action in state["actions"]):
            return "approval"
        return "execute"

    def approval(state: BeaconState) -> dict[str, Any]:
        keys = [action["idempotencyKey"] for action in state["actions"] if action["risk"] == "approval_required"]
        decisions = interrupt(
            {
                "runId": state["run_id"],
                "actionIdempotencyKeys": keys,
                "message": "Review the exact canonical payloads in Hyrule Beacon.",
            }
        )
        return {"decisions": cast(dict[str, Any], decisions)}

    async def execute_phase(
        state: BeaconState,
        *,
        automatic: bool,
    ) -> dict[str, Any]:
        node = "execute_automatic" if automatic else "execute"
        message = (
            "Executing eligible automatic actions before approval waits."
            if automatic
            else "Applying the capability and approval boundary."
        )
        await deps.emitter.emit("node_started", message, node=node)
        decision_rows = state.get("decisions", {}).get("actions", [])
        statuses = {row.get("idempotencyKey"): row.get("status") for row in decision_rows if isinstance(row, dict)}
        executions = list(state.get("executions", []))
        processed = 0
        for action in state["actions"]:
            is_automatic = action["risk"] == "automatic"
            if is_automatic != automatic:
                continue
            processed += 1
            if action["risk"] == "manual":
                execution = {
                    "idempotencyKey": action["idempotencyKey"],
                    "status": "manual_required",
                    "reason": "Policy requires an operator.",
                }
                executions.append(execution)
                await deps.emitter.emit(
                    "action_result",
                    f"{action['actionType']}: manual_required",
                    node=node,
                    data={
                        "idempotencyKey": action["idempotencyKey"],
                        "status": "manual_required",
                        "result": {},
                        "errorMessage": execution["reason"],
                    },
                )
                continue
            status = statuses.get(action["idempotencyKey"])
            if action["risk"] == "approval_required" and status == "rejected":
                executions.append(
                    {
                        "idempotencyKey": action["idempotencyKey"],
                        "status": "rejected",
                    }
                )
                continue
            result = await execute_action(
                action,
                settings=deps.settings,
                client=deps.http,
                store=deps.store,
                approved=status == "approved",
            )
            execution = {"idempotencyKey": action["idempotencyKey"], **result}
            executions.append(execution)
            await deps.emitter.emit(
                "action_result",
                f"{action['actionType']}: {result['status']}",
                node=node,
                data={
                    "idempotencyKey": action["idempotencyKey"],
                    "status": result["status"],
                    "result": {key: value for key, value in result.items() if key not in {"status", "reason"}},
                    "errorMessage": result.get("reason"),
                },
            )
        await deps.emitter.emit(
            "node_completed",
            f"Processed {processed} policy-scoped actions.",
            node=node,
        )
        return {"executions": executions}

    async def execute_automatic(state: BeaconState) -> dict[str, Any]:
        return await execute_phase(state, automatic=True)

    async def execute(state: BeaconState) -> dict[str, Any]:
        return await execute_phase(state, automatic=False)

    async def report(state: BeaconState) -> dict[str, Any]:
        summary = {
            "findings": len(state["findings"]),
            "observations": len(state["observations"]),
            "actions": len(state["actions"]),
            "executions": len(state["executions"]),
        }
        await deps.emitter.emit("node_started", "Finalizing the Beacon run.", node="report")
        await deps.emitter.emit("node_completed", "Beacon run is complete.", node="report", data=summary)
        return {"summary": summary}

    builder = StateGraph(BeaconState)
    builder.add_node("collect", collect)
    builder.add_node("audit", audit)
    builder.add_node("analyze", analyze)
    builder.add_node("plan", plan)
    builder.add_node("execute_automatic", execute_automatic)
    builder.add_node("approval", approval)
    builder.add_node("execute", execute)
    builder.add_node("report", report)
    builder.add_edge(START, "collect")
    builder.add_edge("collect", "audit")
    builder.add_edge("audit", "analyze")
    builder.add_edge("analyze", "plan")
    builder.add_edge("plan", "execute_automatic")
    builder.add_conditional_edges(
        "execute_automatic",
        route_after_automatic,
        {"approval": "approval", "execute": "execute"},
    )
    builder.add_edge("approval", "execute")
    builder.add_edge("execute", "report")
    builder.add_edge("report", END)
    return builder.compile(checkpointer=checkpointer)


async def run_graph(
    *,
    lease: BeaconLease,
    beacon: BeaconClient | None,
    settings: Settings,
    store: Store,
    http: Any,
) -> GraphOutcome:
    """Start, recover, or approval-resume exactly one checkpointed run."""

    checkpoint_path = Path(settings.data_dir) / "beacon-checkpoints.sqlite"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    emitter = EventEmitter(beacon, lease)
    deps = GraphDeps(
        settings=settings,
        store=store,
        http=http,
        emitter=emitter,
        beacon=beacon,
        lease_token=lease.lease_token,
    )
    config = {"configurable": {"thread_id": lease.run.thread_id}}
    async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
        await checkpointer.setup()
        graph = _build_graph(checkpointer, deps)
        snapshot = await graph.aget_state(config)
        if snapshot.next:
            interrupted = any(task.interrupts for task in snapshot.tasks)
            if interrupted:
                proposed = [
                    action
                    for action in lease.run.actions
                    if action.risk == "approval_required" and action.status == "proposed"
                ]
                if proposed:
                    await emitter.emit(
                        "awaiting_approval",
                        f"Waiting for {len(proposed)} remaining operator decisions.",
                        node="approval",
                    )
                    return GraphOutcome(state=dict(snapshot.values), awaiting_approval=True)
                decisions = {"actions": [action.model_dump(by_alias=True, mode="json") for action in lease.run.actions]}
                result = await graph.ainvoke(Command(resume=decisions), config)
            else:
                # A durable checkpoint can also be pending because the worker
                # stopped between nodes. Continue it without fabricating an
                # approval-resume payload.
                result = await graph.ainvoke(None, config)
        elif snapshot.values:
            state = dict(snapshot.values)
            return GraphOutcome(state=state, awaiting_approval=False)
        else:
            result = await graph.ainvoke(initial_state(lease), config)
        state = dict(result)
        awaiting_approval = bool(state.get("__interrupt__"))
    if awaiting_approval:
        await emitter.emit(
            "awaiting_approval",
            "Action payloads are waiting for operator decisions in Hyrule Beacon.",
            node="approval",
        )
    return GraphOutcome(state=state, awaiting_approval=awaiting_approval)
