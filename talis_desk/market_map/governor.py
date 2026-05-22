"""Market-map governor for strategic scout allocation.

The coverage audit answers "what is missing?" The governor answers the next
question: "which missing cells should get the next scouts, which source
surfaces should they touch, and what compact context can an LLM repair planner
use without re-reading the whole desk?"
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .._tic_config import ensure_tic_on_path
from ..information_map.data_substrate import DATA_SURFACES, DataSurface
from .coverage_audit import build_coverage_gap_manifest
from .universe import build_market_universe


GOVERNOR_SCHEMA_VERSION = "market_map_governor_v1"
FRONTIER_LLM_DEFAULT_MODEL = "anthropic:claude-opus-4-7"


@dataclass(frozen=True)
class RankedMarketGap:
    gap_id: str
    entity: str
    horizon: str
    lens: str
    bias_mode: str
    asset_class: str
    status: str
    priority_score: float
    priority_band: str
    reason: str
    missing_surfaces: tuple[str, ...] = ()
    suggested_tools: tuple[str, ...] = ()
    expected_edges: tuple[str, ...] = ()
    seed_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoutBudgetLane:
    lane_id: str
    scout_count: int
    entity_focus: str
    horizon_focus: str
    lens_focus: str
    reason: str
    example_gap_ids: tuple[str, ...] = ()


def build_market_map_governor_plan(
    *,
    cycle_id: str,
    conn: sqlite3.Connection,
    coverage_manifest: Optional[dict[str, Any]] = None,
    scout_budget: int = 1000,
    max_ranked_gaps: int = 64,
    max_seed_cells: int = 48,
    use_llm: bool = False,
    model: str = FRONTIER_LLM_DEFAULT_MODEL,
    llm_seed_share: float = 0.25,
    geometry_cycle_id: Optional[str] = None,
    geometry_weights: Optional[dict[str, Any]] = None,
    routing_thresholds: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Rank market gaps and build a scout-allocation repair plan.

    This function is intentionally deterministic by default. If `use_llm` is
    enabled, the model can propose additions from the context packet, but those
    suggestions are kept in a separate section and must still pass deterministic
    promotion gates before they become seeds or tools.
    """
    from ..swarm.seed_generator import (
        BIAS_MODES,
        DEFAULT_ENTITIES,
        HORIZONS,
        valid_lenses_for_entity,
        entity_asset_class,
    )

    coverage_manifest = coverage_manifest or build_coverage_gap_manifest(
        cycle_id=cycle_id,
        conn=conn,
        max_gap_examples=max(max_ranked_gaps, max_seed_cells, 256),
    )
    universe = build_market_universe(default_entities=DEFAULT_ENTITIES)
    covered_keys, stale_keys = _coverage_key_sets(conn)
    ranked: list[RankedMarketGap] = []

    for entity in universe.entity_symbols():
        normalized_entity = str(entity or "").upper()
        asset_class = entity_asset_class(normalized_entity)
        for horizon in HORIZONS:
            for lens in valid_lenses_for_entity(normalized_entity):
                for bias_mode in BIAS_MODES:
                    key = (normalized_entity, horizon, lens, bias_mode)
                    if key in covered_keys and key not in stale_keys:
                        continue
                    status = "stale" if key in stale_keys else "missing"
                    ranked.append(_rank_gap(
                        entity=normalized_entity,
                        horizon=horizon,
                        lens=lens,
                        bias_mode=bias_mode,
                        asset_class=asset_class,
                        status=status,
                    ))

    ranked.sort(key=lambda gap: gap.priority_score, reverse=True)
    ranked = ranked[: max(1, int(max_ranked_gaps))]
    geometry_context = _geometry_context(
        cycle_id=geometry_cycle_id or _latest_geometry_cycle(exclude_cycle_id=cycle_id, conn=conn) or cycle_id,
        conn=conn,
        geometry_weights=geometry_weights,
        routing_thresholds=routing_thresholds,
    )
    seed_slot_count = max(0, int(max_seed_cells))
    llm_seed_reserve = _llm_seed_reserve(seed_slot_count, enabled=use_llm, share=llm_seed_share)
    deterministic_seed_gaps = ranked[: max(0, seed_slot_count - llm_seed_reserve)]
    budget_lanes = _budget_lanes(ranked, scout_budget=max(0, int(scout_budget)))
    context_packet = _context_packet(
        cycle_id=cycle_id,
        coverage_manifest=coverage_manifest,
        universe=universe.to_dict(),
        ranked_gaps=ranked,
        budget_lanes=budget_lanes,
        scout_budget=scout_budget,
        geometry_context=geometry_context,
    )
    llm_prompt = _llm_governor_prompt(context_packet)
    llm_response: dict[str, Any] | None = None
    llm_seed_cells: list[dict[str, Any]] = []
    llm_rejections: list[dict[str, Any]] = []
    quality_flags: list[str] = []
    if use_llm:
        try:
            llm_response = _run_llm_governor_sync(
                model=model,
                prompt=llm_prompt,
            )
            parsed = llm_response.get("parsed") if isinstance(llm_response, dict) else None
            if isinstance(parsed, dict):
                llm_seed_cells, llm_rejections = _promote_llm_seed_cells(
                    parsed=parsed,
                    cycle_id=cycle_id,
                    ranked=ranked,
                    universe=universe.to_dict(),
                    covered_keys=covered_keys,
                    stale_keys=stale_keys,
                    max_items=llm_seed_reserve,
                )
        except Exception as exc:
            quality_flags.append(f"llm_governor_failed:{type(exc).__name__}")

    suggested_seed_cells = [
        _seed_cell_from_gap(gap, cycle_id=cycle_id, idx=idx)
        for idx, gap in enumerate(deterministic_seed_gaps)
    ]
    used_cells = {
        (
            str(seed.get("entity") or "").upper(),
            str(seed.get("horizon") or ""),
            str(seed.get("lens") or ""),
            str(seed.get("bias_mode") or ""),
        )
        for seed in suggested_seed_cells
    }
    for seed in llm_seed_cells:
        key = (
            str(seed.get("entity") or "").upper(),
            str(seed.get("horizon") or ""),
            str(seed.get("lens") or ""),
            str(seed.get("bias_mode") or ""),
        )
        if key in used_cells:
            continue
        suggested_seed_cells.append(seed)
        used_cells.add(key)
        if len(suggested_seed_cells) >= seed_slot_count:
            break
    if len(suggested_seed_cells) < seed_slot_count:
        for gap in ranked[len(deterministic_seed_gaps):]:
            key = (gap.entity, gap.horizon, gap.lens, gap.bias_mode)
            if key in used_cells:
                continue
            suggested_seed_cells.append(
                _seed_cell_from_gap(gap, cycle_id=cycle_id, idx=len(suggested_seed_cells))
            )
            used_cells.add(key)
            if len(suggested_seed_cells) >= seed_slot_count:
                break

    completion = coverage_manifest.get("coverage") if isinstance(coverage_manifest.get("coverage"), dict) else {}
    coverage_ratio = _float(completion.get("coverage_ratio"), default=0.0)
    return {
        "schema_version": GOVERNOR_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cycle_id": cycle_id,
        "mode": "deterministic_plus_llm" if use_llm else "deterministic",
        "status": "ready" if ranked else "no_gaps",
        "full_market_definition": {
            "boundary": "MarketUniverseManifest x valid entity/horizon/lens/bias lattice x coverage_cells ledger",
            "honest_claim": (
                "The governor only knows the market inside the explicit universe manifest. "
                "New venues, instruments, and source families enter by promoted universe/tool proposals."
            ),
            "entity_count": len(universe.entities),
            "source_quality": universe.source_quality,
            "source_counts": universe.source_counts,
            "valid_cell_count": (coverage_manifest.get("grid") or {}).get("valid_cell_count"),
        },
        "coverage_state": completion,
        "completion_pressure": _completion_pressure(coverage_ratio),
        "ranked_gaps": [asdict(gap) for gap in ranked],
        "suggested_seed_cells": suggested_seed_cells,
        "budget_lanes": [asdict(lane) for lane in budget_lanes],
        "source_surface_priorities": _source_surface_priorities(ranked),
        "alpha_geometry_context": geometry_context,
        "context_packet": context_packet,
        "llm_governor": {
            "enabled": use_llm,
            "model": model,
            "prompt": llm_prompt,
            "response": llm_response,
            "promoted_seed_count": len(llm_seed_cells),
            "rejections": llm_rejections,
            "promotion_gate": (
                "LLM proposals must name an existing or newly proposed universe/source surface, "
                "map to a valid cell, include eval gates, and survive deterministic dedupe."
            ),
        },
        "quality_flags": quality_flags,
    }


def render_market_map_governor_markdown(plan: dict[str, Any]) -> str:
    """Render a compact audit document for the governor output."""
    full = plan.get("full_market_definition") if isinstance(plan.get("full_market_definition"), dict) else {}
    coverage = plan.get("coverage_state") if isinstance(plan.get("coverage_state"), dict) else {}
    lines = [
        "# Market Map Governor",
        "",
        f"- schema: `{plan.get('schema_version')}`",
        f"- status: `{plan.get('status')}`",
        f"- mode: `{plan.get('mode')}`",
        f"- cycle: `{plan.get('cycle_id')}`",
        f"- boundary: {full.get('boundary')}",
        f"- entities: `{full.get('entity_count')}`",
        f"- valid_cells: `{full.get('valid_cell_count')}`",
        f"- covered: `{coverage.get('covered_count')}`",
        f"- missing: `{coverage.get('missing_count')}`",
        f"- pressure: `{plan.get('completion_pressure')}`",
        "",
        "## Budget Lanes",
        "",
    ]
    for lane in plan.get("budget_lanes") or []:
        if not isinstance(lane, dict):
            continue
        lines.extend([
            f"### {lane.get('lane_id')}",
            f"- scouts: `{lane.get('scout_count')}`",
            f"- focus: `{lane.get('entity_focus')} / {lane.get('horizon_focus')} / {lane.get('lens_focus')}`",
            f"- reason: {lane.get('reason')}",
            "",
        ])
    lines.extend(["## Top Gaps", ""])
    for gap in (plan.get("ranked_gaps") or [])[:12]:
        if not isinstance(gap, dict):
            continue
        lines.extend([
            f"- `{gap.get('gap_id')}` score `{gap.get('priority_score')}`: "
            f"{gap.get('entity')} / {gap.get('horizon')} / {gap.get('lens')} / {gap.get('bias_mode')} "
            f"({gap.get('reason')})",
        ])
    llm = plan.get("llm_governor") if isinstance(plan.get("llm_governor"), dict) else {}
    response = llm.get("response") if isinstance(llm.get("response"), dict) else {}
    parsed = response.get("parsed") if isinstance(response.get("parsed"), dict) else {}
    assessment = parsed.get("market_state_assessment") if isinstance(parsed.get("market_state_assessment"), dict) else {}
    lines.extend([
        "",
        "## Frontier LLM Brain",
        "",
        f"- enabled: `{llm.get('enabled')}`",
        f"- model: `{llm.get('model')}`",
        f"- promoted_seed_count: `{llm.get('promoted_seed_count')}`",
        f"- coverage_posture: {assessment.get('coverage_posture')}",
        f"- frontier_thesis: {assessment.get('frontier_thesis')}",
        "",
    ])
    lines.extend([
        "",
        "## LLM Governor Prompt",
        "",
        "```text",
        str((plan.get("llm_governor") or {}).get("prompt") or ""),
        "```",
        "",
    ])
    return "\n".join(lines)


def _rank_gap(
    *,
    entity: str,
    horizon: str,
    lens: str,
    bias_mode: str,
    asset_class: str,
    status: str,
) -> RankedMarketGap:
    surfaces = _surfaces_for_lens(lens=lens, asset_class=asset_class)
    surface_keys = tuple(surface.key for surface in surfaces)
    tools = tuple(dict.fromkeys(
        tool
        for surface in surfaces
        for tool in surface.example_tools
    ))[:8]
    edges = tuple(dict.fromkeys(
        edge
        for surface in surfaces
        for edge in surface.edge_types
    ))[:8]
    score = (
        0.30
        + _asset_score(asset_class)
        + _horizon_score(horizon)
        + _lens_score(lens)
        + _bias_score(bias_mode)
        + (0.07 if status == "stale" else 0.04)
        + (0.04 if entity in {"HYPE", "BTC", "ETH", "SOL"} else 0.0)
    )
    score = round(min(1.0, score), 4)
    reason_bits = [
        f"{status} coverage",
        f"{asset_class} market",
        f"{horizon} horizon",
        f"{lens} lens",
    ]
    if surface_keys:
        reason_bits.append("needs " + ", ".join(surface_keys[:3]))
    return RankedMarketGap(
        gap_id=f"gap_{_short_hash(entity, horizon, lens, bias_mode, status)}",
        entity=entity,
        horizon=horizon,
        lens=lens,
        bias_mode=bias_mode,
        asset_class=asset_class,
        status=status,
        priority_score=score,
        priority_band="high" if score >= 0.78 else "medium" if score >= 0.62 else "low",
        reason="; ".join(reason_bits),
        missing_surfaces=surface_keys,
        suggested_tools=tools,
        expected_edges=edges,
        seed_payload={
            "source": "market_map_governor",
            "repair_status": status,
            "missing_surfaces": list(surface_keys),
            "expected_edges": list(edges),
            "why_this_seed_exists": "Ranked by market-map governor from universe coverage gaps.",
        },
    )


def _surfaces_for_lens(*, lens: str, asset_class: str) -> tuple[DataSurface, ...]:
    by_key = {surface.key: surface for surface in DATA_SURFACES}
    keys_by_lens: dict[str, tuple[str, ...]] = {
        "on_chain": ("wallet_route_state", "our_hl_node", "hydromancer_actor_graph", "builder_flow", "mempool_pending_intent"),
        "smart_money": ("hydromancer_actor_graph", "builder_flow", "wallet_route_state", "our_hl_node"),
        "microstructure": ("market_state", "options_vol_derivatives", "order_flow_sim", "builder_flow"),
        "options_flow": ("options_vol_derivatives", "market_state", "source_health_citations"),
        "vol_surface": ("options_vol_derivatives", "market_state", "order_flow_sim"),
        "catalyst": ("event_feed", "filings_news_social", "parallel_web_attention", "prediction_markets"),
        "filing": ("equity_fundamental_filings", "filings_news_social", "source_health_citations"),
        "polymarket": ("prediction_markets", "parallel_web_attention", "filings_news_social"),
        "sentiment": ("parallel_web_attention", "filings_news_social", "prediction_markets"),
        "macro": ("macro_official", "market_state", "parallel_web_attention"),
        "money_velocity": ("macro_official", "market_state", "options_vol_derivatives"),
        "rotation": ("market_state", "macro_official", "options_vol_derivatives"),
        "factor": ("equity_fundamental_filings", "market_state", "macro_official"),
        "anomaly": ("market_state", "parallel_web_attention", "source_health_citations", "celestial_cycles"),
        "structural": ("macro_official", "regulatory_innovation_gov", "real_economy_alt", "parallel_web_attention"),
    }
    keys = list(keys_by_lens.get(lens, ("market_state", "source_health_citations")))
    if asset_class == "hyperliquid_perp":
        keys = [
            "market_state",
            "our_hl_node",
            "hydromancer_actor_graph",
            "builder_flow",
            *keys,
        ]
    return tuple(
        by_key[key]
        for key in dict.fromkeys(keys)
        if key in by_key
    )


def _budget_lanes(ranked: list[RankedMarketGap], *, scout_budget: int) -> list[ScoutBudgetLane]:
    if scout_budget <= 0 or not ranked:
        return []
    lane_counts: dict[tuple[str, str, str], list[RankedMarketGap]] = {}
    for gap in ranked:
        key = (gap.asset_class, gap.horizon, gap.lens)
        lane_counts.setdefault(key, []).append(gap)
    ordered = sorted(
        lane_counts.items(),
        key=lambda kv: (max(g.priority_score for g in kv[1]), len(kv[1])),
        reverse=True,
    )
    top = ordered[:8]
    weights = [max(0.05, sum(g.priority_score for g in gaps[:8])) for _, gaps in top]
    total_weight = sum(weights) or 1.0
    lanes: list[ScoutBudgetLane] = []
    assigned = 0
    for idx, ((asset_class, horizon, lens), gaps) in enumerate(top):
        count = int(round(scout_budget * (weights[idx] / total_weight)))
        if idx == len(top) - 1:
            count = max(0, scout_budget - assigned)
        assigned += count
        example_ids = tuple(g.gap_id for g in gaps[:5])
        lanes.append(ScoutBudgetLane(
            lane_id=f"lane_{_short_hash(asset_class, horizon, lens)}",
            scout_count=count,
            entity_focus=asset_class,
            horizon_focus=horizon,
            lens_focus=lens,
            reason=f"Top unresolved {asset_class} gaps concentrate in {horizon}/{lens}.",
            example_gap_ids=example_ids,
        ))
    return lanes


def _source_surface_priorities(ranked: list[RankedMarketGap]) -> list[dict[str, Any]]:
    scores: dict[str, float] = {}
    examples: dict[str, list[str]] = {}
    for gap in ranked:
        for surface in gap.missing_surfaces:
            scores[surface] = scores.get(surface, 0.0) + gap.priority_score
            examples.setdefault(surface, []).append(gap.gap_id)
    by_key = {surface.key: surface for surface in DATA_SURFACES}
    out: list[dict[str, Any]] = []
    for key, score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:12]:
        surface = by_key.get(key)
        out.append({
            "surface_key": key,
            "title": surface.title if surface else key,
            "source_family": surface.source_family if surface else "",
            "status": surface.status if surface else "",
            "priority_score": round(score, 4),
            "example_gap_ids": examples.get(key, [])[:8],
            "example_tools": list(surface.example_tools if surface else ()),
            "promotion_gate": (
                "Must produce typed receipts with source timestamps, raw artifact refs, "
                "and evidence_ref IDs that information strings can cite."
            ),
        })
    return out


def _geometry_context(
    *,
    cycle_id: str,
    conn: sqlite3.Connection,
    geometry_weights: Optional[dict[str, Any]],
    routing_thresholds: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Return the map-shape context the frontier cortex sees.

    The cortex is allowed to reason over this geometry, but the deterministic
    alpha-geometry functions still own the coordinates and route directives.
    """
    try:
        from ..information_map.alpha_geometry import (
            compute_alpha_geometry,
            load_alpha_geometry,
            plan_alpha_geometry_actions,
        )
    except Exception as exc:
        return {
            "status": "unavailable",
            "cycle_id": cycle_id,
            "action_plan": _empty_geometry_action_plan(),
            "quality_flags": [f"alpha_geometry_import_failed:{type(exc).__name__}"],
        }

    rows: list[dict[str, Any]] = []
    source = "persisted"
    directives: list[dict[str, Any]] = []
    try:
        rows = load_alpha_geometry(cycle_id=cycle_id, limit=48, conn=conn)
    except Exception:
        rows = []
    if not rows:
        try:
            snapshot = compute_alpha_geometry(
                cycle_id=cycle_id,
                limit=2000,
                persist=False,
                geometry_weights=geometry_weights,
                routing_thresholds=routing_thresholds,
                conn=conn,
            )
            source = "computed_ephemeral"
            rows = [
                {
                    "cell_key": cell.cell_key,
                    "entity": cell.entity,
                    "horizon": cell.horizon,
                    "lens": cell.lens,
                    "theme": cell.theme,
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
                for cell in snapshot.cells[:48]
            ]
            global_metrics = snapshot.global_metrics
            quality_flags = snapshot.quality_flags
            directives = [
                {
                    "cell_key": row.get("cell_key"),
                    "entity": row.get("entity"),
                    "theme": row.get("theme"),
                    "horizon": row.get("horizon"),
                    "lens": row.get("lens"),
                    "route_directive": row.get("route_directive"),
                    "trade_scream_score": row.get("trade_scream_score"),
                    "verifier_readiness": row.get("verifier_readiness"),
                    "why": (
                        f"{row.get('route_directive')}: scream={_float(row.get('trade_scream_score'), default=0.0):.2f}, "
                        f"ready={_float(row.get('verifier_readiness'), default=0.0):.2f}."
                    ),
                    "metrics": row.get("metrics") or {},
                    "quality_flags": row.get("quality_flags") or [],
                }
                for row in rows
                if str(row.get("route_directive") or "observe") != "observe"
            ][:12]
        except Exception as exc:
            return {
                "status": "unavailable",
                "cycle_id": cycle_id,
                "action_plan": _empty_geometry_action_plan(),
                "quality_flags": [f"alpha_geometry_context_failed:{type(exc).__name__}"],
            }
    else:
        directives = [
            {
                "cell_key": row.get("cell_key"),
                "entity": row.get("entity"),
                "theme": row.get("theme"),
                "horizon": row.get("horizon"),
                "lens": row.get("lens"),
                "route_directive": row.get("route_directive"),
                "trade_scream_score": row.get("trade_scream_score"),
                "verifier_readiness": row.get("verifier_readiness"),
                "why": (
                    f"{row.get('route_directive')}: scream={_float(row.get('trade_scream_score'), default=0.0):.2f}, "
                    f"ready={_float(row.get('verifier_readiness'), default=0.0):.2f}."
                ),
                "metrics": row.get("metrics") or {},
                "quality_flags": row.get("quality_flags") or [],
            }
            for row in rows
            if str(row.get("route_directive") or "observe") != "observe"
        ][:12]
        global_metrics = {
            "cell_count": float(len(rows)),
            "avg_trade_scream_score": round(_avg_float(row.get("trade_scream_score") for row in rows), 4),
            "avg_verifier_readiness": round(_avg_float(row.get("verifier_readiness") for row in rows), 4),
            "frontier_trade_candidates": float(sum(
                1 for row in rows
                if "frontier_trade_candidate" in [str(x) for x in (row.get("quality_flags") or [])]
            )),
        }
        quality_flags = ["alpha_geometry_context_from_persisted_rows"]
    try:
        action_plan = plan_alpha_geometry_actions(
            cycle_id=cycle_id,
            limit=64,
            geometry_weights=geometry_weights,
            routing_thresholds=routing_thresholds,
            conn=conn,
        )
    except Exception as exc:
        action_plan = {
            "status": "unavailable",
            "quality_flags": [f"alpha_geometry_action_plan_failed:{type(exc).__name__}"],
            "actions": [],
            "tool_requests": [],
        }

    top_cells = [
        {
            "cell_key": row.get("cell_key"),
            "entity": row.get("entity"),
            "horizon": row.get("horizon"),
            "lens": row.get("lens"),
            "theme": row.get("theme"),
            "route_directive": row.get("route_directive"),
            "trade_scream_score": row.get("trade_scream_score"),
            "verifier_readiness": row.get("verifier_readiness"),
            "coordinates": row.get("coordinates") or {},
            "metrics": row.get("metrics") or {},
            "quality_flags": row.get("quality_flags") or [],
            "source_families": row.get("source_families") or [],
        }
        for row in rows[:16]
    ]
    return {
        "status": "ready" if rows else "empty",
        "cycle_id": cycle_id,
        "source": source,
        "shape_semantics": {
            "x_source_independence": "rightward means more independent source families",
            "y_frontier_pressure": "higher means more novel, less crowded, still supported",
            "z_tension": "height means contradictions worth resolving",
            "color_fragility": "brighter/hotter means evidence is brittle and needs repair",
            "size_support_mass": "larger means more attention/conviction/evidence support",
            "route_directive": "verify_now, repair_sources, resolve_tension, widen_sources, widen_scouts, or observe",
        },
        "global_metrics": global_metrics,
        "top_cells": top_cells,
        "route_directives": directives[:12],
        "action_plan": {
            "status": action_plan.get("status"),
            "global_shape": action_plan.get("global_shape"),
            "actions": (action_plan.get("actions") or [])[:12],
            "tool_requests": (action_plan.get("tool_requests") or [])[:12],
            "cortex_prompt_hint": action_plan.get("cortex_prompt_hint"),
        },
        "quality_flags": quality_flags,
    }


def _avg_float(values: Any) -> float:
    nums: list[float] = []
    for value in values:
        nums.append(_float(value, default=0.0))
    return sum(nums) / max(1, len(nums))


def _empty_geometry_action_plan() -> dict[str, Any]:
    return {
        "status": "unavailable",
        "global_shape": {},
        "actions": [],
        "tool_requests": [],
        "cortex_prompt_hint": "Alpha geometry is unavailable; use coverage gaps and source surfaces until strings exist.",
    }


def _context_packet(
    *,
    cycle_id: str,
    coverage_manifest: dict[str, Any],
    universe: dict[str, Any],
    ranked_gaps: list[RankedMarketGap],
    budget_lanes: list[ScoutBudgetLane],
    scout_budget: int,
    geometry_context: dict[str, Any],
) -> dict[str, Any]:
    coverage = coverage_manifest.get("coverage") if isinstance(coverage_manifest.get("coverage"), dict) else {}
    grid = coverage_manifest.get("grid") if isinstance(coverage_manifest.get("grid"), dict) else {}
    entities = [row for row in (universe.get("entities") or []) if isinstance(row, dict)]
    hyperliquid_entities = [
        row for row in entities
        if str(row.get("venue") or "").lower() == "hyperliquid"
    ]
    return {
        "cycle_id": cycle_id,
        "scout_budget": scout_budget,
        "frontier_brain_task": {
            "role": "Use judgment to push the deterministic coverage map toward a fuller market state.",
            "allowed_actions": [
                "promote valid missing or stale cells to scout seeds",
                "use alpha-geometry shape to decide whether to verify, repair, resolve, or widen cells",
                "request new source/tool surfaces with eval gates",
                "propose universe expansions that can be verified by source adapters",
                "identify strategic blind zones the lattice is underweighting",
            ],
            "hard_boundary": (
                "The LLM may reprioritize and propose expansion, but cannot mark coverage complete "
                "or create tradeable entities without source evidence."
            ),
        },
        "known_market_boundary": {
            "entity_count": len(universe.get("entities") or []),
            "source_quality": universe.get("source_quality"),
            "source_counts": universe.get("source_counts"),
            "valid_cell_count": grid.get("valid_cell_count"),
            "coverage_ratio": coverage.get("coverage_ratio"),
            "hyperliquid_tradeable_count": len(hyperliquid_entities),
            "hyperliquid_sample": [
                {
                    "symbol": row.get("symbol"),
                    "asset_id": row.get("asset_id"),
                    "max_leverage": row.get("max_leverage"),
                    "is_hip3": row.get("is_hip3"),
                    "dex": (row.get("payload") or {}).get("dex") if isinstance(row.get("payload"), dict) else None,
                    "source_quality": row.get("source_quality"),
                }
                for row in hyperliquid_entities[:32]
            ],
        },
        "coverage_state": coverage,
        "alpha_geometry_state": geometry_context,
        "ranked_gap_count": len(ranked_gaps),
        "top_gaps": [
            {
                "gap_id": gap.gap_id,
                "cell": {
                    "entity": gap.entity,
                    "horizon": gap.horizon,
                    "lens": gap.lens,
                    "bias_mode": gap.bias_mode,
                },
                "score": gap.priority_score,
                "missing_surfaces": list(gap.missing_surfaces),
                "expected_edges": list(gap.expected_edges),
            }
            for gap in ranked_gaps[:16]
        ],
        "budget_lanes": [asdict(lane) for lane in budget_lanes],
        "source_surface_count": len(DATA_SURFACES),
        "source_surface_status_counts": _surface_status_counts(),
        "source_surfaces": [
            {
                "key": surface.key,
                "title": surface.title,
                "role": surface.role,
                "question": surface.question,
                "source_family": surface.source_family,
                "status": surface.status,
                "example_tools": list(surface.example_tools),
                "edge_types": list(surface.edge_types),
            }
            for surface in DATA_SURFACES
        ],
        "source_surface_priorities": _source_surface_priorities(ranked_gaps),
    }


def _llm_governor_prompt(context_packet: dict[str, Any]) -> str:
    return (
        "You are the frontier LLM brain for the Talis market map. The deterministic "
        "governor gives you a market-universe manifest, coverage ledger, ranked gaps, "
        "alpha-geometry coordinates, source surfaces, and scout budget lanes. Your job "
        "is to push the desk toward the fullest useful market state: not only broad "
        "coverage, but edge-of-map source repair, node/Hydromancer/mempool intelligence, "
        "and tool requests that increase future sensing power.\n\n"
        "Do not invent coverage. Do not claim the market is complete unless coverage "
        "evidence proves it. You are allowed to be creative only in what to inspect "
        "next, which missing surfaces to build, and which strategic blind zones the "
        "deterministic lattice is underweighting.\n\n"
        "Return JSON:\n"
        "{\n"
        '  "market_state_assessment": {"coverage_posture": "...", "frontier_thesis": "...", '
        '"highest_value_blind_zones": ["..."], "why_this_is_not_complete": "..."},\n'
        '  "routing_adjustments": [\n'
        '    {"cell": {"entity": "...", "horizon": "...", "lens": "...", "bias_mode": "..."}, '
        '"reason": "...", "scout_count": 1, "required_surfaces": ["..."], '
        '"expected_edges": ["..."], "stop_condition": "...", '
        '"allow_deepen_covered_cell": false}\n'
        "  ],\n"
        '  "universe_expansions": [\n'
        '    {"entity_or_surface": "...", "source": "...", "why_missing": "...", '
        '"promotion_gate": "...", "would_expand_axis": "entity|surface|lens|venue|time"}\n'
        "  ],\n"
        '  "tool_requests": [\n'
        '    {"surface": "...", "tool_name": "...", "expected_edge": "...", '
        '"eval_gate": "...", "first_fixture": "...", "stop_condition": "..."}\n'
        "  ],\n"
        '  "strategic_questions": [\n'
        '    {"question": "...", "why_now": "...", "owner": "seed_router|tool_builder|source_integrity|verifier"}\n'
        "  ]\n"
        "}\n\n"
        "Evaluation rules:\n"
        "- Prefer tradeable Hyperliquid perps, fresh event horizons, node/Hydromancer/source-health gaps, and high edge-of-map uncertainty.\n"
        "- Use alpha_geometry_state: verify high trade_scream + readiness, repair high fragility, resolve high tension, widen high frontier with low source independence.\n"
        "- Use alpha_geometry_state.action_plan as the first candidate agenda. If you disagree, explain which shape coordinate makes the deterministic action insufficient.\n"
        "- Use LLM judgment to widen or repair the deterministic plan, not to replace the coverage ledger.\n"
        "- Every proposed scout or tool must name the edge it would add to the information map.\n\n"
        "context_packet:\n"
        f"{json.dumps(context_packet, indent=2, sort_keys=True, ensure_ascii=True, default=str)}"
    )


def _run_llm_governor_sync(*, model: str, prompt: str) -> dict[str, Any]:
    system = (
        "You are a strict JSON market-map routing planner. Return only the JSON object requested."
    )

    async def _go() -> dict[str, Any]:
        models_mod = sys.modules.get("tic.desk.models")
        if models_mod is not None and hasattr(models_mod, "chat"):
            _chat = getattr(models_mod, "chat")
        else:
            ensure_tic_on_path()
            from tic.desk.models import chat as _chat  # type: ignore

        res = await _chat(
            model,
            system,
            prompt,
            max_tokens=5000,
            fallback="anthropic:claude-sonnet-4-6",
        )
        text = str(res.get("text") or "")
        parsed = _extract_first_json(text)
        if not isinstance(parsed, dict):
            raise RuntimeError("market_map_governor_json_unparseable")
        return {
            "model_used": res.get("model_used") or model,
            "provider": res.get("provider") or "?",
            "parsed": parsed,
        }

    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(asyncio.run, _go()).result(timeout=45)
    except RuntimeError:
        return asyncio.run(_go())


def _coverage_key_sets(conn: sqlite3.Connection) -> tuple[set[tuple[str, str, str, str]], set[tuple[str, str, str, str]]]:
    try:
        rows = conn.execute(
            """
            SELECT entity, horizon, lens, bias_mode, last_sampled_at
            FROM coverage_cells
            WHERE transaction_to IS NULL
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return set(), set()
    covered: set[tuple[str, str, str, str]] = set()
    stale: set[tuple[str, str, str, str]] = set()
    now = datetime.now(timezone.utc)
    for row in rows:
        item = _row_to_dict(row)
        key = (
            str(item.get("entity") or "").upper(),
            str(item.get("horizon") or ""),
            str(item.get("lens") or ""),
            str(item.get("bias_mode") or ""),
        )
        if not all(key):
            continue
        covered.add(key)
        sampled = _parse_time(item.get("last_sampled_at"))
        if sampled is None or (now - sampled).total_seconds() > 72 * 3600:
            stale.add(key)
    return covered, stale


def _latest_geometry_cycle(
    *,
    exclude_cycle_id: str,
    conn: sqlite3.Connection,
) -> Optional[str]:
    try:
        row = conn.execute(
            """
            SELECT cycle_id, MAX(created_at) AS last_created_at
            FROM information_geometry_snapshots
            WHERE transaction_to IS NULL
              AND cycle_id != ?
            GROUP BY cycle_id
            ORDER BY last_created_at DESC
            LIMIT 1
            """,
            (exclude_cycle_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return str(row["cycle_id"] if hasattr(row, "keys") else row[0])


def _seed_cell_from_gap(gap: RankedMarketGap, *, cycle_id: str, idx: int) -> dict[str, Any]:
    return {
        "seed_id": f"seed_{cycle_id}_M_{idx:04d}_{gap.gap_id[-8:]}",
        "entity": gap.entity,
        "horizon": gap.horizon,
        "lens": gap.lens,
        "bias_mode": gap.bias_mode,
        "theme": "market_map_gap_repair",
        "weight": gap.priority_score,
        "frontier_boost": 1.0 + gap.priority_score,
        "coverage_penalty": 1.0,
        "payload": {
            **gap.seed_payload,
            "gap_id": gap.gap_id,
            "suggested_tools": list(gap.suggested_tools),
        },
    }


def _llm_seed_reserve(seed_slot_count: int, *, enabled: bool, share: float = 0.25) -> int:
    if not enabled or seed_slot_count <= 0:
        return 0
    try:
        bounded_share = max(0.0, min(0.50, float(share)))
    except Exception:
        bounded_share = 0.25
    return max(1, min(12, int(round(seed_slot_count * bounded_share))))


def _promote_llm_seed_cells(
    *,
    parsed: dict[str, Any],
    cycle_id: str,
    ranked: list[RankedMarketGap],
    universe: dict[str, Any],
    covered_keys: set[tuple[str, str, str, str]],
    stale_keys: set[tuple[str, str, str, str]],
    max_items: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Promote LLM routing proposals into bounded, replayable seed cells.

    The model can suggest a strategic route, but this gate keeps the seed space
    anchored to the known market manifest and the typed source surface catalog.
    """
    if max_items <= 0:
        return [], []
    from ..swarm.seed_generator import BIAS_MODES, HORIZONS, entity_asset_class, valid_lenses_for_entity

    raw_items = _list_of_dicts(parsed.get("routing_adjustments"))
    known_entities = {
        str(symbol or "").upper()
        for symbol in (universe.get("seed_eligible_symbols") or [])
        if str(symbol or "").strip()
    }
    if not known_entities:
        known_entities = {
            str(row.get("symbol") or "").upper()
            for row in (universe.get("entities") or [])
            if isinstance(row, dict) and row.get("seed_eligible", True)
        }
    surface_by_key = {surface.key: surface for surface in DATA_SURFACES}
    ranked_by_cell = {
        (gap.entity, gap.horizon, gap.lens, gap.bias_mode): gap
        for gap in ranked
    }
    out: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for raw in raw_items:
        cell = raw.get("cell") if isinstance(raw.get("cell"), dict) else {}
        entity = str(cell.get("entity") or raw.get("entity") or "").upper().strip()
        horizon = str(cell.get("horizon") or raw.get("horizon") or "").strip()
        lens = str(cell.get("lens") or raw.get("lens") or "").strip()
        bias_mode = str(cell.get("bias_mode") or raw.get("bias_mode") or "frontier").strip()
        reason = str(raw.get("reason") or raw.get("strategic_reason") or "").strip()
        stop_condition = str(raw.get("stop_condition") or "").strip()
        key = (entity, horizon, lens, bias_mode)
        reject_reason = ""
        if not entity or entity not in known_entities:
            reject_reason = "entity_not_in_market_universe"
        elif horizon not in HORIZONS:
            reject_reason = "invalid_horizon"
        elif lens not in valid_lenses_for_entity(entity):
            reject_reason = "invalid_lens_for_entity"
        elif bias_mode not in BIAS_MODES:
            reject_reason = "invalid_bias_mode"
        elif key in seen:
            reject_reason = "duplicate_llm_cell"
        elif key in covered_keys and key not in stale_keys and not bool(raw.get("allow_deepen_covered_cell")):
            reject_reason = "fresh_cell_already_covered"
        elif not reason:
            reject_reason = "missing_reason"
        elif not stop_condition:
            reject_reason = "missing_stop_condition"
        if reject_reason:
            rejections.append({
                "reason": reject_reason,
                "raw": _safe_compact(raw),
            })
            continue

        gap = ranked_by_cell.get(key)
        requested_surfaces = [
            key for key in _string_list(raw.get("required_surfaces") or raw.get("missing_surfaces"))
            if key in surface_by_key
        ]
        if not requested_surfaces:
            asset_class = gap.asset_class if gap else entity_asset_class(entity)
            requested_surfaces = [surface.key for surface in _surfaces_for_lens(
                lens=lens,
                asset_class=asset_class,
            )][:5]
        expected_edges = [
            edge for edge in _string_list(raw.get("expected_edges"))
            if edge
        ] or _edges_for_surfaces(requested_surfaces)[:6]
        suggested_tools = [
            tool for tool in _string_list(raw.get("suggested_tools"))
            if tool
        ] or _tools_for_surfaces(requested_surfaces)[:8]
        try:
            scout_count = max(1, min(24, int(raw.get("scout_count") or 1)))
        except Exception:
            scout_count = 1
        priority_score = min(1.0, max(0.62, (gap.priority_score if gap else 0.74) + 0.04))
        out.append({
            "seed_id": f"seed_{cycle_id}_L_{len(out):04d}_{_short_hash(*key)}",
            "entity": entity,
            "horizon": horizon,
            "lens": lens,
            "bias_mode": bias_mode,
            "theme": "frontier_llm_market_state",
            "weight": round(priority_score, 4),
            "frontier_boost": round(1.0 + priority_score, 4),
            "coverage_penalty": 1.0,
            "payload": {
                "source": "frontier_llm_governor",
                "gap_id": gap.gap_id if gap else f"gap_llm_{_short_hash(*key)}",
                "repair_status": "stale" if key in stale_keys else "missing",
                "missing_surfaces": requested_surfaces,
                "expected_edges": expected_edges,
                "suggested_tools": suggested_tools,
                "frontier_llm_reason": reason[:800],
                "frontier_llm_stop_condition": stop_condition[:500],
                "frontier_llm_requested_scout_count": scout_count,
                "frontier_llm_market_state_assessment": parsed.get("market_state_assessment")
                if isinstance(parsed.get("market_state_assessment"), dict) else {},
                "why_this_seed_exists": "Promoted by frontier LLM governor and accepted by deterministic market-map gates.",
            },
        })
        seen.add(key)
        if len(out) >= max_items:
            break
    return out, rejections[:16]


def _edges_for_surfaces(surface_keys: list[str]) -> list[str]:
    by_key = {surface.key: surface for surface in DATA_SURFACES}
    edges: list[str] = []
    for key in surface_keys:
        surface = by_key.get(key)
        if not surface:
            continue
        for edge in surface.edge_types:
            if edge not in edges:
                edges.append(edge)
    return edges


def _tools_for_surfaces(surface_keys: list[str]) -> list[str]:
    by_key = {surface.key: surface for surface in DATA_SURFACES}
    tools: list[str] = []
    for key in surface_keys:
        surface = by_key.get(key)
        if not surface:
            continue
        for tool in surface.example_tools:
            if tool not in tools:
                tools.append(tool)
    return tools


def _string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def _list_of_dicts(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict)]


def _safe_compact(raw: Any) -> Any:
    try:
        text = json.dumps(raw, ensure_ascii=True, sort_keys=True, default=str)
    except Exception:
        return str(raw)[:500]
    if len(text) <= 500:
        return raw
    return text[:500] + "..."


def _asset_score(asset_class: str) -> float:
    return {
        "hyperliquid_perp": 0.20,
        "crypto": 0.16,
        "equity": 0.10,
        "index_macro": 0.09,
    }.get(asset_class, 0.08)


def _horizon_score(horizon: str) -> float:
    return {
        "intraday": 0.17,
        "1d": 0.15,
        "1w": 0.11,
        "1m": 0.08,
        "1q": 0.06,
        "structural": 0.05,
    }.get(horizon, 0.04)


def _lens_score(lens: str) -> float:
    return {
        "on_chain": 0.17,
        "microstructure": 0.16,
        "smart_money": 0.16,
        "anomaly": 0.14,
        "catalyst": 0.13,
        "options_flow": 0.12,
        "vol_surface": 0.11,
        "polymarket": 0.10,
        "money_velocity": 0.09,
        "macro": 0.08,
        "rotation": 0.08,
        "sentiment": 0.08,
        "structural": 0.07,
        "factor": 0.06,
        "filing": 0.05,
    }.get(lens, 0.05)


def _bias_score(bias_mode: str) -> float:
    return {
        "frontier": 0.09,
        "tail_risk": 0.08,
        "contrarian": 0.06,
        "momentum": 0.05,
        "mean_reversion": 0.04,
        "consensus_confirm": 0.03,
    }.get(bias_mode, 0.03)


def _completion_pressure(coverage_ratio: float) -> str:
    if coverage_ratio < 0.01:
        return "bootstrap_unmapped_market"
    if coverage_ratio < 0.25:
        return "coverage_expansion"
    if coverage_ratio < 0.70:
        return "frontier_and_stale_gap_repair"
    if coverage_ratio < 0.95:
        return "freshness_and_edge_completion"
    return "near_complete_watch_for_regime_drift"


def _surface_status_counts() -> dict[str, int]:
    out: dict[str, int] = {}
    for surface in DATA_SURFACES:
        out[surface.status] = out.get(surface.status, 0) + 1
    return out


def _row_to_dict(row: Any) -> dict[str, Any]:
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(row)


def _parse_time(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _float(raw: Any, *, default: float) -> float:
    try:
        return float(raw)
    except Exception:
        return default


def _short_hash(*parts: str) -> str:
    import hashlib

    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:10]


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


__all__ = [
    "GOVERNOR_SCHEMA_VERSION",
    "FRONTIER_LLM_DEFAULT_MODEL",
    "RankedMarketGap",
    "ScoutBudgetLane",
    "build_market_map_governor_plan",
    "render_market_map_governor_markdown",
]
