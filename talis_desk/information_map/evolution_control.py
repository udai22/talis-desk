"""Shared evolution-control view for alpha geometry and MarketEvolve.

This is the read model operators and visualizers should use when they need to
understand how the market-map shape is steering the next research action.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


def build_evolution_control_payload(
    *,
    cycle_id: str,
    active_program: Any = None,
    market_evolve_step: Any = None,
    best_evaluation: Any = None,
    conn: Any = None,
    source: str = "live_control_plane",
) -> dict[str, Any]:
    """Build the production control-plane view for geometry/evolution routing.

    The payload intentionally stays compact: it includes the action plan, the
    cortex review of the shape, the lineage frontier, and proof flags that say
    whether the shape can direct the next research move.
    """
    from .alpha_geometry import plan_alpha_geometry_actions
    from .geometry_cortex import build_alpha_geometry_cortex_review
    from .market_evolve import build_market_evolve_lineage

    genome = _mapping_value(active_program, "genome", {}) or {}
    geometry_weights = genome.get("geometry_weights") if isinstance(genome, dict) else None
    routing_thresholds = genome.get("routing_thresholds") if isinstance(genome, dict) else None
    eval_obj = best_evaluation or _attr_value(market_evolve_step, "best_evaluation", None)
    best_metrics = _mapping_value(eval_obj, "metrics", None)
    action_plan = plan_alpha_geometry_actions(
        cycle_id=cycle_id,
        limit=64,
        geometry_weights=geometry_weights,
        routing_thresholds=routing_thresholds,
        conn=conn,
    )
    lineage = build_market_evolve_lineage(conn=conn)
    cortex_review = build_alpha_geometry_cortex_review(
        cycle_id=cycle_id,
        action_plan=action_plan,
        lineage=lineage,
        metrics=best_metrics if isinstance(best_metrics, dict) else None,
        use_llm=False,
        conn=conn,
    )
    action_summary = {
        "schema_version": action_plan.get("schema_version"),
        "status": action_plan.get("status"),
        "global_shape": action_plan.get("global_shape") or {},
        "cortex_next_step": action_plan.get("cortex_next_step") or {},
        "routing_queue": (action_plan.get("routing_queue") or [])[:8],
        "cortex_toolkit": (action_plan.get("cortex_toolkit") or [])[:8],
        "actions": (action_plan.get("actions") or [])[:8],
        "quality_flags": action_plan.get("quality_flags") or [],
    }
    cortex_summary = {
        "schema_version": cortex_review.get("schema_version"),
        "status": cortex_review.get("status"),
        "shape_health": cortex_review.get("shape_health") or {},
        "diagnostics": (cortex_review.get("diagnostics") or [])[:8],
        "cortex_work_orders": (cortex_review.get("cortex_work_orders") or [])[:8],
        "proposed_geometry_policy": cortex_review.get("proposed_geometry_policy") or {},
        "shape_can_direct_next": cortex_review.get("shape_can_direct_next"),
        "next_route_task": cortex_review.get("next_route_task") or {},
        "evolution_frontier_top": cortex_review.get("evolution_frontier_top") or {},
        "quality_flags": cortex_review.get("quality_flags") or [],
    }
    lineage_summary = {
        "schema_version": lineage.get("schema_version"),
        "program_count": lineage.get("program_count"),
        "mutation_count": lineage.get("mutation_count"),
        "experiment_count": lineage.get("experiment_count"),
        "active_program_ids": lineage.get("active_program_ids") or [],
        "candidate_program_ids": lineage.get("candidate_program_ids") or [],
        "frontier": (lineage.get("frontier") or [])[:8],
        "quality_flags": lineage.get("quality_flags") or [],
    }
    diagnostics = [
        str(d.get("code"))
        for d in (cortex_summary.get("diagnostics") or [])
        if isinstance(d, dict) and d.get("code")
    ]
    proposed = cortex_summary.get("proposed_geometry_policy") or {}
    policy_patch = proposed.get("policy_patch") if isinstance(proposed, dict) else {}
    return {
        "schema_version": "swarm_evolution_control_v1",
        "cycle_id": cycle_id,
        "source": source,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "active_program": _active_program_summary(active_program),
        "alpha_geometry_action_plan": action_summary,
        "geometry_cortex_review": cortex_summary,
        "market_evolve_lineage": lineage_summary,
        "proof": {
            "action_plan_ready": action_summary.get("status") in {"ready", "empty"},
            "cortex_review_ready": cortex_summary.get("status") in {"ready", "empty"},
            "shape_can_direct_next": bool(cortex_summary.get("shape_can_direct_next")),
            "diagnostic_codes": diagnostics,
            "policy_patch_present": bool(policy_patch),
            "lineage_frontier_count": len(lineage_summary.get("frontier") or []),
            "mutation_kind_hint": proposed.get("mutation_kind_hint") if isinstance(proposed, dict) else None,
        },
    }


def post_evolution_control_work_orders(
    *,
    cycle_id: str,
    evolution_control: dict[str, Any],
    conn: sqlite3.Connection | None = None,
    limit: int = 12,
    source: str = "swarm_evolution_control",
    include_market_evolve_experiment_arms: bool = True,
    max_experiment_pairs: int = 2,
) -> dict[str, Any]:
    """Turn shape/cortex work orders into idempotent task contracts.

    The control plane is read-only until this function is called. `run_swarm`
    calls it after building the control payload so a live cycle has dispatchable
    tasks; monitor rebuilds intentionally do not call it.
    """
    from ..coordination import post_task
    from ..store import get_desk_store

    db = conn or get_desk_store().conn
    route_items = _control_routing_items(evolution_control)
    cortex_items = _control_cortex_items(evolution_control)
    merged = _merge_work_order_items(route_items, cortex_items)[: max(0, int(limit))]
    if include_market_evolve_experiment_arms:
        merged = _with_market_evolve_experiment_arms(
            merged,
            conn=db,
            max_pairs=max_experiment_pairs,
        )[: max(0, int(limit))]
    posted: list[dict[str, Any]] = []
    existing: list[dict[str, Any]] = []
    for item in merged:
        contract = _task_contract_from_work_order(
            cycle_id=cycle_id,
            item=item,
            source=source,
        )
        existing_id = _existing_task_contract_id(
            conn=db,
            cycle_id=cycle_id,
            dedupe_key=contract["dedupe_key"],
        )
        if existing_id:
            existing.append({
                "task_id": existing_id,
                "dedupe_key": contract["dedupe_key"],
                "title": contract["title"],
            })
            continue
        task_id = post_task(
            topic=contract["topic"],
            title=contract["title"],
            description=contract["description"],
            cycle_id=cycle_id,
            priority=contract["priority"],
            budget_usd=contract["budget_usd"],
            ttl_seconds=contract["ttl_seconds"],
            input_schema=contract["input_schema"],
            allowed_tools=contract["allowed_tools"],
            evidence_requirements=contract["evidence_requirements"],
            promotion_criteria=contract["promotion_criteria"],
            kill_criteria=contract["kill_criteria"],
            coverage_cell_key=contract["coverage_cell_key"],
            payload=contract["payload"],
            conn=db,
        )
        posted.append({
            "task_id": task_id,
            "dedupe_key": contract["dedupe_key"],
            "title": contract["title"],
            "topic": contract["topic"],
            "owner": contract["payload"].get("owner"),
            "route_task_id": contract["payload"].get("route_task_id"),
            "market_evolve_program_id": contract["payload"].get("market_evolve_program_id"),
            "market_evolve_experiment_id": contract["payload"].get("market_evolve_experiment_id"),
            "market_evolve_experiment_arm": contract["payload"].get("market_evolve_experiment_arm"),
        })
    db.commit()
    return {
        "schema_version": "evolution_control_task_dispatch_v1",
        "cycle_id": cycle_id,
        "source": source,
        "status": "posted" if posted else "already_posted" if existing else "empty",
        "posted_count": len(posted),
        "existing_count": len(existing),
        "task_count": len(posted) + len(existing),
        "posted_tasks": posted,
        "existing_tasks": existing,
        "quality_flags": [] if merged else ["no_evolution_control_work_orders"],
    }


def _active_program_summary(active_program: Any) -> dict[str, Any] | None:
    if active_program is None:
        return None
    return {
        "program_id": _mapping_value(active_program, "program_id", _mapping_value(active_program, "id", "")),
        "name": _mapping_value(active_program, "name", ""),
        "generation": int(_mapping_value(active_program, "generation", 0) or 0),
        "status": _mapping_value(active_program, "status", ""),
        "score": float(_mapping_value(active_program, "score", 0.0) or 0.0),
    }


def _mapping_value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return _attr_value(obj, key, default)


def _attr_value(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    return getattr(obj, key, default)


def _control_routing_items(evolution_control: dict[str, Any]) -> list[dict[str, Any]]:
    plan = evolution_control.get("alpha_geometry_action_plan")
    if not isinstance(plan, dict):
        return []
    return [
        {**item, "_source_kind": "routing_queue"}
        for item in (plan.get("routing_queue") or [])
        if isinstance(item, dict)
    ]


def _control_cortex_items(evolution_control: dict[str, Any]) -> list[dict[str, Any]]:
    review = evolution_control.get("geometry_cortex_review")
    if not isinstance(review, dict):
        return []
    return [
        {**item, "_source_kind": "cortex_work_order"}
        for item in (review.get("cortex_work_orders") or [])
        if isinstance(item, dict)
    ]


def _merge_work_order_items(
    route_items: list[dict[str, Any]],
    cortex_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for item in route_items:
        key = _work_order_key(item)
        by_key[key] = dict(item)
    for item in cortex_items:
        key = _work_order_key(item)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = dict(item)
            continue
        merged = {**existing, **{k: v for k, v in item.items() if v not in (None, "", [])}}
        merged["_source_kind"] = "routing_queue+cortex_work_order"
        by_key[key] = merged
    return sorted(
        by_key.values(),
        key=lambda item: (
            -float(item.get("priority_score") or 0.0),
            str(item.get("owner") or ""),
            str(item.get("action") or ""),
        ),
    )


def _task_contract_from_work_order(
    *,
    cycle_id: str,
    item: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    action = str(item.get("action") or "route_next_work")
    owner = str(item.get("owner") or _owner_for_action(action))
    route_task_id = str(item.get("route_task_id") or item.get("order_id") or _work_order_key(item))
    cell_key = str(item.get("cell_key") or "")
    topic = _topic_for_owner_action(owner=owner, action=action)
    title = _task_title(owner=owner, action=action, item=item)
    allowed_tools = _allowed_tools_for_item(item)
    success_gate = str(item.get("success_gate") or "work order produces a measured map update or explicit rejection")
    stop_condition = str(item.get("stop_condition") or success_gate)
    missing_edges = [str(x) for x in (item.get("missing_edges") or []) if str(x).strip()]
    priority = float(item.get("priority_score") or 0.0)
    if priority <= 0:
        priority = _default_priority(owner=owner, action=action)
    market_evolve_payload = _market_evolve_payload(item)
    payload = {
        "dedupe_key": _dedupe_key(cycle_id=cycle_id, item=item),
        "source": source,
        "source_kind": item.get("_source_kind"),
        "owner": owner,
        "action": action,
        "route_task_id": route_task_id,
        "order_id": item.get("order_id"),
        "cell_key": cell_key,
        "entity": item.get("entity"),
        "horizon": item.get("horizon"),
        "lens": item.get("lens"),
        "theme": item.get("theme"),
        "route_directive": item.get("route_directive"),
        "reason": item.get("reason") or item.get("diagnosis"),
        "seed_slice": item.get("seed_slice") or {},
        "missing_edges": missing_edges,
        "map_update_rule": item.get("map_update_rule"),
        "stop_condition": stop_condition,
        "raw_work_order": {
            k: v for k, v in item.items()
            if k != "_source_kind"
        },
        **market_evolve_payload,
    }
    return {
        "dedupe_key": payload["dedupe_key"],
        "topic": topic,
        "title": title,
        "description": _task_description(item=item, success_gate=success_gate, stop_condition=stop_condition),
        "priority": priority,
        "budget_usd": _budget_for_owner_action(owner=owner, action=action),
        "ttl_seconds": _ttl_for_owner_action(owner=owner, action=action),
        "coverage_cell_key": cell_key or None,
        "allowed_tools": allowed_tools,
        "evidence_requirements": [*missing_edges, success_gate],
        "promotion_criteria": {
            "success_gate": success_gate,
            "map_update_rule": item.get("map_update_rule"),
            "must_reference_route_task_id": route_task_id,
        },
        "kill_criteria": {
            "stop_condition": stop_condition,
            "kill_signal": item.get("kill_signal") or item.get("stop_condition"),
        },
        "input_schema": {
            "cycle_id": cycle_id,
            "route_task_id": route_task_id,
            "seed_slice": item.get("seed_slice") or {},
            "required_first_tool": item.get("must_call_first"),
            "tool_sequence": item.get("tool_sequence") or [],
            **{
                key: market_evolve_payload.get(key)
                for key in (
                    "market_evolve_program_id",
                    "market_evolve_experiment_id",
                    "market_evolve_experiment_arm",
                )
                if market_evolve_payload.get(key)
            },
        },
        "payload": payload,
    }


def _with_market_evolve_experiment_arms(
    items: list[dict[str, Any]],
    *,
    conn: sqlite3.Connection,
    max_pairs: int,
) -> list[dict[str, Any]]:
    if not items:
        return []
    experiments = _assignable_market_evolve_experiments(conn=conn)
    if not experiments:
        return items
    program_by_id = _market_evolve_programs_by_id(conn=conn)
    out: list[dict[str, Any]] = []
    clone_budget = max(0, int(max_pairs))
    if clone_budget <= 0:
        return items
    cloned = 0
    for item in items:
        out.append(item)
        if cloned >= clone_budget:
            continue
        for experiment in experiments:
            if cloned >= clone_budget:
                break
            base_route_task_id = str(item.get("route_task_id") or item.get("order_id") or _work_order_key(item))
            exp_id = str(experiment.get("id") or "")
            if not exp_id:
                continue
            for arm, program_id_key in (
                ("control", "parent_program_id"),
                ("candidate", "candidate_program_id"),
            ):
                program_id = str(experiment.get(program_id_key) or "")
                program = program_by_id.get(program_id)
                if program is None:
                    continue
                clone = copy.deepcopy(item)
                clone["_source_kind"] = f"{item.get('_source_kind') or 'work_order'}+market_evolve_experiment_arm"
                clone["base_route_task_id"] = base_route_task_id
                clone["route_task_id"] = f"{base_route_task_id}__{_digest({'experiment': exp_id, 'arm': arm})[:8]}_{arm}"
                clone["_market_evolve_program_id"] = program_id
                clone["_market_evolve_program_name"] = _mapping_value(program, "name", "")
                clone["_market_evolve_generation"] = int(_mapping_value(program, "generation", 0) or 0)
                clone["_market_evolve_experiment_id"] = exp_id
                clone["_market_evolve_experiment_arm"] = arm
                genome = _mapping_value(program, "genome", {}) or {}
                clone["_market_evolve_cortex_policy"] = (
                    genome.get("cortex_policy")
                    if isinstance(genome, dict) and isinstance(genome.get("cortex_policy"), dict)
                    else {}
                )
                out.append(clone)
            cloned += 1
    return out


def _assignable_market_evolve_experiments(
    *,
    conn: sqlite3.Connection,
    limit: int = 8,
) -> list[dict[str, Any]]:
    try:
        from .market_evolve import load_market_evolve_experiments

        return [
            exp for exp in load_market_evolve_experiments(status="", limit=limit, conn=conn)
            if exp.get("parent_program_id")
            and exp.get("candidate_program_id")
            and str(exp.get("status") or "") in {"planned", "running", "insufficient_sample"}
        ]
    except Exception:
        return []


def _market_evolve_programs_by_id(*, conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        from .market_evolve import load_market_evolve_programs

        return {
            str(program.program_id): program
            for program in load_market_evolve_programs(status="", limit=128, conn=conn)
        }
    except Exception:
        return {}


def _market_evolve_payload(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for src, dst in (
        ("_market_evolve_program_id", "market_evolve_program_id"),
        ("_market_evolve_program_name", "market_evolve_program_name"),
        ("_market_evolve_generation", "market_evolve_generation"),
        ("_market_evolve_experiment_id", "market_evolve_experiment_id"),
        ("_market_evolve_experiment_arm", "market_evolve_experiment_arm"),
        ("_market_evolve_cortex_policy", "market_evolve_cortex_policy"),
        ("base_route_task_id", "base_route_task_id"),
    ):
        value = item.get(src)
        if value not in (None, "", [], {}):
            out[dst] = value
    return out


def _existing_task_contract_id(
    *,
    conn: sqlite3.Connection,
    cycle_id: str,
    dedupe_key: str,
) -> str:
    try:
        row = conn.execute(
            """
            SELECT id FROM task_contracts
            WHERE cycle_id = ?
              AND payload LIKE ?
              AND status IN ('posted','claimed','running','completed','promoted')
            ORDER BY posted_at DESC
            LIMIT 1
            """,
            (cycle_id, f'%"dedupe_key":"{dedupe_key}"%'),
        ).fetchone()
    except sqlite3.Error:
        return ""
    if row is None:
        return ""
    try:
        return str(row["id"])
    except Exception:
        return str(row[0])


def _work_order_key(item: dict[str, Any]) -> str:
    experiment_id = str(item.get("_market_evolve_experiment_id") or "").strip()
    experiment_arm = str(item.get("_market_evolve_experiment_arm") or "").strip()
    program_id = str(item.get("_market_evolve_program_id") or "").strip()
    for key in ("route_task_id", "order_id"):
        value = str(item.get(key) or "").strip()
        if value:
            if experiment_id or experiment_arm or program_id:
                return _digest({
                    "base_work_order_key": value,
                    "market_evolve_program_id": program_id,
                    "market_evolve_experiment_id": experiment_id,
                    "market_evolve_experiment_arm": experiment_arm,
                })
            return value
    return _digest({
        "owner": item.get("owner"),
        "action": item.get("action"),
        "cell_key": item.get("cell_key"),
        "program_id": item.get("program_id"),
        "market_evolve_program_id": program_id,
        "market_evolve_experiment_id": experiment_id,
        "market_evolve_experiment_arm": experiment_arm,
        "success_gate": item.get("success_gate"),
    })


def _dedupe_key(*, cycle_id: str, item: dict[str, Any]) -> str:
    return _digest({
        "cycle_id": cycle_id,
        "work_order_key": _work_order_key(item),
        "owner": item.get("owner"),
        "action": item.get("action"),
        "market_evolve_program_id": item.get("_market_evolve_program_id"),
        "market_evolve_experiment_id": item.get("_market_evolve_experiment_id"),
        "market_evolve_experiment_arm": item.get("_market_evolve_experiment_arm"),
    })


def _allowed_tools_for_item(item: dict[str, Any]) -> list[str]:
    tools: list[str] = [
        "tic://tool/talis_native/plan_alpha_geometry_actions@v1",
        "tic://tool/talis_native/review_alpha_geometry_cortex@v1",
    ]
    first = str(item.get("must_call_first") or "").strip()
    if first:
        tools.append(first)
    for uri in item.get("tool_sequence") or []:
        uri_s = str(uri).strip()
        if uri_s:
            tools.append(uri_s)
    out: list[str] = []
    seen: set[str] = set()
    for uri in tools:
        if uri in seen:
            continue
        seen.add(uri)
        out.append(uri)
    return out


def _topic_for_owner_action(*, owner: str, action: str) -> str:
    if owner == "market_evolve" or "mutate" in action:
        return "market_evolve.frontier"
    if owner == "tool_builder" or "tool" in action:
        return "tool_atlas.creation"
    if owner == "verifier" or "verify" in action:
        return "alpha_geometry.verify"
    if owner == "seed_router" or "scout" in action or "source" in action:
        return "alpha_geometry.route"
    return "alpha_geometry.cortex"


def _owner_for_action(action: str) -> str:
    if "verify" in action:
        return "verifier"
    if "tool" in action:
        return "tool_builder"
    if "mutate" in action:
        return "market_evolve"
    return "seed_router"


def _task_title(*, owner: str, action: str, item: dict[str, Any]) -> str:
    label = str(item.get("theme") or item.get("cell_key") or item.get("program_id") or item.get("route_task_id") or "market map")
    return f"{owner}: {action.replace('_', ' ')} :: {label}"[:180]


def _task_description(
    *,
    item: dict[str, Any],
    success_gate: str,
    stop_condition: str,
) -> str:
    reason = str(item.get("reason") or item.get("diagnosis") or "The evolution cortex selected this work order.")
    return (
        f"{reason}\n\n"
        f"Success gate: {success_gate}\n"
        f"Stop condition: {stop_condition}"
    )[:3000]


def _default_priority(*, owner: str, action: str) -> float:
    if owner == "market_evolve" or "mutate" in action:
        return 0.72
    if owner == "verifier":
        return 0.68
    if owner == "tool_builder":
        return 0.62
    return 0.55


def _budget_for_owner_action(*, owner: str, action: str) -> float:
    if owner == "market_evolve" or "mutate" in action:
        return 0.05
    if owner == "verifier":
        return 0.15
    if owner == "tool_builder":
        return 0.10
    return 0.03


def _ttl_for_owner_action(*, owner: str, action: str) -> int:
    if owner == "market_evolve" or "mutate" in action:
        return 72 * 3600
    if owner == "verifier":
        return 12 * 3600
    return 24 * 3600


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
