"""Self-healing work orders for the market map.

The map should not only report blind spots. It should assign repair and
expansion work with enough context that a worker agent can act without
rediscovering the entire run.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..store import get_desk_store


@dataclass(frozen=True)
class MarketMapWorkOrder:
    order_id: str
    owner: str
    priority: str
    action: str
    reason: str
    input_refs: tuple[str, ...] = ()
    expected_output: str = ""
    success_criteria: tuple[str, ...] = ()
    prompt_hint: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


def build_market_map_self_healing_plan(trace: dict[str, Any]) -> dict[str, Any]:
    """Build deterministic worker assignments from the canonical trace."""
    final = trace.get("final_results") if isinstance(trace.get("final_results"), dict) else {}
    market = trace.get("market_map_plan") if isinstance(trace.get("market_map_plan"), dict) else {}
    input_packet = trace.get("input_packet") if isinstance(trace.get("input_packet"), dict) else {}
    cell = input_packet.get("cell") if isinstance(input_packet.get("cell"), dict) else {}
    stage_io = [row for row in (trace.get("stage_io") or []) if isinstance(row, dict)]
    persisted = [row for row in (trace.get("persisted_objects") or []) if isinstance(row, dict)]
    data_sources = market.get("data_source_universe") if isinstance(market.get("data_source_universe"), dict) else {}
    surfaces = [row for row in (data_sources.get("surfaces") or []) if isinstance(row, dict)]
    route = str((final.get("geometry") or {}).get("route_directive") or "observe")
    info_ids = tuple(str(x) for x in (final.get("information_string_ids") or []) if x)
    evidence_refs = tuple(str(x) for x in (final.get("evidence_receipts") or []) if x)
    recurrent = final.get("recurrent_loop") if isinstance(final.get("recurrent_loop"), dict) else {}
    worker_assignment = (
        recurrent.get("worker_assignment")
        if isinstance(recurrent.get("worker_assignment"), dict)
        else {}
    )

    orders: list[MarketMapWorkOrder] = []
    if recurrent.get("status") == "ready" and worker_assignment:
        shape_refs = tuple(
            str(x) for x in [
                recurrent.get("shape_tool_call_log_id"),
                recurrent.get("emitted_seed_id"),
            ]
            if x
        )
        orders.append(MarketMapWorkOrder(
            order_id="mwo_shape_reader_followup",
            owner=str(worker_assignment.get("owner") or "seed_router"),
            priority="high" if worker_assignment.get("route_directive") in {"verify_now", "resolve_tension"} else "medium",
            action=str(worker_assignment.get("action") or "execute_shape_route"),
            reason="The native shape reader observed alpha geometry and emitted a concrete next route.",
            input_refs=shape_refs or info_ids,
            expected_output="Execute the emitted seed/work order, then recompute geometry to prove the coordinate moved or expired.",
            success_criteria=(
                "The follow-up references the native shape-reader tool_call_log_id.",
                "The follow-up emits either a verified claim, a repaired source edge, a contradiction resolution, or an expired route.",
            ),
            prompt_hint="Start from the shape-reader output, not the raw prompt. Move exactly the coordinate named in worker_assignment.",
            payload={
                "recurrent_loop": recurrent,
                "worker_assignment": worker_assignment,
            },
        ))

    if route in {"widen_scouts", "widen_sources", "repair_sources", "resolve_tension", "verify_now"}:
        orders.append(MarketMapWorkOrder(
            order_id="mwo_route_followup",
            owner="seed_router",
            priority="high" if route in {"verify_now", "resolve_tension"} else "medium",
            action=f"allocate_{route}",
            reason="Alpha geometry emitted a non-observe route directive.",
            input_refs=info_ids,
            expected_output="Follow-up SeedCell batch with coverage_cell_keys and source-family targets.",
            success_criteria=(
                "Every generated seed points back to the source geometry cell.",
                "The follow-up batch changes a specific geometry coordinate or expires the route.",
            ),
            prompt_hint="Create the smallest seed batch that can decide whether this routed cell deserves more spend.",
            payload={"route_directive": route, "cell": cell},
        ))

    untouched_tool_gap_surfaces = [
        surface for surface in surfaces
        if str(surface.get("status") or "") in {"tool_gap", "experimental_requires_stat_test"}
    ]
    for surface in untouched_tool_gap_surfaces[:4]:
        key = str(surface.get("key") or "surface")
        orders.append(MarketMapWorkOrder(
            order_id=f"mwo_surface_{key}",
            owner="tool_builder",
            priority="high" if key in {"mempool_pending_intent", "source_health_citations"} else "medium",
            action="build_or_validate_source_adapter",
            reason=f"Known source surface is not yet first-class: {surface.get('title')}",
            input_refs=evidence_refs,
            expected_output="Promotable tool proposal with eval fixtures, raw offsets, source health, and dispatch manifest.",
            success_criteria=(
                "Tool returns typed observations with source timestamps.",
                "Tool output can be converted into information-string evidence refs.",
                "Failed or stale data emits a repair flag instead of fake coverage.",
            ),
            prompt_hint=f"Design the next read-only tool for {surface.get('title')} and define its promotion gate.",
            payload={"surface": surface},
        ))

    entity_axis = ((market.get("axes") or {}).get("entity") or {}) if isinstance(market.get("axes"), dict) else {}
    manifest = entity_axis.get("manifest") if isinstance(entity_axis.get("manifest"), dict) else {}
    if manifest.get("source_quality") != "live":
        orders.append(MarketMapWorkOrder(
            order_id="mwo_universe_refresh",
            owner="source_integrity",
            priority="high",
            action="refresh_tradeable_universe",
            reason="Tradeable universe was not sourced from live venue metadata.",
            expected_output="Fresh market_universe_manifest.json with live Hyperliquid meta or explicit outage evidence.",
            success_criteria=(
                "Manifest has source_quality=live or a source-health incident.",
                "Asset IDs, venue, dex, max leverage, and HIP-3 flags are present for HL perps.",
            ),
            prompt_hint="Refresh the venue universe before assigning broad scout coverage.",
            payload={"manifest": manifest},
        ))

    coverage = final.get("data_surface_coverage") if isinstance(final.get("data_surface_coverage"), dict) else {}
    touched = coverage.get("touched")
    total = coverage.get("total")
    if isinstance(touched, int) and isinstance(total, int) and total and touched / total < 0.5:
        orders.append(MarketMapWorkOrder(
            order_id="mwo_source_family_widen",
            owner="context_builder",
            priority="medium",
            action="widen_source_family_context",
            reason=f"This run touched only {touched}/{total} known data surfaces.",
            input_refs=evidence_refs,
            expected_output="A compact context packet that adds the highest-value missing source family without exceeding scout cost caps.",
            success_criteria=(
                "At least one new independent source family is attached.",
                "The added source changes a map edge, route directive, or kill signal.",
            ),
            prompt_hint="Choose one missing source family whose evidence would most change this cell.",
            payload={"coverage": coverage},
        ))

    tool_surfaces = [
        obj for obj in persisted
        if obj.get("surface") == "analysis_tool_proposals" and obj.get("ids")
    ]
    if tool_surfaces:
        orders.append(MarketMapWorkOrder(
            order_id="mwo_promote_tool_backlog",
            owner="learned_tool_runtime",
            priority="high",
            action="evaluate_and_promote_tool_proposals",
            reason="The run produced tool proposals that should become new senses if they pass eval.",
            input_refs=tuple(str(x) for obj in tool_surfaces for x in (obj.get("ids") or [])),
            expected_output="Tool promotion report with pass/fail, fixture coverage, and runtime manifest.",
            success_criteria=(
                "Every proposal has an eval plan or a rejection reason.",
                "Promoted tools are available to future scout atlas retrieval.",
            ),
            prompt_hint="Turn high-priority map gaps into evaluated read-only tools.",
        ))

    context_packet = {
        "cycle_id": trace.get("cycle_id"),
        "cell": cell,
        "route": route,
        "hypothesis": final.get("hypothesis"),
        "information_string_ids": list(info_ids),
        "evidence_receipts": list(evidence_refs),
        "recurrent_loop": recurrent,
        "market_universe": {
            "entity_count": entity_axis.get("count"),
            "source_quality": manifest.get("source_quality"),
            "source_counts": manifest.get("source_counts"),
            "valid_cell_count": (market.get("validity") or {}).get("valid_cell_count")
            if isinstance(market.get("validity"), dict) else None,
        },
        "data_source_universe": {
            "surface_count": data_sources.get("count"),
            "tool_gap_surfaces": [surface.get("key") for surface in untouched_tool_gap_surfaces],
        },
        "stage_ids": [stage.get("stage_id") for stage in stage_io],
    }

    llm_gap_prompt = _llm_gap_prompt(context_packet=context_packet, orders=orders)
    return {
        "schema_version": "market_map_self_healing_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "ready" if orders else "no_orders",
        "context_packet": context_packet,
        "work_orders": [asdict(order) for order in orders],
        "llm_gap_prompt": llm_gap_prompt,
        "quality_flags": [],
    }


def post_market_map_self_healing_work_orders(
    plan: dict[str, Any],
    *,
    cycle_id: str = "",
    conn: sqlite3.Connection | None = None,
    limit: int = 32,
) -> dict[str, Any]:
    """Turn self-healing work orders into durable task contracts.

    The self-healing planner is allowed to be expressive, but execution needs
    the same hard rails as the rest of the desk: an owner, allowed tools,
    input refs, success criteria, stop conditions, and idempotent posting.
    """
    from ..coordination import post_task

    db = conn or get_desk_store().conn
    resolved_cycle = str(cycle_id or plan.get("cycle_id") or plan.get("context_packet", {}).get("cycle_id") or "")
    orders = [
        order for order in (plan.get("work_orders") or [])
        if isinstance(order, dict) and str(order.get("order_id") or "").strip()
    ][: max(0, int(limit))]
    existing = _existing_self_healing_tasks(db, cycle_id=resolved_cycle)
    posted_tasks: list[dict[str, Any]] = []
    existing_tasks: list[dict[str, Any]] = []
    for index, order in enumerate(orders):
        order_id = str(order.get("order_id") or "")
        if order_id in existing:
            existing_tasks.append(existing[order_id])
            continue
        contract = _task_contract_from_self_healing_order(
            order,
            cycle_id=resolved_cycle,
            plan=plan,
            index=index,
        )
        task_id = post_task(conn=db, **contract)
        posted_tasks.append({
            "id": task_id,
            **contract,
        })
        existing[order_id] = {"id": task_id, **contract}
    db.commit()
    return {
        "schema_version": "market_map_self_healing_dispatch_v1",
        "cycle_id": resolved_cycle,
        "status": "posted" if posted_tasks else "idempotent_noop" if existing_tasks else "empty",
        "posted_count": len(posted_tasks),
        "existing_count": len(existing_tasks),
        "requested_count": len(orders),
        "posted_tasks": posted_tasks,
        "existing_tasks": existing_tasks,
        "proof": {
            "orders_became_task_contracts": bool(posted_tasks or existing_tasks),
            "idempotent": True,
            "all_tasks_have_success_gates": all(
                bool((task.get("promotion_criteria") or {}).get("success_gate"))
                for task in [*posted_tasks, *existing_tasks]
            ),
        },
    }


def execute_market_map_self_healing_tasks(
    *,
    cycle_id: str,
    conn: sqlite3.Connection | None = None,
    limit: int = 8,
    agent_id: str = "market_map_self_healing_worker",
    specialist_id: str = "market_map_repair",
) -> dict[str, Any]:
    """Claim and execute posted market-map repair contracts.

    Route work reads the alpha-geometry field through the normal read-only
    harness. Tool/source/context work becomes concrete analysis-tool proposals
    or learned-tool promotion reports. The worker therefore moves the map
    rather than leaving repair orders as inert dashboard text.
    """
    from ..agent_harness import HarnessPolicy, dispatch_harness_tool
    from ..coordination import claim_task, complete_task, fail_task, start_task
    from ..tool_atlas import AgentContext

    db = conn or get_desk_store().conn
    rows = _fetch_posted_self_healing_tasks(conn=db, cycle_id=cycle_id, limit=limit)
    policy = HarnessPolicy(evidence_hard_cap=4, max_retries=0, retry_backoff_s=0.0)
    executions: list[dict[str, Any]] = []
    for row in rows:
        task = _row_dict(row)
        task_id = str(task.get("id") or "")
        payload = _json_load(task.get("payload"), {})
        input_schema = _json_load(task.get("input_schema_json"), {})
        allowed_tools = _json_load(task.get("allowed_tools_json"), [])
        promotion_criteria = _json_load(task.get("promotion_criteria_json"), {})
        kill_criteria = _json_load(task.get("kill_criteria_json"), {})
        execution: dict[str, Any] = {
            "task_id": task_id,
            "topic": task.get("topic"),
            "claimed": False,
            "started": False,
            "completed": False,
            "failed": False,
            "observations": [],
            "tool_proposal_ids": [],
            "promotion_reports": [],
            "quality_flags": [],
            "error": "",
        }
        if not claim_task(task_id, agent_id=agent_id, specialist_id=specialist_id, conn=db):
            execution["quality_flags"] = ["claim_lost"]
            executions.append(execution)
            continue
        execution["claimed"] = True
        execution["started"] = start_task(
            task_id,
            agent_id=agent_id,
            specialist_id=specialist_id,
            conn=db,
        )

        owner = str(payload.get("owner") or input_schema.get("owner") or "")
        action = str(payload.get("action") or input_schema.get("action") or "")
        observations: list[dict[str, Any]] = []
        proposal_ids: list[str] = []
        promotion_reports: list[dict[str, Any]] = []
        quality_flags: list[str] = []

        if _is_tool_backlog_task(owner=owner, action=action, payload=payload):
            promotion_reports = _promote_self_healing_tool_backlog(
                payload=payload,
                cycle_id=cycle_id,
                conn=db,
            )
            quality_flags.append(f"tool_backlog_evaluated:{len(promotion_reports)}")
        elif _self_healing_task_creates_tool(owner=owner, action=action):
            proposal_ids = _persist_tool_proposals_for_self_healing_task(
                task=task,
                payload=payload,
                input_schema=input_schema,
                promotion_criteria=promotion_criteria,
                kill_criteria=kill_criteria,
                conn=db,
            )
            quality_flags.append(f"tool_proposals_created:{len(proposal_ids)}")
        else:
            context = AgentContext(
                cycle_id=str(payload.get("source_cycle_id") or task.get("cycle_id") or cycle_id),
                specialist_id=specialist_id,
                investigation_id=task_id,
            )
            for uri in _self_healing_tool_sequence(
                allowed_tools=allowed_tools,
                input_schema=input_schema,
            ):
                observations.append(dispatch_harness_tool(
                    uri,
                    _self_healing_tool_args(
                        uri,
                        payload=payload,
                        input_schema=input_schema,
                        cycle_id=cycle_id,
                    ),
                    context,
                    policy=policy,
                    phase="market_map_self_heal",
                    requested_by_model=False,
                    request_why=str(payload.get("reason") or task.get("description") or "Repair a market-map gap."),
                    expected_edge=_self_healing_expected_edge(payload, promotion_criteria),
                    expected_info_value=0.72,
                    would_change_decision=True,
                    fallback_if_denied="Leave the self-healing task unresolved and request a source/tool adapter.",
                ))

        success = bool(proposal_ids) or bool(promotion_reports) or any(bool(obs.get("ok")) for obs in observations)
        if not success:
            quality_flags.append("no_repair_action_taken")
        completion_payload = _self_healing_completion_payload(
            task=task,
            payload=payload,
            input_schema=input_schema,
            promotion_criteria=promotion_criteria,
            kill_criteria=kill_criteria,
            observations=observations,
            proposal_ids=proposal_ids,
            promotion_reports=promotion_reports,
            quality_flags=quality_flags,
        )
        if success:
            execution["completed"] = complete_task(
                task_id,
                agent_id=agent_id,
                specialist_id=specialist_id,
                payload=completion_payload,
                conn=db,
            )
        else:
            execution["failed"] = fail_task(
                task_id,
                agent_id=agent_id,
                specialist_id=specialist_id,
                reason="market_map_self_healing_no_observation_or_proposal",
                payload=completion_payload,
                conn=db,
            )
            execution["error"] = "market_map_self_healing_no_observation_or_proposal"
        execution["observations"] = observations
        execution["tool_proposal_ids"] = proposal_ids
        execution["promotion_reports"] = promotion_reports
        execution["quality_flags"] = quality_flags
        executions.append(execution)
    return {
        "schema_version": "market_map_self_healing_worker_batch_v1",
        "cycle_id": cycle_id,
        "task_count": len(executions),
        "claimed_count": sum(1 for item in executions if item.get("claimed")),
        "completed_count": sum(1 for item in executions if item.get("completed")),
        "failed_count": sum(1 for item in executions if item.get("failed")),
        "tool_proposal_count": sum(len(item.get("tool_proposal_ids") or []) for item in executions),
        "promotion_report_count": sum(len(item.get("promotion_reports") or []) for item in executions),
        "observation_count": sum(len(item.get("observations") or []) for item in executions),
        "executions": executions,
        "proof": {
            "self_healing_tasks_claimed": any(item.get("claimed") for item in executions),
            "self_healing_tasks_completed": any(item.get("completed") for item in executions),
            "tool_builder_orders_became_proposals": any(item.get("tool_proposal_ids") for item in executions),
            "tool_backlog_orders_evaluated": any(item.get("promotion_reports") for item in executions),
            "failed_count": sum(1 for item in executions if item.get("failed")),
        },
    }


def render_market_map_self_healing_markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# Market Map Self-Healing Plan",
        "",
        f"- schema: `{plan.get('schema_version')}`",
        f"- status: `{plan.get('status')}`",
        f"- work_orders: `{len(plan.get('work_orders') or [])}`",
        "",
        "## Worker Orders",
        "",
    ]
    for order in plan.get("work_orders") or []:
        if not isinstance(order, dict):
            continue
        lines.extend([
            f"### {order.get('order_id')} - {order.get('owner')}",
            f"- priority: `{order.get('priority')}`",
            f"- action: `{order.get('action')}`",
            f"- reason: {order.get('reason')}",
            f"- expected_output: {order.get('expected_output')}",
            f"- success_criteria: `{json.dumps(order.get('success_criteria') or [])}`",
            "",
        ])
    lines.extend([
        "## LLM Gap Prompt",
        "",
        "```text",
        str(plan.get("llm_gap_prompt") or ""),
        "```",
        "",
    ])
    return "\n".join(lines)


def _task_contract_from_self_healing_order(
    order: dict[str, Any],
    *,
    cycle_id: str,
    plan: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    order_id = str(order.get("order_id") or f"mwo_{index}")
    owner = str(order.get("owner") or "market_map_worker")
    action = str(order.get("action") or "repair_market_map")
    priority_name = str(order.get("priority") or "medium").lower()
    priority_score = {
        "high": 90.0,
        "medium": 55.0,
        "low": 25.0,
    }.get(priority_name, 45.0) - index * 0.01
    input_refs = [str(x) for x in (order.get("input_refs") or []) if x]
    success_criteria = [str(x) for x in (order.get("success_criteria") or []) if x]
    expected_output = str(order.get("expected_output") or "Market-map repair result or explicit rejection.")
    success_gate = " | ".join(success_criteria) or expected_output
    payload = order.get("payload") if isinstance(order.get("payload"), dict) else {}
    contract_payload = {
        "market_map_self_healing": True,
        "market_map_work_order_id": order_id,
        "market_map_work_order_fingerprint": _order_fingerprint(order),
        "source_plan_schema_version": plan.get("schema_version"),
        "source_plan_status": plan.get("status"),
        "owner": owner,
        "action": action,
        "reason": order.get("reason"),
        "expected_output": expected_output,
        "success_criteria": success_criteria,
        "prompt_hint": order.get("prompt_hint"),
        "input_refs": input_refs,
        "order_payload": payload,
    }
    return {
        "topic": f"market_map.self_heal.{_slug(owner)}",
        "title": f"{action}: {order_id}",
        "description": str(order.get("reason") or ""),
        "cycle_id": cycle_id,
        "priority": priority_score,
        "budget_usd": _budget_for_priority(priority_name),
        "ttl_seconds": 6 * 3600 if priority_name == "high" else 24 * 3600,
        "input_schema": {
            "schema_version": "market_map_self_healing_task_v1",
            "order_id": order_id,
            "owner": owner,
            "action": action,
            "input_refs": input_refs,
            "prompt_hint": order.get("prompt_hint"),
            "requires_output": [
                "map_update",
                "evidence_ref",
                "new_seed_batch",
                "tool_proposal",
                "source_health_incident",
                "explicit_rejection_reason",
            ],
        },
        "allowed_tools": _allowed_tools_for_order(order),
        "evidence_requirements": [
            "Use the listed input_refs before broad search." if input_refs else "Name the missing evidence edge before using external sources.",
            "Every produced map update must cite a tool_call_log_id or source ref.",
        ],
        "promotion_criteria": {
            "success_gate": success_gate,
            "expected_output": expected_output,
            "map_update_required": True,
            "allow_explicit_rejection": True,
        },
        "kill_criteria": {
            "stop_condition": (
                payload.get("stop_condition")
                or order.get("stop_condition")
                or "Stop if the missing edge cannot be moved without a new source adapter or mutating tool."
            ),
            "max_empty_attempts": 1,
        },
        "coverage_cell_key": _coverage_cell_key(payload),
        "payload": contract_payload,
    }


def _existing_self_healing_tasks(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
) -> dict[str, dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT id, cycle_id, topic, title, description, status, priority,
                   budget_usd, ttl_seconds, input_schema_json, allowed_tools_json,
                   evidence_requirements_json, promotion_criteria_json,
                   kill_criteria_json, coverage_cell_key, payload
            FROM task_contracts
            WHERE cycle_id = ?
              AND topic LIKE 'market_map.self_heal.%'
              AND transaction_to IS NULL
            ORDER BY posted_at ASC
            """,
            (cycle_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    existing: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = {key: row[key] for key in row.keys()} if hasattr(row, "keys") else dict(row)
        payload = _json_load(item.get("payload"), {})
        order_id = str(payload.get("market_map_work_order_id") or "")
        if not order_id:
            continue
        existing[order_id] = {
            "id": item.get("id"),
            "topic": item.get("topic"),
            "title": item.get("title"),
            "description": item.get("description"),
            "cycle_id": item.get("cycle_id"),
            "status": item.get("status"),
            "priority": item.get("priority"),
            "budget_usd": item.get("budget_usd"),
            "ttl_seconds": item.get("ttl_seconds"),
            "input_schema": _json_load(item.get("input_schema_json"), {}),
            "allowed_tools": _json_load(item.get("allowed_tools_json"), []),
            "evidence_requirements": _json_load(item.get("evidence_requirements_json"), []),
            "promotion_criteria": _json_load(item.get("promotion_criteria_json"), {}),
            "kill_criteria": _json_load(item.get("kill_criteria_json"), {}),
            "coverage_cell_key": item.get("coverage_cell_key"),
            "payload": payload,
        }
    return existing


def _allowed_tools_for_order(order: dict[str, Any]) -> list[str]:
    payload = order.get("payload") if isinstance(order.get("payload"), dict) else {}
    surface = payload.get("surface") if isinstance(payload.get("surface"), dict) else {}
    route = str(payload.get("route_directive") or "").lower()
    owner = str(order.get("owner") or "").lower()
    action = str(order.get("action") or "").lower()
    tools: list[str] = [
        "tic://tool/builtin/query_source_health@v1",
    ]
    if owner in {"seed_router", "verifier"} or "route" in action or route:
        tools.extend([
            "tic://tool/talis_native/plan_alpha_geometry_actions@v1",
            "tic://tool/talis_native/review_alpha_geometry_cortex@v1",
            "tic://tool/builtin/query_events_recent@v1",
            "tic://tool/builtin/query_timeseries@v1",
        ])
    if owner in {"tool_builder", "learned_tool_runtime"} or "tool" in action:
        tools.extend([
            "tic://tool/parallel/parallel_search@v1",
            "tic://tool/builtin/query_events_recent@v1",
            "tic://tool/builtin/query_timeseries@v1",
        ])
    if owner == "source_integrity" or "universe" in action or "source" in action:
        tools.extend([
            "tic://tool/builtin/query_events_recent@v1",
            "tic://tool/builtin/query_source_health@v1",
        ])
    for key in ("suggested_tools", "tool_candidates"):
        tools.extend(str(x) for x in (payload.get(key) or []) if x)
    tools.extend(str(x) for x in (surface.get("example_tools") or []) if x)
    return _unique([tool for tool in tools if str(tool).startswith("tic://tool/")])[:12]


def _coverage_cell_key(payload: dict[str, Any]) -> str | None:
    cell = payload.get("cell") if isinstance(payload.get("cell"), dict) else {}
    entity = str(cell.get("entity") or "").upper()
    horizon = str(cell.get("horizon") or "")
    lens = str(cell.get("lens") or "")
    bias = str(cell.get("bias_mode") or "")
    if entity and horizon and lens and bias:
        return "|".join([entity, horizon, lens, bias])
    return None


def _order_fingerprint(order: dict[str, Any]) -> str:
    raw = json.dumps(order, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _budget_for_priority(priority: str) -> float:
    if priority == "high":
        return 0.08
    if priority == "low":
        return 0.01
    return 0.03


def _slug(raw: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in raw.lower()).strip("_") or "worker"


def _unique(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        out.append(item)
        seen.add(item)
    return out


def _json_load(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return default
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _fetch_posted_self_healing_tasks(
    *,
    conn: sqlite3.Connection,
    cycle_id: str,
    limit: int,
) -> list[Any]:
    try:
        return conn.execute(
            """
            SELECT *
            FROM task_contracts
            WHERE cycle_id = ?
              AND status = 'posted'
              AND topic LIKE 'market_map.self_heal.%'
              AND transaction_to IS NULL
            ORDER BY priority DESC, posted_at ASC
            LIMIT ?
            """,
            (cycle_id, max(1, int(limit or 1))),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _row_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(row)


def _is_tool_backlog_task(*, owner: str, action: str, payload: dict[str, Any]) -> bool:
    owner_l = owner.lower()
    action_l = action.lower()
    return (
        owner_l == "learned_tool_runtime"
        or "promote_tool" in action_l
        or "evaluate_and_promote_tool" in action_l
        or any(str(ref).startswith("atp_") for ref in (payload.get("input_refs") or []))
    )


def _self_healing_task_creates_tool(*, owner: str, action: str) -> bool:
    owner_l = owner.lower()
    action_l = action.lower()
    return (
        owner_l in {"tool_builder", "context_builder", "source_integrity"}
        or "tool" in action_l
        or "source" in action_l
        or "context" in action_l
        or "universe" in action_l
    )


def _persist_tool_proposals_for_self_healing_task(
    *,
    task: dict[str, Any],
    payload: dict[str, Any],
    input_schema: dict[str, Any],
    promotion_criteria: dict[str, Any],
    kill_criteria: dict[str, Any],
    conn: sqlite3.Connection,
) -> list[str]:
    from ..tool_atlas.discovery import AnalysisToolProposal, persist_analysis_tool_proposals

    order_payload = payload.get("order_payload") if isinstance(payload.get("order_payload"), dict) else {}
    surface = order_payload.get("surface") if isinstance(order_payload.get("surface"), dict) else {}
    cell = order_payload.get("cell") if isinstance(order_payload.get("cell"), dict) else {}
    coverage = order_payload.get("coverage") if isinstance(order_payload.get("coverage"), dict) else {}
    owner = str(payload.get("owner") or input_schema.get("owner") or "market_map_worker")
    action = str(payload.get("action") or input_schema.get("action") or "repair_market_map")
    tool_name = _tool_name_for_self_healing_task(surface=surface, owner=owner, action=action)
    source_family = str(surface.get("key") or owner or "market_map_repair")
    entity = str(cell.get("entity") or coverage.get("entity") or "")
    horizon = str(cell.get("horizon") or coverage.get("horizon") or "")
    lens = str(cell.get("lens") or coverage.get("lens") or "")
    expected_edge = _self_healing_expected_edge(payload, promotion_criteria)
    proposal = AnalysisToolProposal(
        cycle_id=str(task.get("cycle_id") or ""),
        artifact_kind="market_map_self_healing_task",
        artifact_id=str(task.get("id") or ""),
        entity=entity,
        horizon=horizon,
        lens=lens,
        proposal_kind="new_tool" if owner != "context_builder" else "context_expansion_tool",
        tool_name=tool_name,
        purpose=str(payload.get("expected_output") or task.get("description") or "Repair a missing market-map edge."),
        source_family=source_family,
        trigger=f"market_map_self_healing:{payload.get('market_map_work_order_id') or input_schema.get('order_id')}",
        input_shape={
            "entity": entity or "string",
            "horizon": horizon or "string",
            "lens": lens or "string",
            "input_refs": list(payload.get("input_refs") or []),
            "surface": surface.get("key") or source_family,
        },
        promotion_gate={
            "expected_edge": expected_edge,
            "expected_info_value": 0.72,
            "would_change_decision": True,
            "success_gate": promotion_criteria.get("success_gate"),
            "stop_condition": kill_criteria.get("stop_condition"),
        },
        eval_plan={
            "source": "market_map_self_healing_worker",
            "fixture": str(surface.get("key") or owner or action),
            "must_emit_source_timestamp_or_rejection": True,
            "must_link_task_id": str(task.get("id") or ""),
        },
        priority="high" if float(task.get("priority") or 0.0) >= 80.0 else "medium",
        created_by="market_map_self_healing_worker",
        quality_flags=[
            "from_market_map_self_healing_task",
            f"self_healing_owner:{owner}",
        ],
    )
    return persist_analysis_tool_proposals([proposal], conn=conn)


def _promote_self_healing_tool_backlog(
    *,
    payload: dict[str, Any],
    cycle_id: str,
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    from ..tool_atlas import promote_analysis_tool_proposal

    reports: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in payload.get("input_refs") or []:
        proposal_id = str(ref or "")
        if not proposal_id.startswith("atp_") or proposal_id in seen:
            continue
        seen.add(proposal_id)
        try:
            report = promote_analysis_tool_proposal(proposal_id, conn=conn)
            d = asdict(report) if hasattr(report, "__dataclass_fields__") else dict(report)
            reports.append(d)
        except Exception as exc:
            reports.append({
                "proposal_id": proposal_id,
                "cycle_id": cycle_id,
                "passed": False,
                "status": "promotion_error",
                "error": f"{type(exc).__name__}: {exc}",
            })
    return reports


def _tool_name_for_self_healing_task(
    *,
    surface: dict[str, Any],
    owner: str,
    action: str,
) -> str:
    for uri in surface.get("example_tools") or []:
        name = str(uri).rsplit("/", 1)[-1].split("@", 1)[0]
        if name:
            return _slug(name)
    key = str(surface.get("key") or "")
    if key:
        return _slug(key)
    if "universe" in action:
        return "live_tradeable_universe_source_health"
    if owner == "context_builder":
        return "source_family_context_expander"
    return _slug(action or owner or "market_map_repair_tool")


def _self_healing_tool_sequence(
    *,
    allowed_tools: Any,
    input_schema: dict[str, Any],
) -> list[str]:
    allowed = [str(uri) for uri in allowed_tools if str(uri or "").strip()] if isinstance(allowed_tools, list) else []
    sequence: list[str] = []
    required = str(input_schema.get("required_first_tool") or "")
    if required:
        sequence.append(required)
    for uri in (
        "tic://tool/talis_native/plan_alpha_geometry_actions@v1",
        "tic://tool/talis_native/review_alpha_geometry_cortex@v1",
    ):
        if uri in allowed:
            sequence.append(uri)
    for uri in input_schema.get("tool_sequence") or []:
        sequence.append(str(uri))
    if not sequence:
        sequence.extend(allowed)
    return [uri for uri in _unique(sequence) if uri in allowed][:4]


def _self_healing_tool_args(
    uri: str,
    *,
    payload: dict[str, Any],
    input_schema: dict[str, Any],
    cycle_id: str,
) -> dict[str, Any]:
    if uri == "tic://tool/talis_native/plan_alpha_geometry_actions@v1":
        return {"cycle_id": str(payload.get("source_cycle_id") or cycle_id), "limit": 64}
    if uri == "tic://tool/talis_native/review_alpha_geometry_cortex@v1":
        return {"cycle_id": str(payload.get("source_cycle_id") or cycle_id), "limit": 64, "use_llm": False}
    order_payload = payload.get("order_payload") if isinstance(payload.get("order_payload"), dict) else {}
    cell = order_payload.get("cell") if isinstance(order_payload.get("cell"), dict) else {}
    args: dict[str, Any] = {}
    for key in ("entity", "horizon", "lens"):
        value = cell.get(key) or payload.get(key) or input_schema.get(key)
        if value:
            args[key] = value
    if args.get("entity"):
        args["entity_symbol"] = args["entity"]
    return args


def _self_healing_expected_edge(
    payload: dict[str, Any],
    promotion_criteria: dict[str, Any],
) -> str:
    order_payload = payload.get("order_payload") if isinstance(payload.get("order_payload"), dict) else {}
    surface = order_payload.get("surface") if isinstance(order_payload.get("surface"), dict) else {}
    if surface.get("key"):
        return f"{surface.get('key')} -> market_map_evidence_edge"
    if payload.get("action"):
        return f"{payload.get('action')} -> market_map_update"
    return str(promotion_criteria.get("success_gate") or "market_map_gap -> repair_result")[:240]


def _self_healing_completion_payload(
    *,
    task: dict[str, Any],
    payload: dict[str, Any],
    input_schema: dict[str, Any],
    promotion_criteria: dict[str, Any],
    kill_criteria: dict[str, Any],
    observations: list[dict[str, Any]],
    proposal_ids: list[str],
    promotion_reports: list[dict[str, Any]],
    quality_flags: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": "market_map_self_healing_execution_v1",
        "task_id": task.get("id"),
        "topic": task.get("topic"),
        "cycle_id": task.get("cycle_id"),
        "order_id": payload.get("market_map_work_order_id") or input_schema.get("order_id"),
        "owner": payload.get("owner") or input_schema.get("owner"),
        "action": payload.get("action") or input_schema.get("action"),
        "success_gate": promotion_criteria.get("success_gate"),
        "stop_condition": kill_criteria.get("stop_condition"),
        "observations": observations,
        "tool_proposal_ids": proposal_ids,
        "promotion_reports": promotion_reports,
        "map_update": {
            "kind": "proposal" if proposal_ids else "promotion_report" if promotion_reports else "harness_observation",
            "expected_edge": _self_healing_expected_edge(payload, promotion_criteria),
            "evidence_refs": [
                str(obs.get("tool_call_log_id"))
                for obs in observations
                if obs.get("tool_call_log_id")
            ],
        },
        "proof": {
            "claimed_task_contract": True,
            "observations_logged": len(observations),
            "tool_proposals_created": len(proposal_ids),
            "tool_backlog_reports": len(promotion_reports),
            "success_gate_present": bool(promotion_criteria.get("success_gate")),
            "stop_condition_present": bool(kill_criteria.get("stop_condition")),
        },
        "quality_flags": quality_flags,
    }


def _llm_gap_prompt(
    *,
    context_packet: dict[str, Any],
    orders: list[MarketMapWorkOrder],
) -> str:
    return (
        "You are the Talis market-map repair planner. Read the context packet "
        "and propose only high-value map expansion or repair work. Do not ask "
        "for broad research. Every order must name the missing edge, data "
        "surface, worker type, expected output, eval gate, and stop condition.\n\n"
        "Return strict JSON only:\n"
        "{\n"
        '  "work_orders": [\n'
        '    {"owner": "seed_router|tool_builder|verifier|source_integrity|context_builder", '
        '"priority": "high|medium|low", "action": "...", "missing_edge": "...", '
        '"expected_output": "...", "eval_gate": "...", "stop_condition": "..."}\n'
        "  ]\n"
        "}\n\n"
        "context_packet:\n"
        f"{json.dumps(context_packet, indent=2, sort_keys=True, ensure_ascii=True, default=str)}\n\n"
        "deterministic_seed_orders:\n"
        f"{json.dumps([asdict(order) for order in orders], indent=2, sort_keys=True, ensure_ascii=True, default=str)}"
    )


__all__ = [
    "MarketMapWorkOrder",
    "build_market_map_self_healing_plan",
    "execute_market_map_self_healing_tasks",
    "post_market_map_self_healing_work_orders",
    "render_market_map_self_healing_markdown",
]
