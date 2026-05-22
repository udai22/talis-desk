"""Cortex supervision for alpha geometry.

The action planner lets agents ask "where should I route next?" This module
answers the next meta-question: "is the shape itself healthy, and how should
the cortex change the geometry policy before more budget is spent?"
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import json
from datetime import datetime, timezone
from typing import Any, Optional

from .._tic_config import ensure_tic_on_path
from ..store import get_desk_store
from .alpha_geometry import (
    normalize_geometry_weights,
    normalize_routing_thresholds,
    plan_alpha_geometry_actions,
)


GEOMETRY_CORTEX_SCHEMA_VERSION = "alpha_geometry_cortex_review_v1"
GEOMETRY_CORTEX_DEFAULT_MODEL = "anthropic:claude-opus-4-7"


def build_alpha_geometry_cortex_review(
    *,
    cycle_id: str,
    limit: int = 64,
    action_plan: Optional[dict[str, Any]] = None,
    lineage: Optional[dict[str, Any]] = None,
    metrics: Optional[dict[str, float]] = None,
    use_llm: bool = False,
    model: str = GEOMETRY_CORTEX_DEFAULT_MODEL,
    conn: Optional[Any] = None,
) -> dict[str, Any]:
    """Return a cortex-readable review of the geometry policy and next actions."""
    if not str(cycle_id or "").strip():
        raise ValueError("cycle_id_required")
    db = conn or get_desk_store().conn
    from .market_evolve import (
        DEFAULT_MARKET_EVOLVE_GENOME,
        build_market_evolve_lineage,
        collect_market_evolve_metrics,
        load_active_market_evolve_program,
    )

    active_program = load_active_market_evolve_program(cycle_id=cycle_id, conn=db)
    genome = copy.deepcopy(active_program.genome or DEFAULT_MARKET_EVOLVE_GENOME)
    current_weights = normalize_geometry_weights(genome.get("geometry_weights"))
    current_thresholds = normalize_routing_thresholds(genome.get("routing_thresholds"))
    plan = action_plan or plan_alpha_geometry_actions(
        cycle_id=cycle_id,
        limit=limit,
        geometry_weights=current_weights,
        routing_thresholds=current_thresholds,
        conn=db,
    )
    lineage_graph = lineage or build_market_evolve_lineage(conn=db)
    metric_packet = metrics or collect_market_evolve_metrics(
        cycle_id=cycle_id,
        geometry_weights=current_weights,
        routing_thresholds=current_thresholds,
        conn=db,
    )

    routing_queue = [x for x in (plan.get("routing_queue") or []) if isinstance(x, dict)]
    global_shape = plan.get("global_shape") if isinstance(plan.get("global_shape"), dict) else {}
    frontier = [x for x in (lineage_graph.get("frontier") or []) if isinstance(x, dict)]
    diagnostics = _shape_diagnostics(
        global_shape=global_shape,
        metrics=metric_packet,
        routing_queue=routing_queue,
        lineage_frontier=frontier,
    )
    policy_patch = _policy_patch_from_diagnostics(
        diagnostics,
        current_weights=current_weights,
        current_thresholds=current_thresholds,
    )
    work_orders = _cortex_work_orders(
        cycle_id=cycle_id,
        diagnostics=diagnostics,
        action_plan=plan,
        lineage_frontier=frontier,
        policy_patch=policy_patch,
    )
    context_packet = _context_packet(
        cycle_id=cycle_id,
        active_program=active_program,
        action_plan=plan,
        lineage=lineage_graph,
        metrics=metric_packet,
        diagnostics=diagnostics,
        policy_patch=policy_patch,
        work_orders=work_orders,
        current_weights=current_weights,
        current_thresholds=current_thresholds,
    )

    llm_review = _empty_llm_review(model=model, context_packet=context_packet)
    quality_flags = ["alpha_geometry_cortex_review"]
    if use_llm:
        try:
            prompt = _llm_cortex_prompt(context_packet)
            llm_review["prompt"] = prompt
            response = _run_llm_cortex_sync(model=model, prompt=prompt)
            llm_review.update({
                "enabled": True,
                "model_used": response.get("model_used") or model,
                "provider": response.get("provider") or "?",
                "raw_text": response.get("text") or "",
            })
            parsed = _extract_first_json(str(response.get("text") or ""))
            accepted = _accepted_llm_review(parsed)
            llm_review["accepted"] = accepted
            if accepted.get("accepted"):
                policy_patch = _merge_policy_patch(policy_patch, accepted.get("policy_patch") or {})
                work_orders = [*work_orders, *accepted.get("additional_work_orders", [])][:12]
                quality_flags.append("llm_cortex_patch_accepted")
            else:
                quality_flags.append("llm_cortex_patch_rejected")
        except Exception as exc:
            llm_review.update({"enabled": True, "error": f"{type(exc).__name__}: {exc}"})
            quality_flags.append(f"llm_cortex_failed:{type(exc).__name__}")

    return {
        "schema_version": GEOMETRY_CORTEX_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cycle_id": cycle_id,
        "status": "ready" if global_shape or routing_queue else "empty",
        "active_program": {
            "program_id": active_program.program_id,
            "name": active_program.name,
            "generation": active_program.generation,
            "score": round(float(active_program.score or 0.0), 4),
            "quality_flags": list(active_program.quality_flags),
        },
        "shape_health": _shape_health(global_shape=global_shape, metrics=metric_packet, routing_queue=routing_queue),
        "current_geometry_policy": {
            "geometry_weights": current_weights,
            "routing_thresholds": current_thresholds,
        },
        "diagnostics": diagnostics,
        "cortex_work_orders": work_orders,
        "proposed_geometry_policy": {
            "policy_patch": policy_patch,
            "mutation_kind_hint": _mutation_kind_hint(diagnostics),
            "target_metrics": _target_metrics(diagnostics),
            "falsification_gates": _falsification_gates(diagnostics, metric_packet),
            "experiment_unit": "matched_seed_cells",
            "why_this_matters": _policy_thesis(diagnostics),
        },
        "shape_can_direct_next": bool(routing_queue and (plan.get("cortex_next_step") or {}).get("status") == "ready"),
        "next_route_task": routing_queue[0] if routing_queue else {},
        "evolution_frontier_top": frontier[0] if frontier else {},
        "llm_cortex": llm_review,
        "context_packet": context_packet,
        "quality_flags": quality_flags,
    }


def _shape_health(
    *,
    global_shape: dict[str, Any],
    metrics: dict[str, float],
    routing_queue: list[dict[str, Any]],
) -> dict[str, Any]:
    directive_counts = dict(global_shape.get("route_directive_counts") or {})
    routed = sum(int(v or 0) for k, v in directive_counts.items() if str(k) != "observe")
    cells = int(global_shape.get("cell_count") or metrics.get("geometry_cell_count") or 0)
    return {
        "cell_count": cells,
        "routed_cell_count": routed,
        "routing_queue_length": len(routing_queue),
        "route_directive_counts": directive_counts,
        "route_action_rate": round(float(metrics.get("geometry_route_action_rate", 0.0)), 4),
        "route_entropy": round(float(metrics.get("geometry_route_entropy", 0.0)), 4),
        "avg_trade_scream_score": global_shape.get("avg_trade_scream_score", metrics.get("avg_trade_scream_score", 0.0)),
        "avg_verifier_readiness": global_shape.get("avg_verifier_readiness", metrics.get("avg_verifier_readiness", 0.0)),
        "avg_frontier_pressure": global_shape.get("avg_frontier_pressure", metrics.get("avg_frontier_pressure", 0.0)),
        "avg_source_independence": global_shape.get("avg_source_independence", metrics.get("avg_source_independence", 0.0)),
        "avg_fragility": global_shape.get("avg_fragility", metrics.get("avg_fragility", 0.0)),
        "fragile_verify_rate": round(float(metrics.get("fragile_verify_rate", 0.0)), 4),
        "high_signal_observe_rate": round(float(metrics.get("high_signal_observe_rate", 0.0)), 4),
        "route_contract_success_rate": round(float(metrics.get("route_contract_success_rate", 0.0)), 4),
    }


def _shape_diagnostics(
    *,
    global_shape: dict[str, Any],
    metrics: dict[str, float],
    routing_queue: list[dict[str, Any]],
    lineage_frontier: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    cell_count = int(global_shape.get("cell_count") or metrics.get("geometry_cell_count") or 0)
    if cell_count <= 0:
        diagnostics.append(_diagnostic(
            "empty_geometry_field",
            "critical",
            "The cortex has no shape to read; ingest strings or recompute geometry before spending scouts.",
            {"routing_thresholds": {"coverage_gap_budget_share": 0.18}},
            ["geometry_cell_count"],
        ))
    if cell_count > 0 and not routing_queue:
        diagnostics.append(_diagnostic(
            "silent_shape",
            "high",
            "Cells exist but no ranked route was emitted; lower hidden-edge thresholds and increase frontier pressure.",
            {
                "geometry_weights": {"frontier_pressure": 0.36, "support_mass": 0.18},
                "routing_thresholds": {"widen_sources_frontier_min": 0.44, "verify_trade_scream_min": 0.58},
            },
            ["geometry_route_action_rate", "high_signal_observe_rate"],
        ))
    if float(metrics.get("fragile_verify_rate", 0.0)) > 0.20:
        diagnostics.append(_diagnostic(
            "fragile_verify_leak",
            "critical",
            "The shape is sending brittle cells toward verification; repair source families before verifier spend.",
            {
                "geometry_weights": {"source_independence": 0.34, "fragility_penalty": 0.34},
                "routing_thresholds": {
                    "verify_allow_fragility_max": 0.35,
                    "verify_source_independence_min": 0.55,
                    "repair_sources_before_verify": True,
                },
            },
            ["fragile_verify_rate", "avg_fragility", "source_independence"],
        ))
    if float(metrics.get("high_signal_observe_rate", 0.0)) > 0.35:
        diagnostics.append(_diagnostic(
            "hidden_edge_under_routing",
            "high",
            "Clean high-signal cells are staying in observe; make the geometry more expressive around frontier/support.",
            {
                "geometry_weights": {"frontier_pressure": 0.38, "verifier_readiness": 0.28},
                "routing_thresholds": {"verify_trade_scream_min": 0.56, "widen_scouts_novelty_min": 0.42},
            },
            ["high_signal_observe_rate", "geometry_observe_rate"],
        ))
    if (
        float(metrics.get("route_contract_eval_count", 0.0)) >= 1.0
        and float(metrics.get("geometry_route_action_rate", 0.0)) > 0.0
        and float(metrics.get("route_contract_success_rate", 1.0)) < 0.65
    ):
        diagnostics.append(_diagnostic(
            "route_contract_not_moving_edges",
            "high",
            "Geometry-routed scouts are seeing the route but not closing the requested missing edge often enough.",
            {
                "routing_thresholds": {"route_contract_min_success_rate": 0.70},
                "prompt_policy": {"route_contract_pressure": "strict"},
            },
            ["route_contract_success_rate", "route_contract_failure_rate"],
        ))
    if (
        float(global_shape.get("avg_frontier_pressure") or metrics.get("avg_frontier_pressure") or 0.0) >= 0.45
        and float(global_shape.get("avg_source_independence") or metrics.get("avg_source_independence") or 0.0) < 0.45
    ):
        diagnostics.append(_diagnostic(
            "frontier_without_source_breadth",
            "medium",
            "The map is finding frontier pressure before enough independent source families exist.",
            {
                "tool_request_policy": {"prefer_missing_source_family": True},
                "routing_thresholds": {"widen_sources_source_max": 0.58},
            },
            ["avg_frontier_pressure", "avg_source_independence"],
        ))
    if lineage_frontier:
        top = lineage_frontier[0]
        if str(top.get("next_action") or "") in {"collect_candidate_experiment_evidence", "continue_matched_experiment"}:
            diagnostics.append(_diagnostic(
                "policy_frontier_needs_evidence",
                "medium",
                "The evolution frontier has a candidate policy that needs matched evidence before another mutation.",
                {"routing_thresholds": {"market_evolve_min_matched_pairs": 20}},
                ["market_evolve_experiment_sample_size", "proof_gate_summary"],
            ))
    if not diagnostics:
        diagnostics.append(_diagnostic(
            "shape_operational",
            "info",
            "The geometry field is readable and has an executable next route; keep routing and measuring.",
            {},
            ["geometry_route_action_rate", "route_contract_success_rate"],
        ))
    return diagnostics


def _diagnostic(
    code: str,
    severity: str,
    diagnosis: str,
    policy_patch: dict[str, Any],
    target_metrics: list[str],
) -> dict[str, Any]:
    severity_score = {"critical": 1.0, "high": 0.78, "medium": 0.55, "info": 0.25}.get(severity, 0.5)
    return {
        "code": code,
        "severity": severity,
        "severity_score": severity_score,
        "diagnosis": diagnosis,
        "policy_patch": policy_patch,
        "target_metrics": target_metrics,
    }


def _policy_patch_from_diagnostics(
    diagnostics: list[dict[str, Any]],
    *,
    current_weights: dict[str, float],
    current_thresholds: dict[str, float],
) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    for diagnostic in diagnostics:
        patch = _merge_policy_patch(patch, diagnostic.get("policy_patch") or {})
    if "geometry_weights" in patch:
        values = dict(current_weights)
        values.update({k: v for k, v in (patch.get("geometry_weights") or {}).items() if isinstance(k, str)})
        patch["geometry_weights"] = normalize_geometry_weights(values)
    if "routing_thresholds" in patch:
        values = dict(current_thresholds)
        values.update({k: v for k, v in (patch.get("routing_thresholds") or {}).items() if isinstance(k, str)})
        normalized = normalize_routing_thresholds(values)
        for key, value in values.items():
            if key not in normalized:
                normalized[key] = value
        patch["routing_thresholds"] = normalized
    return patch


def _cortex_work_orders(
    *,
    cycle_id: str,
    diagnostics: list[dict[str, Any]],
    action_plan: dict[str, Any],
    lineage_frontier: list[dict[str, Any]],
    policy_patch: dict[str, Any],
) -> list[dict[str, Any]]:
    orders: list[dict[str, Any]] = []
    queue = [x for x in (action_plan.get("routing_queue") or []) if isinstance(x, dict)]
    if queue:
        top = queue[0]
        orders.append({
            "order_id": f"gcx_execute_{top.get('route_task_id') or 'top_route'}",
            "owner": top.get("owner") or "seed_router",
            "action": top.get("action") or "execute_top_shape_route",
            "cycle_id": cycle_id,
            "route_task_id": top.get("route_task_id"),
            "cell_key": top.get("cell_key"),
            "must_call_first": top.get("must_call_first"),
            "tool_sequence": top.get("tool_sequence") or [],
            "success_gate": top.get("success_gate"),
            "map_update_rule": top.get("map_update_rule"),
        })
    actionable = [d for d in diagnostics if d.get("policy_patch")]
    if actionable and policy_patch:
        orders.append({
            "order_id": "gcx_propose_geometry_policy_mutation",
            "owner": "market_evolve",
            "action": "spawn_matched_policy_experiment",
            "cycle_id": cycle_id,
            "diagnostic_codes": [str(d.get("code")) for d in actionable],
            "candidate_policy_patch": policy_patch,
            "success_gate": "candidate beats active policy on target metrics and passes falsification gates",
            "map_update_rule": "Promote only through MarketEvolve experiment results, then recompute geometry with the active genome.",
        })
    if lineage_frontier:
        top_frontier = lineage_frontier[0]
        orders.append({
            "order_id": "gcx_evolution_frontier_followup",
            "owner": "market_evolve",
            "action": top_frontier.get("next_action") or "inspect_evolution_frontier",
            "program_id": top_frontier.get("program_id"),
            "mutation_kind": top_frontier.get("mutation_kind"),
            "success_gate": "frontier action either produces matched evidence or an explicit rejected proof packet",
        })
    if not orders:
        orders.append({
            "order_id": "gcx_observe_next_cycle",
            "owner": "cortex",
            "action": "wait_for_new_information_strings",
            "cycle_id": cycle_id,
            "success_gate": "new strings create a non-empty geometry field",
        })
    return orders[:12]


def _context_packet(
    *,
    cycle_id: str,
    active_program: Any,
    action_plan: dict[str, Any],
    lineage: dict[str, Any],
    metrics: dict[str, float],
    diagnostics: list[dict[str, Any]],
    policy_patch: dict[str, Any],
    work_orders: list[dict[str, Any]],
    current_weights: dict[str, float],
    current_thresholds: dict[str, float],
) -> dict[str, Any]:
    return {
        "cycle_id": cycle_id,
        "active_program_id": active_program.program_id,
        "active_program_generation": active_program.generation,
        "global_shape": action_plan.get("global_shape") or {},
        "top_actions": (action_plan.get("actions") or [])[:6],
        "routing_queue": (action_plan.get("routing_queue") or [])[:6],
        "market_evolve_metrics": {
            key: metrics.get(key)
            for key in sorted(metrics)
            if key.startswith("geometry_")
            or key in {
                "avg_fragility",
                "avg_source_independence",
                "fragile_verify_rate",
                "high_signal_observe_rate",
                "route_contract_success_rate",
                "route_contract_failure_rate",
            }
        },
        "evolution_frontier": (lineage.get("frontier") or [])[:6],
        "current_geometry_weights": current_weights,
        "current_routing_thresholds": current_thresholds,
        "deterministic_diagnostics": diagnostics,
        "deterministic_policy_patch": policy_patch,
        "work_orders": work_orders,
    }


def _mutation_kind_hint(diagnostics: list[dict[str, Any]]) -> str:
    codes = {str(d.get("code") or "") for d in diagnostics}
    if "fragile_verify_leak" in codes:
        return "retune_geometry_repair_before_verify"
    if "hidden_edge_under_routing" in codes or "silent_shape" in codes:
        return "retune_geometry_surface_hidden_edges"
    if "route_contract_not_moving_edges" in codes:
        return "tighten_shape_route_contract"
    if "frontier_without_source_breadth" in codes:
        return "retune_geometry_source_frontier_balance"
    return "continue_shape_guided_routing"


def _target_metrics(diagnostics: list[dict[str, Any]]) -> dict[str, str]:
    keys = []
    for diagnostic in diagnostics:
        keys.extend([str(x) for x in (diagnostic.get("target_metrics") or [])])
    out: dict[str, str] = {}
    for key in sorted(set(keys)):
        if "penalty" in key or "failure" in key or key in {"fragile_verify_rate", "high_signal_observe_rate"}:
            out[key] = "decrease"
        else:
            out[key] = "increase"
    return out


def _falsification_gates(diagnostics: list[dict[str, Any]], metrics: dict[str, float]) -> list[dict[str, Any]]:
    gates: list[dict[str, Any]] = []
    codes = {str(d.get("code") or "") for d in diagnostics}
    if "fragile_verify_leak" in codes:
        gates.append({
            "metric": "candidate_fragile_verify_rate",
            "operator": "<=",
            "threshold": max(0.0, min(0.20, float(metrics.get("fragile_verify_rate", 0.20)))),
            "reason": "A geometry retune cannot promote more brittle verifier spend.",
        })
    if "hidden_edge_under_routing" in codes or "silent_shape" in codes:
        gates.append({
            "metric": "candidate_geometry_route_action_rate",
            "operator": ">=",
            "threshold": max(0.10, float(metrics.get("geometry_route_action_rate", 0.0))),
            "reason": "The candidate must route more valid shape work than the current policy.",
        })
    if "route_contract_not_moving_edges" in codes:
        gates.append({
            "metric": "candidate_route_contract_success_rate",
            "operator": ">=",
            "threshold": 0.70,
            "reason": "Shape-routed scouts must close or falsify the missing edge.",
        })
    return gates or [{
        "metric": "candidate_evaluator_score",
        "operator": ">",
        "threshold": float(metrics.get("market_evolve_score", 0.0)),
        "reason": "If the shape is healthy, any mutation still needs to beat the active evaluator score.",
    }]


def _policy_thesis(diagnostics: list[dict[str, Any]]) -> str:
    codes = [str(d.get("code") or "") for d in diagnostics]
    if "shape_operational" in codes:
        return "The cortex should keep executing the current geometry route while collecting proof for the next policy mutation."
    return (
        "The cortex can improve future routing by mutating the geometry policy around "
        + ", ".join(code for code in codes if code)
        + "."
    )


def _empty_llm_review(*, model: str, context_packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": False,
        "model": model,
        "prompt": _llm_cortex_prompt(context_packet),
        "accepted": {"accepted": False, "reason": "llm_disabled"},
    }


def _llm_cortex_prompt(context_packet: dict[str, Any]) -> str:
    return (
        "You are the geometry cortex for Talis. You are allowed to critique the "
        "alpha-geometry shape and propose a small policy patch, but deterministic "
        "gates will reject vague or unsafe changes.\n\n"
        "Return strict JSON only:\n"
        "{\n"
        '  "assessment": "<one paragraph>",\n'
        '  "policy_patch": {"geometry_weights": {}, "routing_thresholds": {}, "tool_request_policy": {}, "prompt_policy": {}},\n'
        '  "additional_work_orders": [{"owner": "seed_router|market_evolve|tool_builder|verifier|cortex", "action": "...", "success_gate": "..."}],\n'
        '  "confidence": 0.0,\n'
        '  "kill_signal": "<what would prove your patch harmful>"\n'
        "}\n\n"
        "Rules: keep patches small, prefer changing one failure mode at a time, "
        "do not increase verifier spend when fragility is the diagnosis, and do "
        "not ask for broad more-data work without a target edge.\n\n"
        "context_packet:\n"
        + json.dumps(context_packet, indent=2, sort_keys=True, ensure_ascii=True, default=str)[:60000]
    )


def _run_llm_cortex_sync(*, model: str, prompt: str) -> dict[str, Any]:
    async def _go() -> dict[str, Any]:
        ensure_tic_on_path()
        from tic.desk.models import chat as _chat  # type: ignore

        return await _chat(
            model,
            "You are a careful market-intelligence systems architect. Return JSON only.",
            prompt,
            max_tokens=4000,
            fallback="anthropic:claude-haiku-4-5",
        )

    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(asyncio.run, _go()).result(timeout=60)
    except RuntimeError:
        return asyncio.run(_go())


def _accepted_llm_review(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"accepted": False, "reason": "not_json_object"}
    confidence = _float(raw.get("confidence"), -1.0)
    if confidence < 0.45 or confidence > 1.0:
        return {"accepted": False, "reason": "confidence_out_of_range_or_too_low"}
    kill_signal = str(raw.get("kill_signal") or "").strip()
    if not kill_signal:
        return {"accepted": False, "reason": "missing_kill_signal"}
    patch = _sanitize_policy_patch(raw.get("policy_patch"))
    orders = _sanitize_work_orders(raw.get("additional_work_orders"))
    if not patch and not orders:
        return {"accepted": False, "reason": "no_actionable_patch_or_order"}
    return {
        "accepted": True,
        "assessment": str(raw.get("assessment") or "")[:1200],
        "policy_patch": patch,
        "additional_work_orders": orders,
        "confidence": round(confidence, 3),
        "kill_signal": kill_signal[:600],
    }


def _sanitize_policy_patch(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    allowed = {
        "geometry_weights",
        "routing_thresholds",
        "tool_request_policy",
        "prompt_policy",
    }
    out: dict[str, Any] = {}
    for key in allowed:
        value = raw.get(key)
        if isinstance(value, dict) and value:
            out[key] = {
                str(k): v
                for k, v in value.items()
                if isinstance(k, str) and isinstance(v, (int, float, str, bool))
            }
    return out


def _sanitize_work_orders(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    allowed_owners = {"seed_router", "market_evolve", "tool_builder", "verifier", "cortex"}
    out: list[dict[str, Any]] = []
    for item in raw[:6]:
        if not isinstance(item, dict):
            continue
        owner = str(item.get("owner") or "cortex")
        if owner not in allowed_owners:
            owner = "cortex"
        action = str(item.get("action") or "").strip()
        success_gate = str(item.get("success_gate") or "").strip()
        if not action or not success_gate:
            continue
        out.append({
            "order_id": f"gcx_llm_{len(out) + 1}",
            "owner": owner,
            "action": action[:180],
            "success_gate": success_gate[:300],
            "source": "llm_geometry_cortex",
        })
    return out


def _merge_policy_patch(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base or {})
    for key, value in (patch or {}).items():
        if isinstance(value, dict):
            existing = out.get(key) if isinstance(out.get(key), dict) else {}
            merged = dict(existing)
            merged.update(value)
            out[key] = merged
        else:
            out[key] = value
    return out


def _extract_first_json(text: str) -> Any:
    start = (text or "").find("{")
    if start < 0:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


def _float(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except Exception:
        return default


__all__ = [
    "GEOMETRY_CORTEX_DEFAULT_MODEL",
    "GEOMETRY_CORTEX_SCHEMA_VERSION",
    "build_alpha_geometry_cortex_review",
]
