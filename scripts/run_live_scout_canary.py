#!/usr/bin/env python
"""Run a live-provider scout canary/ramp before scaling scout spend.

The deterministic readiness slice proves orchestration with a local model/tool
shim. This runner proves the next layer: real provider import, real prompt
formatting, real tool atlas, real evidence dispatch, real string storage, and
the same map/geometry/governor path under a hard cost cap. It can run tiny
canaries, 100-scout distribution ramps, and 1,000-scout shadow candidates; the
tournament evaluator remains the promotion authority.

Live calls require ``--allow-live-spend`` on purpose. Running without it still
writes a preflight report and viewer artifacts, but it will not call a model.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from talis_desk._tic_config import ensure_tic_on_path, get_tic_root
from talis_desk.information_map import (
    apply_market_evolve_policy_to_seeds,
    build_alpha_geometry_cortex_review,
    build_market_evolve_lineage,
    collect_hyperliquid_mid_price_observations,
    compute_alpha_geometry,
    compute_information_perfusion,
    evaluate_information_price_outcomes,
    load_market_evolve_policy_applications,
    prepare_market_evolve_experiment_seed_pairs,
    plan_alpha_geometry_actions,
    persist_price_observations,
    recent_information_strings,
    run_information_synthesis,
    run_market_evolve_step,
)
from talis_desk.information_map.deep_scout_prompt import build_deep_scout_system_prompt
from talis_desk.information_map.live_ramp_policy import (
    apply_live_scout_ramp_policy_to_seeds,
    build_live_scout_ramp_policy_rehearsal,
    load_live_scout_ramp_policy,
)
from talis_desk.market_map.coverage_audit import build_coverage_gap_manifest
from talis_desk.market_map.governor import build_market_map_governor_plan
from talis_desk.market_map.self_healing import (
    build_market_map_self_healing_plan,
    execute_market_map_self_healing_tasks,
    post_market_map_self_healing_work_orders,
)
from talis_desk.market_map.universe import build_market_universe
from talis_desk.store import DeskStore, reset_desk_store_for_test
from talis_desk.swarm.scout_runner import (
    _apply_prompt_contract_pressure,
    _build_user_prompt,
    _prompt_variant_for_seed,
    run_scouts,
)
from talis_desk.swarm.seed_generator import (
    DEFAULT_ENTITIES,
    SeedCell,
    generate_information_perfusion_seeds,
    generate_seeds,
)
from talis_desk.tool_atlas import (
    load_analysis_tool_proposals,
    regenerate_tool_atlas,
    repair_low_quality_analysis_tool_proposals,
)
from talis_desk.tool_atlas.discovery import evaluate_analysis_tool_proposal

from scripts.run_100_scout_readiness_slice import (
    DEFAULT_THEMES,
    _asdict,
    _attach_effective_unique_seed_metrics,
    _market_evolve_hard_experiment_episode,
    _market_evolve_metrics,
    _market_evolve_pair_budget,
    _metrics,
    _prepare_seed,
    _readiness_trace,
    _seed_payload,
    _source_family,
    _supplemental_seeds,
    _trim_preserving_pairs,
    _write_readiness_artifacts,
    _write_json,
    _write_text,
)


def main() -> int:
    args = _parse_args()
    started = time.perf_counter()
    artifact_dir = Path(args.artifact_dir).expanduser().resolve() if args.artifact_dir else _artifact_dir()
    prompt_output_dir = (
        Path(args.prompt_output_dir).expanduser().resolve()
        if args.prompt_output_dir
        else artifact_dir / "prompt_outputs"
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    prompt_output_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TALIS_LEARNED_TOOLS_DIR"] = str(artifact_dir / "learned_tools")

    cycle_id = args.cycle_id or f"cycle_live_scout_canary_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    db_path = Path(args.db).expanduser().resolve() if args.db else artifact_dir / "desk-live-canary.db"
    store = DeskStore(db_path=db_path) if args.preserve_db else reset_desk_store_for_test(db_path)
    restore_chat: Callable[[], None] | None = None
    try:
        preflight = _preflight(cycle_id=cycle_id)
        universe = build_market_universe(default_entities=DEFAULT_ENTITIES)
        market_evolve_pair_budget = _market_evolve_pair_budget(
            args.n_scouts,
            requested=args.market_evolve_pairs,
        )
        base_seed_count = max(1, args.n_scouts - market_evolve_pair_budget)
        seeds, control_seed_routing = _generate_control_aware_live_seeds(
            args=args,
            cycle_id=cycle_id,
            conn=store.conn,
            universe_entities=universe.entity_symbols() or DEFAULT_ENTITIES,
            base_seed_count=base_seed_count,
        )
        _write_json(prompt_output_dir / "live_scout_control_seed_routing.json", control_seed_routing)
        market_evolve_planning_step = run_market_evolve_step(cycle_id=cycle_id, conn=store.conn)
        paired_slices = prepare_market_evolve_experiment_seed_pairs(
            seeds,
            cycle_id=cycle_id,
            conn=store.conn,
            max_pairs=market_evolve_pair_budget,
        )
        if len(seeds) < args.n_scouts:
            seeds.extend(_supplemental_seeds(
                n=args.n_scouts - len(seeds),
                cycle_id=cycle_id,
                universe_entities=universe.entity_symbols() or DEFAULT_ENTITIES,
                rng_seed=args.seed_rng + 991,
                offset=len(seeds),
            ))
        if len(seeds) > args.n_scouts:
            seeds = _trim_preserving_pairs(seeds, args.n_scouts)
        market_evolve_program = apply_market_evolve_policy_to_seeds(
            seeds,
            cycle_id=cycle_id,
            conn=store.conn,
        )
        _force_live_seed_runtime_options(seeds, args=args)
        ramp_policy: dict[str, Any] = {}
        ramp_policy_application: dict[str, Any] = {}
        ramp_policy_rehearsal: dict[str, Any] = {}
        if args.ramp_policy:
            baseline_seeds = copy.deepcopy(seeds)
            ramp_policy = load_live_scout_ramp_policy(args.ramp_policy)
            ramp_policy_application = apply_live_scout_ramp_policy_to_seeds(seeds, ramp_policy)
            ramp_policy_rehearsal = build_live_scout_ramp_policy_rehearsal(
                baseline_seeds=baseline_seeds,
                candidate_seeds=seeds,
                policy=ramp_policy,
                application=ramp_policy_application,
            )
        seed_path = prompt_output_dir / "live_scout_canary_seeds.json"
        _write_json(seed_path, [_seed_payload(seed) for seed in seeds])
        if ramp_policy_rehearsal:
            _write_json(prompt_output_dir / "live_scout_ramp_policy_rehearsal.json", ramp_policy_rehearsal)
        prompt_preview = _write_live_prompt_preview(
            prompt_output_dir=prompt_output_dir,
            seeds=seeds,
        )
        slice_preview = _write_live_slice_preview(
            prompt_output_dir=prompt_output_dir,
            cycle_id=cycle_id,
            seeds=seeds,
        )
        price_observation_start = _collect_live_price_observations(
            enabled=args.collect_price_observations,
            stage="start",
            prompt_output_dir=prompt_output_dir,
            seeds=seeds,
            conn=store.conn,
            timeout_s=args.price_source_timeout_s,
        )

        if not args.allow_live_spend:
            report = _blocked_report(
                args=args,
                cycle_id=cycle_id,
                db_path=db_path,
                artifact_dir=artifact_dir,
                prompt_output_dir=prompt_output_dir,
                seed_path=seed_path,
                prompt_preview=prompt_preview,
                slice_preview=slice_preview,
                preflight=preflight,
                ramp_policy=ramp_policy,
                ramp_policy_application=ramp_policy_application,
                ramp_policy_rehearsal=ramp_policy_rehearsal,
                price_observation_start=price_observation_start,
                reason="explicit_live_spend_flag_missing",
                elapsed_s=time.perf_counter() - started,
            )
            _write_canary_report(prompt_output_dir, report)
            _print_report_paths(prompt_output_dir, report)
            return 0

        if not preflight.get("provider_import_ok"):
            report = _blocked_report(
                args=args,
                cycle_id=cycle_id,
                db_path=db_path,
                artifact_dir=artifact_dir,
                prompt_output_dir=prompt_output_dir,
                seed_path=seed_path,
                prompt_preview=prompt_preview,
                slice_preview=slice_preview,
                preflight=preflight,
                ramp_policy=ramp_policy,
                ramp_policy_application=ramp_policy_application,
                ramp_policy_rehearsal=ramp_policy_rehearsal,
                price_observation_start=price_observation_start,
                reason="provider_import_failed",
                elapsed_s=time.perf_counter() - started,
            )
            _write_canary_report(prompt_output_dir, report)
            _print_report_paths(prompt_output_dir, report)
            return 0

        if ramp_policy_rehearsal and ramp_policy_rehearsal.get("status") != "pass":
            report = _blocked_report(
                args=args,
                cycle_id=cycle_id,
                db_path=db_path,
                artifact_dir=artifact_dir,
                prompt_output_dir=prompt_output_dir,
                seed_path=seed_path,
                prompt_preview=prompt_preview,
                slice_preview=slice_preview,
                preflight=preflight,
                ramp_policy=ramp_policy,
                ramp_policy_application=ramp_policy_application,
                ramp_policy_rehearsal=ramp_policy_rehearsal,
                price_observation_start=price_observation_start,
                reason="ramp_policy_rehearsal_failed",
                elapsed_s=time.perf_counter() - started,
            )
            _write_canary_report(prompt_output_dir, report)
            _print_report_paths(prompt_output_dir, report)
            return 0

        transcript: dict[str, Any] = {"calls": []}
        restore_chat = _install_live_chat_recorder(
            transcript,
            timeout_s=args.provider_timeout_s,
            progress_path=prompt_output_dir / "live_scout_canary_transcript_progress.json",
        )
        atlas = regenerate_tool_atlas()
        scouts = run_scouts(
            seeds=seeds,
            cycle_id=cycle_id,
            model=args.model,
            fallback=args.fallback,
            concurrency=args.concurrency,
            cost_cap_usd=args.cost_cap_usd,
        )
        outputs_path = prompt_output_dir / "live_scout_canary_outputs.json"
        _write_json(outputs_path, [_asdict(row) for row in scouts])
        _write_primary_live_artifacts(
            prompt_output_dir=prompt_output_dir,
            seeds=seeds,
            scouts=scouts,
            transcript=transcript,
        )

        strings = recent_information_strings(cycle_id=cycle_id, conn=store.conn, limit=max(200, args.n_scouts * 10))
        price_observation_final = _collect_live_price_observations(
            enabled=args.collect_price_observations,
            stage="final",
            prompt_output_dir=prompt_output_dir,
            seeds=seeds,
            conn=store.conn,
            timeout_s=args.price_source_timeout_s,
        )
        information_price_outcomes = _evaluate_live_information_price_outcomes(
            enabled=args.collect_price_observations,
            cycle_id=cycle_id,
            prompt_output_dir=prompt_output_dir,
            conn=store.conn,
            min_move_threshold_pct=args.price_outcome_threshold_pct,
            limit=max(500, args.n_scouts * 10),
        )
        synthesis = run_information_synthesis(
            cycle_id=cycle_id,
            max_strings=max(50, args.n_scouts * 3),
            use_llm=False,
        )
        geometry = compute_alpha_geometry(cycle_id=cycle_id, conn=store.conn, persist=True)
        perfusion = compute_information_perfusion(
            cycle_id=cycle_id,
            scout_budget=max(8, min(64, args.n_scouts)),
            conn=store.conn,
            persist=True,
        )
        _write_json(
            prompt_output_dir / "information_perfusion.json",
            {
                "schema_version": "information_perfusion_export_v1",
                "cycle_id": cycle_id,
                "global_metrics": getattr(perfusion, "global_metrics", {}),
                "quality_flags": getattr(perfusion, "quality_flags", []),
                "cells": [_asdict(cell) for cell in (getattr(perfusion, "cells", []) or [])],
            },
        )
        action_plan = plan_alpha_geometry_actions(cycle_id=cycle_id, conn=store.conn, limit=64)
        cortex_review = build_alpha_geometry_cortex_review(
            cycle_id=cycle_id,
            conn=store.conn,
            action_plan=action_plan,
            use_llm=False,
        )
        coverage = build_coverage_gap_manifest(cycle_id=cycle_id, conn=store.conn)
        governor = build_market_map_governor_plan(
            cycle_id=cycle_id,
            conn=store.conn,
            coverage_manifest=coverage,
            scout_budget=args.n_scouts,
            use_llm=False,
        )
        trace = _readiness_trace(
            cycle_id=cycle_id,
            scouts=scouts,
            strings=strings,
            geometry=geometry,
            action_plan=action_plan,
            coverage=coverage,
            governor=governor,
        )
        self_healing_plan = build_market_map_self_healing_plan(trace)
        self_healing_dispatch = post_market_map_self_healing_work_orders(
            self_healing_plan,
            cycle_id=cycle_id,
            conn=store.conn,
            limit=args.self_healing_limit,
        )
        self_healing_worker = execute_market_map_self_healing_tasks(
            cycle_id=cycle_id,
            conn=store.conn,
            limit=args.self_healing_limit,
        )
        tool_creation_contract_repair = _tool_creation_contract_repair_report(
            cycle_id=cycle_id,
            conn=store.conn,
            enabled=args.repair_tool_proposal_contracts,
            limit=args.tool_proposal_repair_limit,
        )
        _write_json(
            prompt_output_dir / "tool_creation_contract_repair.json",
            tool_creation_contract_repair,
        )
        metrics = _metrics(
            cycle_id=cycle_id,
            seeds=seeds,
            scouts=scouts,
            strings=strings,
            synthesis=synthesis,
            geometry=geometry,
            action_plan=action_plan,
            coverage=coverage,
            governor=governor,
            self_healing_plan=self_healing_plan,
            self_healing_dispatch=self_healing_dispatch,
            self_healing_worker=self_healing_worker,
            atlas_n_tools=getattr(atlas, "n_tools", 0),
            atlas_n_sources=getattr(atlas, "n_sources", 0),
            transcript=transcript,
            elapsed_s=time.perf_counter() - started,
        )
        metrics["tool_creation_contract_repair"] = tool_creation_contract_repair
        metrics["price_observations"] = {
            "start": _price_observation_summary(price_observation_start),
            "final": _price_observation_summary(price_observation_final),
        }
        metrics["information_price_outcomes"] = information_price_outcomes.get("summary", {})
        market_evolve_step = run_market_evolve_step(cycle_id=cycle_id, conn=store.conn)
        market_evolve_lineage = build_market_evolve_lineage(conn=store.conn)
        policy_apps = load_market_evolve_policy_applications(cycle_id=cycle_id, conn=store.conn, limit=max(500, len(seeds) * 3))
        market_evolve_hard_experiment = _market_evolve_hard_experiment_episode(
            cycle_id=cycle_id,
            planning_step=market_evolve_planning_step,
            final_step=market_evolve_step,
            policy_applications=policy_apps,
            paired_slices=paired_slices,
            active_program_id=market_evolve_program.program_id,
        )
        metrics["market_evolve"] = _market_evolve_metrics(
            planning_step=market_evolve_planning_step,
            final_step=market_evolve_step,
            lineage=market_evolve_lineage,
            policy_applications=policy_apps,
            paired_slices=paired_slices,
        )
        _attach_effective_unique_seed_metrics(metrics)
        _write_readiness_artifacts(
            prompt_output_dir=prompt_output_dir,
            cycle_id=cycle_id,
            geometry=geometry,
            action_plan=action_plan,
            cortex_review=cortex_review,
            coverage=coverage,
            governor=governor,
            self_healing_plan=self_healing_plan,
            self_healing_dispatch=self_healing_dispatch,
            self_healing_worker=self_healing_worker,
            market_evolve_step=market_evolve_step,
            market_evolve_hard_experiment=market_evolve_hard_experiment,
            market_evolve_lineage=market_evolve_lineage,
        )
        transcript_summary = _transcript_summary(transcript)
        verdict = _live_canary_verdict(
            metrics,
            cost_cap_usd=args.cost_cap_usd,
            n_scouts=args.n_scouts,
            transcript_summary=transcript_summary,
        )
        report = {
            "schema_version": "talis_live_scout_canary_v1",
            "mode": "live_provider_cost_capped",
            "cycle_id": cycle_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "db_path": str(db_path),
            "artifact_dir": str(artifact_dir),
            "prompt_output_dir": str(prompt_output_dir),
            "n_scouts_requested": args.n_scouts,
            "seed_rng": args.seed_rng,
            "model": args.model,
            "fallback": args.fallback,
            "concurrency": args.concurrency,
            "cost_cap_usd": args.cost_cap_usd,
            "provider_timeout_s": args.provider_timeout_s,
            "prompt_variant_override": args.prompt_variant,
            "max_tool_iterations": args.max_tool_iterations,
            "repair_tool_proposal_contracts": args.repair_tool_proposal_contracts,
            "tool_proposal_repair_limit": args.tool_proposal_repair_limit,
            "ramp_policy_path": args.ramp_policy,
            "preserve_db": args.preserve_db,
            "collect_price_observations": args.collect_price_observations,
            "ramp_policy": ramp_policy,
            "ramp_policy_application": ramp_policy_application,
            "ramp_policy_rehearsal": ramp_policy_rehearsal,
            "price_observations": {
                "start": price_observation_start,
                "final": price_observation_final,
            },
            "information_price_outcomes": information_price_outcomes,
            "preflight": preflight,
            "prompt_preview": prompt_preview,
            "slice_preview": slice_preview,
            "metrics": metrics,
            "tool_creation_contract_repair": tool_creation_contract_repair,
            "verdict": verdict,
            "artifacts": {
                "seeds": str(seed_path),
                "outputs": str(outputs_path),
                "slice_preview": str(prompt_output_dir / "live_scout_slice_preview.json"),
                "price_observations_start": str(prompt_output_dir / "live_price_observations_start.json"),
                "price_observations_final": str(prompt_output_dir / "live_price_observations_final.json"),
                "information_price_outcomes": str(prompt_output_dir / "information_price_outcomes.json"),
                "tool_creation_contract_repair": str(prompt_output_dir / "tool_creation_contract_repair.json"),
            },
            "transcript_summary": transcript_summary,
            "scale_decision": _scale_decision(verdict, n_scouts=args.n_scouts),
        }
        _write_canary_report(prompt_output_dir, report)
        _print_report_paths(prompt_output_dir, report)
        return 0 if verdict["status"] in {"pass", "warn"} else 1
    finally:
        if restore_chat is not None:
            restore_chat()
        store.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-scouts", type=int, default=10)
    parser.add_argument("--cycle-id", default="")
    parser.add_argument("--db", default="")
    parser.add_argument("--artifact-dir", default="")
    parser.add_argument("--prompt-output-dir", default="")
    parser.add_argument("--seed-rng", type=int, default=20260522)
    parser.add_argument("--theme-share", type=float, default=0.20)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--cost-cap-usd", type=float, default=0.10)
    parser.add_argument("--self-healing-limit", type=int, default=6)
    parser.add_argument("--provider-timeout-s", type=float, default=45.0)
    parser.add_argument(
        "--preserve-db",
        action="store_true",
        help="Open the desk DB in append mode instead of resetting it; intended for always-on sentinel ticks.",
    )
    parser.add_argument(
        "--collect-price-observations",
        action="store_true",
        help="Collect Hyperliquid allMids before/after scouts and evaluate information strings against price.",
    )
    parser.add_argument("--price-source-timeout-s", type=float, default=8.0)
    parser.add_argument("--price-outcome-threshold-pct", type=float, default=0.02)
    parser.add_argument("--prompt-variant", default="", help="Optional forced scout prompt variant for this canary.")
    parser.add_argument("--max-tool-iterations", type=int, default=1)
    parser.add_argument(
        "--control-decision",
        default="",
        help="Cadence control decision that should shape the next scout slice.",
    )
    parser.add_argument(
        "--control-allowed-next-step",
        default="",
        help="Cadence allowed_next_step paired with --control-decision.",
    )
    parser.add_argument(
        "--perfusion-source-cycle-id",
        default="",
        help="Optional previous cycle id to read information_perfusion routes from.",
    )
    parser.add_argument(
        "--repair-tool-proposal-contracts",
        action="store_true",
        help=(
            "Run the proposal-contract repair loop after scouts/self-healing emit "
            "analysis_tool_proposals, before tournament evaluation."
        ),
    )
    parser.add_argument("--tool-proposal-repair-limit", type=int, default=500)
    parser.add_argument(
        "--ramp-policy",
        default="",
        help="Optional live_scout_ramp_policy.json from a prior learning report. Applies repair/work-order policy to this slice.",
    )
    parser.add_argument("--model", default="deepseek:v4-flash")
    parser.add_argument("--fallback", default="anthropic:claude-haiku-4-5")
    parser.add_argument(
        "--market-evolve-pairs",
        type=int,
        default=-1,
        help="Matched control/candidate policy pairs inside the scout slice. -1 chooses a sample-aware default.",
    )
    parser.add_argument(
        "--allow-live-spend",
        action="store_true",
        help="Actually call the configured live model provider under the cost cap.",
    )
    return parser.parse_args()


def _generate_control_aware_live_seeds(
    *,
    args: argparse.Namespace,
    cycle_id: str,
    conn: Any,
    universe_entities: list[str],
    base_seed_count: int,
) -> tuple[list[SeedCell], dict[str, Any]]:
    objective = _control_perfusion_objective(
        decision=str(args.control_decision or ""),
        allowed_next_step=str(args.control_allowed_next_step or ""),
    )
    perfusion_seeds: list[SeedCell] = []
    if objective:
        perfusion_seeds = generate_information_perfusion_seeds(
            cycle_id=cycle_id,
            n_seed_budget=base_seed_count,
            source_cycle_id=str(args.perfusion_source_cycle_id or "") or None,
            max_seeds=base_seed_count,
            replicate_recommended=True,
            route_objective=objective,
            conn=conn,
        )
    broad_needed = max(0, base_seed_count - len(perfusion_seeds))
    broad_seeds: list[SeedCell] = []
    if broad_needed:
        broad_seeds = generate_seeds(
            n_seeds=broad_needed,
            cycle_id=cycle_id,
            entities=universe_entities,
            themes=DEFAULT_THEMES,
            rng_seed=args.seed_rng,
            theme_share=args.theme_share,
        )
    seeds = [_prepare_live_seed(seed, args=args) for seed in [*perfusion_seeds, *broad_seeds]]
    report = {
        "schema_version": "talis_live_control_seed_routing_v1",
        "cycle_id": cycle_id,
        "control_decision": str(args.control_decision or ""),
        "control_allowed_next_step": str(args.control_allowed_next_step or ""),
        "perfusion_source_cycle_id": str(args.perfusion_source_cycle_id or ""),
        "route_objective": objective or "broad_market_grid",
        "requested_seed_count": int(base_seed_count),
        "perfusion_seed_count": len(perfusion_seeds),
        "broad_seed_count": len(broad_seeds),
        "status": "perfusion_routed" if perfusion_seeds else "broad_market_grid",
        "quality_flags": (
            []
            if not objective or perfusion_seeds
            else ["control_requested_perfusion_but_no_prior_perfusion_routes"]
        ),
    }
    return seeds, report


def _control_perfusion_objective(*, decision: str, allowed_next_step: str) -> str:
    text = f"{decision} {allowed_next_step}".lower()
    if "perfusion_latch" in text:
        return "latch_repair"
    if "source_oxygenation" in text or "oxygenation" in text:
        return "source_oxygenation"
    if "perfusion_pressure" in text:
        return "pressure"
    return ""


def _prepare_live_seed(seed: SeedCell, *, args: argparse.Namespace) -> SeedCell:
    seed = _prepare_seed(seed)
    payload = dict(seed.payload or {})
    if args.prompt_variant:
        payload["prompt_variant"] = args.prompt_variant
    payload["max_tool_iterations"] = max(0, int(args.max_tool_iterations or 0))
    payload["prompt_contract_pressure"] = "strict"
    payload["prompt_min_information_strings"] = max(2, int(payload.get("prompt_min_information_strings") or 0))
    payload["prompt_require_mechanism"] = True
    payload["prompt_require_kill_signal"] = True
    payload["prompt_require_evidence_refs"] = True
    seed.payload = payload
    return seed


def _force_live_seed_runtime_options(seeds: list[SeedCell], *, args: argparse.Namespace) -> None:
    for seed in seeds:
        payload = dict(seed.payload or {})
        if args.prompt_variant:
            payload["prompt_variant"] = args.prompt_variant
        payload["max_tool_iterations"] = max(0, int(args.max_tool_iterations or 0))
        payload["prompt_contract_pressure"] = "strict"
        payload["prompt_min_information_strings"] = max(2, int(payload.get("prompt_min_information_strings") or 0))
        payload["prompt_require_mechanism"] = True
        payload["prompt_require_kill_signal"] = True
        payload["prompt_require_evidence_refs"] = True
        seed.payload = payload


def _preflight(*, cycle_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "cycle_id": cycle_id,
        "tic_root_ok": False,
        "provider_import_ok": False,
        "tool_atlas_ok": False,
        "market_universe_ok": False,
    }
    try:
        root = get_tic_root()
        out["tic_root_ok"] = True
        out["tic_root"] = str(root)
    except Exception as exc:
        out["tic_root_error"] = f"{type(exc).__name__}: {exc}"
    try:
        ensure_tic_on_path()
        from tic.desk.models import chat as _chat  # type: ignore

        out["provider_import_ok"] = callable(_chat)
        out["provider_import"] = "tic.desk.models.chat"
    except Exception as exc:
        out["provider_import_error"] = f"{type(exc).__name__}: {exc}"
    try:
        atlas = regenerate_tool_atlas()
        out["tool_atlas_ok"] = bool(getattr(atlas, "n_tools", 0) or getattr(atlas, "n_sources", 0))
        out["tool_atlas"] = {
            "tools": getattr(atlas, "n_tools", 0),
            "sources": getattr(atlas, "n_sources", 0),
            "skills": getattr(atlas, "n_skills", 0),
        }
    except Exception as exc:
        out["tool_atlas_error"] = f"{type(exc).__name__}: {exc}"
    try:
        universe = build_market_universe(default_entities=DEFAULT_ENTITIES)
        out["market_universe_ok"] = bool(universe.entity_symbols())
        out["market_universe"] = {
            "entity_count": len(universe.entity_symbols()),
            "source_quality": getattr(universe, "source_quality", ""),
        }
    except Exception as exc:
        out["market_universe_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _collect_live_price_observations(
    *,
    enabled: bool,
    stage: str,
    prompt_output_dir: Path,
    seeds: list[SeedCell],
    conn: Any,
    timeout_s: float,
) -> dict[str, Any]:
    path = prompt_output_dir / f"live_price_observations_{stage}.json"
    if not enabled:
        payload = {
            "schema_version": "talis_live_price_observation_stage_v1",
            "stage": stage,
            "status": "skipped",
            "observed_count": 0,
            "quality_flags": ["price_observation_collection_disabled"],
        }
        _write_json(path, payload)
        return payload
    entities = _seed_entities(seeds)
    batch = collect_hyperliquid_mid_price_observations(
        entities,
        timeout_s=timeout_s,
    )
    persisted_ids = persist_price_observations(batch.observations, conn=conn)
    payload = {
        "schema_version": "talis_live_price_observation_stage_v1",
        "stage": stage,
        "status": "collected" if batch.observations else "empty",
        "observed_count": len(batch.observations),
        "persisted_count": len(persisted_ids),
        **batch.to_dict(),
    }
    _write_json(path, payload)
    return payload


def _evaluate_live_information_price_outcomes(
    *,
    enabled: bool,
    cycle_id: str,
    prompt_output_dir: Path,
    conn: Any,
    min_move_threshold_pct: float,
    limit: int,
) -> dict[str, Any]:
    path = prompt_output_dir / "information_price_outcomes.json"
    if not enabled:
        payload = {
            "schema_version": "information_price_outcome_report_v1",
            "cycle_id": cycle_id,
            "status": "skipped",
            "evaluated_count": 0,
            "summary": {},
            "quality_flags": ["price_observation_collection_disabled"],
        }
        _write_json(path, payload)
        return payload
    report = evaluate_information_price_outcomes(
        cycle_id=cycle_id,
        min_move_threshold_pct=min_move_threshold_pct,
        limit=limit,
        conn=conn,
    )
    report["status"] = "evaluated"
    _write_json(path, report)
    return report


def _price_observation_summary(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {
        "status": payload.get("status"),
        "source": payload.get("source"),
        "requested": len(payload.get("requested_entities") or []),
        "resolved": len(payload.get("resolved_entities") or []),
        "missing": len(payload.get("missing_entities") or []),
        "persisted_count": int(payload.get("persisted_count") or 0),
        "quality_flags": payload.get("quality_flags") or [],
    }


def _seed_entities(seeds: list[SeedCell]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for seed in seeds:
        entity = str(getattr(seed, "entity", "") or "").strip()
        key = entity.upper()
        if not entity or key in seen:
            continue
        seen.add(key)
        out.append(entity)
    return out


def _install_live_chat_recorder(
    transcript: dict[str, Any],
    *,
    timeout_s: float,
    progress_path: Path,
) -> Callable[[], None]:
    ensure_tic_on_path()
    from tic.desk import models as tic_models  # type: ignore

    real_chat = tic_models.chat

    async def recorded_chat(
        model: str,
        system: str,
        user: str,
        *,
        max_tokens: int,
        fallback: str | None = None,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        call: dict[str, Any] = {
            "index": len(transcript.setdefault("calls", [])),
            "model": model,
            "fallback": fallback,
            "max_tokens": max_tokens,
            "system_prompt": system,
            "user_prompt": user,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            res = await asyncio.wait_for(
                real_chat(model, system, user, max_tokens=max_tokens, fallback=fallback),
                timeout=max(1.0, timeout_s),
            )
        except Exception as primary_exc:
            call["primary_error"] = f"{type(primary_exc).__name__}: {primary_exc}"
            if fallback and fallback != model:
                try:
                    res = await asyncio.wait_for(
                        real_chat(fallback, system, user, max_tokens=max_tokens, fallback=None),
                        timeout=max(1.0, timeout_s),
                    )
                    call["fallback_after_primary_error"] = True
                except Exception as fallback_exc:
                    call["elapsed_s"] = round(time.perf_counter() - t0, 3)
                    call["error"] = f"{type(fallback_exc).__name__}: {fallback_exc}"
                    call["fallback_error"] = call["error"]
                    transcript.setdefault("calls", []).append(call)
                    _safe_write_progress(progress_path, transcript)
                    raise
            else:
                call["elapsed_s"] = round(time.perf_counter() - t0, 3)
                call["error"] = call["primary_error"]
                transcript.setdefault("calls", []).append(call)
                _safe_write_progress(progress_path, transcript)
                raise
        call["elapsed_s"] = round(time.perf_counter() - t0, 3)
        call["response_envelope"] = {k: v for k, v in res.items() if k != "text"}
        call["text"] = str(res.get("text") or "")
        transcript.setdefault("calls", []).append(call)
        _safe_write_progress(progress_path, transcript)
        return res

    tic_models.chat = recorded_chat

    def restore() -> None:
        tic_models.chat = real_chat

    return restore


def _write_primary_live_artifacts(
    *,
    prompt_output_dir: Path,
    seeds: list[SeedCell],
    scouts: list[Any],
    transcript: dict[str, Any],
) -> None:
    first_call = next((c for c in transcript.get("calls") or [] if isinstance(c, dict)), {})
    first_scout = next((s for s in scouts if not getattr(s, "error", None)), scouts[0] if scouts else None)
    first_seed = next(
        (seed for seed in seeds if first_scout is not None and seed.seed_id == getattr(first_scout, "seed_id", "")),
        seeds[0] if seeds else None,
    )
    system_prompt = str(first_call.get("system_prompt") or "")
    user_prompt = str(first_call.get("user_prompt") or "")
    if first_seed is not None and first_scout is not None:
        evidence = getattr(first_scout, "tool_evidence", []) or []
        if not user_prompt:
            user_prompt = _build_user_prompt(first_seed, tool_evidence=evidence)
        if not system_prompt:
            system_prompt = _apply_prompt_contract_pressure(
                build_deep_scout_system_prompt(getattr(first_scout, "prompt_variant", "") or "receptive_field_v1"),
                first_seed,
            )
    else:
        evidence = []
    response_text = str(first_call.get("text") or "")
    parsed_response: Any = None
    if response_text:
        parsed_response = _extract_first_json(response_text)
    response_envelope = {
        **{k: v for k, v in first_call.get("response_envelope", {}).items() if k != "text"},
        "text": response_text,
    }
    _write_text(prompt_output_dir / "live_scout_system_prompt.md", system_prompt)
    _write_text(prompt_output_dir / "live_scout_user_prompt.md", user_prompt)
    _write_json(prompt_output_dir / "live_scout_tool_evidence.json", evidence)
    _write_json(prompt_output_dir / "live_scout_model_output.json", parsed_response if parsed_response is not None else {"raw_text": response_text})
    _write_json(prompt_output_dir / "live_scout_model_response_envelope.json", response_envelope)
    _write_json(prompt_output_dir / "live_scout_persisted_output.json", _asdict(first_scout) if first_scout is not None else {})
    _write_json(prompt_output_dir / "live_scout_transcript.json", transcript)


def _write_live_prompt_preview(
    *,
    prompt_output_dir: Path,
    seeds: list[SeedCell],
) -> dict[str, Any]:
    seed = seeds[0] if seeds else None
    if seed is None:
        payload = {
            "schema_version": "talis_live_scout_prompt_preview_v1",
            "status": "no_seed_available",
        }
        _write_json(prompt_output_dir / "live_scout_prompt_preview.json", payload)
        return payload
    variant = _prompt_variant_for_seed(seed)
    system_prompt = _apply_prompt_contract_pressure(
        build_deep_scout_system_prompt(variant),
        seed,
    )
    user_prompt = _build_user_prompt(seed, tool_evidence=[])
    seed_payload = _seed_payload(seed)
    payload_dict = seed.payload if isinstance(seed.payload, dict) else {}
    tool_candidates = [
        str(x)
        for x in (payload_dict.get("tool_candidates") or [])
        if str(x).strip()
    ]
    preview = {
        "schema_version": "talis_live_scout_prompt_preview_v1",
        "status": "ready",
        "seed": seed_payload,
        "prompt_variant": variant,
        "prompt_contract_pressure": payload_dict.get("prompt_contract_pressure"),
        "minimum_information_strings": payload_dict.get("prompt_min_information_strings"),
        "market_evolve": {
            "program_id": payload_dict.get("market_evolve_program_id"),
            "program_name": payload_dict.get("market_evolve_program_name"),
            "generation": payload_dict.get("market_evolve_generation"),
            "experiment_id": payload_dict.get("market_evolve_experiment_id"),
            "experiment_arm": payload_dict.get("market_evolve_experiment_arm"),
            "applied": bool(payload_dict.get("market_evolve_applied")),
        },
        "tool_policy": {
            "allowed_tool_candidates": tool_candidates,
            "tool_candidate_count": len(tool_candidates),
            "max_evidence_tools": payload_dict.get("max_evidence_tools"),
            "max_tool_iterations": payload_dict.get("max_tool_iterations"),
            "source_family_targets": payload_dict.get("source_family_targets") or [],
            "prefer_learned_tools": bool(payload_dict.get("prefer_learned_tools")),
        },
        "learning_policy": {
            "policy_id": payload_dict.get("learning_policy_id"),
            "watch_metrics": payload_dict.get("learning_watch_metrics") or [],
            "repair_work_order_ids": payload_dict.get("learning_repair_work_order_ids") or [],
            "prompt_repair_modes": payload_dict.get("prompt_repair_modes") or [],
            "geometry_replication_targets": payload_dict.get("learning_geometry_replication_targets") or [],
        },
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "system_prompt_chars": len(system_prompt),
        "user_prompt_chars": len(user_prompt),
        "artifacts": {
            "json": str(prompt_output_dir / "live_scout_prompt_preview.json"),
            "system_prompt": str(prompt_output_dir / "live_scout_preview_system_prompt.md"),
            "user_prompt": str(prompt_output_dir / "live_scout_preview_user_prompt.md"),
        },
    }
    _write_text(prompt_output_dir / "live_scout_preview_system_prompt.md", system_prompt)
    _write_text(prompt_output_dir / "live_scout_preview_user_prompt.md", user_prompt)
    _write_json(prompt_output_dir / "live_scout_prompt_preview.json", preview)
    return preview


def _write_live_slice_preview(
    *,
    prompt_output_dir: Path,
    cycle_id: str,
    seeds: list[SeedCell],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    cell_counts: Counter[str] = Counter()
    distributions: dict[str, Counter[str]] = {
        "asset_class": Counter(),
        "horizon": Counter(),
        "lens": Counter(),
        "bias_mode": Counter(),
        "theme": Counter(),
        "prompt_variant": Counter(),
        "market_evolve_arm": Counter(),
        "source_family": Counter(),
    }
    tool_counts: list[int] = []
    for idx, seed in enumerate(seeds):
        payload = dict(seed.payload or {})
        tool_candidates = [
            str(x)
            for x in (payload.get("tool_candidates") or [])
            if str(x).strip()
        ]
        source_families = sorted({_source_family(uri) for uri in tool_candidates})
        for family in source_families:
            distributions["source_family"][family] += 1
        prompt_variant = _prompt_variant_for_seed(seed)
        arm = str(payload.get("market_evolve_experiment_arm") or "active")
        asset_class = str(_seed_payload(seed).get("asset_class") or "unknown")
        theme = str(seed.theme or "unassigned")
        cell_key = "|".join([
            str(seed.entity),
            str(seed.horizon),
            str(seed.lens),
            str(seed.bias_mode),
            theme,
        ])
        cell_counts[cell_key] += 1
        for key, value in (
            ("asset_class", asset_class),
            ("horizon", seed.horizon),
            ("lens", seed.lens),
            ("bias_mode", seed.bias_mode),
            ("theme", theme),
            ("prompt_variant", prompt_variant),
            ("market_evolve_arm", arm),
        ):
            distributions[key][str(value or "unknown")] += 1
        tool_counts.append(len(tool_candidates))
        rows.append({
            "index": idx,
            "seed_id": seed.seed_id,
            "entity": seed.entity,
            "asset_class": asset_class,
            "horizon": seed.horizon,
            "lens": seed.lens,
            "bias_mode": seed.bias_mode,
            "theme": seed.theme,
            "cell_key": cell_key,
            "weight": seed.weight,
            "coverage_penalty": seed.coverage_penalty,
            "frontier_boost": seed.frontier_boost,
            "prompt_variant": prompt_variant,
            "prompt_contract_pressure": payload.get("prompt_contract_pressure"),
            "minimum_information_strings": payload.get("prompt_min_information_strings"),
            "max_evidence_tools": payload.get("max_evidence_tools"),
            "max_tool_iterations": payload.get("max_tool_iterations"),
            "tool_candidate_count": len(tool_candidates),
            "allowed_tool_candidates_head": tool_candidates[:8],
            "source_families": source_families,
            "source_family_targets": [
                str(x)
                for x in (payload.get("source_family_targets") or [])
                if str(x).strip()
            ],
            "market_evolve": {
                "program_id": payload.get("market_evolve_program_id"),
                "program_name": payload.get("market_evolve_program_name"),
                "generation": payload.get("market_evolve_generation"),
                "experiment_id": payload.get("market_evolve_experiment_id"),
                "experiment_arm": payload.get("market_evolve_experiment_arm"),
                "pair_id": payload.get("market_evolve_pair_id"),
                "applied": bool(payload.get("market_evolve_applied")),
            },
            "learning_policy": {
                "policy_id": payload.get("learning_policy_id"),
                "watch_metrics": payload.get("learning_watch_metrics") or [],
                "repair_work_order_ids": payload.get("learning_repair_work_order_ids") or [],
                "geometry_replication_targets": payload.get("learning_geometry_replication_targets") or [],
            },
        })
    duplicate_cells = {
        key: count
        for key, count in cell_counts.items()
        if count > 1
    }
    preview = {
        "schema_version": "talis_live_scout_slice_preview_v1",
        "status": "ready" if rows else "no_seed_available",
        "cycle_id": cycle_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_scouts": len(rows),
        "unique_cell_count": len(cell_counts),
        "duplicate_cell_count": sum(count - 1 for count in cell_counts.values() if count > 1),
        "duplicate_cells": duplicate_cells,
        "tool_candidate_count_stats": _int_stats(tool_counts),
        "distributions": {
            key: dict(counter.most_common())
            for key, counter in distributions.items()
        },
        "seed_rows": rows,
        "artifacts": {
            "json": str(prompt_output_dir / "live_scout_slice_preview.json"),
            "seeds": str(prompt_output_dir / "live_scout_canary_seeds.json"),
        },
    }
    _write_json(prompt_output_dir / "live_scout_slice_preview.json", preview)
    return preview


def _tool_creation_contract_repair_report(
    *,
    cycle_id: str,
    conn: Any,
    enabled: bool,
    limit: int,
) -> dict[str, Any]:
    before_rows = load_analysis_tool_proposals(
        cycle_id=cycle_id,
        limit=max(1, int(limit)),
        conn=conn,
    )
    before = _tool_proposal_contract_metrics(before_rows)
    repairs = []
    if enabled:
        repairs = repair_low_quality_analysis_tool_proposals(
            cycle_id=cycle_id,
            limit=max(1, int(limit)),
            conn=conn,
        )
    after_rows = load_analysis_tool_proposals(
        cycle_id=cycle_id,
        limit=max(1, int(limit) * 2),
        conn=conn,
    )
    after = _tool_proposal_contract_metrics(after_rows)
    gates = _tool_contract_repair_gates(after)
    failed = [name for name, ok in gates.items() if not ok]
    required = before.get("proposal_count", 0) > 0
    return {
        "schema_version": "talis_tool_creation_contract_repair_v1",
        "cycle_id": cycle_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "enabled": bool(enabled),
        "required": required,
        "limit": max(1, int(limit)),
        "status": "pass" if (not required or not failed) else "blocked",
        "before": before,
        "after": after,
        "repairs_created": len(repairs),
        "repairs": [
            {
                "parent_proposal_id": r.parent_proposal_id,
                "repaired_proposal_id": r.repaired_proposal_id,
                "tool_name": r.tool_name,
                "previous_score": r.previous_score,
                "repaired_score": r.repaired_score,
                "status": r.status,
                "quality_flags": r.quality_flags,
            }
            for r in repairs
        ],
        "gates": gates,
        "failed_gates": failed,
        "quality_flags": (
            []
            if enabled or not required else
            ["tool_creation_contract_repair_available_but_not_enabled"]
        ),
    }


def _tool_proposal_contract_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    frontier = _frontier_tool_proposal_rows(rows)
    status_counts = dict(Counter(str(row.get("status") or "unknown") for row in rows))
    tool_counts = dict(Counter(str(row.get("tool_name") or "unknown") for row in rows))
    pass_count = 0
    eval_plan_count = 0
    promotion_gate_count = 0
    expected_edge_count = 0
    would_change_count = 0
    scores: list[float] = []
    flags: Counter[str] = Counter()
    for row in frontier:
        promotion_gate = row.get("promotion_gate_json")
        if not isinstance(promotion_gate, dict):
            promotion_gate = {}
        eval_plan = row.get("eval_plan_json")
        if not isinstance(eval_plan, dict):
            eval_plan = {}
        if eval_plan:
            eval_plan_count += 1
        if promotion_gate:
            promotion_gate_count += 1
        if str(promotion_gate.get("expected_edge") or "").strip():
            expected_edge_count += 1
        if promotion_gate.get("would_change_decision") is True:
            would_change_count += 1
        quality = evaluate_analysis_tool_proposal(row)
        scores.append(float(quality.score))
        if quality.passed:
            pass_count += 1
        flags.update(quality.flags)
    n = len(frontier)
    return {
        "proposal_count": len(rows),
        "frontier_proposal_count": n,
        "status_counts": status_counts,
        "top_tools": [
            {"tool_name": name, "count": count}
            for name, count in Counter(tool_counts).most_common(10)
        ],
        "metrics": {
            "avg_quality_score": round(sum(scores) / max(1, len(scores)), 4),
            "quality_pass_rate": round(pass_count / max(1, n), 4),
            "eval_plan_rate": round(eval_plan_count / max(1, n), 4),
            "promotion_gate_rate": round(promotion_gate_count / max(1, n), 4),
            "expected_edge_rate": round(expected_edge_count / max(1, n), 4),
            "would_change_decision_rate": round(would_change_count / max(1, n), 4),
        },
        "top_quality_flags": [
            {"flag": name, "count": count}
            for name, count in flags.most_common(10)
        ],
    }


def _frontier_tool_proposal_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parent_ids = {
        str(row.get("parent_proposal_id") or "")
        for row in rows
        if str(row.get("parent_proposal_id") or "").strip()
    }
    return [
        row for row in rows
        if str(row.get("id") or "") not in parent_ids
        and str(row.get("status") or "").lower() != "superseded"
    ]


def _tool_contract_repair_gates(metrics: dict[str, Any]) -> dict[str, bool]:
    n = int(metrics.get("frontier_proposal_count") or 0)
    values = metrics.get("metrics") if isinstance(metrics.get("metrics"), dict) else {}
    if n <= 0:
        return {}
    return {
        "tool_contract_frontier_quality_ge_0_70": _to_float(values.get("quality_pass_rate")) >= 0.70,
        "tool_contract_eval_plan_rate_ge_0_85": _to_float(values.get("eval_plan_rate")) >= 0.85,
        "tool_contract_expected_edge_rate_ge_0_60": _to_float(values.get("expected_edge_rate")) >= 0.60,
        "tool_contract_would_change_decision_rate_ge_0_60": _to_float(values.get("would_change_decision_rate")) >= 0.60,
    }


def _live_canary_verdict(
    metrics: dict[str, Any],
    *,
    cost_cap_usd: float,
    n_scouts: int,
    transcript_summary: dict[str, Any],
) -> dict[str, Any]:
    scouts = metrics.get("scouts") if isinstance(metrics.get("scouts"), dict) else {}
    info = metrics.get("information_map") if isinstance(metrics.get("information_map"), dict) else {}
    geometry = metrics.get("geometry") if isinstance(metrics.get("geometry"), dict) else {}
    self_healing = metrics.get("self_healing") if isinstance(metrics.get("self_healing"), dict) else {}
    cost = _to_float(scouts.get("total_cost_usd_estimate"))
    flags = scouts.get("top_quality_flags") if isinstance(scouts.get("top_quality_flags"), dict) else {}
    provider_error_count = sum(
        int(v)
        for k, v in flags.items()
        if "provider" in str(k).lower() or "json_unparseable" in str(k).lower()
    )
    scout_error_count = int(scouts.get("errored") or 0)
    sample_n = int(n_scouts or 0)
    stage_error_budget = max(1, int(sample_n * 0.02))
    gates = {
        "sample_size_ge_10": sample_n >= 10,
        "cost_below_cap": cost <= max(0.0, cost_cap_usd),
        "provider_call_errors_eq_0": len(transcript_summary.get("errors") or []) == 0,
        "scout_success_rate_ge_0_70": _to_float(scouts.get("success_rate")) >= 0.70,
        "avg_strings_ge_1_00": _to_float(scouts.get("avg_information_strings_per_scout")) >= 1.00,
        "evidence_ok_rate_ge_0_60": _to_float(scouts.get("evidence_ok_rate")) >= 0.60,
        "duplicate_hypothesis_rate_le_0_35": _to_float(scouts.get("duplicate_hypothesis_rate"), default=1.0) <= 0.35,
        "provider_json_errors_within_stage_budget": provider_error_count <= stage_error_budget,
        "scout_errors_within_stage_budget": scout_error_count <= stage_error_budget,
        "information_strings_created": int(info.get("string_count") or 0) >= 1,
        "synthesis_promoted": int(info.get("promoted_hypotheses") or 0) >= 1,
        "geometry_cells_created": int(geometry.get("cell_count") or 0) >= 1,
        "self_healing_no_failures": int(self_healing.get("failed_tasks") or 0) == 0,
    }
    failed = [name for name, ok in gates.items() if not ok]
    status = "pass" if not failed else "warn" if len(failed) <= 2 else "fail"
    ready_for_next = status == "pass" and sample_n >= 10
    ready_for_100 = ready_for_next and sample_n < 100
    ready_for_tournament = ready_for_next and sample_n >= 100
    if ready_for_next and sample_n >= 1000:
        interpretation = (
            "The 1,000-scout live shadow candidate has clean raw canary gates. "
            "Run the tournament evaluator next; only the tournament can promote it "
            "to a repeat shadow trial, and scheduled production stays blocked until "
            "repeatability is proven."
        )
    elif ready_for_tournament:
        interpretation = (
            "The 100-scout live ramp is clean. Run the tournament evaluator before "
            "any 1,000-scout spend."
        )
    elif ready_for_100:
        interpretation = (
            "The live provider canary is clean. Run a 100-scout live ramp next, "
            "then promote to 1,000 only if the same quality curve holds."
        )
    elif status in {"pass", "warn"} and sample_n < 10:
        interpretation = "This was a useful live smoke, but not enough to open the next spend gate."
    else:
        interpretation = "The live canary found provider/data-quality issues. Fix these before increasing spend."
    return {
        "status": status,
        "ready_for_next_live_100": ready_for_100,
        "ready_for_live_1000_tournament": ready_for_tournament,
        "ready_for_direct_live_1000": False,
        "gates": gates,
        "failed_gates": failed,
        "interpretation": interpretation,
    }


def _blocked_report(
    *,
    args: argparse.Namespace,
    cycle_id: str,
    db_path: Path,
    artifact_dir: Path,
    prompt_output_dir: Path,
    seed_path: Path,
    prompt_preview: dict[str, Any],
    slice_preview: dict[str, Any],
    preflight: dict[str, Any],
    ramp_policy: dict[str, Any],
    ramp_policy_application: dict[str, Any],
    ramp_policy_rehearsal: dict[str, Any],
    price_observation_start: dict[str, Any],
    reason: str,
    elapsed_s: float,
) -> dict[str, Any]:
    gates = {
        "tic_root_ok": bool(preflight.get("tic_root_ok")),
        "provider_import_ok": bool(preflight.get("provider_import_ok")),
        "tool_atlas_ok": bool(preflight.get("tool_atlas_ok")),
        "market_universe_ok": bool(preflight.get("market_universe_ok")),
        "explicit_live_spend_allowed": bool(args.allow_live_spend),
    }
    if ramp_policy:
        gates["ramp_policy_rehearsal_ok"] = ramp_policy_rehearsal.get("status") == "pass"
    return {
        "schema_version": "talis_live_scout_canary_v1",
        "mode": "preflight_no_live_spend",
        "cycle_id": cycle_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(db_path),
        "artifact_dir": str(artifact_dir),
        "prompt_output_dir": str(prompt_output_dir),
        "n_scouts_requested": args.n_scouts,
        "seed_rng": args.seed_rng,
        "model": args.model,
        "fallback": args.fallback,
        "concurrency": args.concurrency,
        "cost_cap_usd": args.cost_cap_usd,
        "provider_timeout_s": args.provider_timeout_s,
        "prompt_variant_override": args.prompt_variant,
        "max_tool_iterations": args.max_tool_iterations,
        "preserve_db": args.preserve_db,
        "collect_price_observations": args.collect_price_observations,
        "repair_tool_proposal_contracts": args.repair_tool_proposal_contracts,
        "tool_proposal_repair_limit": args.tool_proposal_repair_limit,
        "ramp_policy_path": args.ramp_policy,
        "ramp_policy": ramp_policy,
        "ramp_policy_application": ramp_policy_application,
        "ramp_policy_rehearsal": ramp_policy_rehearsal,
        "price_observations": {
            "start": price_observation_start,
            "final": {},
        },
        "preflight": preflight,
        "prompt_preview": prompt_preview,
        "slice_preview": slice_preview,
        "verdict": {
            "status": "blocked",
            "reason": reason,
            "ready_for_next_live_100": False,
            "ready_for_direct_live_1000": False,
            "gates": gates,
            "failed_gates": [name for name, ok in gates.items() if not ok],
            "interpretation": "No live model calls were made. This is an anti-waste preflight artifact.",
        },
        "metrics": {
            "elapsed_s": round(elapsed_s, 3),
            "price_observations": {
                "start": _price_observation_summary(price_observation_start),
                "final": {},
            },
        },
        "artifacts": {
            "seeds": str(seed_path),
            "prompt_preview": str(prompt_output_dir / "live_scout_prompt_preview.json"),
            "slice_preview": str(prompt_output_dir / "live_scout_slice_preview.json"),
            "price_observations_start": str(prompt_output_dir / "live_price_observations_start.json"),
            "ramp_policy_rehearsal": str(prompt_output_dir / "live_scout_ramp_policy_rehearsal.json"),
            "preview_system_prompt": str(prompt_output_dir / "live_scout_preview_system_prompt.md"),
            "preview_user_prompt": str(prompt_output_dir / "live_scout_preview_user_prompt.md"),
        },
        "scale_decision": {
            "decision": "do_not_scale_yet",
            "next_step": "Run this same script with --allow-live-spend under a tiny cost cap, then inspect the live canary gates.",
        },
    }


def _scale_decision(verdict: dict[str, Any], *, n_scouts: int) -> dict[str, Any]:
    if verdict.get("ready_for_live_1000_tournament") and int(n_scouts or 0) >= 1000:
        return {
            "decision": "evaluate_shadow_production_trial",
            "next_step": (
                "Run the live scout tournament evaluator over this 1,000-scout report. "
                "Promote only to a repeat 1,000-scout shadow trial if scale gates pass; "
                "scheduled production requires repeatability across independent 1,000-scout runs."
            ),
            "why_not_scheduled_production": (
                "A single 1,000-scout pass proves scale shape, not operational repeatability. "
                "The next proof is a second independent 1,000-scout shadow run with stable "
                "provider reliability, prompt structure, geometry, and coverage deltas."
            ),
        }
    if verdict.get("ready_for_live_1000_tournament"):
        return {
            "decision": "evaluate_live_1000_ramp_next",
            "next_step": "Run the live scout tournament evaluator over the 100-scout report. Promote to 1,000 only if the distribution gates pass.",
            "why_not_direct_1000": "A 100-scout ramp proves a broader distribution than the 10-scout canary, but the tournament still needs to check provider reliability, redundancy, prompt quality, temporal structure, and geometry before a 1,000-scout run.",
        }
    if verdict.get("ready_for_next_live_100"):
        return {
            "decision": "run_live_100_ramp_next",
            "next_step": "Run 100 live scouts with the same prompt-output capture and stop immediately if canary gates regress.",
            "why_not_direct_1000": f"A {n_scouts}-scout canary proves provider compatibility and quality shape, but the next paid step should validate distributional stability at 100.",
        }
    if verdict.get("status") in {"pass", "warn"} and int(n_scouts or 0) < 10:
        return {
            "decision": "finish_10_scout_live_canary",
            "next_step": "Run the full 10-scout live canary with the same hard cap and transcript capture before a 100-scout ramp.",
        }
    return {
        "decision": "do_not_scale_yet",
        "next_step": "Inspect failed live canary gates, repair prompt/tool/data failures, and repeat the 10-scout canary.",
    }


def _transcript_summary(transcript: dict[str, Any]) -> dict[str, Any]:
    calls = [c for c in transcript.get("calls") or [] if isinstance(c, dict)]
    return {
        "call_count": len(calls),
        "models": sorted({str(c.get("model") or "") for c in calls if c.get("model")}),
        "providers": sorted({
            str((c.get("response_envelope") or {}).get("provider") or "")
            for c in calls
            if isinstance(c.get("response_envelope"), dict) and (c.get("response_envelope") or {}).get("provider")
        }),
        "errors": [str(c.get("error")) for c in calls if c.get("error")],
        "prompt_chars": sum(len(str(c.get("system_prompt") or "")) + len(str(c.get("user_prompt") or "")) for c in calls),
        "response_chars": sum(len(str(c.get("text") or "")) for c in calls),
    }


def _write_canary_report(prompt_output_dir: Path, report: dict[str, Any]) -> None:
    _write_json(prompt_output_dir / "live_scout_canary_report.json", report)
    _write_text(prompt_output_dir / "live_scout_canary_report.md", _render_markdown(report))


def _safe_write_progress(path: Path, transcript: dict[str, Any]) -> None:
    try:
        _write_json(path, transcript)
    except Exception:
        pass


def _render_markdown(report: dict[str, Any]) -> str:
    verdict = report.get("verdict") if isinstance(report.get("verdict"), dict) else {}
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    scouts = metrics.get("scouts") if isinstance(metrics.get("scouts"), dict) else {}
    info = metrics.get("information_map") if isinstance(metrics.get("information_map"), dict) else {}
    repair = (
        report.get("tool_creation_contract_repair")
        if isinstance(report.get("tool_creation_contract_repair"), dict)
        else metrics.get("tool_creation_contract_repair")
        if isinstance(metrics.get("tool_creation_contract_repair"), dict)
        else {}
    )
    repair_after = repair.get("after") if isinstance(repair.get("after"), dict) else {}
    repair_after_metrics = repair_after.get("metrics") if isinstance(repair_after.get("metrics"), dict) else {}
    lines = [
        "# Live Scout Canary",
        "",
        f"- status: `{verdict.get('status')}`",
        f"- mode: `{report.get('mode')}`",
        f"- cycle: `{report.get('cycle_id')}`",
        f"- scouts: `{report.get('n_scouts_requested')}`",
        f"- cost_cap_usd: `{report.get('cost_cap_usd')}`",
        f"- estimated_cost_usd: `{scouts.get('total_cost_usd_estimate', 0)}`",
        f"- success_rate: `{scouts.get('success_rate', 0)}`",
        f"- avg_strings_per_scout: `{scouts.get('avg_information_strings_per_scout', 0)}`",
        f"- information_strings: `{info.get('string_count', 0)}`",
        "",
        "## Gates",
        "",
    ]
    for name, ok in (verdict.get("gates") or {}).items():
        lines.append(f"- {'PASS' if ok else 'FAIL'} `{name}`")
    if repair:
        lines.extend([
            "",
            "## Tool Creation Contract Repair",
            "",
            f"- enabled: `{repair.get('enabled')}`",
            f"- status: `{repair.get('status')}`",
            f"- repairs_created: `{repair.get('repairs_created')}`",
            f"- frontier_proposals: `{repair_after.get('frontier_proposal_count')}`",
            f"- quality_pass_rate: `{repair_after_metrics.get('quality_pass_rate')}`",
            f"- eval_plan_rate: `{repair_after_metrics.get('eval_plan_rate')}`",
            f"- expected_edge_rate: `{repair_after_metrics.get('expected_edge_rate')}`",
        ])
        for name, ok in (repair.get("gates") or {}).items():
            lines.append(f"- {'PASS' if ok else 'FAIL'} `{name}`")
    lines.extend([
        "",
        "## Decision",
        "",
        str((report.get("scale_decision") or {}).get("decision") or ""),
        "",
        str((report.get("scale_decision") or {}).get("next_step") or ""),
    ])
    return "\n".join(lines) + "\n"


def _print_report_paths(prompt_output_dir: Path, report: dict[str, Any]) -> None:
    verdict = report.get("verdict") if isinstance(report.get("verdict"), dict) else {}
    print(f"LIVE_CANARY_STATUS={verdict.get('status')}")
    print(f"LIVE_CANARY_READY_FOR_NEXT_100={verdict.get('ready_for_next_live_100')}")
    print(f"LIVE_CANARY_REPORT_JSON={prompt_output_dir / 'live_scout_canary_report.json'}")
    print(f"LIVE_CANARY_REPORT_MD={prompt_output_dir / 'live_scout_canary_report.md'}")
    print(f"LIVE_CANARY_PROMPT_OUTPUT_DIR={prompt_output_dir}")


def _artifact_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path(tempfile.gettempdir()) / f"talis-live-scout-canary-{stamp}"
    root.mkdir(parents=True, exist_ok=False)
    return root


def _to_float(raw: Any, *, default: float = 0.0) -> float:
    try:
        return float(raw)
    except Exception:
        return default


def _int_stats(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"min": 0, "max": 0, "avg": 0.0}
    return {
        "min": min(values),
        "max": max(values),
        "avg": round(sum(values) / max(1, len(values)), 3),
    }


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


if __name__ == "__main__":
    raise SystemExit(main())
