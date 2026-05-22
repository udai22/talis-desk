#!/usr/bin/env python
"""Ground-up smoke test for the information-map/node-intelligence stack.

This is intentionally offline and deterministic. It proves the substrate can
ingest a Hyperview-style event, enrich it with Hydromancer + node evidence,
propose missing tools, synthesize promoted strings, and expose the result to
the monitor payload before any large live scout spend.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import types
import asyncio
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from talis_desk.information_map import (
    InformationString,
    MarketEvolveEvaluation,
    alpha_geometry_seed_directives,
    build_alpha_geometry_cortex_review,
    build_evolution_control_payload,
    collect_market_evolve_metrics,
    compute_alpha_geometry,
    evaluate_prompt_scale_gate,
    event_intelligence_from_tool_evidence,
    event_intelligence_to_information_string,
    load_alpha_geometry,
    node_intelligence_from_tool_evidence,
    node_intelligence_to_information_string,
    plan_alpha_geometry_actions,
    persist_event_intelligence,
    persist_information_strings,
    post_evolution_control_work_orders,
    build_system_trace,
    read_prompt_trace_artifacts,
    persist_node_intelligence,
    apply_market_evolve_policy_to_seeds,
    build_market_evolve_lineage,
    load_market_evolve_experiment_results,
    load_market_evolve_experiments,
    load_market_evolve_policy_applications,
    load_market_evolve_programs,
    load_active_market_evolve_program,
    prepare_market_evolve_experiment_seed_pairs,
    propose_market_evolve_mutation,
    recent_information_strings,
    render_system_trace_markdown,
    run_market_evolve_step,
    run_information_synthesis,
    score_event_intelligence,
    score_market_evolve_metrics,
    score_node_intelligence,
)
from talis_desk.information_map.deep_scout_prompt import (
    build_deep_scout_system_prompt,
    score_deep_scout_output,
)
from talis_desk.agent_harness import (
    HarnessPolicy,
    execute_cortex_task_queue,
)
from talis_desk.monitor.server import (
    _build_research_evolution_payload,
    _build_scout_inspector_payload,
    _fetch_scout_rows,
)
from talis_desk.market_map.universe import build_market_universe
from talis_desk.market_map.self_healing import (
    build_market_map_self_healing_plan,
    execute_market_map_self_healing_tasks,
    post_market_map_self_healing_work_orders,
    render_market_map_self_healing_markdown,
)
from talis_desk.market_map.coverage_audit import build_coverage_gap_manifest
from talis_desk.market_map.governor import (
    build_market_map_governor_plan,
    render_market_map_governor_markdown,
)
from talis_desk.schema.migrations import get_schema_version
from talis_desk.store import reset_desk_store_for_test
from talis_desk.swarm.scout_runner import (
    _build_user_prompt,
    _event_bundles_for_scout,
    _infer_tool_args,
    _node_snapshots_for_scout,
    _prompt_variant_for_seed,
    _run_one_scout,
)
from talis_desk.swarm.seed_generator import (
    ALPHA_GEOMETRY_ACTION_TOOL_URI,
    DEFAULT_ENTITIES,
    SeedCell,
    generate_alpha_geometry_route_seeds,
)
from talis_desk.tool_atlas.discovery import (
    AnalysisToolProposal,
    iterate_tool_proposal,
    load_analysis_tool_proposals,
    persist_analysis_tool_proposals,
    propose_tools_from_quality_flags,
)
from talis_desk.tool_atlas import (
    AgentContext,
    dispatch_uri,
    promote_analysis_tool_proposal,
    regenerate_tool_atlas,
)


CYCLE_ID = "cycle_groundup_node_20260521"
ENTITY = "HYPE"


def main() -> int:
    artifact_dir = _artifact_dir()
    prompt_output_dir = artifact_dir / "prompt_outputs"
    prompt_output_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TALIS_LEARNED_TOOLS_DIR"] = str(artifact_dir / "learned_tools")
    log_path = artifact_dir / "groundup.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler(sys.stdout)],
    )
    logger = logging.getLogger("groundup")
    report: dict[str, Any] = {
        "status": "running",
        "cycle_id": CYCLE_ID,
        "entity": ENTITY,
        "started_at": _now(),
        "artifact_dir": str(artifact_dir),
        "prompt_output_dir": str(prompt_output_dir),
        "layers": [],
    }
    db_path = artifact_dir / "desk-smoke.db"
    store = reset_desk_store_for_test(db_path)
    logger.info("fresh desk db: %s", db_path)

    def layer(name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        logger.info("layer:start %s", name)
        try:
            details = fn()
            row = {"name": name, "status": "pass", **details}
            logger.info("layer:pass %s %s", name, _one_line(details))
        except Exception as exc:
            row = {"name": name, "status": "fail", "error": f"{type(exc).__name__}: {exc}"}
            logger.exception("layer:fail %s", name)
        report["layers"].append(row)
        return row

    event_bundle_holder: dict[str, Any] = {}
    node_snapshot_holder: dict[str, Any] = {}
    information_string_ids: list[str] = []
    tool_proposal_ids: list[str] = []

    def schema_layer() -> dict[str, Any]:
        version = get_schema_version(store.conn)
        tables = {
            row[0]
            for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {
            "information_strings",
            "information_syntheses",
            "information_geometry_snapshots",
            "market_event_intelligence",
            "market_event_data_points",
            "market_event_watch_triggers",
            "node_intelligence_snapshots",
            "node_intelligence_observations",
            "analysis_tool_proposals",
            "market_evolve_programs",
            "market_evolve_evaluations",
            "market_evolve_mutations",
            "market_evolve_experiments",
            "market_evolve_experiment_results",
        }
        missing = sorted(required - tables)
        _assert(version >= 20, f"schema version {version} < 20")
        _assert(not missing, f"missing tables: {missing}")
        return {"schema_version": version, "required_tables": sorted(required)}

    def prompt_gate_layer() -> dict[str, Any]:
        prompt = build_deep_scout_system_prompt("mycelial_network_v1")
        _assert("event_intelligence" in prompt, "prompt missing event_intelligence contract")
        _assert("node_intelligence" in prompt, "prompt missing node_intelligence contract")
        as_of = datetime.now(timezone.utc)
        event_start = as_of + timedelta(minutes=30)
        event_end = as_of + timedelta(hours=2)
        parsed = {
            "hypothesis": "HYPE reprices only if the aHYPE unstake routes to sellable liquidity while node-informed wallets fail to absorb.",
            "confidence": 0.63,
            "rationale_brief": "Event row plus actor route, Hydromancer wallet quality, and node rejects decide whether this is supply or noise.",
            "suggested_tools": [
                "tic://tool/hydromancer/get_hl_pnl_leaderboard@v1",
                "tic://tool/hydromancer/get_builder_fills@v1",
            ],
            "information_strings": [
                {
                    "title": "HYPE unstake route test",
                    "thesis": "The aHYPE unstake matters if tokens move toward sellable liquidity before informed wallets absorb.",
                    "entities_chain": ["HYPE", "aHYPE", "sellable liquidity", "informed wallets"],
                    "mechanism": "Unlock supply only becomes price-impacting after route and absorption are known.",
                    "depth_layers": [
                        {"layer": 1, "claim": "unstake creates potential supply"},
                        {"layer": 2, "claim": "route decides sellability"},
                        {"layer": 3, "claim": "node-informed wallet behavior decides absorption"},
                    ],
                    "expected_outcome": "Higher risk if CEX route plus weak Hydromancer/node absorption appears.",
                    "time_horizon": "intraday",
                    "time_scale": "intraday",
                    "event_time_start": event_start.isoformat(),
                    "event_time_end": event_end.isoformat(),
                    "observed_at": as_of.isoformat(),
                    "source_time_basis": "event_time",
                    "kill_signal": "No transfer or benign restake by T+2h.",
                    "extends_or_contradicts": "new",
                    "would_change_decision": True,
                    "expires_at": event_end.isoformat(),
                    "crowdedness": 0.38,
                    "conviction": 0.71,
                    "novelty_score": 0.78,
                    "evidence_refs": ["tc_events", "tc_hydro_leaders"],
                    "prior_thread_refs": [],
                    "rollup_parent_ids": [],
                    "lower_timeframe_refs": ["tc_node_rejects"],
                    "higher_timeframe_context_refs": ["tc_market_state"],
                    "temporal_confidence": 0.74,
                }
            ],
        }
        quality = score_deep_scout_output(
            parsed,
            allowed_tools=[
                "tic://tool/hydromancer/get_hl_pnl_leaderboard@v1",
                "tic://tool/hydromancer/get_builder_fills@v1",
            ],
        )
        gate = evaluate_prompt_scale_gate([
            _prompt_eval_payload(quality),
            {**_prompt_eval_payload(quality), "score": max(0.74, quality.score - 0.02)},
            {**_prompt_eval_payload(quality), "score": min(1.0, quality.score + 0.01)},
        ])
        _assert(quality.passed, f"prompt output failed: {quality.flags}")
        _assert(gate.passed, f"scale gate failed: {gate.flags}")
        system_path = prompt_output_dir / "prompt_gate_system_prompt.md"
        output_path = prompt_output_dir / "prompt_gate_sample_output.json"
        quality_path = prompt_output_dir / "prompt_gate_quality.json"
        _write_text(system_path, prompt)
        _write_json(output_path, parsed)
        _write_json(quality_path, {
            "prompt_quality": _asdict(quality),
            "scale_gate": _asdict(gate),
        })
        return {
            "prompt_quality": _asdict(quality),
            "scale_gate": _asdict(gate),
            "prompt_artifacts": {
                "system_prompt": str(system_path),
                "sample_output": str(output_path),
                "quality": str(quality_path),
            },
        }

    def event_layer() -> dict[str, Any]:
        bundles = event_intelligence_from_tool_evidence(
            cycle_id=CYCLE_ID,
            entity=ENTITY,
            horizon="intraday",
            lens="on_chain",
            tool_evidence=_event_evidence(),
        )
        _assert(len(bundles) == 1, f"expected 1 event bundle, got {len(bundles)}")
        bundle = bundles[0]
        quality = score_event_intelligence(bundle)
        _assert(quality.passed, f"event quality failed: {quality.flags}")
        bundle_id = persist_event_intelligence(bundle, conn=store.conn)
        info = event_intelligence_to_information_string(bundle)
        ids = persist_information_strings(
            conn=store.conn,
            cycle_id=CYCLE_ID,
            scout_id="smoke_event_scout",
            seed_id="seed_event",
            entity=ENTITY,
            theme="staking_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            coverage_cell_key="HYPE|intraday|on_chain|frontier|staking_unstake",
            strings=[info],
            source_tool_call_ids=["tc_events", "tc_ts"],
            model_used="smoke",
            provider="local",
        )
        information_string_ids.extend(ids)
        event_bundle_holder["bundle"] = bundle
        return {
            "event_bundle_id": bundle_id,
            "information_string_ids": ids,
            "event_score": quality.score,
            "data_points": quality.n_data_points,
            "flags": quality.flags,
        }

    def node_layer() -> dict[str, Any]:
        snapshot = node_intelligence_from_tool_evidence(
            cycle_id=CYCLE_ID,
            entity=ENTITY,
            horizon="intraday",
            lens="on_chain",
            tool_evidence=_node_evidence(),
        )
        _assert(snapshot is not None, "node snapshot was not recovered from evidence")
        quality = score_node_intelligence(snapshot)
        _assert(quality.passed, f"node quality failed: {quality.flags}")
        snapshot_id = persist_node_intelligence(snapshot, conn=store.conn)
        info = node_intelligence_to_information_string(snapshot)
        ids = persist_information_strings(
            conn=store.conn,
            cycle_id=CYCLE_ID,
            scout_id="smoke_node_scout",
            seed_id="seed_node",
            entity=ENTITY,
            theme="node_intelligence",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            coverage_cell_key="HYPE|intraday|on_chain|frontier|node_intelligence",
            strings=[info],
            source_tool_call_ids=["tc_hydro_leaders", "tc_builder", "tc_node_rejects", "tc_market_state"],
            model_used="smoke",
            provider="local",
        )
        information_string_ids.extend(ids)
        node_snapshot_holder["snapshot"] = snapshot
        return {
            "node_snapshot_id": snapshot_id,
            "information_string_ids": ids,
            "node_score": quality.score,
            "source_families": quality.source_families,
            "observations": quality.n_observations,
            "flags": quality.flags,
            "embedded_tool_proposals": len(snapshot.coverage.get("tool_proposals") or []),
        }

    def tool_creation_layer() -> dict[str, Any]:
        event_quality = score_event_intelligence(event_bundle_holder["bundle"])
        node_quality = score_node_intelligence(node_snapshot_holder["snapshot"])
        proposals = propose_tools_from_quality_flags(
            cycle_id=CYCLE_ID,
            artifact_kind="groundup_smoke",
            artifact_id="event_node_join",
            entity=ENTITY,
            horizon="intraday",
            lens="on_chain",
            quality_flags=[
                "missing_source_refs",
                "missing_hydromancer",
                "missing_our_hl_node",
                *event_quality.flags,
                *node_quality.flags,
            ],
        )
        _assert(proposals, "expected at least one tool proposal")
        ids = persist_analysis_tool_proposals(proposals, conn=store.conn)
        tool_proposal_ids.extend(ids)
        parent = proposals[0]
        parent.proposal_id = ids[0]
        improved = iterate_tool_proposal(
            parent,
            critique_flags=["needs_raw_offsets", "needs_before_after_window"],
            improvement_note="Add raw node offsets and explicit before/after windows for grading.",
            promotion_gate_delta={"requires_raw_offsets": True, "has_before_after_window": True},
        )
        improved_ids = persist_analysis_tool_proposals([improved], conn=store.conn)
        tool_proposal_ids.extend(improved_ids)
        rows = load_analysis_tool_proposals(cycle_id=CYCLE_ID, conn=store.conn)
        _assert(any(r["parent_proposal_id"] == ids[0] for r in rows), "iteration row missing parent link")
        learned_tool: dict[str, Any] = {}
        hl_node_id = next(
            (
                pid for pid, proposal in zip(ids, proposals)
                if proposal.tool_name == "hl_node_stream_reader"
            ),
            "",
        )
        if hl_node_id:
            promotion = promote_analysis_tool_proposal(hl_node_id, conn=store.conn)
            _assert(promotion.passed, f"learned tool promotion failed: {promotion.eval_report}")
            dispatched = dispatch_uri(
                promotion.tool_uri,
                {"coin": ENTITY, "wallets": ["0xabc"], "lookback_minutes": 90},
                AgentContext(CYCLE_ID, "groundup_smoke", "learned_tool_dispatch"),
            )
            _assert(dispatched.ok, f"learned tool dispatch failed: {dispatched.error}")
            _assert(dispatched.result.get("n_observations", 0) >= 1, "learned node tool returned no observations")
            learned_tool = {
                "proposal_id": promotion.proposal_id,
                "tool_uri": promotion.tool_uri,
                "tool_dir": promotion.tool_dir,
                "eval_report": promotion.eval_report,
                "dispatch_tool_call_log_id": dispatched.tool_call_log_id,
                "dispatch_n_observations": dispatched.result.get("n_observations"),
            }
        return {
            "proposal_ids": ids,
            "iterated_proposal_ids": improved_ids,
            "proposal_count": len(rows),
            "tool_names": sorted({r["tool_name"] for r in rows}),
            "learned_tool": learned_tool,
        }

    def scout_wiring_layer() -> dict[str, Any]:
        seed = SeedCell(
            seed_id="seed_node_event",
            entity=ENTITY,
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            theme="validator_unstake",
            payload={"wallet_address": "0xabc0000000000000000000000000000000000000"},
        )
        leaderboard_args = _infer_tool_args("tic://tool/hydromancer/get_hl_pnl_leaderboard@v1", seed)
        builder_args = _infer_tool_args("tic://tool/hydromancer/get_builder_fills@v1", seed)
        wallet_args = _infer_tool_args("tic://tool/hydromancer/get_wallet_historical_orders@v1", seed)
        _assert(leaderboard_args and leaderboard_args.get("top_n") == 25, "bad leaderboard args")
        _assert(builder_args and builder_args.get("lookback_hours") == 24, "bad builder args")
        _assert(
            wallet_args
            and wallet_args.get("wallet_address") == "0xabc0000000000000000000000000000000000000",
            "bad wallet args",
        )
        parsed = {
            "hypothesis": "HYPE sell-pressure risk is gated by actor route and informed-wallet absorption.",
            "confidence": 0.66,
            "rationale_brief": "Event + Hydromancer + node reject corpus should route verifier spend.",
            "suggested_tools": ["tic://tool/hydromancer/get_hl_pnl_leaderboard@v1"],
            "information_strings": [],
        }
        bundles = _event_bundles_for_scout(
            parsed=parsed,
            seed=seed,
            cycle_id=CYCLE_ID,
            tool_evidence=_event_evidence(),
        )
        snapshots = _node_snapshots_for_scout(
            parsed=parsed,
            seed=seed,
            cycle_id=CYCLE_ID,
            tool_evidence=_node_evidence(),
        )
        _assert(bundles, "scout fallback did not create event bundles")
        _assert(snapshots, "scout fallback did not create node snapshots")
        return {
            "leaderboard_args": leaderboard_args,
            "builder_args": builder_args,
            "wallet_args": wallet_args,
            "event_bundles": len(bundles),
            "node_snapshots": len(snapshots),
            "event_flags": bundles[0].quality_flags,
            "node_flags": snapshots[0].quality_flags,
        }

    def synthesis_layer() -> dict[str, Any]:
        strings = recent_information_strings(cycle_id=CYCLE_ID, conn=store.conn, limit=20)
        _assert(len(strings) >= 2, f"expected at least two strings, got {len(strings)}")
        synthesis = run_information_synthesis(cycle_id=CYCLE_ID, use_llm=False)
        _assert(synthesis.promoted_hypotheses, "synthesis promoted no hypotheses")
        return {
            "strings": len(strings),
            "synthesis_id": synthesis.synthesis_id,
            "summary": synthesis.summary,
            "confluences": len(synthesis.confluences),
            "tensions": len(synthesis.tensions),
            "promoted_hypotheses": len(synthesis.promoted_hypotheses),
            "promoted_string_ids": synthesis.promoted_string_ids,
        }

    def live_scout_layer() -> dict[str, Any]:
        transcript: dict[str, Any] = {}
        _install_fake_tic_chat(transcript)
        import talis_desk.tool_atlas as tool_atlas

        original_dispatch = tool_atlas.dispatch_uri
        tool_atlas.dispatch_uri = _fake_dispatch_uri
        seed = SeedCell(
            seed_id="seed_live_scout",
            entity=ENTITY,
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            theme="validator_unstake",
            payload={
                "tool_candidates": [
                    "tic://tool/builtin/query_events_recent@v1",
                    "tic://tool/hydromancer/get_hl_pnl_leaderboard@v1",
                    "tic://source/hl/hl_reject_corpus",
                    "tic://tool/builtin/query_timeseries@v1",
                ]
            },
        )
        try:
            out = asyncio.run(_run_one_scout(
                seed,
                cycle_id=CYCLE_ID,
                model="deepseek:v4-flash",
                fallback="anthropic:claude-haiku-4-5",
                cost_counter={},
                cost_cap=1.0,
            ))
        finally:
            tool_atlas.dispatch_uri = original_dispatch
        _assert(out.hypothesis_id, "live scout did not persist hypothesis")
        _assert(out.information_string_ids, "live scout did not persist information strings")
        _assert(out.event_intelligence_ids, "live scout did not persist event intelligence")
        _assert(out.node_intelligence_ids, "live scout did not persist node intelligence")
        _assert("node_intelligence_not_promoted" not in out.quality_flags, "node intelligence was not promotable")
        attached = _fetch_scout_rows(
            store.conn,
            cycle_id=CYCLE_ID,
            strings=[],
            events=[],
            node_intel=[],
            tool_proposals=[],
            limit=1,
        )
        _assert(attached and attached[0]["information_strings"], "monitor did not attach scout strings by id")
        _assert(attached[0]["event_intelligence"], "monitor did not attach event intelligence by id")
        _assert(attached[0]["node_intelligence"], "monitor did not attach node intelligence by id")
        live_artifacts = _write_live_scout_prompt_artifacts(
            prompt_output_dir=prompt_output_dir,
            seed=seed,
            transcript=transcript,
            scout_output=_asdict(out),
            monitor_row=attached[0],
        )
        return {
            "scout_id": out.scout_id,
            "hypothesis_id": out.hypothesis_id,
            "information_string_ids": out.information_string_ids,
            "event_intelligence_ids": out.event_intelligence_ids,
            "node_intelligence_ids": out.node_intelligence_ids,
            "quality_flags": out.quality_flags,
            "monitor_attached_strings": len(attached[0]["information_strings"]),
            "monitor_attached_events": len(attached[0]["event_intelligence"]),
            "monitor_attached_node": len(attached[0]["node_intelligence"]),
            "prompt_artifacts": live_artifacts,
        }

    def monitor_payload_layer() -> dict[str, Any]:
        payload = _build_scout_inspector_payload(store.conn, cycle_id=CYCLE_ID, limit=20)
        _assert(payload["status"] == "ok", f"monitor payload status {payload['status']}")
        summary = payload.get("summary") or {}
        _assert(summary.get("strings", 0) >= 2, "monitor summary missing strings")
        _assert(summary.get("event_intelligence", 0) >= 1, "monitor summary missing event intelligence")
        _assert(summary.get("node_intelligence", 0) >= 1, "monitor summary missing node intelligence")
        _assert(len(payload.get("tool_proposals") or []) >= 1, "monitor payload missing tool proposals")
        research_payload = _build_research_evolution_payload(store.conn, cycle_id=CYCLE_ID, limit=20)
        control = research_payload.get("evolution_control") or {}
        _assert(
            control.get("schema_version") == "swarm_evolution_control_v1",
            "research evolution payload missing evolution control schema",
        )
        _assert(
            control.get("geometry_cortex_review", {}).get("schema_version") == "alpha_geometry_cortex_review_v1",
            "research evolution payload missing cortex review",
        )
        _assert(
            control.get("proof", {}).get("cortex_review_ready") is True,
            "research evolution control proof did not mark cortex review ready",
        )
        monitor_path = prompt_output_dir / "monitor_payload.json"
        research_monitor_path = prompt_output_dir / "research_evolution_payload.json"
        _write_json(monitor_path, payload)
        _write_json(research_monitor_path, research_payload)
        return {
            "summary": summary,
            "strings": len(payload.get("strings") or []),
            "event_intelligence": len(payload.get("event_intelligence") or []),
            "node_intelligence": len(payload.get("node_intelligence") or []),
            "tool_proposals": len(payload.get("tool_proposals") or []),
            "monitor_payload_artifact": str(monitor_path),
            "research_evolution_artifact": str(research_monitor_path),
            "evolution_control_source": control.get("source"),
            "shape_can_direct_next": control.get("proof", {}).get("shape_can_direct_next"),
        }

    def alpha_geometry_market_evolve_layer() -> dict[str, Any]:
        snapshot = compute_alpha_geometry(cycle_id=CYCLE_ID, conn=store.conn, persist=True)
        rows = load_alpha_geometry(cycle_id=CYCLE_ID, conn=store.conn, limit=12)
        directives = alpha_geometry_seed_directives(snapshot, max_items=6)
        action_plan = plan_alpha_geometry_actions(cycle_id=CYCLE_ID, conn=store.conn, limit=64)
        cortex_review = build_alpha_geometry_cortex_review(
            cycle_id=CYCLE_ID,
            conn=store.conn,
            action_plan=action_plan,
            use_llm=False,
        )
        _assert(snapshot.cells, "alpha geometry produced no cells")
        _assert(rows, "alpha geometry did not persist/load rows")
        _assert(action_plan.get("routing_queue"), "alpha geometry action plan emitted no routing queue")
        _assert(
            cortex_review.get("cortex_work_orders"),
            "geometry cortex emitted no work orders",
        )
        _assert(
            cortex_review.get("shape_can_direct_next") is True,
            "geometry cortex did not recognize executable shape routing",
        )
        top = snapshot.cells[0]
        _assert(top.trade_scream_score > 0.0, "top geometry cell has no trade-scream signal")

        step = run_market_evolve_step(cycle_id=CYCLE_ID, conn=store.conn)
        _assert(step.evaluations, "MarketEvolve produced no evaluator output")
        _assert(step.best_evaluation is not None, "MarketEvolve has no best evaluation")
        _assert(step.mutations, "MarketEvolve proposed no policy mutation")
        _assert(step.experiment_plans, "MarketEvolve created no hard experiment plan")

        geometry_path = prompt_output_dir / "alpha_geometry.json"
        cortex_review_path = prompt_output_dir / "alpha_geometry_cortex_review.json"
        evolve_path = prompt_output_dir / "market_evolve_step.json"
        geometry_payload = {
            "cycle_id": CYCLE_ID,
            "global_metrics": snapshot.global_metrics,
            "quality_flags": snapshot.quality_flags,
            "cells": [_asdict(cell) for cell in snapshot.cells],
            "loaded_rows": rows,
            "directives": directives,
            "action_plan": action_plan,
            "routing_queue": action_plan.get("routing_queue") or [],
            "cortex_next_step": action_plan.get("cortex_next_step") or {},
            "cortex_toolkit": action_plan.get("cortex_toolkit") or [],
            "top_cell": _asdict(top),
        }
        evolve_payload = _market_evolve_payload(step)
        _write_json(geometry_path, geometry_payload)
        _write_json(cortex_review_path, cortex_review)
        _write_json(evolve_path, evolve_payload)

        best = step.best_evaluation
        mutation_kinds = [m.mutation_kind for m in step.mutations]
        return {
            "geometry_cells": len(snapshot.cells),
            "top_cell_key": top.cell_key,
            "top_route_directive": top.route_directive,
            "top_trade_scream_score": top.trade_scream_score,
            "top_verifier_readiness": top.verifier_readiness,
            "market_evolve_score": best.score,
            "market_evolve_passed": best.passed,
            "market_evolve_mutations": mutation_kinds,
            "experiment_plans": len(step.experiment_plans),
            "geometry_cortex_work_orders": len(cortex_review.get("cortex_work_orders") or []),
            "geometry_cortex_mutation_hint": (
                (cortex_review.get("proposed_geometry_policy") or {}).get("mutation_kind_hint")
            ),
            "artifacts": {
                "alpha_geometry": str(geometry_path),
                "alpha_geometry_cortex_review": str(cortex_review_path),
                "market_evolve_step": str(evolve_path),
            },
        }

    def shape_recurrent_loop_layer() -> dict[str, Any]:
        """Prove the map shape can call itself and emit the next routed work."""
        regenerate_tool_atlas()
        route_seeds = generate_alpha_geometry_route_seeds(
            cycle_id=f"{CYCLE_ID}_next",
            n_seed_budget=1000,
            source_cycle_id=CYCLE_ID,
            max_seeds=4,
            conn=store.conn,
        )
        _assert(route_seeds, "alpha geometry emitted no next-cycle route seeds")
        seed = route_seeds[0]
        tool_candidates = [str(x) for x in (seed.payload or {}).get("tool_candidates") or []]
        _assert(
            tool_candidates and tool_candidates[0] == ALPHA_GEOMETRY_ACTION_TOOL_URI,
            "shape-routed seed did not receive the native shape reader first",
        )
        args = _infer_tool_args(ALPHA_GEOMETRY_ACTION_TOOL_URI, seed)
        _assert(args and args.get("cycle_id") == CYCLE_ID, "shape reader args did not point at source geometry cycle")
        result = dispatch_uri(
            ALPHA_GEOMETRY_ACTION_TOOL_URI,
            args,
            AgentContext(f"{CYCLE_ID}_next", "shape_cortex", "shape_recurrent_loop"),
        )
        _assert(result.ok, f"shape reader dispatch failed: {result.error}")
        action_plan = result.result if isinstance(result.result, dict) else {}
        actions = [row for row in (action_plan.get("actions") or []) if isinstance(row, dict)]
        routing_queue = [row for row in (action_plan.get("routing_queue") or []) if isinstance(row, dict)]
        _assert(actions, "shape reader returned no actions")
        _assert(routing_queue, "shape reader returned no routing queue")
        top_action = actions[0]
        top_route_task = routing_queue[0]
        cortex_next_step = (
            action_plan.get("cortex_next_step")
            if isinstance(action_plan.get("cortex_next_step"), dict)
            else {}
        )
        _assert(
            cortex_next_step.get("status") == "ready",
            "shape reader did not emit a ready cortex next-step packet",
        )
        _assert(
            cortex_next_step.get("primary_action") == top_action.get("action"),
            "cortex next-step packet diverged from top geometry action",
        )
        _assert(
            cortex_next_step.get("route_task_id") == top_route_task.get("route_task_id"),
            "cortex next-step route task diverged from the routing queue",
        )
        directive = str((seed.payload or {}).get("alpha_geometry_route_directive") or "")
        _assert(directive and directive != "observe", "shape route did not alter the next seed directive")
        scout_prompt_preview = _build_user_prompt(seed, tool_evidence=[])
        _assert(
            "alpha_geometry_route_contract:" in scout_prompt_preview,
            "shape-routed scout prompt omitted the alpha-geometry route contract",
        )
        _assert(
            str(top_action.get("action") or "") in scout_prompt_preview,
            "shape-routed scout prompt omitted the cortex action",
        )
        route_transcript: dict[str, Any] = {}
        _install_fake_tic_chat(route_transcript)
        import talis_desk.tool_atlas as tool_atlas

        original_dispatch = tool_atlas.dispatch_uri

        def route_dispatch(uri: str, tool_args: dict[str, Any], context: Any) -> Any:
            if uri == ALPHA_GEOMETRY_ACTION_TOOL_URI:
                return original_dispatch(uri, tool_args, context)
            return _fake_dispatch_uri(uri, tool_args, context)

        tool_atlas.dispatch_uri = route_dispatch
        try:
            routed_out = asyncio.run(_run_one_scout(
                seed,
                cycle_id=f"{CYCLE_ID}_next",
                model="deepseek:v4-flash",
                fallback="anthropic:claude-haiku-4-5",
                cost_counter={},
                cost_cap=1.0,
            ))
        finally:
            tool_atlas.dispatch_uri = original_dispatch
        route_contract_flags = [
            flag for flag in routed_out.quality_flags
            if str(flag).startswith("route_contract")
        ]
        _assert("route_contract_satisfied" in route_contract_flags, "shape-routed scout failed its route contract")
        _assert(
            any(str(flag).startswith("route_contract_edge_addressed:") for flag in route_contract_flags),
            "shape-routed scout did not address a missing geometry edge",
        )
        routed_scout_path = prompt_output_dir / "shape_routed_scout_output.json"
        routed_transcript_path = prompt_output_dir / "shape_routed_scout_transcript.json"
        _write_json(routed_scout_path, _asdict(routed_out))
        _write_json(routed_transcript_path, route_transcript)
        worker_assignment = {
            "owner": cortex_next_step.get("primary_owner") or top_action.get("owner") or "seed_router",
            "action": cortex_next_step.get("primary_action") or top_action.get("action") or f"allocate_{directive}",
            "route_directive": directive,
            "success_gate": cortex_next_step.get("success_gate") or top_action.get("success_gate"),
            "missing_edges": top_action.get("missing_edges") or [],
            "source_cell_key": (seed.payload or {}).get("alpha_geometry_cell_key"),
            "route_task_id": cortex_next_step.get("route_task_id") or top_action.get("route_task_id"),
            "immediate_tool_sequence": cortex_next_step.get("immediate_tool_sequence") or [],
            "must_call_first": top_route_task.get("must_call_first"),
            "tool_sequence": top_route_task.get("tool_sequence") or [],
            "post_action_map_update": cortex_next_step.get("post_action_map_update"),
        }
        recurrent = {
            "schema_version": "shape_recurrent_loop_v1",
            "cycle_id": CYCLE_ID,
            "next_cycle_id": f"{CYCLE_ID}_next",
            "shape_observed": {
                "status": action_plan.get("status"),
                "global_shape": action_plan.get("global_shape"),
                "top_action": top_action,
                "top_route_task": top_route_task,
                "routing_queue": routing_queue[:6],
                "cortex_toolkit": action_plan.get("cortex_toolkit") or [],
            },
            "native_tool_call": {
                "tool_uri": ALPHA_GEOMETRY_ACTION_TOOL_URI,
                "args": args,
                "ok": result.ok,
                "tool_call_log_id": result.tool_call_log_id,
                "cost_usd": result.cost_usd,
            },
            "cortex_next_step": cortex_next_step,
            "scout_prompt_preview": scout_prompt_preview,
            "route_contract_evaluation": {
                "status": "satisfied" if "route_contract_satisfied" in route_contract_flags else "failed",
                "quality_flags": route_contract_flags,
                "scout_id": routed_out.scout_id,
                "hypothesis_id": routed_out.hypothesis_id,
                "information_string_ids": routed_out.information_string_ids,
                "tool_request_count": len(routed_out.tool_requests),
                "tool_iteration_count": routed_out.tool_iteration_count,
                "prompt_variant": routed_out.prompt_variant,
                "artifacts": {
                    "routed_scout_output": str(routed_scout_path),
                    "routed_scout_transcript": str(routed_transcript_path),
                },
            },
            "route_decision": {
                "emitted_seed_id": seed.seed_id,
                "entity": seed.entity,
                "horizon": seed.horizon,
                "lens": seed.lens,
                "bias_mode": seed.bias_mode,
                "directive": directive,
                "why_this_seed_exists": (seed.payload or {}).get("why_this_seed_exists"),
            },
            "emitted_seed": _asdict(seed),
            "worker_assignment": worker_assignment,
            "proof": {
                "shape_tool_was_first_candidate": tool_candidates[0] == ALPHA_GEOMETRY_ACTION_TOOL_URI,
                "route_changed_from_observe": directive != "observe",
                "tool_call_logged": bool(result.tool_call_log_id),
                "actions_returned": len(actions),
                "cortex_next_step_ready": cortex_next_step.get("status") == "ready",
                "scout_prompt_contains_route_contract": "alpha_geometry_route_contract:" in scout_prompt_preview,
                "routed_scout_satisfied_route_contract": "route_contract_satisfied" in route_contract_flags,
                "routed_scout_addressed_missing_edge": any(
                    str(flag).startswith("route_contract_edge_addressed:")
                    for flag in route_contract_flags
                ),
            },
        }
        recurrent_path = prompt_output_dir / "shape_recurrent_loop.json"
        _write_json(recurrent_path, recurrent)
        return {
            "artifact": str(recurrent_path),
            "shape_tool_call_log_id": result.tool_call_log_id,
            "emitted_seed_id": seed.seed_id,
            "route_directive": directive,
            "top_action": top_action.get("action"),
            "worker_owner": worker_assignment["owner"],
            "tool_candidates": tool_candidates[:6],
            "route_contract_status": recurrent["route_contract_evaluation"]["status"],
            "routed_scout_id": routed_out.scout_id,
        }

    def market_evolve_hard_experiment_layer() -> dict[str, Any]:
        """Execute a deterministic two-cycle candidate-vs-control policy experiment."""
        experiments = load_market_evolve_experiments(status="", limit=8, conn=store.conn)
        open_experiments = [
            exp for exp in experiments
            if str(exp.get("status") or "") in {"planned", "running", "insufficient_sample"}
        ]
        _assert(open_experiments, "no open MarketEvolve experiment available for execution")
        experiment = open_experiments[0]
        experiment_id = str(experiment.get("id") or "")
        candidate_program_id = str(experiment.get("candidate_program_id") or "")
        _assert(experiment_id and candidate_program_id, "experiment missing id or candidate program")

        cycles: list[dict[str, Any]] = []
        for offset, cycle in enumerate((f"{CYCLE_ID}_ab1", f"{CYCLE_ID}_ab2"), start=1):
            seeds = [
                SeedCell(
                    seed_id=f"seed_hard_ab_{offset}_{i:02d}",
                    entity=ENTITY,
                    horizon="intraday",
                    lens="on_chain",
                    bias_mode="frontier",
                    theme=f"hard_experiment_slice_{i:02d}",
                    payload={"tool_candidates": [f"tic://tool/test/tool_{j}@v1" for j in range(12)]},
                )
                for i in range(28)
            ]
            pair_count = prepare_market_evolve_experiment_seed_pairs(
                seeds,
                cycle_id=cycle,
                conn=store.conn,
                max_pairs=20,
            )
            _assert(pair_count >= 20, f"expected at least 20 matched pairs, got {pair_count}")
            apply_market_evolve_policy_to_seeds(seeds, cycle_id=cycle, conn=store.conn)
            apps = load_market_evolve_policy_applications(cycle_id=cycle, conn=store.conn, limit=200)
            control_apps = [
                row for row in apps
                if row.get("experiment_id") == experiment_id and row.get("experiment_arm") == "control"
            ]
            candidate_apps = [
                row for row in apps
                if row.get("experiment_id") == experiment_id and row.get("experiment_arm") == "candidate"
            ]
            _assert(len(control_apps) >= 20, f"control arm under-sampled: {len(control_apps)}")
            _assert(len(candidate_apps) >= 20, f"candidate arm under-sampled: {len(candidate_apps)}")
            _persist_candidate_experiment_strings(
                cycle_id=cycle,
                candidate_apps=candidate_apps[:8],
                suffix=f"cycle_{offset}",
                conn=store.conn,
            )
            step = run_market_evolve_step(cycle_id=cycle, conn=store.conn)
            results = [
                row for row in load_market_evolve_experiment_results(cycle_id=cycle, conn=store.conn)
                if row.get("experiment_id") == experiment_id
            ]
            _assert(results, f"no experiment result for {cycle}")
            cycles.append({
                "cycle_id": cycle,
                "pair_count": pair_count,
                "control_seed_count": len(control_apps),
                "candidate_seed_count": len(candidate_apps),
                "step": _market_evolve_payload(step),
                "result": results[0],
            })

        final_result = cycles[-1]["result"]
        _assert(final_result.get("decision") == "promote_candidate", f"candidate was not promoted: {final_result}")
        _assert(final_result.get("falsification_gate_results"), "experiment did not persist proof-gate evaluations")
        programs = {
            p.program_id: p
            for p in load_market_evolve_programs(status="", limit=100, conn=store.conn)
        }
        candidate = programs.get(candidate_program_id)
        _assert(candidate is not None and candidate.status == "active", "candidate program did not become active")
        episode = {
            "schema_version": "market_evolve_hard_experiment_episode_v1",
            "experiment_id": experiment_id,
            "parent_program_id": experiment.get("parent_program_id"),
            "candidate_program_id": candidate_program_id,
            "experiment_kind": experiment.get("experiment_kind"),
            "success_criteria": experiment.get("success_criteria"),
            "matched_slice": experiment.get("matched_slice"),
            "cycles": cycles,
            "final_decision": final_result.get("decision"),
            "final_score_delta": final_result.get("score_delta"),
            "active_program_after": _asdict(candidate),
            "proof": {
                "paired_seed_slices": all(c["pair_count"] >= 20 for c in cycles),
                "candidate_won_two_cycles": final_result.get("decision") == "promote_candidate",
                "falsification_gates_evaluated": bool(final_result.get("falsification_gate_results")),
                "candidate_program_active": candidate.status == "active",
                "quality_flags": final_result.get("quality_flags"),
            },
        }
        episode_path = prompt_output_dir / "market_evolve_hard_experiment.json"
        lineage = build_market_evolve_lineage(conn=store.conn)
        lineage_path = prompt_output_dir / "market_evolve_lineage.json"
        _write_json(episode_path, episode)
        _write_json(lineage_path, lineage)
        return {
            "artifact": str(episode_path),
            "lineage_artifact": str(lineage_path),
            "experiment_id": experiment_id,
            "candidate_program_id": candidate_program_id,
            "cycles": len(cycles),
            "final_decision": final_result.get("decision"),
            "final_score_delta": final_result.get("score_delta"),
            "active_program_after": candidate.program_id,
            "lineage_frontier_size": len(lineage.get("frontier") or []),
        }

    def geometry_cortex_review_driven_mutation_layer() -> dict[str, Any]:
        """Prove a bad shape review can directly create an experimentable mutation."""
        program = load_active_market_evolve_program(cycle_id=f"{CYCLE_ID}_cortex_mutation", conn=store.conn)
        program.program_id = f"{program.program_id}_cortex_fixture"
        program.name = f"{program.name}__cortex_fixture"
        metrics = collect_market_evolve_metrics(cycle_id=CYCLE_ID, conn=store.conn)
        metrics.update({
            "route_contract_eval_count": 3.0,
            "route_contract_success_rate": 0.0,
            "route_contract_failure_rate": 1.0,
            "geometry_route_action_rate": max(0.20, float(metrics.get("geometry_route_action_rate", 0.0))),
        })
        review = build_alpha_geometry_cortex_review(
            cycle_id=CYCLE_ID,
            conn=store.conn,
            metrics=metrics,
            use_llm=False,
        )
        diagnostic_codes = [
            str(d.get("code"))
            for d in (review.get("diagnostics") or [])
            if isinstance(d, dict) and d.get("code")
        ]
        _assert(
            "route_contract_not_moving_edges" in diagnostic_codes,
            f"geometry cortex did not diagnose route contract failure: {diagnostic_codes}",
        )
        evaluation = MarketEvolveEvaluation(
            evaluation_id="meval_geometry_cortex_review_fixture",
            program_id=program.program_id,
            cycle_id=f"{CYCLE_ID}_cortex_mutation",
            evaluator_version="geometry_cortex_review_fixture",
            score=0.0,
            metrics=metrics,
            baseline_metrics={},
            passed=True,
            rationale="Fixture proving cortex review can become a candidate mutation.",
            quality_flags=["geometry_cortex_review_fixture"],
        )
        child = propose_market_evolve_mutation(
            program=program,
            evaluation=evaluation,
            cycle_id=f"{CYCLE_ID}_cortex_mutation",
            cortex_review=review,
            conn=store.conn,
        )
        _assert(child is not None, "geometry cortex review did not create a candidate program")
        row = store.conn.execute(
            """
            SELECT id, mutation_kind, mutation_json
            FROM market_evolve_mutations
            WHERE child_program_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (child.program_id,),
        ).fetchone()
        _assert(row is not None, "missing geometry-cortex mutation row")
        mutation_payload = json.loads(row["mutation_json"])
        proof = mutation_payload.get("_evolution_proof") or {}
        _assert(
            mutation_payload.get("_geometry_cortex_review", {}).get("source") == "alpha_geometry_cortex_review",
            "mutation did not preserve geometry cortex review source packet",
        )
        _assert(
            proof.get("mutation_source") == "alpha_geometry_cortex_review",
            "proof packet did not attribute mutation to geometry cortex review",
        )
        artifact = {
            "schema_version": "geometry_cortex_review_driven_mutation_v1",
            "cycle_id": f"{CYCLE_ID}_cortex_mutation",
            "parent_program_id": program.program_id,
            "child_program_id": child.program_id,
            "mutation_id": row["id"],
            "mutation_kind": row["mutation_kind"],
            "diagnostic_codes": diagnostic_codes,
            "shape_health": review.get("shape_health"),
            "cortex_work_orders": review.get("cortex_work_orders"),
            "policy_patch": (review.get("proposed_geometry_policy") or {}).get("policy_patch"),
            "mutation_source": mutation_payload.get("_geometry_cortex_review"),
            "evolution_proof": proof,
            "proof": {
                "diagnosis_was_observed": "route_contract_not_moving_edges" in diagnostic_codes,
                "candidate_program_created": child.status == "candidate",
                "mutation_source_preserved": proof.get("mutation_source") == "alpha_geometry_cortex_review",
                "experiment_plan_created": bool(proof.get("falsification_gates")),
            },
        }
        path = prompt_output_dir / "geometry_cortex_mutation_path.json"
        _write_json(path, artifact)
        return {
            "artifact": str(path),
            "mutation_kind": row["mutation_kind"],
            "child_program_id": child.program_id,
            "diagnostic_codes": diagnostic_codes,
            "mutation_source": proof.get("mutation_source"),
        }

    def evolution_control_task_dispatch_layer() -> dict[str, Any]:
        program = load_active_market_evolve_program(cycle_id=CYCLE_ID, conn=store.conn)
        control = build_evolution_control_payload(
            cycle_id=CYCLE_ID,
            active_program=program,
            conn=store.conn,
            source="groundup_smoke",
        )
        dispatch = post_evolution_control_work_orders(
            cycle_id=CYCLE_ID,
            evolution_control=control,
            conn=store.conn,
            source="groundup_smoke",
        )
        repeated = post_evolution_control_work_orders(
            cycle_id=CYCLE_ID,
            evolution_control=control,
            conn=store.conn,
            source="groundup_smoke",
        )
        _assert(dispatch.get("task_count", 0) >= 1, "evolution control posted no dispatchable tasks")
        _assert(repeated.get("posted_count") == 0, "evolution control dispatch is not idempotent")
        rows = store.conn.execute(
            """
            SELECT id, topic, title, priority, allowed_tools_json,
                   promotion_criteria_json, kill_criteria_json, payload
            FROM task_contracts
            WHERE cycle_id = ?
            ORDER BY priority DESC
            """,
            (CYCLE_ID,),
        ).fetchall()
        artifact = {
            "control": control,
            "dispatch": dispatch,
            "repeat_dispatch": repeated,
            "tasks": [
                {
                    **dict(row),
                    "allowed_tools": json.loads(row["allowed_tools_json"] or "[]"),
                    "promotion_criteria": json.loads(row["promotion_criteria_json"] or "{}"),
                    "kill_criteria": json.loads(row["kill_criteria_json"] or "{}"),
                    "payload": json.loads(row["payload"] or "{}"),
                }
                for row in rows
            ],
        }
        path = prompt_output_dir / "evolution_control_task_dispatch.json"
        _write_json(path, artifact)
        return {
            "artifact": str(path),
            "posted_count": dispatch.get("posted_count"),
            "existing_count": repeated.get("existing_count"),
            "task_count": dispatch.get("task_count"),
            "first_topic": dict(rows[0]).get("topic") if rows else None,
        }

    def cortex_task_worker_execution_layer() -> dict[str, Any]:
        """Prove posted cortex work orders can be claimed and executed."""
        regenerate_tool_atlas()
        execution = execute_cortex_task_queue(
            cycle_id=CYCLE_ID,
            conn=store.conn,
            limit=16,
            policy=HarnessPolicy(evidence_hard_cap=4, max_retries=0, retry_backoff_s=0.0),
            execute_followup_tools=True,
        )
        _assert(execution.get("task_count", 0) >= 1, "cortex worker found no posted task contracts")
        _assert(execution.get("completed_count", 0) >= 1, "cortex worker completed no task contracts")
        _assert(execution.get("failed_count", 0) == 0, f"cortex worker failures: {execution}")
        executions = [
            row for row in (execution.get("executions") or [])
            if isinstance(row, dict)
        ]
        _assert(
            any(
                any(
                    obs.get("uri") == ALPHA_GEOMETRY_ACTION_TOOL_URI and obs.get("ok")
                    for obs in (row.get("observations") or [])
                    if isinstance(obs, dict)
                )
                for row in executions
            ),
            "cortex worker did not observe the alpha-geometry shape tool",
        )
        followup_observation_count = sum(
            1
            for row in executions
            for obs in (row.get("observations") or [])
            if isinstance(obs, dict) and obs.get("phase") == "cortex_followup"
        )
        rows = store.conn.execute(
            """
            SELECT id, topic, status, owner_agent_id, owner_specialist_id, completed_at
            FROM task_contracts
            WHERE cycle_id = ? AND topic IN ('alpha_geometry.route', 'market_evolve.frontier')
            ORDER BY priority DESC
            """,
            (CYCLE_ID,),
        ).fetchall()
        events = store.conn.execute(
            """
            SELECT event_type, task_id, agent_id, specialist_id, payload
            FROM blackboard_events
            WHERE cycle_id = ? AND event_type IN ('task.claimed', 'task.started', 'task.completed', 'task.failed')
            ORDER BY occurred_at ASC
            """,
            (CYCLE_ID,),
        ).fetchall()
        artifact = {
            "execution": execution,
            "tasks": [dict(row) for row in rows],
            "events": [
                {
                    **dict(row),
                    "payload": json.loads(row["payload"] or "{}"),
                }
                for row in events
            ],
            "proof": {
                "worker_claimed_tasks": execution.get("claimed_count", 0) >= 1,
                "worker_completed_tasks": execution.get("completed_count", 0) >= 1,
                "shape_tool_observed": True,
                "bounded_followup_tools_enabled": execution.get("execute_followup_tools") is True,
                "followup_observation_count": followup_observation_count,
                "failed_count": execution.get("failed_count", 0),
            },
        }
        path = prompt_output_dir / "cortex_task_worker_execution.json"
        _write_json(path, artifact)
        return {
            "artifact": str(path),
            "task_count": execution.get("task_count"),
            "completed_count": execution.get("completed_count"),
            "failed_count": execution.get("failed_count"),
            "followup_observation_count": followup_observation_count,
            "first_completed_task": (
                next((row.get("task_id") for row in executions if row.get("completed")), None)
            ),
        }

    def cortex_task_feedback_evaluator_layer() -> dict[str, Any]:
        """Prove completed cortex tasks become evaluator pressure."""
        program = load_active_market_evolve_program(cycle_id=CYCLE_ID, conn=store.conn)
        metrics = collect_market_evolve_metrics(cycle_id=CYCLE_ID, conn=store.conn)
        score = score_market_evolve_metrics(metrics, program=program)
        experiments = load_market_evolve_experiments(status="", limit=8, conn=store.conn)
        dispatched_arm_payloads: dict[str, dict[str, dict[str, Any]]] = {}
        for row in store.conn.execute(
            "SELECT payload FROM task_contracts WHERE cycle_id = ?",
            (CYCLE_ID,),
        ).fetchall():
            try:
                payload = json.loads(row["payload"] or "{}")
            except Exception:
                continue
            exp_id = str(payload.get("market_evolve_experiment_id") or "")
            arm = str(payload.get("market_evolve_experiment_arm") or "")
            program_id = str(payload.get("market_evolve_program_id") or "")
            if exp_id and arm in {"control", "candidate"} and program_id:
                dispatched_arm_payloads.setdefault(exp_id, {})[arm] = payload
        selected_experiment_id = ""
        selected_arms: dict[str, dict[str, Any]] = {}
        for exp_id, arms in dispatched_arm_payloads.items():
            if "control" in arms and "candidate" in arms:
                selected_experiment_id = exp_id
                selected_arms = arms
                break
        experiment = next(
            (
                row for row in experiments
                if str(row.get("id") or "") == selected_experiment_id
            ),
            {},
        )
        control_metrics: dict[str, float] = {}
        candidate_metrics: dict[str, float] = {}
        if selected_experiment_id and selected_arms:
            control_metrics = collect_market_evolve_metrics(
                cycle_id=CYCLE_ID,
                program_id=str(selected_arms["control"].get("market_evolve_program_id") or ""),
                experiment_id=selected_experiment_id,
                experiment_arm="control",
                conn=store.conn,
            )
            candidate_metrics = collect_market_evolve_metrics(
                cycle_id=CYCLE_ID,
                program_id=str(selected_arms["candidate"].get("market_evolve_program_id") or ""),
                experiment_id=selected_experiment_id,
                experiment_arm="candidate",
                conn=store.conn,
            )
        _assert(metrics.get("cortex_task_count", 0.0) >= 1.0, "evaluator did not see cortex tasks")
        _assert(
            metrics.get("cortex_task_completion_rate", 0.0) >= 1.0,
            f"evaluator saw incomplete cortex tasks: {metrics}",
        )
        _assert(
            metrics.get("cortex_shape_observation_rate", 0.0) >= 1.0,
            f"evaluator did not see shape-observed cortex completions: {metrics}",
        )
        _assert(
            "cortex_followup_execution_rate" in metrics,
            f"evaluator did not expose follow-up execution metrics: {metrics}",
        )
        _assert(
            not selected_experiment_id or (
                control_metrics.get("cortex_task_count", 0.0) >= 1.0
                and candidate_metrics.get("cortex_task_count", 0.0) >= 1.0
            ),
            "evaluator did not expose separate control/candidate cortex task metrics",
        )
        artifact = {
            "schema_version": "cortex_task_feedback_evaluator_v1",
            "cycle_id": CYCLE_ID,
            "program_id": program.program_id,
            "score": score,
            "metrics": metrics,
            "experiment_arm_metrics": {
                "experiment_id": selected_experiment_id,
                "control_program_id": (
                    selected_arms.get("control", {}).get("market_evolve_program_id")
                ),
                "candidate_program_id": (
                    selected_arms.get("candidate", {}).get("market_evolve_program_id")
                ),
                "control": control_metrics,
                "candidate": candidate_metrics,
            },
            "metric_names": [
                "cortex_task_count",
                "cortex_task_completion_rate",
                "cortex_task_failure_rate",
                "cortex_task_pending_rate",
                "cortex_shape_observation_rate",
                "cortex_observations_per_task",
                "cortex_deferred_followup_rate",
                "cortex_followup_execution_rate",
                "cortex_followup_observations_per_task",
                "cortex_shape_blocked_followup_rate",
            ],
            "proof": {
                "evaluator_saw_worker_tasks": metrics.get("cortex_task_count", 0.0) >= 1.0,
                "worker_completion_is_rewarded": metrics.get("cortex_task_completion_rate", 0.0) >= 1.0,
                "shape_observation_is_rewarded": metrics.get("cortex_shape_observation_rate", 0.0) >= 1.0,
                "followup_execution_is_measured": "cortex_followup_execution_rate" in metrics,
                "experiment_arm_cortex_metrics_are_scoped": (
                    not selected_experiment_id
                    or (
                        control_metrics.get("cortex_task_count", 0.0) >= 1.0
                        and candidate_metrics.get("cortex_task_count", 0.0) >= 1.0
                    )
                ),
                "worker_failures_are_penalizable": "cortex_task_failure_rate" in metrics,
            },
        }
        path = prompt_output_dir / "cortex_task_feedback_evaluator.json"
        _write_json(path, artifact)
        return {
            "artifact": str(path),
            "score": score,
            "cortex_task_count": metrics.get("cortex_task_count"),
            "completion_rate": metrics.get("cortex_task_completion_rate"),
            "shape_observation_rate": metrics.get("cortex_shape_observation_rate"),
            "followup_execution_rate": metrics.get("cortex_followup_execution_rate"),
            "experiment_arm_metrics_scoped": (
                not selected_experiment_id
                or (
                    control_metrics.get("cortex_task_count", 0.0) >= 1.0
                    and candidate_metrics.get("cortex_task_count", 0.0) >= 1.0
                )
            ),
            "failure_rate": metrics.get("cortex_task_failure_rate"),
        }

    layer("schema_migrations", schema_layer)
    layer("prompt_contract_and_scale_gate", prompt_gate_layer)
    layer("event_intelligence_ingest", event_layer)
    layer("node_intelligence_ingest", node_layer)
    layer("analysis_tool_creation_iteration", tool_creation_layer)
    layer("scout_wiring_fallbacks", scout_wiring_layer)
    layer("information_synthesis_budget_gate", synthesis_layer)
    layer("live_scout_execution_and_monitor_join", live_scout_layer)
    layer("alpha_geometry_market_evolve", alpha_geometry_market_evolve_layer)
    layer("shape_recurrent_loop", shape_recurrent_loop_layer)
    layer("market_evolve_hard_experiment", market_evolve_hard_experiment_layer)
    layer("geometry_cortex_review_driven_mutation", geometry_cortex_review_driven_mutation_layer)
    layer("evolution_control_task_dispatch", evolution_control_task_dispatch_layer)
    layer("cortex_task_worker_execution", cortex_task_worker_execution_layer)
    layer("cortex_task_feedback_evaluator", cortex_task_feedback_evaluator_layer)
    layer("monitor_payload_surface", monitor_payload_layer)

    failed = [row for row in report["layers"] if row["status"] != "pass"]
    report["status"] = "fail" if failed else "pass"
    report["finished_at"] = _now()
    report["db_path"] = str(db_path)
    report["log_path"] = str(log_path)
    report["prompt_output_dir"] = str(prompt_output_dir)
    report["information_string_ids"] = information_string_ids
    report["tool_proposal_ids"] = tool_proposal_ids
    market_universe_path = prompt_output_dir / "market_universe_manifest.json"
    market_universe = build_market_universe(default_entities=DEFAULT_ENTITIES)
    _write_json(market_universe_path, market_universe.to_dict())
    coverage_gap_path = prompt_output_dir / "coverage_gap_manifest.json"
    coverage_gap_manifest = build_coverage_gap_manifest(
        cycle_id=CYCLE_ID,
        conn=store.conn,
    )
    _write_json(coverage_gap_path, coverage_gap_manifest)
    governor = build_market_map_governor_plan(
        cycle_id=CYCLE_ID,
        conn=store.conn,
        coverage_manifest=coverage_gap_manifest,
        scout_budget=1000,
        use_llm=False,
    )
    governor_json_path = prompt_output_dir / "market_map_governor.json"
    governor_md_path = prompt_output_dir / "market_map_governor.md"
    _write_json(governor_json_path, governor)
    _write_text(governor_md_path, render_market_map_governor_markdown(governor))
    report["market_universe"] = {
        "manifest": str(market_universe_path),
        "entity_count": len(market_universe.entities),
        "source_quality": market_universe.source_quality,
        "source_counts": market_universe.source_counts,
        "errors": list(market_universe.errors),
    }
    report["coverage_gap_manifest"] = {
        "json": str(coverage_gap_path),
        "valid_cell_count": coverage_gap_manifest.get("grid", {}).get("valid_cell_count"),
        "covered_count": coverage_gap_manifest.get("coverage", {}).get("covered_count"),
        "missing_count": coverage_gap_manifest.get("coverage", {}).get("missing_count"),
        "coverage_ratio": coverage_gap_manifest.get("coverage", {}).get("coverage_ratio"),
    }
    report["market_map_governor"] = {
        "json": str(governor_json_path),
        "markdown": str(governor_md_path),
        "status": governor.get("status"),
        "completion_pressure": governor.get("completion_pressure"),
        "ranked_gaps": len(governor.get("ranked_gaps") or []),
        "suggested_seed_cells": len(governor.get("suggested_seed_cells") or []),
        "budget_lanes": len(governor.get("budget_lanes") or []),
    }
    system_trace_artifacts = read_prompt_trace_artifacts(prompt_output_dir)
    system_trace = build_system_trace(report=report, artifacts=system_trace_artifacts)
    system_trace_json_path = prompt_output_dir / "system_trace.json"
    system_trace_md_path = prompt_output_dir / "system_trace.md"
    _write_json(system_trace_json_path, system_trace)
    _write_text(system_trace_md_path, render_system_trace_markdown(system_trace))
    self_healing = build_market_map_self_healing_plan(system_trace)
    self_healing_dispatch = post_market_map_self_healing_work_orders(
        self_healing,
        cycle_id=CYCLE_ID,
        conn=store.conn,
        limit=8,
    )
    repeated_self_healing_dispatch = post_market_map_self_healing_work_orders(
        self_healing,
        cycle_id=CYCLE_ID,
        conn=store.conn,
        limit=8,
    )
    self_healing_worker = execute_market_map_self_healing_tasks(
        cycle_id=CYCLE_ID,
        conn=store.conn,
        limit=8,
    )
    if self_healing.get("work_orders"):
        _assert(
            self_healing_dispatch.get("posted_count", 0) >= 1,
            "self-healing work orders did not become task contracts",
        )
        _assert(
            repeated_self_healing_dispatch.get("existing_count", 0)
            == self_healing_dispatch.get("posted_count", 0),
            "self-healing task dispatch is not idempotent",
        )
        _assert(
            self_healing_dispatch.get("proof", {}).get("all_tasks_have_success_gates") is True,
            "self-healing task contracts missing success gates",
        )
        _assert(
            self_healing_worker.get("task_count", 0) >= 1,
            "self-healing worker found no posted task contracts to execute",
        )
        _assert(
            self_healing_worker.get("completed_count", 0) >= 1,
            "self-healing worker did not complete any repair task",
        )
        _assert(
            self_healing_worker.get("failed_count", 0) == 0,
            "self-healing worker failed a repair task",
        )
    self_healing_json_path = prompt_output_dir / "market_map_self_healing.json"
    self_healing_md_path = prompt_output_dir / "market_map_self_healing.md"
    self_healing_dispatch_path = prompt_output_dir / "market_map_self_healing_dispatch.json"
    self_healing_worker_path = prompt_output_dir / "market_map_self_healing_worker.json"
    _write_json(self_healing_json_path, self_healing)
    _write_json(
        self_healing_dispatch_path,
        {
            "dispatch": self_healing_dispatch,
            "repeat_dispatch": repeated_self_healing_dispatch,
        },
    )
    _write_json(self_healing_worker_path, self_healing_worker)
    _write_text(self_healing_md_path, render_market_map_self_healing_markdown(self_healing))
    report["system_trace"] = {
        "json": str(system_trace_json_path),
        "markdown": str(system_trace_md_path),
        "schema_version": system_trace.get("schema_version"),
        "quality_flags": system_trace.get("quality_flags", []),
    }
    report["market_map_self_healing"] = {
        "json": str(self_healing_json_path),
        "markdown": str(self_healing_md_path),
        "status": self_healing.get("status"),
        "work_orders": len(self_healing.get("work_orders") or []),
        "dispatch_artifact": str(self_healing_dispatch_path),
        "worker_artifact": str(self_healing_worker_path),
        "posted_tasks": self_healing_dispatch.get("posted_count"),
        "repeat_existing_tasks": repeated_self_healing_dispatch.get("existing_count"),
        "worker_completed_tasks": self_healing_worker.get("completed_count"),
        "worker_failed_tasks": self_healing_worker.get("failed_count"),
        "worker_tool_proposals": self_healing_worker.get("tool_proposal_count"),
        "worker_promotion_reports": self_healing_worker.get("promotion_report_count"),
        "orders_became_task_contracts": (
            self_healing_dispatch.get("proof", {}).get("orders_became_task_contracts")
        ),
        "tasks_completed_by_worker": (
            self_healing_worker.get("proof", {}).get("self_healing_tasks_completed")
        ),
    }
    _write_prompt_output_index(prompt_output_dir)

    json_path = artifact_dir / "groundup_report.json"
    md_path = artifact_dir / "groundup_report.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(_render_markdown(report), encoding="utf-8")
    logger.info("report json: %s", json_path)
    logger.info("report md: %s", md_path)
    print(f"GROUNDUP_STATUS={report['status']}")
    print(f"GROUNDUP_REPORT_JSON={json_path}")
    print(f"GROUNDUP_REPORT_MD={md_path}")
    print(f"GROUNDUP_LOG={log_path}")
    print(f"GROUNDUP_PROMPT_OUTPUT_DIR={prompt_output_dir}")
    print(f"GROUNDUP_SYSTEM_TRACE_JSON={system_trace_json_path}")
    print(f"GROUNDUP_SYSTEM_TRACE_MD={system_trace_md_path}")
    print(f"GROUNDUP_MARKET_GOVERNOR_JSON={governor_json_path}")
    print(f"GROUNDUP_SELF_HEALING_JSON={self_healing_json_path}")
    store.close()
    return 1 if failed else 0


def _event_evidence() -> list[dict[str, Any]]:
    return [
        {
            "uri": "tic://tool/builtin/query_events_recent@v1",
            "ok": True,
            "tool_call_log_id": "tc_events",
            "result": {
                "events": [
                    {
                        "entity": ENTITY,
                        "event_type": "unstake",
                        "headline": "Upcoming unstaking aHYPE 558.1K HYPE in 3h.",
                        "source": "hyperview",
                        "event_time": "2026-05-21T11:30:00Z",
                        "metadata": {
                            "label": "aHYPE",
                            "address": "0xabc",
                            "actor_type": "protocol_wrapper",
                            "depth_1pct_ratio": 2.4,
                            "funding_crowding_score": 0.63,
                        },
                    }
                ]
            },
        },
        {
            "uri": "tic://tool/builtin/query_timeseries@v1",
            "ok": True,
            "tool_call_log_id": "tc_ts",
            "result": {
                "points": [
                    {"ts": "2026-05-21T10:00:00Z", "metric": "spot_volume_24h", "value": 7_150_000},
                    {"ts": "2026-05-21T10:00:00Z", "metric": "perp_open_interest", "value": 0.71},
                    {"ts": "2026-05-20T10:00:00Z", "metric": "prior_unstake_return_2h", "value": -0.012},
                ]
            },
        },
    ]


def _node_evidence() -> list[dict[str, Any]]:
    return [
        {
            "uri": "tic://tool/hydromancer/get_hl_pnl_leaderboard@v1",
            "ok": True,
            "tool_call_log_id": "tc_hydro_leaders",
            "result": {
                "leaders": [
                    {
                        "rank": 1,
                        "wallet": "0xabc",
                        "realized_pnl_usd": 1_250_000,
                        "win_rate_pct": 68.2,
                        "volume_usd": 25_000_000,
                        "n_completed_trades": 144,
                    },
                    {
                        "rank": 7,
                        "wallet": "0xdef",
                        "realized_pnl_usd": 410_000,
                        "win_rate_pct": 57.4,
                        "volume_usd": 8_900_000,
                        "n_completed_trades": 73,
                    },
                ],
                "headline": "Top wallets active in HYPE tape.",
            },
        },
        {
            "uri": "tic://tool/hydromancer/get_builder_fills@v1",
            "ok": True,
            "tool_call_log_id": "tc_builder",
            "result": {
                "builder": "0xaf99",
                "total_volume_usd": 3_500_000,
                "total_builder_fee_usd": 420,
                "unique_users": 7,
                "top_coins": [["HYPE", 19]],
                "top_users_by_volume": [["0xabc", 2_100_000]],
                "fills_sample": [{"coin": "HYPE", "user": "0xabc", "px": 35, "sz": 1000}],
            },
        },
        {
            "uri": "tic://source/hl/hl_reject_corpus",
            "ok": True,
            "tool_call_log_id": "tc_node_rejects",
            "result": {
                "wallet": "0xabc",
                "reject_rate_pct": 1.7,
                "status_counts": {"filled": 116, "rejected": 2},
                "top_reject_reasons": [["insufficient_margin", 2]],
            },
        },
        {
            "uri": "tic://tool/builtin/query_timeseries@v1",
            "ok": True,
            "tool_call_log_id": "tc_market_state",
            "result": {
                "points": [
                    {"ts": "2026-05-21T10:00:00Z", "metric": "HYPE_perp_open_interest", "value": 910_000_000},
                    {"ts": "2026-05-21T10:00:00Z", "metric": "HYPE_orderbook_depth_1pct", "value": 8_000_000},
                    {"ts": "2026-05-21T10:00:00Z", "metric": "HYPE_funding_crowding_score", "value": 0.63},
                ]
            },
        },
    ]


def _fake_dispatch_uri(uri: str, args: dict[str, Any], context: Any) -> Any:
    if "query_events_recent" in uri:
        return SimpleNamespace(
            ok=True,
            error=None,
            cost_usd=0.0,
            tool_call_log_id="tc_live_events",
            result={
                "events": [
                    {
                        "entity": ENTITY,
                        "event_type": "unstake",
                        "headline": "Upcoming unstaking aHYPE 558.1K HYPE in 3h.",
                        "event_time": "2026-05-21T11:30:00Z",
                        "metadata": {
                            "label": "aHYPE",
                            "address": "0xabc0000000000000000000000000000000000000",
                            "depth_1pct_ratio": 2.4,
                            "funding_crowding_score": 0.63,
                        },
                    }
                ]
            },
        )
    if "get_hl_pnl_leaderboard" in uri:
        return SimpleNamespace(
            ok=True,
            error=None,
            cost_usd=0.0,
            tool_call_log_id="tc_live_hydro",
            result={
                "leaders": [
                    {
                        "rank": 1,
                        "wallet": "0xabc0000000000000000000000000000000000000",
                        "realized_pnl_usd": 1_250_000,
                        "win_rate_pct": 68.2,
                        "volume_usd": 25_000_000,
                    }
                ]
            },
        )
    if "hl_reject_corpus" in uri:
        return SimpleNamespace(
            ok=True,
            error=None,
            cost_usd=0.0,
            tool_call_log_id="tc_live_node",
            result={
                "wallet": "0xabc0000000000000000000000000000000000000",
                "reject_rate_pct": 1.7,
                "status_counts": {"filled": 116, "rejected": 2},
                "top_reject_reasons": [["insufficient_margin", 2]],
                "source": "our_hl_node",
            },
        )
    return SimpleNamespace(
        ok=True,
        error=None,
        cost_usd=0.0,
        tool_call_log_id="tc_live_market",
        result={
            "points": [
                {"ts": "2026-05-21T10:00:00Z", "metric": "HYPE_perp_open_interest", "value": 910_000_000},
                {"ts": "2026-05-21T10:00:00Z", "metric": "HYPE_orderbook_depth_1pct", "value": 8_000_000},
            ]
        },
    )


def _write_live_scout_prompt_artifacts(
    *,
    prompt_output_dir: Path,
    seed: SeedCell,
    transcript: dict[str, Any],
    scout_output: dict[str, Any],
    monitor_row: dict[str, Any],
) -> dict[str, str]:
    variant = str(transcript.get("prompt_variant") or _prompt_variant_for_seed(seed))
    preview_evidence = _preview_live_tool_evidence(seed)
    system_prompt = str(
        transcript.get("system_prompt")
        or build_deep_scout_system_prompt(variant)  # type: ignore[arg-type]
    )
    user_prompt = str(
        transcript.get("user_prompt")
        or _build_user_prompt(seed, tool_evidence=preview_evidence)
    )
    model_output = transcript.get("model_output") or _fake_model_output_payload()
    response_envelope = transcript.get("response_envelope") or {
        "text": json.dumps(model_output),
        "model_used": "deepseek:v4-flash",
        "provider": "fake",
    }
    artifacts = {
        "system_prompt": prompt_output_dir / "live_scout_system_prompt.md",
        "user_prompt": prompt_output_dir / "live_scout_user_prompt.md",
        "tool_evidence": prompt_output_dir / "live_scout_tool_evidence.json",
        "model_output": prompt_output_dir / "live_scout_model_output.json",
        "model_response_envelope": prompt_output_dir / "live_scout_model_response_envelope.json",
        "persisted_scout_output": prompt_output_dir / "live_scout_persisted_output.json",
        "monitor_row": prompt_output_dir / "live_scout_monitor_row.json",
        "transcript": prompt_output_dir / "live_scout_transcript.json",
    }
    _write_text(artifacts["system_prompt"], system_prompt)
    _write_text(artifacts["user_prompt"], user_prompt)
    _write_json(artifacts["tool_evidence"], preview_evidence)
    _write_json(artifacts["model_output"], model_output)
    _write_json(artifacts["model_response_envelope"], response_envelope)
    _write_json(artifacts["persisted_scout_output"], scout_output)
    _write_json(artifacts["monitor_row"], monitor_row)
    _write_json(artifacts["transcript"], {
        "seed": _asdict(seed),
        "prompt_variant": variant,
        "system_prompt_path": str(artifacts["system_prompt"]),
        "user_prompt_path": str(artifacts["user_prompt"]),
        "tool_evidence_path": str(artifacts["tool_evidence"]),
        "model_output_path": str(artifacts["model_output"]),
        "persisted_scout_output_path": str(artifacts["persisted_scout_output"]),
        "monitor_row_path": str(artifacts["monitor_row"]),
    })
    return {key: str(path) for key, path in artifacts.items()}


def _preview_live_tool_evidence(seed: SeedCell) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for uri in list((seed.payload or {}).get("tool_candidates") or []):
        args = _infer_tool_args(uri, seed)
        if args is None:
            continue
        res = _fake_dispatch_uri(uri, args, None)
        result = getattr(res, "result", None)
        out.append({
            "uri": uri,
            "args": args,
            "ok": bool(getattr(res, "ok", False)),
            "error": getattr(res, "error", None),
            "summary": _short_json(result, limit=900),
            "result": result,
            "tool_call_log_id": getattr(res, "tool_call_log_id", None),
            "cost_usd": float(getattr(res, "cost_usd", 0.0) or 0.0),
        })
        if len(out) >= 4:
            break
    return out


def _fake_model_output_payload() -> dict[str, Any]:
    as_of = datetime.now(timezone.utc)
    event_start = as_of + timedelta(minutes=30)
    event_end = as_of + timedelta(hours=2)
    return {
        "hypothesis": "HYPE reprices if aHYPE unstake routes to sellable liquidity before informed wallets absorb.",
        "confidence": 0.64,
        "rationale_brief": "Event row plus Hydromancer actor quality and our-node reject state route verifier spend.",
        "suggested_tools": [
            "tic://tool/builtin/query_events_recent@v1",
            "tic://tool/hydromancer/get_hl_pnl_leaderboard@v1",
        ],
        "information_strings": [
            {
                "title": "HYPE unstake route",
                "thesis": "HYPE risk rises only if the aHYPE unstake becomes sellable before high-quality node actors absorb it.",
                "mechanism": "Unlock supply needs route, liquidity, and actor-quality confirmation before it is price-impacting.",
                "expected_outcome": "Verifier should watch CEX route, order-book absorption, funding, and informed wallet behavior.",
                "time_horizon": "intraday",
                "time_scale": "intraday",
                "event_time_start": event_start.isoformat(),
                "event_time_end": event_end.isoformat(),
                "observed_at": as_of.isoformat(),
                "source_time_basis": "event_time",
                "kill_signal": "No transfer, benign restake, or strong informed-wallet absorption by T+2h.",
                "extends_or_contradicts": "new",
                "would_change_decision": True,
                "expires_at": event_end.isoformat(),
                "crowdedness": 0.35,
                "conviction": 0.74,
                "novelty_score": 0.78,
                "entities_chain": ["HYPE", "aHYPE", "sellable liquidity", "informed wallets"],
                "depth_layers": [
                    {"layer": 1, "claim": "unstake creates potential supply"},
                    {"layer": 2, "claim": "route decides whether supply can hit market"},
                    {"layer": 3, "claim": "node actor quality decides absorption"},
                ],
                "evidence_refs": ["tc_live_events", "tc_live_hydro", "tc_live_node", "tc_live_market"],
                "temporal_confidence": 0.78,
            }
        ],
    }


def _fake_route_model_output_payload(user_prompt: str) -> dict[str, Any]:
    as_of = datetime.now(timezone.utc)
    expires_at = as_of + timedelta(hours=2)
    evidence_refs: list[str] = []
    for token in str(user_prompt or "").replace("\n", " ").split():
        if token.startswith("tool_call_log_id="):
            ref = token.split("=", 1)[1].strip().strip(",;")
            if ref and ref not in evidence_refs:
                evidence_refs.append(ref)
    if not evidence_refs:
        evidence_refs = ["tc_shape_route_contract"]
    return {
        "hypothesis": (
            "HYPE route cell deserves another scout only if independent replication "
            "confirms or kills the thin-cell edge before verifier spend."
        ),
        "confidence": 0.72,
        "rationale_brief": "The shape route asked for independent replication, so the scout tests that edge directly.",
        "suggested_tools": [
            "tic://tool/builtin/query_events_recent@v1",
            "tic://tool/builtin/query_timeseries@v1",
        ],
        "tool_requests": [
            {
                "tool_uri": "tic://tool/builtin/query_events_recent@v1",
                "why": "Second scout should confirm, contradict, or kill the independent replication edge.",
                "expected_edge": "thin_cell -> independent_replication",
                "expected_info_value": 0.82,
                "would_change_decision": True,
                "fallback_if_denied": "Mark the thin cell unresolved and route to source repair.",
                "priority": "high",
            }
        ],
        "information_strings": [
            {
                "title": "Independent replication moves the thin HYPE cell",
                "thesis": (
                    "A second scout confirms the thin_cell -> independent_replication "
                    "edge by checking HYPE node route evidence through a separate source path."
                ),
                "mechanism": (
                    "The original shape was novel but thin; independent replication lowers "
                    "fragility before verifier budget is spent."
                ),
                "expected_outcome": (
                    "The second scout confirms, contradicts, or kills the route string, "
                    "then the map closes the edge or re-routes the cell."
                ),
                "time_horizon": "intraday",
                "time_scale": "intraday",
                "observed_at": as_of.isoformat(),
                "source_time_basis": "event_time",
                "kill_signal": "No independent source can reproduce the HYPE route-quality signal by T+2h.",
                "extends_or_contradicts": "extends",
                "would_change_decision": True,
                "expires_at": expires_at.isoformat(),
                "crowdedness": 0.26,
                "conviction": 0.83,
                "novelty_score": 0.79,
                "entities_chain": ["HYPE", "thin cell", "independent replication", "shape route"],
                "depth_layers": [
                    {"layer": 1, "claim": "frontier cell gets a next receptive field"},
                    {"layer": 2, "claim": "thin cell gets independent replication"},
                    {"layer": 3, "claim": "route contract can be satisfied or killed before verifier spend"},
                ],
                "evidence_refs": evidence_refs[:3],
                "temporal_confidence": 0.76,
            }
        ],
    }


def _persist_candidate_experiment_strings(
    *,
    cycle_id: str,
    candidate_apps: list[dict[str, Any]],
    suffix: str,
    conn: Any,
) -> list[str]:
    families = ["hydromancer", "market_microstructure", "our_hl_node", "event_store"]
    out: list[str] = []
    persist_analysis_tool_proposals(
        [
            AnalysisToolProposal(
                cycle_id=cycle_id,
                artifact_kind="market_evolve_experiment",
                artifact_id=f"hard_experiment_{suffix}",
                entity=ENTITY,
                horizon="intraday",
                lens="on_chain",
                proposal_kind="promote_tool",
                tool_name="hl_node_stream_reader",
                purpose="Candidate arm used the learned node stream reader to turn route-quality edge discovery into repeatable evidence.",
                source_family="our_hl_node",
                trigger="candidate learned-tool surface should activate a proven node tool during the hard experiment",
                input_shape={"cycle_id": cycle_id, "entity": ENTITY, "source": "hard_experiment_fixture"},
                promotion_gate={
                    "expected_edge": "learned_node_tool -> route_quality_edge",
                    "expected_info_value": 0.85,
                    "would_change_decision": True,
                },
                eval_plan={
                    "fixture": "hard_experiment_candidate_uses_learned_tool",
                    "pass_condition": "tool_activation_rate reaches 1.0 and candidate strings beat control",
                },
                priority="high",
                status="active",
                quality_flags=["hard_experiment_fixture_active_tool"],
            )
        ],
        conn=conn,
    )
    for i, app in enumerate(candidate_apps):
        family = families[i % len(families)]
        ids = persist_information_strings(
            conn=conn,
            cycle_id=cycle_id,
            scout_id=f"scout_hard_experiment_{suffix}_{i}",
            seed_id=str(app.get("seed_id") or f"seed_hard_experiment_{i}"),
            entity=ENTITY,
            theme="hard_experiment_route_quality",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"Candidate arm finds {family}",
                    thesis="Candidate research policy finds source-diverse HYPE route intelligence before the control arm.",
                    mechanism="The candidate policy changes scout prompting/tool use so more independent source families support the same route-quality claim.",
                    expected_outcome="If the policy is better, candidate arm strings remain valid, source-diverse, and verifier-ready out of sample.",
                    kill_signal="Control arm produces equal or better valid source-diverse strings at similar cost.",
                    conviction=0.88,
                    novelty_score=0.82,
                    crowdedness=0.18,
                    entities_chain=["HYPE", "aHYPE", "route quality", family],
                    depth_layers=[
                        {"layer": 1, "claim": "route quality"},
                        {"layer": 2, "claim": "source-family breadth"},
                    ],
                    evidence_refs=[f"tic://source/{family}/hard_experiment/{suffix}"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            source_tool_call_ids=[f"tc_hard_experiment_{suffix}_{i}_{family}"],
        )
        out.extend(ids)
    return out


def _install_fake_tic_chat(transcript: dict[str, Any] | None = None) -> None:
    async def fake_chat(model: str, system: str, user: str, *, max_tokens: int, fallback: str | None = None) -> dict[str, Any]:
        model_output = (
            _fake_route_model_output_payload(user)
            if "alpha_geometry_route_contract:" in str(user or "")
            else _fake_model_output_payload()
        )
        response = {
            "text": json.dumps(model_output),
            "model_used": model,
            "provider": "fake",
        }
        if transcript is not None:
            transcript.clear()
            transcript.update({
                "model": model,
                "fallback": fallback,
                "max_tokens": max_tokens,
                "prompt_variant": "temporal_pyramid_v1",
                "system_prompt": system,
                "user_prompt": user,
                "model_output": model_output,
                "response_envelope": response,
            })
        return response

    tic = types.ModuleType("tic")
    desk = types.ModuleType("tic.desk")
    models = types.ModuleType("tic.desk.models")
    models.chat = fake_chat
    desk.models = models
    tic.desk = desk
    sys.modules["tic"] = tic
    sys.modules["tic.desk"] = desk
    sys.modules["tic.desk.models"] = models


def _artifact_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path(tempfile.gettempdir()) / f"talis-groundup-smoke-{stamp}"
    root.mkdir(parents=True, exist_ok=False)
    return root


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Talis Ground-Up Smoke Report",
        "",
        f"- status: `{report['status']}`",
        f"- cycle: `{report['cycle_id']}`",
        f"- entity: `{report['entity']}`",
        f"- db: `{report.get('db_path', '')}`",
        f"- log: `{report.get('log_path', '')}`",
        f"- prompt_outputs: `{report.get('prompt_output_dir', '')}`",
        "",
        "## Layers",
        "",
    ]
    for row in report["layers"]:
        lines.append(f"### {row['name']} - {row['status']}")
        for key, value in row.items():
            if key in {"name", "status"}:
                continue
            lines.append(f"- {key}: `{_short_json(value)}`")
        lines.append("")
    return "\n".join(lines)


def _write_prompt_output_index(prompt_output_dir: Path) -> None:
    lines = [
        "# Talis Prompt And Output Artifacts",
        "",
        "Open these files to inspect exactly what the micro test sent to the scout and what came back.",
        "",
        "## Prompt Gate",
        "",
        "- `prompt_gate_system_prompt.md`: static DeepSeek scout contract for the prompt-gate sample.",
        "- `prompt_gate_sample_output.json`: representative output scored before scale-up.",
        "- `prompt_gate_quality.json`: deterministic quality and scale-gate result.",
        "",
        "## Live Scout Layer",
        "",
        "- `live_scout_system_prompt.md`: actual system prompt passed to the scout chat shim.",
        "- `live_scout_user_prompt.md`: actual cell/evidence/tool prompt passed to the scout chat shim.",
        "- `live_scout_tool_evidence.json`: deterministic evidence slice available to the scout.",
        "- `live_scout_model_output.json`: parsed model JSON output.",
        "- `live_scout_model_response_envelope.json`: full model response envelope before parsing.",
        "- `live_scout_persisted_output.json`: persisted ScoutOutput after normalization and storage.",
        "- `live_scout_monitor_row.json`: row shape consumed by the scout monitor.",
        "- `alpha_geometry.json`: persisted information-geometry field and route directives.",
        "- `alpha_geometry_cortex_review.json`: cortex review of map-shape health, policy pressure, work orders, and optional LLM context.",
        "- `geometry_cortex_mutation_path.json`: deterministic proof that a high-severity shape review can create a MarketEvolve candidate mutation.",
        "- `evolution_control_task_dispatch.json`: posted task contracts proving shape/cortex work orders become dispatchable coordination work.",
        "- `cortex_task_worker_execution.json`: worker claim/start/complete events plus harness observations from dispatched cortex tasks.",
        "- `cortex_task_feedback_evaluator.json`: MarketEvolve metrics proving worker completion/shape observation feed evaluator pressure.",
        "- `market_evolve_step.json`: evaluator score, policy mutation, and hard experiment plan.",
        "- `shape_recurrent_loop.json`: proof that the cortex read the map shape through a native tool and emitted the next route.",
        "- `market_evolve_hard_experiment.json`: two-cycle matched A/B proof that a candidate research policy can beat control and become active.",
        "- `monitor_payload.json`: full `/api/scouts` inspector payload.",
        "- `research_evolution_payload.json`: full `/api/evolution` inspector payload with shared evolution-control proof.",
        "- `system_trace.json`: canonical layer-by-layer data-in/data-out trace.",
        "- `system_trace.md`: readable version of the canonical system trace.",
        "- `market_universe_manifest.json`: entity/source universe used to prove coverage boundaries.",
        "- `market_map_governor.json`: ranked frontier gaps, scout budget lanes, and LLM governor context.",
        "- `market_map_governor.md`: readable market-map governor plan.",
        "- `market_map_self_healing.json`: worker orders for map repair, expansion, and tool creation.",
        "- `market_map_self_healing.md`: readable self-healing work-order plan.",
        "- `market_map_self_healing_worker.json`: claimed/completed repair tasks, tool proposals, and shape-reader observations.",
        "- `coverage_gap_manifest.json`: deterministic covered/missing/stale audit against the known market lattice.",
        "- `market_evolve_lineage.json`: program ancestry, mutation edges, proof-gate verdicts, and frontier priorities.",
    ]
    _write_text(prompt_output_dir / "README.md", "\n".join(lines) + "\n")


def _market_evolve_payload(step: Any) -> dict[str, Any]:
    best = step.best_evaluation
    return {
        "cycle_id": step.cycle_id,
        "quality_flags": list(step.quality_flags),
        "best_evaluation": _asdict(best) if best is not None else None,
        "programs": [_asdict(p) for p in step.programs],
        "evaluations": [_asdict(e) for e in step.evaluations],
        "mutations": [_asdict(m) for m in step.mutations],
        "child_programs": [_asdict(c) for c in step.child_programs],
        "experiment_plans": list(step.experiment_plans),
        "experiment_results": list(step.experiment_results),
    }


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _asdict(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value


def _prompt_eval_payload(quality: Any) -> dict[str, Any]:
    return {
        "score": float(quality.score),
        "passed": bool(quality.passed),
        "flags": list(quality.flags),
        "n_strings": int(quality.n_strings),
        "n_valid_tools": int(quality.n_valid_tools),
    }


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_json(value: Any, limit: int = 500) -> str:
    try:
        text = json.dumps(value, sort_keys=True, default=str)
    except Exception:
        text = str(value)
    return text[:limit]


def _one_line(value: Any) -> str:
    return _short_json(value, limit=220).replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
