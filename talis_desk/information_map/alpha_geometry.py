"""Alpha geometry for the information map.

This module is the research-grade scoring layer for the map. It turns many
scout strings into a small field of market cells whose shape is meaningful:
support, contradiction, source independence, fragility, frontier pressure, and
verifier readiness. Projection UIs can render this, but the numbers themselves
are the routing substrate.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..store import get_desk_store
from .store import recent_information_strings


DEFAULT_GEOMETRY_WEIGHTS: dict[str, float] = {
    # The default preserves the pre-policy scoring shape. MarketEvolve
    # programs can override this to learn a different map geometry.
    "frontier_pressure": 0.38,
    "tension": 0.24,
    "verifier_readiness": 0.24,
    "support_mass": 0.14,
    "source_independence": 0.0,
    "fragility_penalty": 0.18,
}


DEFAULT_ROUTING_THRESHOLDS: dict[str, float] = {
    # These preserve the original route-directive behavior. MarketEvolve
    # programs can override them when a candidate policy learns different
    # graduation, repair, or coverage rules.
    "verify_trade_scream_min": 0.62,
    "verify_readiness_min": 0.55,
    "repair_fragility_min": 0.55,
    "tension_min": 0.45,
    "tension_readiness_min": 0.45,
    "widen_sources_frontier_min": 0.50,
    "widen_sources_source_max": 0.45,
    "widen_scouts_novelty_min": 0.35,
    # Default values preserve the original permissive verifier route. Evolved
    # programs can tighten these so the cortex treats brittle high-scream cells
    # as source-repair work before verifier spend.
    "verify_allow_fragility_max": 1.0,
    "verify_source_independence_min": 0.0,
}


@dataclass
class AlphaGeometryCell:
    cell_key: str
    cycle_id: str
    entity: str
    horizon: str
    lens: str
    theme: str = ""
    string_count: int = 0
    scout_count: int = 0
    evidence_ref_count: int = 0
    source_families: list[str] = field(default_factory=list)
    coordinates: dict[str, float] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    route_directive: str = "observe"
    quality_flags: list[str] = field(default_factory=list)

    @property
    def trade_scream_score(self) -> float:
        return float(self.metrics.get("trade_scream_score") or 0.0)

    @property
    def verifier_readiness(self) -> float:
        return float(self.metrics.get("verifier_readiness") or 0.0)


@dataclass
class AlphaGeometrySnapshot:
    cycle_id: str
    created_at: str
    cells: list[AlphaGeometryCell] = field(default_factory=list)
    global_metrics: dict[str, float] = field(default_factory=dict)
    quality_flags: list[str] = field(default_factory=list)


def compute_alpha_geometry(
    *,
    cycle_id: str,
    limit: int = 2000,
    persist: bool = True,
    geometry_weights: Optional[dict[str, Any]] = None,
    routing_thresholds: Optional[dict[str, Any]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> AlphaGeometrySnapshot:
    """Compute and optionally persist the information geometry for a cycle."""
    db = conn or get_desk_store().conn
    weights = normalize_geometry_weights(geometry_weights)
    thresholds = normalize_routing_thresholds(routing_thresholds)
    rows = recent_information_strings(cycle_id=cycle_id, limit=limit, conn=db)
    now = datetime.now(timezone.utc).isoformat()
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = _cell_key(row)
        groups.setdefault(key, []).append(row)

    cells = [
        _cell_from_rows(
            cycle_id=cycle_id,
            cell_key=key,
            rows=cell_rows,
            geometry_weights=weights,
            routing_thresholds=thresholds,
        )
        for key, cell_rows in groups.items()
    ]
    cells.sort(key=lambda c: c.trade_scream_score, reverse=True)
    snapshot = AlphaGeometrySnapshot(
        cycle_id=cycle_id,
        created_at=now,
        cells=cells,
        global_metrics=_global_metrics(cells, rows),
        quality_flags=_snapshot_flags(
            cells,
            rows,
            policy_weighted=geometry_weights is not None,
            policy_routed=routing_thresholds is not None,
        ),
    )
    if persist:
        persist_alpha_geometry(snapshot, conn=db)
    return snapshot


def persist_alpha_geometry(
    snapshot: AlphaGeometrySnapshot,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> list[str]:
    db = conn or get_desk_store().conn
    _ensure_alpha_geometry_table(db)
    ids: list[str] = []
    for cell in snapshot.cells:
        row_id = _geometry_id(snapshot.cycle_id, cell.cell_key)
        ids.append(row_id)
        db.execute(
            """
            INSERT OR REPLACE INTO information_geometry_snapshots (
                id, cycle_id, cell_key, entity, theme, horizon, lens,
                string_count, scout_count, evidence_ref_count,
                source_families_json, coordinates_json, metrics_json,
                route_directive, trade_scream_score, verifier_readiness,
                quality_flags, created_at, valid_from, transaction_from,
                transaction_to
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                row_id,
                snapshot.cycle_id,
                cell.cell_key,
                cell.entity,
                cell.theme,
                cell.horizon,
                cell.lens,
                cell.string_count,
                cell.scout_count,
                cell.evidence_ref_count,
                json.dumps(cell.source_families),
                json.dumps(cell.coordinates),
                json.dumps(cell.metrics),
                cell.route_directive,
                cell.trade_scream_score,
                cell.verifier_readiness,
                json.dumps(cell.quality_flags),
                snapshot.created_at,
                snapshot.created_at,
                snapshot.created_at,
            ),
        )
    db.commit()
    return ids


def load_alpha_geometry(
    *,
    cycle_id: str,
    limit: int = 64,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    db = conn or get_desk_store().conn
    _ensure_alpha_geometry_table(db)
    rows = db.execute(
        """
        SELECT * FROM information_geometry_snapshots
        WHERE cycle_id = ?
        ORDER BY trade_scream_score DESC, verifier_readiness DESC
        LIMIT ?
        """,
        (cycle_id, int(limit)),
    ).fetchall()
    return [_geometry_row_to_dict(row) for row in rows]


def alpha_geometry_seed_directives(
    snapshot: AlphaGeometrySnapshot,
    *,
    max_items: int = 12,
) -> list[dict[str, Any]]:
    """Return routing directives for the next scout/specialist pass."""
    out: list[dict[str, Any]] = []
    for cell in snapshot.cells:
        if cell.route_directive == "observe":
            continue
        out.append({
            "cell_key": cell.cell_key,
            "entity": cell.entity,
            "theme": cell.theme,
            "horizon": cell.horizon,
            "lens": cell.lens,
            "route_directive": cell.route_directive,
            "trade_scream_score": cell.trade_scream_score,
            "verifier_readiness": cell.verifier_readiness,
            "why": _directive_reason(cell),
            "metrics": cell.metrics,
            "quality_flags": cell.quality_flags,
        })
        if len(out) >= max_items:
            break
    return out


def plan_alpha_geometry_actions(
    *,
    cycle_id: str,
    limit: int = 64,
    geometry_weights: Optional[dict[str, Any]] = None,
    routing_thresholds: Optional[dict[str, Any]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    """Translate map shape into concrete next actions.

    This is the "what should the cortex do after seeing the field?" tool. It
    keeps the geometry deterministic while making the shape legible as routing,
    verifier, source-repair, and tool-building work.
    """
    db = conn or get_desk_store().conn
    rows = load_alpha_geometry(cycle_id=cycle_id, limit=limit, conn=db)
    source = "persisted"
    if not rows:
        snapshot = compute_alpha_geometry(
            cycle_id=cycle_id,
            limit=2000,
            persist=False,
            geometry_weights=geometry_weights,
            routing_thresholds=routing_thresholds,
            conn=db,
        )
        source = "computed_ephemeral"
        rows = [
            {
                "cell_key": cell.cell_key,
                "entity": cell.entity,
                "theme": cell.theme,
                "horizon": cell.horizon,
                "lens": cell.lens,
                "string_count": cell.string_count,
                "scout_count": cell.scout_count,
                "evidence_ref_count": cell.evidence_ref_count,
                "source_families": cell.source_families,
                "coordinates": cell.coordinates,
                "metrics": cell.metrics,
                "route_directive": cell.route_directive,
                "trade_scream_score": cell.trade_scream_score,
                "verifier_readiness": cell.verifier_readiness,
                "quality_flags": cell.quality_flags,
            }
            for cell in snapshot.cells
        ]
    actions: list[dict[str, Any]] = []
    for row in rows:
        action = _shape_action_from_geometry_row(row)
        if action:
            actions.append(action)
    actions.sort(key=lambda a: (float(a.get("priority_score") or 0.0), str(a.get("action") or "")), reverse=True)
    global_shape = _shape_global_summary(rows)
    tool_requests = _shape_tool_requests(actions)
    routing_queue = _cortex_routing_queue(actions, global_shape, tool_requests)
    top_cells = rows[: min(16, len(rows))]
    return {
        "schema_version": "alpha_geometry_action_plan_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cycle_id": cycle_id,
        "source": source,
        "status": "ready" if rows else "empty",
        "shape_semantics": {
            "verify_now": "high trade-scream score and verifier readiness",
            "repair_sources": "fragile evidence, failed evidence flags, or low source independence",
            "resolve_tension": "contradictory strings with enough evidence to adjudicate",
            "widen_sources": "novel/frontier cell with insufficient independent source families",
            "widen_scouts": "novel cell needing independent scout replication",
        },
        "global_shape": global_shape,
        "top_cells": top_cells,
        "actions": actions[:24],
        "routing_queue": routing_queue,
        "tool_requests": tool_requests,
        "cortex_next_step": _cortex_next_step(actions, global_shape, tool_requests),
        "cortex_toolkit": _cortex_toolkit(actions, tool_requests),
        "cortex_prompt_hint": (
            "Scan actions first. If verify_now dominates, route verifier/specialist spend. "
            "If repair_sources or widen_sources dominate, allocate scouts/tool-builders before verification. "
            "If resolve_tension dominates, assign contradiction-resolution scouts."
        ),
        "quality_flags": ["alpha_geometry_action_plan"],
    }


def _shape_action_from_geometry_row(row: dict[str, Any]) -> dict[str, Any] | None:
    metrics = dict(row.get("metrics") or {})
    coordinates = dict(row.get("coordinates") or {})
    flags = [str(x) for x in (row.get("quality_flags") or [])]
    directive = str(row.get("route_directive") or "observe")
    if directive == "observe" and not flags:
        return None
    fragility = _float(metrics.get("fragility") or coordinates.get("color_fragility"), 0.0)
    source_independence = _float(metrics.get("source_independence") or coordinates.get("x_source_independence"), 0.0)
    frontier_pressure = _float(metrics.get("frontier_pressure") or coordinates.get("y_frontier_pressure"), 0.0)
    tension = _float(metrics.get("tension") or coordinates.get("z_tension"), 0.0)
    readiness = _float(row.get("verifier_readiness") or metrics.get("verifier_readiness"), 0.0)
    scream = _float(row.get("trade_scream_score") or metrics.get("trade_scream_score"), 0.0)
    if directive == "verify_now":
        action = "send_to_verifier_council"
        owner = "verifier"
        success_gate = "2_of_3 verifier majority with independent source receipts"
        reason = "The geometry is both high-signal and verifier-ready."
    elif directive == "repair_sources":
        action = "repair_source_family"
        owner = "source_integrity"
        success_gate = "source_independence rises or fragility falls below threshold"
        reason = "The shape is hot but brittle; source repair must precede conviction."
    elif directive == "resolve_tension":
        action = "assign_tension_resolution_scouts"
        owner = "seed_router"
        success_gate = "contradiction resolves into a supported mechanism or explicit kill signal"
        reason = "The map contains a contradiction that can itself be alpha."
    elif directive == "widen_sources":
        action = "widen_independent_sources"
        owner = "seed_router"
        success_gate = "new source family creates a decision-changing edge"
        reason = "The frontier cell needs source breadth before it deserves more spend."
    elif directive == "widen_scouts":
        action = "replicate_with_independent_scouts"
        owner = "seed_router"
        success_gate = "second scout confirms, contradicts, or kills the string"
        reason = "The cell is novel but still needs independent scout replication."
    else:
        if fragility >= 0.55:
            action = "repair_source_family"
            owner = "source_integrity"
            success_gate = "fragility falls below 0.45"
            reason = "Fragility is high even without a route directive."
        elif tension >= 0.45:
            action = "assign_tension_resolution_scouts"
            owner = "seed_router"
            success_gate = "tension becomes resolved or promoted"
            reason = "Tension is elevated."
        else:
            return None
    priority_score = _clamp01(
        scream * 0.34
        + readiness * 0.20
        + frontier_pressure * 0.18
        + tension * 0.14
        + fragility * 0.10
        + (1.0 - source_independence) * 0.04
    )
    missing_edges = _missing_edges_for_shape(
        action=action,
        metrics=metrics,
        flags=flags,
        source_independence=source_independence,
        fragility=fragility,
    )
    route_task_id = _route_task_id(
        cell_key=str(row.get("cell_key") or ""),
        action=action,
        route_directive=directive,
        missing_edges=missing_edges,
    )
    return {
        "route_task_id": route_task_id,
        "action": action,
        "owner": owner,
        "priority_score": round(priority_score, 4),
        "cell_key": row.get("cell_key"),
        "entity": row.get("entity"),
        "horizon": row.get("horizon"),
        "lens": row.get("lens"),
        "theme": row.get("theme"),
        "route_directive": directive,
        "coordinates": coordinates,
        "metrics": metrics,
        "quality_flags": flags,
        "reason": reason,
        "missing_edges": missing_edges,
        "success_gate": success_gate,
        "suggested_next_tools": _tools_for_shape_action(action),
    }


def _cortex_next_step(
    actions: list[dict[str, Any]],
    global_shape: dict[str, Any],
    tool_requests: list[dict[str, Any]],
) -> dict[str, Any]:
    if not actions:
        return {
            "status": "idle",
            "primary_action": "observe",
            "primary_owner": "cortex",
            "reason": "No geometry cell crossed an action threshold.",
            "immediate_tool_sequence": [],
            "seed_template": {},
            "success_gate": "A future cycle emits a non-observe route directive.",
            "post_action_map_update": "Recompute alpha geometry after the next information strings land.",
        }
    top = actions[0]
    directive = str(top.get("route_directive") or "observe")
    first_tools = [str(x) for x in (top.get("suggested_next_tools") or [])[:4]]
    if not first_tools:
        first_tools = [str(x.get("tool_uri")) for x in tool_requests[:4] if x.get("tool_uri")]
    return {
        "status": "ready",
        "primary_owner": str(top.get("owner") or "seed_router"),
        "primary_action": str(top.get("action") or "route_next_seed"),
        "read_shape_as": directive,
        "route_task_id": top.get("route_task_id"),
        "source_cell_key": top.get("cell_key"),
        "priority_score": top.get("priority_score"),
        "reason": top.get("reason"),
        "global_shape_summary": {
            "cell_count": global_shape.get("cell_count"),
            "route_directive_counts": global_shape.get("route_directive_counts"),
            "avg_trade_scream_score": global_shape.get("avg_trade_scream_score"),
            "avg_verifier_readiness": global_shape.get("avg_verifier_readiness"),
            "avg_frontier_pressure": global_shape.get("avg_frontier_pressure"),
        },
        "immediate_tool_sequence": first_tools,
        "seed_template": {
            "source": "alpha_geometry_route",
            "must_call_first": "tic://tool/talis_native/plan_alpha_geometry_actions@v1",
            "route_task_id": top.get("route_task_id"),
            "entity": top.get("entity"),
            "horizon": top.get("horizon"),
            "lens": top.get("lens"),
            "theme": top.get("theme") or f"alpha_geometry_{directive}",
            "bias_mode": _bias_for_route_directive(directive),
            "payload": {
                "alpha_geometry_cell_key": top.get("cell_key"),
                "alpha_geometry_route_directive": directive,
                "missing_edges": top.get("missing_edges") or [],
            },
        },
        "success_gate": top.get("success_gate"),
        "post_action_map_update": (
            "Persist the next scout strings with source-family and tool-call edges, "
            "then recompute alpha geometry and close or re-route this cell."
        ),
    }


def _bias_for_route_directive(directive: str) -> str:
    return {
        "verify_now": "verify",
        "repair_sources": "source_repair",
        "resolve_tension": "resolve_tension",
        "widen_sources": "frontier",
        "widen_scouts": "frontier",
    }.get(directive, "frontier")


def _cortex_routing_queue(
    actions: list[dict[str, Any]],
    global_shape: dict[str, Any],
    tool_requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    fallback_tools = [
        str(req.get("tool_uri"))
        for req in tool_requests
        if isinstance(req, dict) and str(req.get("tool_uri") or "").strip()
    ]
    for rank, action in enumerate(actions[:24], start=1):
        directive = str(action.get("route_directive") or "observe")
        tool_sequence = [str(x) for x in (action.get("suggested_next_tools") or []) if str(x).strip()]
        if not tool_sequence:
            tool_sequence = fallback_tools[:4]
        route_task_id = str(action.get("route_task_id") or _route_task_id(
            cell_key=str(action.get("cell_key") or ""),
            action=str(action.get("action") or ""),
            route_directive=directive,
            missing_edges=[str(x) for x in (action.get("missing_edges") or [])],
        ))
        queue.append({
            "route_task_id": route_task_id,
            "rank": rank,
            "owner": action.get("owner") or "seed_router",
            "action": action.get("action") or "route_next_seed",
            "route_directive": directive,
            "priority_score": action.get("priority_score"),
            "cell_key": action.get("cell_key"),
            "entity": action.get("entity"),
            "horizon": action.get("horizon"),
            "lens": action.get("lens"),
            "theme": action.get("theme"),
            "reason": action.get("reason"),
            "missing_edges": action.get("missing_edges") or [],
            "success_gate": action.get("success_gate"),
            "must_call_first": "tic://tool/talis_native/plan_alpha_geometry_actions@v1",
            "tool_sequence": tool_sequence[:6],
            "seed_slice": {
                "source": "alpha_geometry_route",
                "route_task_id": route_task_id,
                "entity": action.get("entity"),
                "horizon": action.get("horizon"),
                "lens": action.get("lens"),
                "theme": action.get("theme") or f"alpha_geometry_{directive}",
                "bias_mode": _bias_for_route_directive(directive),
                "payload": {
                    "alpha_geometry_cell_key": action.get("cell_key"),
                    "alpha_geometry_route_directive": directive,
                    "alpha_geometry_route_task_id": route_task_id,
                    "alpha_geometry_missing_edges": action.get("missing_edges") or [],
                    "alpha_geometry_success_gate": action.get("success_gate"),
                },
            },
            "map_update_rule": _map_update_rule_for_action(str(action.get("action") or "")),
            "stop_condition": _stop_condition_for_action(str(action.get("action") or "")),
            "global_shape_summary": {
                "cell_count": global_shape.get("cell_count"),
                "route_directive_counts": global_shape.get("route_directive_counts"),
                "avg_trade_scream_score": global_shape.get("avg_trade_scream_score"),
                "avg_verifier_readiness": global_shape.get("avg_verifier_readiness"),
            },
        })
    return queue


def _cortex_toolkit(
    actions: list[dict[str, Any]],
    tool_requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tool_uris: list[str] = [
        "tic://tool/talis_native/plan_alpha_geometry_actions@v1",
        "tic://tool/talis_native/review_alpha_geometry_cortex@v1",
    ]
    for action in actions[:6]:
        for uri in action.get("suggested_next_tools") or []:
            if str(uri).strip():
                tool_uris.append(str(uri))
    for req in tool_requests[:6]:
        if isinstance(req, dict) and str(req.get("tool_uri") or "").strip():
            tool_uris.append(str(req["tool_uri"]))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for uri in tool_uris:
        if uri in seen:
            continue
        seen.add(uri)
        out.append({
            "tool_uri": uri,
            "purpose": (
                "read_current_shape_and_rank_next_work"
                if uri == "tic://tool/talis_native/plan_alpha_geometry_actions@v1"
                else "review_shape_health_and_propose_geometry_policy_work"
                if uri == "tic://tool/talis_native/review_alpha_geometry_cortex@v1"
                else "move_or_falsify_top_shape_edge"
            ),
        })
    return out[:12]


def _route_task_id(
    *,
    cell_key: str,
    action: str,
    route_directive: str,
    missing_edges: list[str],
) -> str:
    raw = "|".join([cell_key, action, route_directive, *missing_edges])
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"shape_task_{digest}"


def _map_update_rule_for_action(action: str) -> str:
    if action == "send_to_verifier_council":
        return "Attach verifier votes and independent source receipts to the source cell, then recompute readiness and fragility."
    if action == "repair_source_family":
        return "Persist new source-family evidence edges, then recompute source independence and fragility before verifier routing."
    if action == "assign_tension_resolution_scouts":
        return "Persist confirm/contradict/kill edges, then recompute tension and promote only resolved mechanisms."
    if action == "widen_independent_sources":
        return "Persist the first decision-changing independent source edge, then recompute frontier pressure and source independence."
    if action == "replicate_with_independent_scouts":
        return "Persist the independent scout string as a replication edge, then recompute support mass and novelty."
    return "Persist the next information strings and recompute alpha geometry before spending another route."


def _stop_condition_for_action(action: str) -> str:
    if action == "send_to_verifier_council":
        return "Stop when verifier majority passes/fails or source receipts remain insufficient."
    if action == "repair_source_family":
        return "Stop when fragility falls below threshold, source independence rises, or no independent source exists."
    if action == "assign_tension_resolution_scouts":
        return "Stop when the contradiction resolves into pass/fail/kill, not when it merely gets restated."
    if action == "widen_independent_sources":
        return "Stop when one new source family changes the expected edge or all likely source families are exhausted."
    if action == "replicate_with_independent_scouts":
        return "Stop when an independent scout confirms, contradicts, or kills the original cell."
    return "Stop when the route contract is satisfied or falsified."


def _shape_global_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = [dict(row.get("metrics") or {}) for row in rows]
    directives: dict[str, int] = {}
    for row in rows:
        directive = str(row.get("route_directive") or "observe")
        directives[directive] = directives.get(directive, 0) + 1
    return {
        "cell_count": len(rows),
        "route_directive_counts": directives,
        "avg_trade_scream_score": round(_avg([_float(row.get("trade_scream_score"), 0.0) for row in rows]), 4),
        "avg_verifier_readiness": round(_avg([_float(row.get("verifier_readiness"), 0.0) for row in rows]), 4),
        "avg_fragility": round(_avg([_float(m.get("fragility"), 0.0) for m in metrics]), 4),
        "avg_source_independence": round(_avg([_float(m.get("source_independence"), 0.0) for m in metrics]), 4),
        "avg_frontier_pressure": round(_avg([_float(m.get("frontier_pressure"), 0.0) for m in metrics]), 4),
    }


def _missing_edges_for_shape(
    *,
    action: str,
    metrics: dict[str, Any],
    flags: list[str],
    source_independence: float,
    fragility: float,
) -> list[str]:
    edges: list[str] = []
    if action in {"repair_source_family", "widen_independent_sources"} or source_independence < 0.45:
        edges.append("claim -> independent_source_family")
    if action == "send_to_verifier_council":
        edges.append("claim -> verifier_decision")
    if action == "assign_tension_resolution_scouts":
        edges.append("contradiction -> adjudicating_evidence")
    if action == "replicate_with_independent_scouts":
        if "multi_string_single_scout_cell" in flags:
            edges.append("single_scout_cell -> independent_replication")
        else:
            edges.append("thin_cell -> independent_replication")
    if fragility >= 0.55 or "low_evidence_coverage" in flags:
        edges.append("claim -> citation_resolved_evidence")
    if _float(metrics.get("frontier_pressure"), 0.0) >= 0.50:
        edges.append("frontier_cell -> next_receptive_field")
    return sorted(set(edges))


def _tools_for_shape_action(action: str) -> list[str]:
    if action == "repair_source_family":
        return [
            "tic://tool/builtin/query_source_health@v1",
            "tic://tool/learned/evidence_ref_resolver@v1",
        ]
    if action == "widen_independent_sources":
        return [
            "tic://tool/parallel/parallel_search@v1",
            "tic://tool/perplexity/web_search@v1",
            "tic://tool/hydromancer/get_hl_pnl_leaderboard@v1",
        ]
    if action == "assign_tension_resolution_scouts":
        return [
            "tic://tool/builtin/query_timeseries@v1",
            "tic://tool/parallel/parallel_search@v1",
        ]
    if action == "replicate_with_independent_scouts":
        return [
            "tic://tool/builtin/query_events_recent@v1",
            "tic://tool/builtin/query_timeseries@v1",
        ]
    if action == "send_to_verifier_council":
        return [
            "tic://tool/builtin/query_source_health@v1",
            "tic://tool/builtin/query_timeseries@v1",
        ]
    return []


def _shape_tool_requests(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for action in actions:
        for tool in action.get("suggested_next_tools") or []:
            key = (str(action.get("cell_key") or ""), str(tool))
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "tool_uri": tool,
                "cell_key": action.get("cell_key"),
                "expected_edge": (action.get("missing_edges") or ["shape -> next_action"])[0],
                "why": action.get("reason"),
                "eval_gate": action.get("success_gate"),
            })
            if len(out) >= 16:
                return out
    return out


def _cell_from_rows(
    *,
    cycle_id: str,
    cell_key: str,
    rows: list[dict[str, Any]],
    geometry_weights: Optional[dict[str, float]] = None,
    routing_thresholds: Optional[dict[str, float]] = None,
) -> AlphaGeometryCell:
    first = rows[0] if rows else {}
    string_count = len(rows)
    scout_ids = {str(r.get("scout_id") or "") for r in rows if r.get("scout_id")}
    source_families = sorted({
        fam
        for row in rows
        for fam in _source_families(row)
        if fam
    })
    evidence_refs = sorted({
        ref
        for row in rows
        for ref in [*_string_list(row.get("evidence_refs")), *_string_list(row.get("source_tool_call_ids"))]
        if ref
    })
    attentions = [_float(r.get("attention_score"), 0.0) for r in rows]
    convictions = [_float(r.get("conviction"), 0.0) for r in rows]
    novelties = [_float(r.get("novelty_score"), 0.0) for r in rows]
    crowdedness = [_float(r.get("crowdedness"), 0.5) for r in rows]
    relations = [str(r.get("extends_or_contradicts") or "new").lower() for r in rows]
    row_flags = [
        flag
        for row in rows
        for flag in _string_list(row.get("quality_flags"))
    ]

    avg_attention = _avg(attentions)
    avg_conviction = _avg(convictions)
    avg_novelty = _avg(novelties)
    avg_crowdedness = _avg(crowdedness)
    evidence_coverage = sum(1 for r in rows if r.get("evidence_refs") or r.get("source_tool_call_ids")) / max(1, string_count)
    source_entropy = _entropy(source_families)
    source_independence = min(1.0, source_entropy * min(1.0, len(source_families) / 3.0))
    scout_independence = min(1.0, len(scout_ids) / max(1, min(4, string_count)))
    contradiction_density = sum(1 for rel in relations if rel == "contradicts") / max(1, string_count)
    high_novel = sum(1 for n in novelties if n >= 0.65)
    high_crowded = sum(1 for c in crowdedness if c >= 0.70)
    tension = _clamp01(
        contradiction_density * 0.52
        + (0.20 if high_novel and high_crowded else 0.0)
        + min(0.28, _std(convictions) + _std(novelties))
    )
    fragility = _clamp01(
        (1.0 - evidence_coverage) * 0.26
        + (1.0 - source_independence) * 0.20
        + _flag_rate(row_flags, ("adversarial_decision:quarantine", "failed_call_as_evidence", "missing_kill_signal")) * 0.34
        + _flag_rate(row_flags, ("missing_mechanism", "missing_depth_layers", "no_supported_tool_refs")) * 0.20
    )
    support_mass = _clamp01(avg_attention * 0.62 + avg_conviction * 0.24 + evidence_coverage * 0.14)
    novelty_pressure = _clamp01(avg_novelty * (1.0 - avg_crowdedness) * 1.25)
    frontier_pressure = _clamp01(
        novelty_pressure * 0.42
        + source_independence * 0.24
        + scout_independence * 0.14
        + support_mass * 0.20
        - fragility * 0.22
    )
    verifier_readiness = _clamp01(
        support_mass * 0.30
        + evidence_coverage * 0.24
        + source_independence * 0.22
        + scout_independence * 0.14
        + (1.0 - fragility) * 0.10
    )
    trade_scream_score = score_alpha_geometry_components(
        source_independence=source_independence,
        frontier_pressure=frontier_pressure,
        verifier_readiness=verifier_readiness,
        tension=tension,
        support_mass=support_mass,
        fragility=fragility,
        geometry_weights=geometry_weights,
    )
    thresholds = normalize_routing_thresholds(routing_thresholds)

    coordinates = {
        "x_source_independence": round(source_independence, 4),
        "y_frontier_pressure": round(frontier_pressure, 4),
        "z_tension": round(tension, 4),
        "color_fragility": round(fragility, 4),
        "size_support_mass": round(support_mass, 4),
    }
    metrics = {
        "support_mass": round(support_mass, 4),
        "source_entropy": round(source_entropy, 4),
        "source_independence": round(source_independence, 4),
        "scout_independence": round(scout_independence, 4),
        "evidence_coverage": round(evidence_coverage, 4),
        "tension": round(tension, 4),
        "fragility": round(fragility, 4),
        "novelty_pressure": round(novelty_pressure, 4),
        "frontier_pressure": round(frontier_pressure, 4),
        "verifier_readiness": round(verifier_readiness, 4),
        "trade_scream_score": round(trade_scream_score, 4),
        "avg_attention": round(avg_attention, 4),
        "avg_conviction": round(avg_conviction, 4),
        "avg_novelty": round(avg_novelty, 4),
        "avg_crowdedness": round(avg_crowdedness, 4),
        "contradiction_density": round(contradiction_density, 4),
        **{
            f"geometry_weight_{key}": round(value, 4)
            for key, value in normalize_geometry_weights(geometry_weights).items()
        },
        **{
            f"routing_threshold_{key}": round(value, 4)
            for key, value in thresholds.items()
        },
    }
    quality_flags = _cell_flags(
        string_count=string_count,
        scout_count=len(scout_ids),
        source_families=source_families,
        evidence_coverage=evidence_coverage,
        tension=tension,
        fragility=fragility,
        trade_scream_score=trade_scream_score,
        verifier_readiness=verifier_readiness,
        row_flags=row_flags,
        routing_thresholds=thresholds,
    )
    return AlphaGeometryCell(
        cell_key=cell_key,
        cycle_id=cycle_id,
        entity=str(first.get("entity") or "UNKNOWN"),
        horizon=str(first.get("horizon") or first.get("time_horizon") or "unknown"),
        lens=str(first.get("lens") or "unknown"),
        theme=str(first.get("theme") or ""),
        string_count=string_count,
        scout_count=len(scout_ids),
        evidence_ref_count=len(evidence_refs),
        source_families=source_families,
        coordinates=coordinates,
        metrics=metrics,
        route_directive=_route_directive(metrics, quality_flags, routing_thresholds=thresholds),
        quality_flags=quality_flags,
    )


def _cell_key(row: dict[str, Any]) -> str:
    value = str(row.get("coverage_cell_key") or "").strip()
    if value:
        return value
    parts = [
        str(row.get("entity") or "UNKNOWN"),
        str(row.get("horizon") or row.get("time_horizon") or "unknown"),
        str(row.get("lens") or "unknown"),
        str(row.get("theme") or ""),
    ]
    return "|".join(parts)


def _source_families(row: dict[str, Any]) -> list[str]:
    families: list[str] = []
    for flag in _string_list(row.get("quality_flags")):
        if flag.startswith("source_family:"):
            families.append(flag.split(":", 1)[1])
    for ref in [*_string_list(row.get("evidence_refs")), *_string_list(row.get("source_tool_call_ids"))]:
        family = _family_from_ref(ref)
        if family:
            families.append(family)
    return sorted(set(families))


def _family_from_ref(ref: str) -> str:
    text = str(ref or "").lower()
    if not text:
        return ""
    if any(tok in text for tok in ("farm_grok_x_alpha", "grok", "x_search", "xai", "twitter", "x.com")):
        return "grok_x_alpha"
    if "hydromancer" in text or "wallet" in text or "builder" in text:
        return "hydromancer"
    if "hl_reject" in text or "our_hl_node" in text or "node" in text:
        return "our_hl_node"
    if "l4" in text or "orderbook" in text or "funding" in text or "coinalyze" in text:
        return "market_microstructure"
    if "web" in text or "parallel" in text or "gdelt" in text or "news" in text:
        return "web_attention"
    if "sec" in text or "filing" in text or "analyst" in text:
        return "fundamentals_filings"
    if "macro" in text or "fred" in text or "treasury" in text or "fomc" in text:
        return "macro_official"
    if "event" in text:
        return "event_store"
    return ""


def _cell_flags(
    *,
    string_count: int,
    scout_count: int,
    source_families: list[str],
    evidence_coverage: float,
    tension: float,
    fragility: float,
    trade_scream_score: float,
    verifier_readiness: float,
    row_flags: list[str],
    routing_thresholds: Optional[dict[str, Any]] = None,
) -> list[str]:
    thresholds = normalize_routing_thresholds(routing_thresholds)
    flags: set[str] = set()
    if string_count < 2:
        flags.add("thin_cell")
    if scout_count < 2:
        flags.add("single_scout_cell")
        if string_count >= 2:
            flags.add("multi_string_single_scout_cell")
    if len(source_families) < 2:
        flags.add("low_source_entropy")
    if evidence_coverage < 0.5:
        flags.add("low_evidence_coverage")
    if tension >= thresholds["tension_min"]:
        flags.add("tension_resolution_candidate")
    if fragility >= thresholds["repair_fragility_min"]:
        flags.add("fragile_geometry")
    if (
        trade_scream_score >= thresholds["verify_trade_scream_min"]
        and verifier_readiness >= thresholds["verify_readiness_min"]
    ):
        flags.add("frontier_trade_candidate")
    if any("adversarial_decision:quarantine" in flag for flag in row_flags):
        flags.add("contains_quarantined_string")
    if any("failed_call_as_evidence" in flag for flag in row_flags):
        flags.add("contains_failed_evidence_flag")
    return sorted(flags)


def _route_directive(
    metrics: dict[str, float],
    flags: list[str],
    *,
    routing_thresholds: Optional[dict[str, Any]] = None,
) -> str:
    thresholds = normalize_routing_thresholds(routing_thresholds)
    verifier_shape_is_clean = (
        metrics.get("fragility", 0.0) <= thresholds["verify_allow_fragility_max"]
        and metrics.get("source_independence", 0.0) >= thresholds["verify_source_independence_min"]
    )
    if (
        "multi_string_single_scout_cell" in flags
        and metrics.get("novelty_pressure", 0.0) >= thresholds["widen_scouts_novelty_min"]
    ):
        return "widen_scouts"
    if "frontier_trade_candidate" in flags and verifier_shape_is_clean:
        return "verify_now"
    if "fragile_geometry" in flags or "contains_failed_evidence_flag" in flags:
        return "repair_sources"
    if (
        metrics.get("tension", 0.0) >= thresholds["tension_min"]
        and metrics.get("verifier_readiness", 0.0) >= thresholds["tension_readiness_min"]
    ):
        return "resolve_tension"
    if (
        metrics.get("frontier_pressure", 0.0) >= thresholds["widen_sources_frontier_min"]
        and metrics.get("source_independence", 0.0) < thresholds["widen_sources_source_max"]
    ):
        return "widen_sources"
    if (
        metrics.get("novelty_pressure", 0.0) >= thresholds["widen_scouts_novelty_min"]
        and ("thin_cell" in flags or "multi_string_single_scout_cell" in flags)
    ):
        return "widen_scouts"
    return "observe"


def _directive_reason(cell: AlphaGeometryCell) -> str:
    return (
        f"{cell.route_directive}: scream={cell.trade_scream_score:.2f}, "
        f"ready={cell.verifier_readiness:.2f}, "
        f"source_ind={cell.metrics.get('source_independence', 0.0):.2f}, "
        f"fragility={cell.metrics.get('fragility', 0.0):.2f}."
    )


def _global_metrics(cells: list[AlphaGeometryCell], rows: list[dict[str, Any]]) -> dict[str, float]:
    cell_weights = [float(c.string_count) for c in cells]
    family_counts: dict[str, int] = {}
    for cell in cells:
        for family in cell.source_families:
            family_counts[family] = family_counts.get(family, 0) + 1
    return {
        "string_count": float(len(rows)),
        "cell_count": float(len(cells)),
        "unique_entities": float(len({c.entity for c in cells if c.entity})),
        "coverage_entropy": round(_entropy_from_weights(cell_weights), 4),
        "source_family_entropy": round(_entropy_from_weights(list(family_counts.values())), 4),
        "avg_trade_scream_score": round(_avg([c.trade_scream_score for c in cells]), 4),
        "avg_verifier_readiness": round(_avg([c.verifier_readiness for c in cells]), 4),
        "avg_fragility": round(_avg([c.metrics.get("fragility", 0.0) for c in cells]), 4),
        "frontier_trade_candidates": float(sum(1 for c in cells if "frontier_trade_candidate" in c.quality_flags)),
    }


def normalize_geometry_weights(raw: Optional[dict[str, Any]] = None) -> dict[str, float]:
    weights = dict(DEFAULT_GEOMETRY_WEIGHTS)
    for key, value in (raw or {}).items():
        if key not in weights:
            continue
        weights[key] = max(0.0, _float(value, weights[key]))
    return weights


def normalize_routing_thresholds(raw: Optional[dict[str, Any]] = None) -> dict[str, float]:
    thresholds = dict(DEFAULT_ROUTING_THRESHOLDS)
    source = dict(raw or {})

    aliases: dict[str, str] = {
        "exploit_trade_scream_min": "verify_trade_scream_min",
        "min_verifier_readiness": "verify_readiness_min",
        "min_source_independence": "widen_sources_source_max",
    }
    for src, dst in aliases.items():
        if src in source:
            thresholds[dst] = _clamp01(_float(source[src], thresholds[dst]))

    if "repair_fragility_min" in source:
        thresholds["repair_fragility_min"] = _clamp01(
            _float(source["repair_fragility_min"], thresholds["repair_fragility_min"])
        )
    elif source.get("repair_sources_before_verify") and "max_fragility" in source:
        thresholds["repair_fragility_min"] = _clamp01(
            _float(source["max_fragility"], thresholds["repair_fragility_min"])
        )

    for key, value in source.items():
        if key not in thresholds:
            continue
        thresholds[key] = _clamp01(_float(value, thresholds[key]))
    return thresholds


def score_alpha_geometry_components(
    *,
    source_independence: float,
    frontier_pressure: float,
    verifier_readiness: float,
    tension: float,
    support_mass: float,
    fragility: float,
    geometry_weights: Optional[dict[str, Any]] = None,
) -> float:
    weights = normalize_geometry_weights(geometry_weights)
    positives = {
        "source_independence": source_independence,
        "frontier_pressure": frontier_pressure,
        "verifier_readiness": verifier_readiness,
        "tension": tension,
        "support_mass": support_mass,
    }
    total_positive_weight = sum(weights.get(key, 0.0) for key in positives) or 1.0
    weighted_positive = sum(
        _clamp01(value) * weights.get(key, 0.0)
        for key, value in positives.items()
    ) / total_positive_weight
    return round(_clamp01(
        weighted_positive - _clamp01(fragility) * weights.get("fragility_penalty", 0.0)
    ), 4)


def _snapshot_flags(
    cells: list[AlphaGeometryCell],
    rows: list[dict[str, Any]],
    *,
    policy_weighted: bool = False,
    policy_routed: bool = False,
) -> list[str]:
    flags: set[str] = {"alpha_geometry_v1"}
    if policy_weighted:
        flags.add("policy_weighted_geometry")
    if policy_routed:
        flags.add("policy_routed_geometry")
    if not rows:
        flags.add("no_information_strings")
    if cells and sum(1 for c in cells if "low_source_entropy" in c.quality_flags) / len(cells) > 0.60:
        flags.add("market_map_under_sourced")
    if cells and sum(
        1 for c in cells
        if "thin_cell" in c.quality_flags or "multi_string_single_scout_cell" in c.quality_flags
    ) / len(cells) > 0.60:
        flags.add("market_map_under_covered")
    if any("frontier_trade_candidate" in c.quality_flags for c in cells):
        flags.add("has_frontier_trade_candidates")
    return sorted(flags)


def _ensure_alpha_geometry_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS information_geometry_snapshots (
            id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            cell_key TEXT NOT NULL,
            entity TEXT,
            theme TEXT,
            horizon TEXT,
            lens TEXT,
            string_count INTEGER NOT NULL DEFAULT 0,
            scout_count INTEGER NOT NULL DEFAULT 0,
            evidence_ref_count INTEGER NOT NULL DEFAULT 0,
            source_families_json TEXT NOT NULL DEFAULT '[]',
            coordinates_json TEXT NOT NULL DEFAULT '{}',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            route_directive TEXT NOT NULL DEFAULT 'observe',
            trade_scream_score REAL NOT NULL DEFAULT 0.0,
            verifier_readiness REAL NOT NULL DEFAULT 0.0,
            quality_flags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_geometry_cycle "
        "ON information_geometry_snapshots(cycle_id, trade_scream_score DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_geometry_route "
        "ON information_geometry_snapshots(route_directive, cycle_id, verifier_readiness DESC)"
    )


def _geometry_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for src, dst, default in (
        ("source_families_json", "source_families", []),
        ("coordinates_json", "coordinates", {}),
        ("metrics_json", "metrics", {}),
        ("quality_flags", "quality_flags", []),
    ):
        try:
            d[dst] = json.loads(d.get(src) or json.dumps(default))
        except Exception:
            d[dst] = default
        if src != dst:
            d.pop(src, None)
    return d


def _geometry_id(cycle_id: str, cell_key: str) -> str:
    raw = f"{cycle_id}|{cell_key}".encode()
    return "igeo_" + hashlib.sha256(raw).hexdigest()[:16]


def _entropy(families: list[str]) -> float:
    if not families:
        return 0.0
    counts: dict[str, int] = {}
    for family in families:
        counts[family] = counts.get(family, 0) + 1
    return _entropy_from_weights(list(counts.values()))


def _entropy_from_weights(weights: list[float]) -> float:
    weights = [float(w) for w in weights if float(w) > 0]
    if len(weights) <= 1:
        return 0.0
    total = sum(weights)
    raw = -sum((w / total) * math.log(w / total) for w in weights)
    return _clamp01(raw / math.log(len(weights)))


def _flag_rate(flags: list[str], needles: tuple[str, ...]) -> float:
    if not flags:
        return 0.0
    hits = sum(1 for flag in flags if any(needle in flag for needle in needles))
    return min(1.0, hits / max(1, len(flags)))


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    avg = _avg(values)
    return math.sqrt(sum((v - avg) ** 2 for v in values) / len(values))


def _float(raw: Any, default: float) -> float:
    try:
        return float(raw)
    except Exception:
        return default


def _clamp01(raw: float) -> float:
    return max(0.0, min(1.0, float(raw)))


def _string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        if not raw.strip():
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            return [raw.strip()]
        return [raw.strip()]
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


__all__ = [
    "AlphaGeometryCell",
    "AlphaGeometrySnapshot",
    "DEFAULT_GEOMETRY_WEIGHTS",
    "DEFAULT_ROUTING_THRESHOLDS",
    "alpha_geometry_seed_directives",
    "compute_alpha_geometry",
    "load_alpha_geometry",
    "normalize_geometry_weights",
    "normalize_routing_thresholds",
    "plan_alpha_geometry_actions",
    "persist_alpha_geometry",
    "score_alpha_geometry_components",
]
