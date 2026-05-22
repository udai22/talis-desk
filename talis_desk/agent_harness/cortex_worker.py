"""Bounded worker for cortex/geometry task contracts.

The evolution-control layer posts work orders into ``task_contracts``. This
module is the next hop: claim a posted shape/evolution task, inspect the map
through the read-only harness, and close the task with auditable observations.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

from ..coordination import claim_task, complete_task, fail_task, start_task
from ..store import get_desk_store
from ..tool_atlas import AgentContext
from .tools import HarnessPolicy, dispatch_harness_tool


ALPHA_GEOMETRY_ACTION_TOOL_URI = "tic://tool/talis_native/plan_alpha_geometry_actions@v1"
ALPHA_GEOMETRY_CORTEX_TOOL_URI = "tic://tool/talis_native/review_alpha_geometry_cortex@v1"
DEFAULT_CORTEX_TASK_TOPICS = (
    "alpha_geometry.route",
    "alpha_geometry.verify",
    "alpha_geometry.cortex",
    "market_evolve.frontier",
)


@dataclass
class CortexTaskExecution:
    task_id: str
    topic: str
    status: str
    claimed: bool = False
    started: bool = False
    completed: bool = False
    failed: bool = False
    observations: list[dict[str, Any]] = field(default_factory=list)
    deferred_tool_sequence: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    quality_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "topic": self.topic,
            "status": self.status,
            "claimed": self.claimed,
            "started": self.started,
            "completed": self.completed,
            "failed": self.failed,
            "observations": self.observations,
            "deferred_tool_sequence": self.deferred_tool_sequence,
            "payload": self.payload,
            "error": self.error,
            "quality_flags": self.quality_flags,
        }


def execute_cortex_task_queue(
    *,
    cycle_id: str,
    topics: tuple[str, ...] = DEFAULT_CORTEX_TASK_TOPICS,
    limit: int = 8,
    agent_id: str = "cortex_task_worker",
    specialist_id: str = "alpha_geometry_cortex",
    policy: HarnessPolicy | None = None,
    execute_followup_tools: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    """Claim and execute posted cortex task contracts for one cycle."""
    db = conn or get_desk_store().conn
    rows = _fetch_posted_cortex_tasks(
        cycle_id=cycle_id,
        topics=topics,
        limit=limit,
        conn=db,
    )
    executions = [
        execute_cortex_task(
            str(row["id"]),
            agent_id=agent_id,
            specialist_id=specialist_id,
            policy=policy,
            execute_followup_tools=execute_followup_tools,
            conn=db,
        )
        for row in rows
    ]
    out = [execution.to_dict() for execution in executions]
    return {
        "schema_version": "cortex_task_worker_batch_v1",
        "cycle_id": cycle_id,
        "topics": list(topics),
        "task_count": len(out),
        "claimed_count": sum(1 for item in out if item.get("claimed")),
        "completed_count": sum(1 for item in out if item.get("completed")),
        "failed_count": sum(1 for item in out if item.get("failed")),
        "skipped_count": sum(1 for item in out if item.get("status") == "skipped"),
        "execute_followup_tools": execute_followup_tools,
        "executions": out,
    }


def execute_cortex_task(
    task_id: str,
    *,
    agent_id: str = "cortex_task_worker",
    specialist_id: str = "alpha_geometry_cortex",
    policy: HarnessPolicy | None = None,
    execute_followup_tools: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> CortexTaskExecution:
    """Execute one posted cortex task through the read-only harness."""
    db = conn or get_desk_store().conn
    row = _load_task(task_id, conn=db)
    if row is None:
        return CortexTaskExecution(
            task_id=task_id,
            topic="",
            status="missing",
            error="task_not_found",
            quality_flags=["task_not_found"],
        )
    topic = str(row.get("topic") or "")
    if str(row.get("status") or "") != "posted":
        return CortexTaskExecution(
            task_id=task_id,
            topic=topic,
            status="skipped",
            quality_flags=[f"task_status:{row.get('status')}"],
        )

    policy = policy or HarnessPolicy(evidence_hard_cap=4, max_retries=0)
    execution = CortexTaskExecution(task_id=task_id, topic=topic, status="running")
    if not claim_task(task_id, agent_id=agent_id, specialist_id=specialist_id, conn=db):
        execution.status = "skipped"
        execution.quality_flags.append("claim_lost")
        return execution
    execution.claimed = True
    execution.started = start_task(
        task_id,
        agent_id=agent_id,
        specialist_id=specialist_id,
        conn=db,
    )

    payload = _json_value(row.get("payload"), {})
    input_schema = _json_value(row.get("input_schema_json"), {})
    allowed_tools = _json_value(row.get("allowed_tools_json"), [])
    promotion_criteria = _json_value(row.get("promotion_criteria_json"), {})
    kill_criteria = _json_value(row.get("kill_criteria_json"), {})
    policy, execute_followup_tools, policy_proof = _effective_task_policy(
        base_policy=policy,
        base_execute_followups=execute_followup_tools,
        payload=payload,
    )
    tool_plan = _execution_tool_plan(
        allowed_tools=allowed_tools,
        input_schema=input_schema,
        execute_followup_tools=execute_followup_tools,
        policy=policy,
    )
    execution.deferred_tool_sequence = list(tool_plan["deferred"])
    shape_tools = list(tool_plan["shape"])
    followup_tools = list(tool_plan["followup"])
    if not shape_tools:
        execution.status = "failed"
        execution.failed = fail_task(
            task_id,
            agent_id=agent_id,
            specialist_id=specialist_id,
            reason="no_executable_shape_tools",
            payload=_completion_payload(
                row=row,
                payload=payload,
                observations=[],
                deferred_tool_sequence=execution.deferred_tool_sequence,
                promotion_criteria=promotion_criteria,
                kill_criteria=kill_criteria,
                quality_flags=["no_executable_shape_tools"],
                policy_proof=policy_proof,
            ),
            conn=db,
        )
        execution.error = "no_executable_shape_tools"
        execution.quality_flags.append("no_executable_shape_tools")
        return execution

    source_cycle_id = str(payload.get("source_cycle_id") or row.get("cycle_id") or "")
    context = AgentContext(
        cycle_id=source_cycle_id,
        specialist_id=specialist_id,
        investigation_id=task_id,
    )
    for uri in shape_tools:
        args = _tool_args(
            uri,
            cycle_id=source_cycle_id,
            payload=payload,
            input_schema=input_schema,
        )
        observation = dispatch_harness_tool(
            uri,
            args,
            context,
            policy=policy,
            phase="cortex_task",
            requested_by_model=False,
            request_why=_tool_request_why(uri, payload=payload, topic=topic),
            expected_edge=str(payload.get("expected_edge") or payload.get("route_task_id") or ""),
            expected_info_value=_float_or_none(payload.get("expected_info_value")),
            would_change_decision=True,
            fallback_if_denied="Preserve the work order as unresolved and route to source/tool repair.",
        )
        execution.observations.append(observation)
    shape_observed = _has_successful_shape_observation(execution.observations)
    if followup_tools and shape_observed:
        for uri in followup_tools:
            args = _tool_args(
                uri,
                cycle_id=source_cycle_id,
                payload=payload,
                input_schema=input_schema,
            )
            observation = dispatch_harness_tool(
                uri,
                args,
                context,
                policy=policy,
                phase="cortex_followup",
                requested_by_model=False,
                request_why=_tool_request_why(uri, payload=payload, topic=topic),
                expected_edge=str(payload.get("expected_edge") or payload.get("route_task_id") or ""),
                expected_info_value=_float_or_none(payload.get("expected_info_value")),
                would_change_decision=True,
                fallback_if_denied="Preserve the work order as unresolved and route to source/tool repair.",
            )
            execution.observations.append(observation)
    elif followup_tools:
        execution.deferred_tool_sequence = _dedupe_tool_sequence(
            [*execution.deferred_tool_sequence, *followup_tools]
        )

    quality_flags = _execution_quality_flags(
        observations=execution.observations,
        required_first_tool=str(input_schema.get("required_first_tool") or ""),
        deferred_tool_sequence=execution.deferred_tool_sequence,
        followup_tools=followup_tools,
        shape_observed=shape_observed,
    )
    completion = _completion_payload(
        row=row,
        payload=payload,
        observations=execution.observations,
        deferred_tool_sequence=execution.deferred_tool_sequence,
        promotion_criteria=promotion_criteria,
        kill_criteria=kill_criteria,
        quality_flags=quality_flags,
        policy_proof=policy_proof,
    )
    if shape_observed:
        execution.status = "completed"
        execution.completed = complete_task(
            task_id,
            agent_id=agent_id,
            specialist_id=specialist_id,
            payload=completion,
            conn=db,
        )
    else:
        execution.status = "failed"
        execution.failed = fail_task(
            task_id,
            agent_id=agent_id,
            specialist_id=specialist_id,
            reason="no_successful_shape_observation",
            payload=completion,
            conn=db,
        )
        execution.error = "no_successful_shape_observation"
    execution.payload = completion
    execution.quality_flags = quality_flags
    return execution


def _fetch_posted_cortex_tasks(
    *,
    cycle_id: str,
    topics: tuple[str, ...],
    limit: int,
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:
    topics = tuple(str(topic) for topic in topics if str(topic or "").strip())
    if not topics:
        return []
    placeholders = ",".join("?" for _ in topics)
    return conn.execute(
        f"""
        SELECT *
        FROM task_contracts
        WHERE cycle_id = ?
          AND status = 'posted'
          AND topic IN ({placeholders})
        ORDER BY priority DESC, posted_at ASC
        LIMIT ?
        """,
        (cycle_id, *topics, max(1, int(limit or 1))),
    ).fetchall()


def _load_task(task_id: str, *, conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM task_contracts WHERE id = ?",
        (task_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _execution_tool_plan(
    *,
    allowed_tools: Any,
    input_schema: dict[str, Any],
    execute_followup_tools: bool,
    policy: HarnessPolicy,
) -> dict[str, list[str]]:
    allowed = [str(uri) for uri in allowed_tools if str(uri or "").strip()] if isinstance(allowed_tools, list) else []
    sequence: list[str] = []
    required = str(input_schema.get("required_first_tool") or "")
    if required:
        sequence.append(required)
    if ALPHA_GEOMETRY_ACTION_TOOL_URI in allowed:
        sequence.append(ALPHA_GEOMETRY_ACTION_TOOL_URI)
    if ALPHA_GEOMETRY_CORTEX_TOOL_URI in allowed:
        sequence.append(ALPHA_GEOMETRY_CORTEX_TOOL_URI)
    for uri in input_schema.get("tool_sequence") or []:
        sequence.append(str(uri))
    if execute_followup_tools:
        sequence.extend(allowed)

    deduped: list[str] = []
    seen: set[str] = set()
    for uri in sequence:
        if uri in seen or uri not in allowed:
            continue
        seen.add(uri)
        deduped.append(uri)

    cap = max(1, int(policy.evidence_hard_cap or 1))
    shape_tools = [
        uri for uri in deduped
        if uri in {ALPHA_GEOMETRY_ACTION_TOOL_URI, ALPHA_GEOMETRY_CORTEX_TOOL_URI}
    ][:cap]
    remaining_cap = max(0, cap - len(shape_tools))
    candidate_followups = [
        uri for uri in deduped
        if uri not in set(shape_tools)
    ]
    if execute_followup_tools and remaining_cap > 0:
        followup_tools = candidate_followups[:remaining_cap]
        deferred = candidate_followups[remaining_cap:]
    else:
        followup_tools = []
        deferred = candidate_followups
    return {
        "execute": [*shape_tools, *followup_tools],
        "shape": shape_tools,
        "followup": followup_tools,
        "deferred": deferred,
    }


def _tool_args(
    uri: str,
    *,
    cycle_id: str,
    payload: dict[str, Any],
    input_schema: dict[str, Any],
) -> dict[str, Any]:
    limit = _bounded_int(
        payload.get("shape_reader_limit", input_schema.get("shape_reader_limit")),
        default=64,
        low=1,
        high=512,
    )
    if uri == ALPHA_GEOMETRY_ACTION_TOOL_URI:
        return {"cycle_id": cycle_id, "limit": limit}
    if uri == ALPHA_GEOMETRY_CORTEX_TOOL_URI:
        return {"cycle_id": cycle_id, "limit": limit, "use_llm": False}
    args: dict[str, Any] = {}
    entity = payload.get("entity")
    if entity:
        args["entity"] = entity
        args["entity_symbol"] = entity
    horizon = payload.get("horizon")
    if horizon:
        args["horizon"] = horizon
    lens = payload.get("lens")
    if lens:
        args["lens"] = lens
    return args


def _completion_payload(
    *,
    row: dict[str, Any],
    payload: dict[str, Any],
    observations: list[dict[str, Any]],
    deferred_tool_sequence: list[str],
    promotion_criteria: dict[str, Any],
    kill_criteria: dict[str, Any],
    quality_flags: list[str],
    policy_proof: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "cortex_task_execution_v1",
        "task_id": row.get("id"),
        "topic": row.get("topic"),
        "cycle_id": row.get("cycle_id"),
        "market_evolve_program_id": payload.get("market_evolve_program_id"),
        "market_evolve_program_name": payload.get("market_evolve_program_name"),
        "market_evolve_generation": payload.get("market_evolve_generation"),
        "market_evolve_experiment_id": payload.get("market_evolve_experiment_id"),
        "market_evolve_experiment_arm": payload.get("market_evolve_experiment_arm"),
        "route_task_id": payload.get("route_task_id"),
        "base_route_task_id": payload.get("base_route_task_id"),
        "source_cell_key": payload.get("source_cell_key") or payload.get("cell_key"),
        "owner": payload.get("owner"),
        "action": payload.get("action"),
        "success_gate": promotion_criteria.get("success_gate"),
        "stop_condition": kill_criteria.get("stop_condition"),
        "map_update_rule": payload.get("post_action_map_update") or payload.get("map_update_rule"),
        "observations": observations,
        "deferred_tool_sequence": deferred_tool_sequence,
        "proof": {
            "claimed_task_contract": True,
            "shape_tool_observed": _has_successful_shape_observation(observations),
            "observations_logged": len(observations),
            "followup_observations_logged": sum(
                1 for obs in observations
                if str(obs.get("phase") or "") == "cortex_followup"
            ),
            "deferred_tool_count": len(deferred_tool_sequence),
            "shape_first_gate_passed": _has_successful_shape_observation(observations),
            "followup_tools_blocked_by_shape": (
                bool(deferred_tool_sequence)
                and not _has_successful_shape_observation(observations)
            ),
            "task_level_cortex_policy_applied": bool(
                policy_proof
                and policy_proof.get("source") == "task_market_evolve_cortex_policy"
            ),
        },
        "effective_cortex_policy": policy_proof or {},
        "quality_flags": quality_flags,
    }


def _effective_task_policy(
    *,
    base_policy: HarnessPolicy,
    base_execute_followups: bool,
    payload: dict[str, Any],
) -> tuple[HarnessPolicy, bool, dict[str, Any]]:
    raw = payload.get("market_evolve_cortex_policy")
    cortex_policy = raw if isinstance(raw, dict) else {}
    if not cortex_policy:
        return base_policy, base_execute_followups, {
            "source": "worker_default_cortex_policy",
            "evidence_hard_cap": int(base_policy.evidence_hard_cap),
            "execute_bounded_followup_tools": bool(base_execute_followups),
        }
    evidence_hard_cap = _bounded_int(
        cortex_policy.get("max_tools_per_task"),
        default=int(base_policy.evidence_hard_cap or 4),
        low=1,
        high=8,
    )
    execute_followups = bool(
        cortex_policy.get("execute_bounded_followup_tools", base_execute_followups)
    )
    effective = HarnessPolicy(
        evidence_hard_cap=evidence_hard_cap,
        max_tool_iterations=base_policy.max_tool_iterations,
        max_followup_tools_per_iteration=base_policy.max_followup_tools_per_iteration,
        max_retries=base_policy.max_retries,
        retry_backoff_s=base_policy.retry_backoff_s,
        allowed_uri_prefixes=base_policy.allowed_uri_prefixes,
        allow_mutating_tools=base_policy.allow_mutating_tools,
    )
    return effective, execute_followups, {
        "source": "task_market_evolve_cortex_policy",
        "market_evolve_program_id": payload.get("market_evolve_program_id"),
        "market_evolve_experiment_id": payload.get("market_evolve_experiment_id"),
        "market_evolve_experiment_arm": payload.get("market_evolve_experiment_arm"),
        "evidence_hard_cap": evidence_hard_cap,
        "execute_bounded_followup_tools": execute_followups,
        "raw_cortex_policy": cortex_policy,
    }


def _execution_quality_flags(
    *,
    observations: list[dict[str, Any]],
    required_first_tool: str,
    deferred_tool_sequence: list[str],
    followup_tools: list[str],
    shape_observed: bool,
) -> list[str]:
    flags: list[str] = []
    if deferred_tool_sequence:
        flags.append(f"deferred_followup_tools:{len(deferred_tool_sequence)}")
    followup_observations = [
        obs for obs in observations
        if str(obs.get("phase") or "") == "cortex_followup"
    ]
    if followup_observations:
        flags.append(f"executed_followup_tools:{len(followup_observations)}")
    elif followup_tools and not shape_observed:
        flags.append("followup_tools_blocked_by_shape_read")
    if required_first_tool:
        first = next((obs for obs in observations if obs.get("uri") == required_first_tool), None)
        if first and not first.get("ok"):
            flags.append("required_first_tool_failed")
    if not shape_observed:
        flags.append("no_successful_shape_observation")
    return flags


def _dedupe_tool_sequence(uris: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for uri in uris:
        uri_s = str(uri or "").strip()
        if not uri_s or uri_s in seen:
            continue
        seen.add(uri_s)
        out.append(uri_s)
    return out


def _has_successful_shape_observation(observations: list[dict[str, Any]]) -> bool:
    return any(
        bool(obs.get("ok"))
        and str(obs.get("uri") or "") in {ALPHA_GEOMETRY_ACTION_TOOL_URI, ALPHA_GEOMETRY_CORTEX_TOOL_URI}
        for obs in observations
    )


def _tool_request_why(uri: str, *, payload: dict[str, Any], topic: str) -> str:
    if uri == ALPHA_GEOMETRY_ACTION_TOOL_URI:
        return "Read the current alpha-geometry field before spending worker budget."
    if uri == ALPHA_GEOMETRY_CORTEX_TOOL_URI:
        return "Review shape health and cortex work orders before mutating research policy."
    return str(payload.get("reason") or f"Execute follow-up read for {topic}.")[:500]


def _json_value(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw or ""))
    except Exception:
        return default


def _bounded_int(raw: Any, *, default: int, low: int, high: int) -> int:
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(low, min(high, value))


def _float_or_none(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return max(0.0, min(1.0, float(raw)))
    except Exception:
        return None


__all__ = [
    "ALPHA_GEOMETRY_ACTION_TOOL_URI",
    "ALPHA_GEOMETRY_CORTEX_TOOL_URI",
    "CortexTaskExecution",
    "DEFAULT_CORTEX_TASK_TOPICS",
    "execute_cortex_task",
    "execute_cortex_task_queue",
]
