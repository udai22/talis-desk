#!/usr/bin/env python
"""Evaluate live scout canary reports as a prompt/provider tournament.

This is the anti-waste gate between a tiny live canary and a wider 100/1,000
scout run. It does not spend model calls. It reads completed canary artifacts,
scores the prompt/provider policy, and writes a promotion decision the viewer
can render for phone inspection.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shlex
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_THRESHOLDS = {
    "min_scouts": 10,
    "min_success_rate": 0.70,
    "max_provider_error_rate": 0.10,
    "max_duplicate_rate": 0.35,
    "min_strings_per_scout": 1.00,
    "min_evidence_ok_rate": 0.60,
    "max_cost_per_scout_usd": 0.008,
    "max_avg_latency_s": 45.0,
    "max_structural_flag_rate": 0.20,
    "min_avg_prompt_quality": 0.70,
    "max_low_prompt_quality_rate": 0.20,
    "max_tool_error_rate": 0.02,
    "distribution_min_scouts": 100,
    "distribution_min_success_rate": 0.90,
    "distribution_max_provider_error_rate": 0.02,
    "distribution_max_duplicate_rate": 0.20,
    "distribution_max_structural_flag_rate": 0.10,
    "distribution_max_tool_error_rate": 0.02,
    "distribution_min_geometry_cells": 50,
    "distribution_min_market_evolve_pairs": 20,
    "scale_min_scouts": 1000,
    "scale_min_success_rate": 0.90,
    "scale_max_provider_error_rate": 0.02,
    "scale_max_duplicate_rate": 0.20,
    "scale_max_structural_flag_rate": 0.10,
    "scale_max_tool_error_rate": 0.02,
    "scale_min_geometry_cells": 500,
    "scale_min_information_strings": 1000,
    "scale_min_market_evolve_pairs": 20,
    "production_min_shadow_runs": 2,
    "production_max_success_rate_delta": 0.05,
    "production_max_duplicate_rate_delta": 0.08,
    "production_max_structural_flag_rate_delta": 0.04,
    "production_min_geometry_cell_ratio": 0.80,
    "production_min_information_string_ratio": 0.80,
    "ramp_policy_rehearsal_min_tool_candidate_refresh_rate": 0.95,
    "ramp_policy_rehearsal_min_source_target_coverage_rate": 0.60,
    "ramp_policy_rehearsal_min_policy_attached_rate": 1.00,
    "ramp_policy_rehearsal_min_repair_ids_attached_rate": 1.00,
    "ramp_policy_rehearsal_min_watch_metrics_attached_rate": 1.00,
    "ramp_policy_rehearsal_min_strict_contract_rate": 1.00,
    "ramp_policy_rehearsal_max_over_limit_count": 0,
    "tool_creation_min_quality_pass_rate": 0.70,
    "tool_creation_min_eval_plan_rate": 0.85,
    "tool_creation_min_expected_edge_rate": 0.60,
    "tool_creation_min_would_change_decision_rate": 0.60,
    "tool_creation_max_eval_failed_rate": 0.25,
    "tool_creation_max_runtime_adapter_backlog_rate": 0.50,
}


def main() -> int:
    args = _parse_args()
    thresholds = {
        **DEFAULT_THRESHOLDS,
        "min_scouts": args.min_scouts,
        "min_success_rate": args.min_success_rate,
        "max_provider_error_rate": args.max_provider_error_rate,
        "max_duplicate_rate": args.max_duplicate_rate,
        "min_strings_per_scout": args.min_strings_per_scout,
        "max_avg_latency_s": args.max_avg_latency_s,
        "max_structural_flag_rate": args.max_structural_flag_rate,
        "min_avg_prompt_quality": args.min_avg_prompt_quality,
        "max_low_prompt_quality_rate": args.max_low_prompt_quality_rate,
        "max_tool_error_rate": args.max_tool_error_rate,
        "distribution_min_scouts": args.distribution_min_scouts,
        "distribution_min_success_rate": args.distribution_min_success_rate,
        "distribution_max_provider_error_rate": args.distribution_max_provider_error_rate,
        "distribution_max_duplicate_rate": args.distribution_max_duplicate_rate,
        "distribution_max_structural_flag_rate": args.distribution_max_structural_flag_rate,
        "distribution_max_tool_error_rate": args.distribution_max_tool_error_rate,
        "distribution_min_geometry_cells": args.distribution_min_geometry_cells,
        "distribution_min_market_evolve_pairs": args.distribution_min_market_evolve_pairs,
        "scale_min_scouts": args.scale_min_scouts,
        "scale_min_success_rate": args.scale_min_success_rate,
        "scale_max_provider_error_rate": args.scale_max_provider_error_rate,
        "scale_max_duplicate_rate": args.scale_max_duplicate_rate,
        "scale_max_structural_flag_rate": args.scale_max_structural_flag_rate,
        "scale_max_tool_error_rate": args.scale_max_tool_error_rate,
        "scale_min_geometry_cells": args.scale_min_geometry_cells,
        "scale_min_information_strings": args.scale_min_information_strings,
        "scale_min_market_evolve_pairs": args.scale_min_market_evolve_pairs,
        "production_min_shadow_runs": args.production_min_shadow_runs,
        "production_max_success_rate_delta": args.production_max_success_rate_delta,
        "production_max_duplicate_rate_delta": args.production_max_duplicate_rate_delta,
        "production_max_structural_flag_rate_delta": args.production_max_structural_flag_rate_delta,
        "production_min_geometry_cell_ratio": args.production_min_geometry_cell_ratio,
        "production_min_information_string_ratio": args.production_min_information_string_ratio,
        "ramp_policy_rehearsal_min_tool_candidate_refresh_rate": args.ramp_policy_rehearsal_min_tool_candidate_refresh_rate,
        "ramp_policy_rehearsal_min_source_target_coverage_rate": args.ramp_policy_rehearsal_min_source_target_coverage_rate,
        "ramp_policy_rehearsal_min_policy_attached_rate": args.ramp_policy_rehearsal_min_policy_attached_rate,
        "ramp_policy_rehearsal_min_repair_ids_attached_rate": args.ramp_policy_rehearsal_min_repair_ids_attached_rate,
        "ramp_policy_rehearsal_min_watch_metrics_attached_rate": args.ramp_policy_rehearsal_min_watch_metrics_attached_rate,
        "ramp_policy_rehearsal_min_strict_contract_rate": args.ramp_policy_rehearsal_min_strict_contract_rate,
        "ramp_policy_rehearsal_max_over_limit_count": args.ramp_policy_rehearsal_max_over_limit_count,
        "tool_creation_min_quality_pass_rate": args.tool_creation_min_quality_pass_rate,
        "tool_creation_min_eval_plan_rate": args.tool_creation_min_eval_plan_rate,
        "tool_creation_min_expected_edge_rate": args.tool_creation_min_expected_edge_rate,
        "tool_creation_min_would_change_decision_rate": args.tool_creation_min_would_change_decision_rate,
        "tool_creation_max_eval_failed_rate": args.tool_creation_max_eval_failed_rate,
        "tool_creation_max_runtime_adapter_backlog_rate": args.tool_creation_max_runtime_adapter_backlog_rate,
    }
    report_paths = [Path(p).expanduser().resolve() for p in args.reports]
    tournament = evaluate_live_scout_tournament(report_paths, thresholds=thresholds)
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else _default_output_dir(report_paths)
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "live_scout_tournament_report.json"
    md_path = output_dir / "live_scout_tournament_report.md"
    _write_json(json_path, tournament)
    _write_text(md_path, render_tournament_markdown(tournament))
    print(f"LIVE_SCOUT_TOURNAMENT_DECISION={tournament['promotion_decision']['decision']}")
    print(f"LIVE_SCOUT_TOURNAMENT_READY_FOR_100={tournament['promotion_decision']['ready_for_live_100']}")
    print(f"LIVE_SCOUT_TOURNAMENT_REPORT_JSON={json_path}")
    print(f"LIVE_SCOUT_TOURNAMENT_REPORT_MD={md_path}")
    return 0 if tournament["promotion_decision"]["ready_for_live_100"] else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", help="One or more live_scout_canary_report.json files.")
    parser.add_argument("--output-dir", default="", help="Directory for tournament report artifacts.")
    parser.add_argument("--min-scouts", type=int, default=DEFAULT_THRESHOLDS["min_scouts"])
    parser.add_argument("--min-success-rate", type=float, default=DEFAULT_THRESHOLDS["min_success_rate"])
    parser.add_argument("--max-provider-error-rate", type=float, default=DEFAULT_THRESHOLDS["max_provider_error_rate"])
    parser.add_argument("--max-duplicate-rate", type=float, default=DEFAULT_THRESHOLDS["max_duplicate_rate"])
    parser.add_argument("--min-strings-per-scout", type=float, default=DEFAULT_THRESHOLDS["min_strings_per_scout"])
    parser.add_argument("--max-avg-latency-s", type=float, default=DEFAULT_THRESHOLDS["max_avg_latency_s"])
    parser.add_argument("--max-structural-flag-rate", type=float, default=DEFAULT_THRESHOLDS["max_structural_flag_rate"])
    parser.add_argument("--min-avg-prompt-quality", type=float, default=DEFAULT_THRESHOLDS["min_avg_prompt_quality"])
    parser.add_argument("--max-low-prompt-quality-rate", type=float, default=DEFAULT_THRESHOLDS["max_low_prompt_quality_rate"])
    parser.add_argument("--max-tool-error-rate", type=float, default=DEFAULT_THRESHOLDS["max_tool_error_rate"])
    parser.add_argument("--distribution-min-scouts", type=int, default=DEFAULT_THRESHOLDS["distribution_min_scouts"])
    parser.add_argument("--distribution-min-success-rate", type=float, default=DEFAULT_THRESHOLDS["distribution_min_success_rate"])
    parser.add_argument("--distribution-max-provider-error-rate", type=float, default=DEFAULT_THRESHOLDS["distribution_max_provider_error_rate"])
    parser.add_argument("--distribution-max-duplicate-rate", type=float, default=DEFAULT_THRESHOLDS["distribution_max_duplicate_rate"])
    parser.add_argument("--distribution-max-structural-flag-rate", type=float, default=DEFAULT_THRESHOLDS["distribution_max_structural_flag_rate"])
    parser.add_argument("--distribution-max-tool-error-rate", type=float, default=DEFAULT_THRESHOLDS["distribution_max_tool_error_rate"])
    parser.add_argument("--distribution-min-geometry-cells", type=int, default=DEFAULT_THRESHOLDS["distribution_min_geometry_cells"])
    parser.add_argument("--distribution-min-market-evolve-pairs", type=int, default=DEFAULT_THRESHOLDS["distribution_min_market_evolve_pairs"])
    parser.add_argument("--scale-min-scouts", type=int, default=DEFAULT_THRESHOLDS["scale_min_scouts"])
    parser.add_argument("--scale-min-success-rate", type=float, default=DEFAULT_THRESHOLDS["scale_min_success_rate"])
    parser.add_argument("--scale-max-provider-error-rate", type=float, default=DEFAULT_THRESHOLDS["scale_max_provider_error_rate"])
    parser.add_argument("--scale-max-duplicate-rate", type=float, default=DEFAULT_THRESHOLDS["scale_max_duplicate_rate"])
    parser.add_argument("--scale-max-structural-flag-rate", type=float, default=DEFAULT_THRESHOLDS["scale_max_structural_flag_rate"])
    parser.add_argument("--scale-max-tool-error-rate", type=float, default=DEFAULT_THRESHOLDS["scale_max_tool_error_rate"])
    parser.add_argument("--scale-min-geometry-cells", type=int, default=DEFAULT_THRESHOLDS["scale_min_geometry_cells"])
    parser.add_argument("--scale-min-information-strings", type=int, default=DEFAULT_THRESHOLDS["scale_min_information_strings"])
    parser.add_argument("--scale-min-market-evolve-pairs", type=int, default=DEFAULT_THRESHOLDS["scale_min_market_evolve_pairs"])
    parser.add_argument("--production-min-shadow-runs", type=int, default=DEFAULT_THRESHOLDS["production_min_shadow_runs"])
    parser.add_argument("--production-max-success-rate-delta", type=float, default=DEFAULT_THRESHOLDS["production_max_success_rate_delta"])
    parser.add_argument("--production-max-duplicate-rate-delta", type=float, default=DEFAULT_THRESHOLDS["production_max_duplicate_rate_delta"])
    parser.add_argument("--production-max-structural-flag-rate-delta", type=float, default=DEFAULT_THRESHOLDS["production_max_structural_flag_rate_delta"])
    parser.add_argument("--production-min-geometry-cell-ratio", type=float, default=DEFAULT_THRESHOLDS["production_min_geometry_cell_ratio"])
    parser.add_argument("--production-min-information-string-ratio", type=float, default=DEFAULT_THRESHOLDS["production_min_information_string_ratio"])
    parser.add_argument("--ramp-policy-rehearsal-min-tool-candidate-refresh-rate", type=float, default=DEFAULT_THRESHOLDS["ramp_policy_rehearsal_min_tool_candidate_refresh_rate"])
    parser.add_argument("--ramp-policy-rehearsal-min-source-target-coverage-rate", type=float, default=DEFAULT_THRESHOLDS["ramp_policy_rehearsal_min_source_target_coverage_rate"])
    parser.add_argument("--ramp-policy-rehearsal-min-policy-attached-rate", type=float, default=DEFAULT_THRESHOLDS["ramp_policy_rehearsal_min_policy_attached_rate"])
    parser.add_argument("--ramp-policy-rehearsal-min-repair-ids-attached-rate", type=float, default=DEFAULT_THRESHOLDS["ramp_policy_rehearsal_min_repair_ids_attached_rate"])
    parser.add_argument("--ramp-policy-rehearsal-min-watch-metrics-attached-rate", type=float, default=DEFAULT_THRESHOLDS["ramp_policy_rehearsal_min_watch_metrics_attached_rate"])
    parser.add_argument("--ramp-policy-rehearsal-min-strict-contract-rate", type=float, default=DEFAULT_THRESHOLDS["ramp_policy_rehearsal_min_strict_contract_rate"])
    parser.add_argument("--ramp-policy-rehearsal-max-over-limit-count", type=int, default=DEFAULT_THRESHOLDS["ramp_policy_rehearsal_max_over_limit_count"])
    parser.add_argument("--tool-creation-min-quality-pass-rate", type=float, default=DEFAULT_THRESHOLDS["tool_creation_min_quality_pass_rate"])
    parser.add_argument("--tool-creation-min-eval-plan-rate", type=float, default=DEFAULT_THRESHOLDS["tool_creation_min_eval_plan_rate"])
    parser.add_argument("--tool-creation-min-expected-edge-rate", type=float, default=DEFAULT_THRESHOLDS["tool_creation_min_expected_edge_rate"])
    parser.add_argument("--tool-creation-min-would-change-decision-rate", type=float, default=DEFAULT_THRESHOLDS["tool_creation_min_would_change_decision_rate"])
    parser.add_argument("--tool-creation-max-eval-failed-rate", type=float, default=DEFAULT_THRESHOLDS["tool_creation_max_eval_failed_rate"])
    parser.add_argument("--tool-creation-max-runtime-adapter-backlog-rate", type=float, default=DEFAULT_THRESHOLDS["tool_creation_max_runtime_adapter_backlog_rate"])
    return parser.parse_args()


def evaluate_live_scout_tournament(
    report_paths: list[Path],
    *,
    thresholds: dict[str, float | int] | None = None,
) -> dict[str, Any]:
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    candidates = []
    for input_order, path in enumerate(report_paths):
        if not path.exists():
            continue
        candidate = evaluate_live_scout_candidate(path, thresholds=thresholds)
        if candidate:
            candidate["input_order"] = input_order
            candidates.append(candidate)
    candidates = [c for c in candidates if c]
    candidates.sort(key=lambda row: (_stage_level(row), bool(row["promotion_eligible"]), row["score"]), reverse=True)
    top_stage = _stage_level(candidates[0]) if candidates else 0
    promoted = [c for c in candidates if c["promotion_eligible"] and _stage_level(c) == top_stage]
    if not promoted and top_stage >= 3:
        top_stage_latest_order = max(
            int(c.get("input_order") or 0)
            for c in candidates
            if _stage_level(c) == top_stage
        )
        promoted = [
            c for c in candidates
            if c["promotion_eligible"]
            and _stage_level(c) >= 2
            and int(c.get("input_order") or 0) > top_stage_latest_order
        ]
    winner = promoted[0] if promoted else (candidates[0] if candidates else None)
    repeatability = _shadow_repeatability_evidence(candidates, thresholds=thresholds)
    decision = _promotion_decision(
        winner=winner,
        promoted=promoted,
        thresholds=thresholds,
        repeatability=repeatability,
    )
    report = {
        "schema_version": "talis_live_scout_tournament_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "purpose": (
            "Decide whether a live prompt/provider policy is good enough to spend on "
            "the next 100-scout, 1,000-scout, or shadow-production ramp."
        ),
        "thresholds": thresholds,
        "input_reports": [str(p) for p in report_paths],
        "promotion_decision": decision,
        "shadow_repeatability": repeatability,
        "winner": winner,
        "candidates": candidates,
        "system_performance": _system_performance(
            candidates,
            winner=winner,
            repeatability=repeatability,
            decision=decision,
        ),
        "next_experiment_plan": _next_experiment_plan(
            winner=winner,
            candidates=candidates,
            thresholds=thresholds,
            repeatability=repeatability,
            decision=decision,
        ),
    }
    return report


def evaluate_live_scout_candidate(path: Path, *, thresholds: dict[str, float | int]) -> dict[str, Any]:
    report = _read_json(path)
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    scouts = metrics.get("scouts") if isinstance(metrics.get("scouts"), dict) else {}
    info = metrics.get("information_map") if isinstance(metrics.get("information_map"), dict) else {}
    geometry = metrics.get("geometry") if isinstance(metrics.get("geometry"), dict) else {}
    coverage = metrics.get("coverage") if isinstance(metrics.get("coverage"), dict) else {}
    self_healing = metrics.get("self_healing") if isinstance(metrics.get("self_healing"), dict) else {}
    market_evolve = _market_evolve_proof_metrics(report, path)
    transcript = _load_transcript(path)
    transcript_summary = report.get("transcript_summary") if isinstance(report.get("transcript_summary"), dict) else {}
    original_verdict = report.get("verdict") if isinstance(report.get("verdict"), dict) else {}
    original_status = str(original_verdict.get("status") or "").lower()
    outputs = _read_sibling_json(path, "live_scout_canary_outputs.json")
    output_rows = outputs if isinstance(outputs, list) else []

    n_requested = int(report.get("n_scouts_requested") or 0)
    completed = int(scouts.get("completed") or 0)
    success_rate = _to_float(scouts.get("success_rate"), default=(completed / n_requested if n_requested else 0.0))
    string_count = int(info.get("string_count") or 0)
    strings_per_scout = _to_float(
        scouts.get("avg_information_strings_per_scout"),
        default=(string_count / n_requested if n_requested else 0.0),
    )
    evidence_ok_rate = _to_float(scouts.get("evidence_ok_rate"))
    duplicate_rate = _to_float(scouts.get("duplicate_hypothesis_rate"), default=1.0)
    cost_usd = _to_float(scouts.get("total_cost_usd_estimate"))
    cost_per_scout = cost_usd / max(n_requested, 1)
    call_count = int(transcript_summary.get("call_count") or len(transcript.get("calls") or []) or 0)
    transcript_errors = _transcript_errors(transcript, transcript_summary)
    quality_provider_errors = _provider_quality_errors(scouts)
    structural_flag_count = _structural_quality_flags(scouts)
    structural_flag_rate = structural_flag_count / max(n_requested, 1)
    prompt_quality = _prompt_quality_metrics(scouts, n_requested=n_requested)
    tool_errors = _tool_call_error_metrics(report)
    tool_call_count = int(tool_errors.get("tool_call_count") or 0)
    tool_error_count = int(tool_errors.get("tool_error_count") or 0)
    tool_error_rate = _to_float(tool_errors.get("tool_error_rate"))
    ramp_policy_path = _ramp_policy_path(report, path)
    ramp_policy_rehearsal = _ramp_policy_rehearsal_metrics(
        report,
        path,
        thresholds=thresholds,
        n_requested=n_requested,
        ramp_policy_path=ramp_policy_path,
    )
    tool_creation = _tool_creation_evolution_metrics(
        report,
        thresholds=thresholds,
        n_requested=n_requested,
        expected_tool_proposals=int(self_healing.get("tool_proposals") or 0),
    )
    provider_error_rate = max(
        len(transcript_errors) / max(call_count, 1),
        quality_provider_errors / max(n_requested, 1),
    )
    latencies = [
        _to_float(call.get("elapsed_s"), default=math.nan)
        for call in transcript.get("calls") or []
        if isinstance(call, dict)
    ]
    latencies = [x for x in latencies if not math.isnan(x)]
    avg_latency_s = round(sum(latencies) / len(latencies), 3) if latencies else None
    p95_latency_s = _percentile(latencies, 0.95) if latencies else None
    prompt_chars_per_call = (
        int(transcript_summary.get("prompt_chars") or 0) / max(call_count, 1)
        if call_count else 0.0
    )
    response_chars_per_call = (
        int(transcript_summary.get("response_chars") or 0) / max(call_count, 1)
        if call_count else 0.0
    )

    gates = {
        "sample_size_ge_min": n_requested >= int(thresholds["min_scouts"]),
        "success_rate_ge_min": success_rate >= float(thresholds["min_success_rate"]),
        "provider_error_rate_le_max": provider_error_rate <= float(thresholds["max_provider_error_rate"]),
        "duplicate_rate_le_max": duplicate_rate <= float(thresholds["max_duplicate_rate"]),
        "strings_per_scout_ge_min": strings_per_scout >= float(thresholds["min_strings_per_scout"]),
        "evidence_ok_rate_ge_min": evidence_ok_rate >= float(thresholds["min_evidence_ok_rate"]),
        "cost_per_scout_le_max": cost_per_scout <= float(thresholds["max_cost_per_scout_usd"]),
        "avg_latency_le_max": avg_latency_s is not None and avg_latency_s <= float(thresholds["max_avg_latency_s"]),
        "structural_flag_rate_le_max": structural_flag_rate <= float(thresholds["max_structural_flag_rate"]),
        "avg_prompt_quality_ge_min": prompt_quality["avg_prompt_quality"] >= float(thresholds["min_avg_prompt_quality"]),
        "low_prompt_quality_rate_le_max": prompt_quality["low_prompt_quality_rate"] <= float(thresholds["max_low_prompt_quality_rate"]),
        "tool_error_rate_le_max": tool_error_rate <= float(thresholds["max_tool_error_rate"]),
        "original_canary_status_pass": original_status in {"", "pass"},
        "information_strings_created": string_count > 0,
        "synthesis_promoted": int(info.get("promoted_hypotheses") or 0) > 0,
        "geometry_cells_created": int(geometry.get("cell_count") or 0) > 0,
        "self_healing_no_failures": int(self_healing.get("failed_tasks") or 0) == 0,
    }
    gates.update(ramp_policy_rehearsal.get("gates") or {})
    gates.update(tool_creation.get("gates") or {})
    if n_requested >= int(thresholds["distribution_min_scouts"]):
        gates.update({
            "distribution_sample_size_ge_100": n_requested >= int(thresholds["distribution_min_scouts"]),
            "distribution_success_rate_ge_0_90": success_rate >= float(thresholds["distribution_min_success_rate"]),
            "distribution_provider_error_rate_le_0_02": provider_error_rate <= float(thresholds["distribution_max_provider_error_rate"]),
            "distribution_duplicate_rate_le_0_20": duplicate_rate <= float(thresholds["distribution_max_duplicate_rate"]),
            "distribution_structural_flag_rate_le_0_10": structural_flag_rate <= float(thresholds["distribution_max_structural_flag_rate"]),
            "distribution_tool_error_rate_le_0_02": tool_error_rate <= float(thresholds["distribution_max_tool_error_rate"]),
            "distribution_geometry_cells_ge_50": int(geometry.get("cell_count") or 0) >= int(thresholds["distribution_min_geometry_cells"]),
            "distribution_market_evolve_policy_applied": int(market_evolve.get("policy_application_count") or 0) >= n_requested,
            "distribution_market_evolve_pairs_ge_min": int(market_evolve.get("paired_seed_slices") or 0) >= int(thresholds["distribution_min_market_evolve_pairs"]),
            "distribution_market_evolve_control_candidate_arms": bool(market_evolve.get("control_arm_present")) and bool(market_evolve.get("candidate_arm_present")),
            "distribution_market_evolve_hard_experiment_planned": bool(market_evolve.get("hard_experiment_planned")),
            "distribution_market_evolve_result_evaluated": bool(market_evolve.get("experiment_result_evaluated")),
            "distribution_market_evolve_falsification_gates_evaluated": bool(market_evolve.get("falsification_gates_evaluated")),
        })
    if n_requested >= int(thresholds["scale_min_scouts"]):
        gates.update({
            "scale_sample_size_ge_1000": n_requested >= int(thresholds["scale_min_scouts"]),
            "scale_success_rate_ge_0_90": success_rate >= float(thresholds["scale_min_success_rate"]),
            "scale_provider_error_rate_le_0_02": provider_error_rate <= float(thresholds["scale_max_provider_error_rate"]),
            "scale_duplicate_rate_le_0_20": duplicate_rate <= float(thresholds["scale_max_duplicate_rate"]),
            "scale_structural_flag_rate_le_0_10": structural_flag_rate <= float(thresholds["scale_max_structural_flag_rate"]),
            "scale_tool_error_rate_le_0_02": tool_error_rate <= float(thresholds["scale_max_tool_error_rate"]),
            "scale_geometry_cells_ge_500": int(geometry.get("cell_count") or 0) >= int(thresholds["scale_min_geometry_cells"]),
            "scale_information_strings_ge_1000": string_count >= int(thresholds["scale_min_information_strings"]),
            "scale_market_evolve_policy_applied": int(market_evolve.get("policy_application_count") or 0) >= n_requested,
            "scale_market_evolve_pairs_ge_min": int(market_evolve.get("paired_seed_slices") or 0) >= int(thresholds["scale_min_market_evolve_pairs"]),
            "scale_market_evolve_control_candidate_arms": bool(market_evolve.get("control_arm_present")) and bool(market_evolve.get("candidate_arm_present")),
            "scale_market_evolve_hard_experiment_planned": bool(market_evolve.get("hard_experiment_planned")),
            "scale_market_evolve_result_evaluated": bool(market_evolve.get("experiment_result_evaluated")),
            "scale_market_evolve_falsification_gates_evaluated": bool(market_evolve.get("falsification_gates_evaluated")),
        })
    failed_gates = [name for name, ok in gates.items() if not ok]
    prompt_variants = _prompt_variants(report, scouts, output_rows)
    candidate = {
        "candidate_id": _candidate_id(report, prompt_variants),
        "source_report": str(path),
        "cycle_id": report.get("cycle_id"),
        "configuration": {
            "model": report.get("model"),
            "fallback": report.get("fallback"),
            "prompt_variants": prompt_variants,
            "prompt_variant_override": report.get("prompt_variant_override") or "",
            "max_tool_iterations": report.get("max_tool_iterations"),
            "provider_timeout_s": report.get("provider_timeout_s"),
            "concurrency": report.get("concurrency"),
            "seed_rng": report.get("seed_rng"),
            "ramp_policy_path": ramp_policy_path,
        },
        "sample": {
            "requested": n_requested,
            "completed": completed,
            "errored": int(scouts.get("errored") or 0),
            "provider_calls": call_count,
        },
        "quality": {
            "success_rate": round(success_rate, 4),
            "provider_error_rate": round(provider_error_rate, 4),
            "transcript_error_count": len(transcript_errors),
            "quality_provider_error_count": quality_provider_errors,
            "tool_call_count": tool_call_count,
            "tool_error_count": tool_error_count,
            "tool_error_rate": round(tool_error_rate, 4),
            "structural_flag_count": structural_flag_count,
            "structural_flag_rate": round(structural_flag_rate, 4),
            **prompt_quality,
            "strings_per_scout": round(strings_per_scout, 4),
            "string_count": string_count,
            "evidence_ok_rate": round(evidence_ok_rate, 4),
            "duplicate_hypothesis_rate": round(duplicate_rate, 4),
            "cost_usd": round(cost_usd, 6),
            "cost_per_scout_usd": round(cost_per_scout, 6),
            "avg_latency_s": avg_latency_s,
            "p95_latency_s": p95_latency_s,
            "prompt_chars_per_call": round(prompt_chars_per_call, 1),
            "response_chars_per_call": round(response_chars_per_call, 1),
        },
        "map_effect": {
            "information_strings": string_count,
            "cells_with_strings": int(info.get("cells_with_strings") or 0),
            "confluences": int(info.get("confluences") or 0),
            "tensions": int(info.get("tensions") or 0),
            "promoted_hypotheses": int(info.get("promoted_hypotheses") or 0),
            "geometry_cells": int(geometry.get("cell_count") or 0),
            "geometry_routing_queue": int(geometry.get("routing_queue_count") or 0),
            "coverage_ratio": _to_float(coverage.get("coverage_ratio")),
            "covered_count": int(coverage.get("covered_count") or 0),
            "valid_cell_count": int(coverage.get("valid_cell_count") or 0),
            "self_healing_completed": int(self_healing.get("completed_tasks") or 0),
            "self_healing_failed": int(self_healing.get("failed_tasks") or 0),
            "tool_proposals": int(self_healing.get("tool_proposals") or 0),
        },
        "market_evolve": market_evolve,
        "ramp_policy_rehearsal": ramp_policy_rehearsal,
        "tool_creation_evolution": tool_creation,
        "original_canary_verdict": original_verdict,
        "gates": gates,
        "failed_gates": failed_gates,
        "score": 0.0,
        "promotion_eligible": False,
        "interpretation": "",
    }
    candidate["score"] = _score_candidate(candidate)
    candidate["promotion_eligible"] = not failed_gates
    candidate["interpretation"] = _interpret_candidate(candidate)
    return candidate


def _market_evolve_proof_metrics(report: dict[str, Any], path: Path) -> dict[str, Any]:
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    raw = metrics.get("market_evolve") if isinstance(metrics.get("market_evolve"), dict) else {}
    hard = _read_sibling_json(path, "market_evolve_hard_experiment.json")
    hard = hard if isinstance(hard, dict) else {}
    proof = hard.get("proof") if isinstance(hard.get("proof"), dict) else {}
    results = hard.get("results") if isinstance(hard.get("results"), list) else []
    plans = hard.get("plans") if isinstance(hard.get("plans"), list) else []
    latest_result = results[0] if results and isinstance(results[0], dict) else {}
    raw_arms = raw.get("arm_counts") if isinstance(raw.get("arm_counts"), dict) else {}
    hard_arms = hard.get("arm_counts") if isinstance(hard.get("arm_counts"), dict) else {}
    arm_counts = {**raw_arms, **hard_arms}
    planning_count = max(
        int(raw.get("planning_experiment_count") or 0),
        int(raw.get("experiment_plan_count") or 0),
        len(plans),
    )
    result_count = max(int(raw.get("experiment_result_count") or 0), len(results))
    status = str(hard.get("status") or "").strip()
    control_arm_present = bool(proof.get("control_arm_present")) or int(arm_counts.get("control") or 0) > 0
    candidate_arm_present = bool(proof.get("candidate_arm_present")) or int(arm_counts.get("candidate") or 0) > 0
    experiment_result_evaluated = (
        bool(proof.get("experiment_result_evaluated"))
        or result_count > 0
        or status == "evaluated"
    )
    falsification_gates_evaluated = (
        bool(proof.get("falsification_gates_evaluated"))
        or bool(latest_result.get("falsification_gate_results"))
    )
    return {
        "hard_experiment_observed": bool(hard),
        "hard_experiment_status": status,
        "hard_experiment_planned": planning_count > 0 or status in {"planned", "evaluated"},
        "hard_experiment_final_decision": hard.get("final_decision") or raw.get("latest_experiment_decision"),
        "policy_application_count": max(
            int(raw.get("policy_application_count") or 0),
            int(hard.get("policy_application_count") or 0),
        ),
        "paired_seed_slices": max(
            int(raw.get("paired_seed_slices") or 0),
            int(hard.get("paired_seed_slices") or 0),
        ),
        "arm_counts": arm_counts,
        "planning_experiment_count": planning_count,
        "experiment_result_count": result_count,
        "latest_experiment_decision": raw.get("latest_experiment_decision") or hard.get("final_decision"),
        "final_score": raw.get("final_score"),
        "final_passed": raw.get("final_passed"),
        "final_score_delta": hard.get("final_score_delta"),
        "policy_stamped_on_seeds": bool(proof.get("policy_stamped_on_seeds")) or int(raw.get("policy_application_count") or 0) > 0,
        "matched_seed_pairs_present": bool(proof.get("matched_seed_pairs_present")) or int(raw.get("paired_seed_slices") or 0) > 0,
        "control_arm_present": control_arm_present,
        "candidate_arm_present": candidate_arm_present,
        "experiment_result_evaluated": experiment_result_evaluated,
        "falsification_gates_evaluated": falsification_gates_evaluated,
        "candidate_promoted_or_continued": bool(proof.get("candidate_promoted_or_continued")),
        "quality_flags": list(proof.get("quality_flags") or []),
    }


def _ramp_policy_path(report: dict[str, Any], canary_report_path: Path) -> str:
    raw = str(report.get("ramp_policy_path") or "").strip()
    if raw:
        return raw
    artifacts = report.get("artifacts") if isinstance(report.get("artifacts"), dict) else {}
    for key in ("ramp_policy", "live_scout_ramp_policy_json"):
        raw = str(artifacts.get(key) or "").strip()
        if raw:
            return raw
    sibling = canary_report_path.parent / "live_scout_ramp_policy.json"
    if sibling.exists():
        return str(sibling)
    return ""


def _ramp_policy_rehearsal_metrics(
    report: dict[str, Any],
    path: Path,
    *,
    thresholds: dict[str, float | int],
    n_requested: int,
    ramp_policy_path: str,
) -> dict[str, Any]:
    inline = report.get("ramp_policy_rehearsal") if isinstance(report.get("ramp_policy_rehearsal"), dict) else {}
    sibling = _read_sibling_json(path, "live_scout_ramp_policy_rehearsal.json")
    sibling = sibling if isinstance(sibling, dict) else {}
    rehearsal = inline or sibling
    observed = bool(rehearsal)
    required = bool(ramp_policy_path) or n_requested >= int(thresholds["distribution_min_scouts"])
    if not required and not observed:
        return {
            "required": False,
            "observed": False,
            "status": "not_required",
            "decision": "",
            "score": 0.0,
            "metrics": {},
            "target_source_family_hits": {},
            "gates": {},
            "failed_gates": [],
        }

    metrics = rehearsal.get("metrics") if isinstance(rehearsal.get("metrics"), dict) else {}
    target_hits = (
        rehearsal.get("target_source_family_hits")
        if isinstance(rehearsal.get("target_source_family_hits"), dict)
        else {}
    )
    status = str(rehearsal.get("status") or ("missing" if required else "not_captured"))
    decision = str(rehearsal.get("decision") or "")
    tool_refresh = _to_float(metrics.get("tool_candidate_refresh_rate"), default=0.0)
    source_coverage = _to_float(metrics.get("source_target_coverage_rate"), default=0.0)
    policy_attached = _to_float(metrics.get("policy_attached_rate"), default=0.0)
    repair_attached = _to_float(metrics.get("repair_ids_attached_rate"), default=0.0)
    watch_attached = _to_float(metrics.get("watch_metrics_attached_rate"), default=0.0)
    strict_contract = _to_float(metrics.get("strict_contract_rate"), default=0.0)
    over_limit = int(metrics.get("over_limit_count") or 0)

    gates = {
        "ramp_policy_rehearsal_observed": observed,
        "ramp_policy_rehearsal_status_pass": status == "pass",
        "ramp_policy_rehearsal_decision_can_gate_spend": decision == "policy_can_gate_live_spend",
        "ramp_policy_rehearsal_tool_refresh_rate_ge_0_95": (
            tool_refresh >= float(thresholds["ramp_policy_rehearsal_min_tool_candidate_refresh_rate"])
        ),
        "ramp_policy_rehearsal_source_coverage_ge_0_60": (
            source_coverage >= float(thresholds["ramp_policy_rehearsal_min_source_target_coverage_rate"])
        ),
        "ramp_policy_rehearsal_policy_attached_rate_ge_1_00": (
            policy_attached >= float(thresholds["ramp_policy_rehearsal_min_policy_attached_rate"])
        ),
        "ramp_policy_rehearsal_repair_ids_attached_rate_ge_1_00": (
            repair_attached >= float(thresholds["ramp_policy_rehearsal_min_repair_ids_attached_rate"])
        ),
        "ramp_policy_rehearsal_watch_metrics_attached_rate_ge_1_00": (
            watch_attached >= float(thresholds["ramp_policy_rehearsal_min_watch_metrics_attached_rate"])
        ),
        "ramp_policy_rehearsal_strict_contract_rate_ge_1_00": (
            strict_contract >= float(thresholds["ramp_policy_rehearsal_min_strict_contract_rate"])
        ),
        "ramp_policy_rehearsal_over_limit_count_eq_0": (
            over_limit <= int(thresholds["ramp_policy_rehearsal_max_over_limit_count"])
        ),
    }
    if not required:
        gates = {name: ok for name, ok in gates.items() if observed}
    failed = [name for name, ok in gates.items() if not ok]
    return {
        "required": required,
        "observed": observed,
        "source": "inline_report" if inline else ("sibling_artifact" if sibling else "missing"),
        "path": str(path.parent / "live_scout_ramp_policy_rehearsal.json") if sibling else "",
        "policy_path": ramp_policy_path,
        "status": status,
        "decision": decision,
        "score": _to_float(rehearsal.get("score"), default=0.0),
        "metrics": {
            "tool_candidate_refresh_rate": round(tool_refresh, 4),
            "source_target_coverage_rate": round(source_coverage, 4),
            "policy_attached_rate": round(policy_attached, 4),
            "repair_ids_attached_rate": round(repair_attached, 4),
            "watch_metrics_attached_rate": round(watch_attached, 4),
            "strict_contract_rate": round(strict_contract, 4),
            "over_limit_count": over_limit,
            "tool_candidate_added_count": int(metrics.get("tool_candidate_added_count") or 0),
            "candidate_tool_delta_avg": _to_float(metrics.get("candidate_tool_delta_avg"), default=0.0),
        },
        "target_source_family_hits": target_hits,
        "gates": gates,
        "failed_gates": failed,
    }


def _tool_creation_evolution_metrics(
    report: dict[str, Any],
    *,
    thresholds: dict[str, float | int],
    n_requested: int,
    expected_tool_proposals: int,
) -> dict[str, Any]:
    required = n_requested >= int(thresholds["distribution_min_scouts"]) or expected_tool_proposals > 0
    db_path = Path(str(report.get("db_path") or ""))
    if not db_path.exists():
        gates = {}
        if required:
            gates = {
                "tool_creation_db_observed": False,
                "tool_creation_proposal_rows_observed": False,
            }
        return {
            "required": required,
            "observed": False,
            "db_path": str(db_path) if str(db_path) else "",
            "expected_tool_proposals": expected_tool_proposals,
            "proposal_count": 0,
            "status_counts": {},
            "top_tools": [],
            "metrics": {},
            "gates": gates,
            "failed_gates": [name for name, ok in gates.items() if not ok],
            "quality_flags": ["tool_creation_db_missing"] if required else [],
        }
    rows: list[dict[str, Any]] = []
    quality_flags: list[str] = []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            if not _table_exists(conn, "analysis_tool_proposals"):
                quality_flags.append("analysis_tool_proposals_table_missing")
            else:
                columns = _table_columns(conn, "analysis_tool_proposals")
                where = "WHERE cycle_id = ?"
                if "transaction_to" in columns:
                    where += " AND transaction_to IS NULL"
                rows = [
                    dict(row)
                    for row in conn.execute(
                        f"SELECT * FROM analysis_tool_proposals {where}",
                        (str(report.get("cycle_id") or ""),),
                    ).fetchall()
                ]
    except Exception as exc:
        quality_flags.append(f"tool_creation_db_error:{type(exc).__name__}")
        rows = []

    try:
        from talis_desk.tool_atlas.discovery import evaluate_analysis_tool_proposal
    except Exception:
        evaluate_analysis_tool_proposal = None  # type: ignore[assignment]
        quality_flags.append("tool_creation_evaluator_unavailable")

    status_counts: dict[str, int] = {}
    tool_counts: dict[str, int] = {}
    scores: list[float] = []
    pass_count = 0
    eval_plan_count = 0
    promotion_gate_count = 0
    expected_edge_count = 0
    would_change_count = 0
    eval_failed_count = 0
    runtime_adapter_count = 0
    row_flags: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        tool_name = str(row.get("tool_name") or "unknown")
        tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
        if status == "eval_failed":
            eval_failed_count += 1
        if status in {"needs_runtime_adapter", "adapter_requested"}:
            runtime_adapter_count += 1
        eval_plan = _json_dict(row.get("eval_plan_json") or row.get("eval_plan"))
        promotion_gate = _json_dict(row.get("promotion_gate_json") or row.get("promotion_gate"))
        if eval_plan:
            eval_plan_count += 1
        if promotion_gate:
            promotion_gate_count += 1
        if str(promotion_gate.get("expected_edge") or "").strip():
            expected_edge_count += 1
        if promotion_gate.get("would_change_decision") is True:
            would_change_count += 1
        if evaluate_analysis_tool_proposal is not None:
            q = evaluate_analysis_tool_proposal(row)
            scores.append(float(q.score))
            if q.passed:
                pass_count += 1
            for flag in q.flags:
                row_flags[flag] = row_flags.get(flag, 0) + 1
    n = len(rows)
    avg_score = sum(scores) / max(1, len(scores))
    quality_pass_rate = pass_count / max(1, n)
    eval_plan_rate = eval_plan_count / max(1, n)
    expected_edge_rate = expected_edge_count / max(1, n)
    would_change_rate = would_change_count / max(1, n)
    eval_failed_rate = eval_failed_count / max(1, n)
    runtime_adapter_rate = runtime_adapter_count / max(1, n)
    gates = {}
    if required or n:
        gates = {
            "tool_creation_db_observed": db_path.exists(),
            "tool_creation_proposal_rows_observed": n >= max(1, expected_tool_proposals),
            "tool_creation_quality_pass_rate_ge_0_70": quality_pass_rate >= float(thresholds["tool_creation_min_quality_pass_rate"]),
            "tool_creation_eval_plan_rate_ge_0_85": eval_plan_rate >= float(thresholds["tool_creation_min_eval_plan_rate"]),
            "tool_creation_expected_edge_rate_ge_0_60": expected_edge_rate >= float(thresholds["tool_creation_min_expected_edge_rate"]),
            "tool_creation_would_change_decision_rate_ge_0_60": would_change_rate >= float(thresholds["tool_creation_min_would_change_decision_rate"]),
            "tool_creation_eval_failed_rate_le_0_25": eval_failed_rate <= float(thresholds["tool_creation_max_eval_failed_rate"]),
            "tool_creation_runtime_adapter_backlog_rate_le_0_50": runtime_adapter_rate <= float(thresholds["tool_creation_max_runtime_adapter_backlog_rate"]),
        }
    failed = [name for name, ok in gates.items() if not ok]
    return {
        "required": required,
        "observed": bool(n),
        "db_path": str(db_path),
        "expected_tool_proposals": expected_tool_proposals,
        "proposal_count": n,
        "status_counts": status_counts,
        "top_tools": [
            {"tool_name": name, "count": count}
            for name, count in sorted(tool_counts.items(), key=lambda kv: kv[1], reverse=True)[:12]
        ],
        "metrics": {
            "avg_quality_score": round(avg_score, 4),
            "quality_pass_rate": round(quality_pass_rate, 4),
            "eval_plan_rate": round(eval_plan_rate, 4),
            "promotion_gate_rate": round(promotion_gate_count / max(1, n), 4),
            "expected_edge_rate": round(expected_edge_rate, 4),
            "would_change_decision_rate": round(would_change_rate, 4),
            "eval_failed_rate": round(eval_failed_rate, 4),
            "runtime_adapter_backlog_rate": round(runtime_adapter_rate, 4),
            "eval_failed_count": eval_failed_count,
            "runtime_adapter_backlog_count": runtime_adapter_count,
        },
        "top_quality_flags": [
            {"flag": flag, "count": count}
            for flag, count in sorted(row_flags.items(), key=lambda kv: kv[1], reverse=True)[:12]
        ],
        "gates": gates,
        "failed_gates": failed,
        "quality_flags": sorted(set(quality_flags)),
    }


def render_tournament_markdown(report: dict[str, Any]) -> str:
    decision = report.get("promotion_decision") if isinstance(report.get("promotion_decision"), dict) else {}
    repeatability = report.get("shadow_repeatability") if isinstance(report.get("shadow_repeatability"), dict) else {}
    lines = [
        "# Live Scout Tournament",
        "",
        f"- decision: `{decision.get('decision')}`",
        f"- ready_for_live_100: `{decision.get('ready_for_live_100')}`",
        f"- ready_for_live_1000: `{decision.get('ready_for_live_1000')}`",
        f"- ready_for_scheduled_production: `{decision.get('ready_for_scheduled_production')}`",
        f"- reason: {decision.get('reason')}",
        f"- repeatability_ready: `{repeatability.get('ready_for_scheduled_production')}`",
        f"- repeatability_runs: `{repeatability.get('shadow_run_count')}/{repeatability.get('required_shadow_runs')}`",
        "",
        "## Candidates",
        "",
    ]
    for c in report.get("candidates") or []:
        q = c.get("quality") or {}
        sample = c.get("sample") or {}
        evolve = c.get("market_evolve") or {}
        rehearsal = c.get("ramp_policy_rehearsal") or {}
        rehearsal_metrics = rehearsal.get("metrics") if isinstance(rehearsal.get("metrics"), dict) else {}
        tool_creation = c.get("tool_creation_evolution") or {}
        tool_creation_metrics = tool_creation.get("metrics") if isinstance(tool_creation.get("metrics"), dict) else {}
        lines.extend([
            f"### {c.get('candidate_id')}",
            "",
            f"- score: `{c.get('score')}`",
            f"- promotion_eligible: `{c.get('promotion_eligible')}`",
            f"- scouts: `{sample.get('completed')}/{sample.get('requested')}`",
            f"- success_rate: `{q.get('success_rate')}`",
            f"- provider_error_rate: `{q.get('provider_error_rate')}`",
            f"- tool_error_rate: `{q.get('tool_error_rate')}`",
            f"- strings_per_scout: `{q.get('strings_per_scout')}`",
            f"- duplicate_rate: `{q.get('duplicate_hypothesis_rate')}`",
            f"- avg_latency_s: `{q.get('avg_latency_s')}`",
            f"- market_evolve_pairs: `{evolve.get('paired_seed_slices')}`",
            f"- market_evolve_decision: `{evolve.get('latest_experiment_decision') or evolve.get('hard_experiment_final_decision')}`",
            f"- market_evolve_proof: `policy={evolve.get('policy_stamped_on_seeds')} arms={evolve.get('control_arm_present') and evolve.get('candidate_arm_present')} falsified={evolve.get('falsification_gates_evaluated')}`",
            f"- ramp_policy_rehearsal: `required={rehearsal.get('required')} observed={rehearsal.get('observed')} status={rehearsal.get('status')} decision={rehearsal.get('decision')}`",
            f"- ramp_policy_metrics: `tool_refresh={rehearsal_metrics.get('tool_candidate_refresh_rate')} source_coverage={rehearsal_metrics.get('source_target_coverage_rate')} over_limit={rehearsal_metrics.get('over_limit_count')}`",
            f"- tool_creation: `required={tool_creation.get('required')} proposals={tool_creation.get('proposal_count')} quality_pass={tool_creation_metrics.get('quality_pass_rate')} eval_plan={tool_creation_metrics.get('eval_plan_rate')} expected_edge={tool_creation_metrics.get('expected_edge_rate')}`",
            f"- failed_gates: `{', '.join(c.get('failed_gates') or []) or 'none'}`",
            "",
            str(c.get("interpretation") or ""),
            "",
        ])
    lines.extend(["## Next Experiment Plan", ""])
    for item in report.get("next_experiment_plan") or []:
        lines.extend([
            f"- `{item.get('id')}`: {item.get('purpose')}",
            f"  - command: `{item.get('command')}`",
            f"  - promotion_rule: {item.get('promotion_rule')}",
        ])
    return "\n".join(lines) + "\n"


def _score_candidate(candidate: dict[str, Any]) -> float:
    q = candidate["quality"]
    sample = candidate["sample"]
    m = candidate["map_effect"]
    e = candidate.get("market_evolve") if isinstance(candidate.get("market_evolve"), dict) else {}
    tc = candidate.get("tool_creation_evolution") if isinstance(candidate.get("tool_creation_evolution"), dict) else {}
    tc_metrics = tc.get("metrics") if isinstance(tc.get("metrics"), dict) else {}
    success = _to_float(q.get("success_rate"))
    strings = min(1.0, _to_float(q.get("strings_per_scout")) / 1.5)
    evidence = _to_float(q.get("evidence_ok_rate"))
    prompt_quality = _to_float(q.get("avg_prompt_quality"), default=0.0)
    geometry = 1.0 if m.get("geometry_cells") else 0.0
    self_healing = 1.0 if not m.get("self_healing_failed") else 0.0
    provider_error = _to_float(q.get("provider_error_rate"))
    tool_error = _to_float(q.get("tool_error_rate"))
    tool_creation_quality = _to_float(tc_metrics.get("quality_pass_rate"), default=1.0 if not tc.get("required") else 0.0)
    tool_creation_missing_eval = max(0.0, 1.0 - _to_float(tc_metrics.get("eval_plan_rate"), default=1.0))
    duplicate = _to_float(q.get("duplicate_hypothesis_rate"), default=1.0)
    cost_per = _to_float(q.get("cost_per_scout_usd"))
    cost_eff = max(0.0, 1.0 - min(1.0, cost_per / DEFAULT_THRESHOLDS["max_cost_per_scout_usd"]))
    avg_latency = q.get("avg_latency_s")
    latency = max(0.0, 1.0 - (_to_float(avg_latency) / 60.0)) if avg_latency is not None else 0.35
    requested = int(sample.get("requested") or 0)
    sample_bonus = min(1.0, requested / DEFAULT_THRESHOLDS["min_scouts"])
    if requested >= DEFAULT_THRESHOLDS["distribution_min_scouts"]:
        market_evolve_proof = sum(
            1.0 for ok in (
                e.get("policy_stamped_on_seeds"),
                e.get("matched_seed_pairs_present"),
                e.get("control_arm_present") and e.get("candidate_arm_present"),
                e.get("hard_experiment_planned"),
                e.get("experiment_result_evaluated"),
                e.get("falsification_gates_evaluated"),
            )
            if ok
        ) / 6.0
    else:
        market_evolve_proof = 0.5
    score = (
        0.24 * success
        + 0.18 * strings
        + 0.12 * evidence
        + 0.08 * prompt_quality
        + 0.10 * geometry
        + 0.08 * self_healing
        + 0.06 * market_evolve_proof
        + 0.04 * tool_creation_quality
        + 0.08 * cost_eff
        + 0.10 * latency
        + 0.02 * sample_bonus
        - 0.22 * provider_error
        - 0.18 * tool_error
        - 0.08 * tool_creation_missing_eval
        - 0.10 * duplicate
    )
    return round(max(0.0, min(1.0, score)), 4)


def _stage_level(candidate: dict[str, Any]) -> int:
    sample = candidate.get("sample") if isinstance(candidate.get("sample"), dict) else {}
    requested = int(sample.get("requested") or 0)
    if requested >= DEFAULT_THRESHOLDS["scale_min_scouts"]:
        return 3
    if requested >= DEFAULT_THRESHOLDS["distribution_min_scouts"]:
        return 2
    if requested >= DEFAULT_THRESHOLDS["min_scouts"]:
        return 1
    return 0


def _shadow_repeatability_evidence(
    candidates: list[dict[str, Any]],
    *,
    thresholds: dict[str, float | int],
) -> dict[str, Any]:
    clean_scale = [
        c for c in candidates
        if c.get("promotion_eligible") and _stage_level(c) >= 3
    ]
    if not clean_scale:
        return {
            "schema_version": "live_scout_shadow_repeatability_v1",
            "ready_for_scheduled_production": False,
            "reason": "No clean 1,000-scout shadow runs are available yet.",
            "shadow_run_count": 0,
            "required_shadow_runs": int(thresholds["production_min_shadow_runs"]),
            "stability_gates": {},
            "failed_gates": ["production_min_shadow_runs"],
        }
    groups: dict[str, list[dict[str, Any]]] = {}
    for candidate in clean_scale:
        groups.setdefault(_production_policy_signature(candidate), []).append(candidate)
    scored_groups = [
        _repeatability_for_group(signature, rows, thresholds=thresholds)
        for signature, rows in groups.items()
    ]
    ready = [row for row in scored_groups if row.get("ready_for_scheduled_production")]
    if ready:
        return sorted(
            ready,
            key=lambda row: (row.get("avg_score", 0.0), row.get("shadow_run_count", 0)),
            reverse=True,
        )[0]
    return sorted(
        scored_groups,
        key=lambda row: (row.get("shadow_run_count", 0), row.get("avg_score", 0.0)),
        reverse=True,
    )[0]


def _production_policy_signature(candidate: dict[str, Any]) -> str:
    cfg = candidate.get("configuration") if isinstance(candidate.get("configuration"), dict) else {}
    variants = "|".join(str(x) for x in (cfg.get("prompt_variants") or []) if str(x).strip())
    parts = [
        str(cfg.get("model") or ""),
        str(cfg.get("fallback") or ""),
        variants,
        str(cfg.get("max_tool_iterations") if cfg.get("max_tool_iterations") is not None else ""),
        str(cfg.get("provider_timeout_s") or ""),
    ]
    return "::".join(parts)


def _ramp_policy_command_arg(candidate: dict[str, Any]) -> str:
    cfg = candidate.get("configuration") if isinstance(candidate.get("configuration"), dict) else {}
    path = str(cfg.get("ramp_policy_path") or "").strip()
    if not path:
        return ""
    return f"--ramp-policy {shlex.quote(path)} "


def _repeatability_for_group(
    signature: str,
    rows: list[dict[str, Any]],
    *,
    thresholds: dict[str, float | int],
) -> dict[str, Any]:
    ordered = sorted(
        rows,
        key=lambda c: int(c.get("input_order") or 0),
        reverse=True,
    )
    n_required = int(thresholds["production_min_shadow_runs"])
    selected = ordered[:max(n_required, 2)]
    shadow_count = len(selected)
    unique_cycles = {
        str(c.get("cycle_id") or "")
        for c in selected
        if str(c.get("cycle_id") or "").strip()
    }
    unique_seed_rng = {
        str(((c.get("configuration") or {}).get("seed_rng")))
        for c in selected
        if ((c.get("configuration") or {}).get("seed_rng")) is not None
    }
    success_values = [_q(c, "success_rate") for c in selected]
    duplicate_values = [_q(c, "duplicate_hypothesis_rate") for c in selected]
    structural_values = [_q(c, "structural_flag_rate") for c in selected]
    geometry_values = [float((c.get("map_effect") or {}).get("geometry_cells") or 0) for c in selected]
    string_values = [float((c.get("map_effect") or {}).get("information_strings") or 0) for c in selected]
    score_values = [float(c.get("score") or 0.0) for c in selected]
    geometry_ratio = _min_max_ratio(geometry_values)
    string_ratio = _min_max_ratio(string_values)
    success_delta = _delta(success_values)
    duplicate_delta = _delta(duplicate_values)
    structural_delta = _delta(structural_values)
    gates = {
        "production_shadow_runs_ge_required": shadow_count >= n_required,
        "production_independent_cycles": len(unique_cycles) >= n_required,
        "production_independent_seed_rng": len(unique_seed_rng) >= n_required,
        "production_success_rate_delta_le_max": success_delta <= float(thresholds["production_max_success_rate_delta"]),
        "production_duplicate_rate_delta_le_max": duplicate_delta <= float(thresholds["production_max_duplicate_rate_delta"]),
        "production_structural_flag_delta_le_max": structural_delta <= float(thresholds["production_max_structural_flag_rate_delta"]),
        "production_geometry_cell_ratio_ge_min": geometry_ratio >= float(thresholds["production_min_geometry_cell_ratio"]),
        "production_information_string_ratio_ge_min": string_ratio >= float(thresholds["production_min_information_string_ratio"]),
    }
    failed = [name for name, ok in gates.items() if not ok]
    return {
        "schema_version": "live_scout_shadow_repeatability_v1",
        "ready_for_scheduled_production": not failed,
        "policy_signature": signature,
        "shadow_run_count": shadow_count,
        "required_shadow_runs": n_required,
        "candidate_ids": [str(c.get("candidate_id") or "") for c in selected],
        "source_reports": [str(c.get("source_report") or "") for c in selected],
        "cycle_ids": sorted(unique_cycles),
        "seed_rngs": sorted(unique_seed_rng),
        "avg_score": round(sum(score_values) / max(len(score_values), 1), 4),
        "metrics": {
            "success_rate_min": round(min(success_values or [0.0]), 4),
            "success_rate_max": round(max(success_values or [0.0]), 4),
            "success_rate_delta": round(success_delta, 4),
            "duplicate_rate_min": round(min(duplicate_values or [0.0]), 4),
            "duplicate_rate_max": round(max(duplicate_values or [0.0]), 4),
            "duplicate_rate_delta": round(duplicate_delta, 4),
            "structural_flag_rate_min": round(min(structural_values or [0.0]), 4),
            "structural_flag_rate_max": round(max(structural_values or [0.0]), 4),
            "structural_flag_rate_delta": round(structural_delta, 4),
            "geometry_cell_min": int(min(geometry_values or [0])),
            "geometry_cell_max": int(max(geometry_values or [0])),
            "geometry_cell_ratio": round(geometry_ratio, 4),
            "information_string_min": int(min(string_values or [0])),
            "information_string_max": int(max(string_values or [0])),
            "information_string_ratio": round(string_ratio, 4),
        },
        "stability_gates": gates,
        "failed_gates": failed,
        "reason": (
            "The shadow policy repeated cleanly across independent 1,000-scout runs."
            if not failed else
            "Repeat shadow evidence exists, but scheduled production is still blocked by: "
            + ", ".join(failed)
        ),
    }


def _q(candidate: dict[str, Any], key: str) -> float:
    quality = candidate.get("quality") if isinstance(candidate.get("quality"), dict) else {}
    return _to_float(quality.get(key), default=0.0)


def _delta(values: list[float]) -> float:
    if not values:
        return 1.0
    return max(values) - min(values)


def _min_max_ratio(values: list[float]) -> float:
    positives = [v for v in values if v > 0]
    if not positives:
        return 0.0
    return min(positives) / max(positives)


def _promotion_decision(
    *,
    winner: dict[str, Any] | None,
    promoted: list[dict[str, Any]],
    thresholds: dict[str, float | int],
    repeatability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    repeatability = repeatability if isinstance(repeatability, dict) else {}
    if not winner:
        return {
            "decision": "no_candidates",
            "promoted_candidate_id": "",
            "ready_for_live_100": False,
            "ready_for_live_1000": False,
            "ready_for_scheduled_production": False,
            "reason": "No readable canary reports were provided.",
        }
    if repeatability.get("ready_for_scheduled_production"):
        promoted_ids = [
            str(candidate_id)
            for candidate_id in (repeatability.get("candidate_ids") or [])
            if str(candidate_id).strip()
        ] or [winner["candidate_id"]]
        return {
            "decision": "promote_to_scheduled_production_candidate",
            "promoted_candidate_id": promoted_ids[0],
            "promoted_candidate_ids": promoted_ids,
            "ready_for_live_100": True,
            "ready_for_live_1000": True,
            "ready_for_scheduled_production": True,
            "reason": (
                "Two independent 1,000-scout shadow runs passed scale gates under the same "
                "prompt/provider policy, and their repeatability metrics stayed inside the "
                "production stability band. The system may move to a scheduled shadow-production "
                "candidate posture with hard caps, audit capture, and no trade execution."
            ),
            "repeatability": repeatability,
        }
    if promoted:
        sample = winner.get("sample") if isinstance(winner.get("sample"), dict) else {}
        if int(sample.get("requested") or 0) >= int(thresholds["scale_min_scouts"]):
            return {
                "decision": "promote_to_shadow_production_trial",
                "promoted_candidate_id": winner["candidate_id"],
                "ready_for_live_100": True,
                "ready_for_live_1000": True,
                "ready_for_scheduled_production": False,
                "reason": (
                    "The 1,000-scout live distribution passed provider reliability, "
                    "string yield, duplicate, prompt-quality, temporal-structure, "
                    "geometry, self-healing, MarketEvolve proof, and scale-volume gates. "
                    "It earns a repeat 1,000-scout shadow-production trial. Scheduled "
                    "production remains blocked until repeatability is proven across independent runs."
                ),
            }
        if int(sample.get("requested") or 0) >= int(thresholds["distribution_min_scouts"]):
            return {
                "decision": "promote_to_1000_scout_ramp",
                "promoted_candidate_id": winner["candidate_id"],
                "ready_for_live_100": True,
                "ready_for_live_1000": True,
                "ready_for_scheduled_production": False,
                "reason": (
                    "The 100+ scout live distribution passed provider reliability, "
                    "string yield, duplicate, prompt-quality, temporal-structure, "
                    "geometry, self-healing, and MarketEvolve proof gates. A 1,000-scout "
                    "ramp is allowed under a hard cap; it is still a ramp, not an always-on "
                    "production schedule."
                ),
            }
        return {
            "decision": "promote_to_100_scout_ramp",
            "promoted_candidate_id": winner["candidate_id"],
            "ready_for_live_100": True,
            "ready_for_live_1000": False,
            "ready_for_scheduled_production": False,
            "reason": (
                "The best live candidate passed sample size, provider reliability, "
                "string yield, evidence, duplicate, geometry, and self-healing gates. "
                "A 100-scout ramp is allowed; direct 1,000 remains blocked until the "
                "100-scout distribution is clean."
            ),
        }
    return {
        "decision": "no_promotion",
        "promoted_candidate_id": "",
        "ready_for_live_100": False,
        "ready_for_live_1000": False,
        "ready_for_scheduled_production": False,
        "reason": (
            f"The top candidate `{winner['candidate_id']}` still failed: "
            f"{', '.join(winner.get('failed_gates') or [])}. "
            f"Do not promote the {_stage_name(winner)} stage yet."
        ),
    }


def _next_experiment_plan(
    *,
    winner: dict[str, Any] | None,
    candidates: list[dict[str, Any]],
    thresholds: dict[str, float | int],
    repeatability: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    repeatability = repeatability if isinstance(repeatability, dict) else {}
    decision = decision if isinstance(decision, dict) else {}
    if not winner:
        return [
            {
                "id": "run_initial_live_canary",
                "purpose": "Create the first measured live artifact.",
                "command": (
                    "PYTHONPATH=. python scripts/run_live_scout_canary.py --n-scouts 10 "
                    "--concurrency 1 --cost-cap-usd 0.10 --provider-timeout-s 45 "
                    "--prompt-variant flash_compact_v2 --max-tool-iterations 0 --allow-live-spend"
                ),
                "promotion_rule": "Only promote if every tournament hard gate passes.",
            }
        ]
    if decision.get("ready_for_scheduled_production"):
        variant = (winner.get("configuration") or {}).get("prompt_variants") or ["flash_temporal_v4"]
        prompt_variant = str(variant[0] or "flash_temporal_v4")
        max_tool_iterations = int((winner.get("configuration") or {}).get("max_tool_iterations") or 0)
        ramp_policy_arg = _ramp_policy_command_arg(winner)
        return [
            {
                "id": "schedule_guarded_shadow_production",
                "purpose": (
                    "Move from one-off canaries to a scheduled shadow-production job while keeping "
                    "hard spend caps, full artifact capture, and no trade execution."
                ),
                "command": (
                    "PYTHONPATH=.:talis_tic python scripts/run_live_scout_canary.py --n-scouts 1000 "
                    "--concurrency 8 --cost-cap-usd 5.00 --provider-timeout-s 45 "
                    f"--prompt-variant {prompt_variant} --max-tool-iterations {max_tool_iterations} "
                    f"{ramp_policy_arg}--allow-live-spend"
                ),
                "promotion_rule": (
                    "A scheduled job remains shadow-only unless daily tournament reports keep "
                    "provider errors, duplicate rate, structural flags, geometry cells, and "
                    "information-string yield inside the proven repeatability envelope."
                ),
                "repeatability_evidence": {
                    "shadow_run_count": repeatability.get("shadow_run_count"),
                    "policy_signature": repeatability.get("policy_signature"),
                    "candidate_ids": repeatability.get("candidate_ids"),
                    "stability_gates": repeatability.get("stability_gates"),
                },
            }
        ]
    if winner.get("promotion_eligible"):
        sample = winner.get("sample") if isinstance(winner.get("sample"), dict) else {}
        max_tool_iterations = int((winner.get("configuration") or {}).get("max_tool_iterations") or 0)
        ramp_policy_arg = _ramp_policy_command_arg(winner)
        if int(sample.get("requested") or 0) >= int(thresholds["scale_min_scouts"]):
            return [
                {
                    "id": "repeat_1000_shadow_trial",
                    "purpose": "Prove the 1,000-scout policy is repeatable before any scheduled production posture.",
                    "command": (
                        "PYTHONPATH=.:talis_tic python scripts/run_live_scout_canary.py --n-scouts 1000 "
                        "--concurrency 8 --cost-cap-usd 5.00 --provider-timeout-s 45 "
                        f"--prompt-variant {winner['configuration']['prompt_variants'][0]} "
                        f"--max-tool-iterations {max_tool_iterations} --seed-rng 20260523 "
                        f"{ramp_policy_arg}--allow-live-spend"
                    ),
                    "promotion_rule": (
                        "Scheduled production requires two independent 1,000-scout shadow runs with provider errors <= "
                        f"{thresholds['scale_max_provider_error_rate']}, success >= {thresholds['scale_min_success_rate']}, "
                        f"duplicate rate <= {thresholds['scale_max_duplicate_rate']}, structural flag rate <= "
                        f"{thresholds['scale_max_structural_flag_rate']}, and stable geometry/coverage deltas."
                    ),
                }
            ]
        if int(sample.get("requested") or 0) >= int(thresholds["distribution_min_scouts"]):
            return [
                {
                    "id": "live_1000_ramp",
                    "purpose": "Validate the winning policy at broad market-sensing scale under a hard cap.",
                    "command": (
                        "PYTHONPATH=.:talis_tic python scripts/run_live_scout_canary.py --n-scouts 1000 "
                        "--concurrency 8 --cost-cap-usd 5.00 --provider-timeout-s 45 "
                        f"--prompt-variant {winner['configuration']['prompt_variants'][0]} "
                        f"--max-tool-iterations {max_tool_iterations} {ramp_policy_arg}--allow-live-spend"
                    ),
                    "promotion_rule": (
                        "Promote to a repeat 1,000-scout shadow trial only if the 1,000-scout run keeps provider errors <= "
                        f"{thresholds['distribution_max_provider_error_rate']}, success >= {thresholds['distribution_min_success_rate']}, "
                        f"duplicate rate <= {thresholds['distribution_max_duplicate_rate']}, structural misses <= "
                        f"{thresholds['distribution_max_structural_flag_rate']}, and produces usable geometry/coverage deltas. "
                        "Scheduled production remains blocked until an independent repeat 1,000 run passes."
                    ),
                }
            ]
        return [
            {
                "id": "live_100_ramp",
                "purpose": "Validate the winning policy at distributional scale.",
                "command": (
                    "PYTHONPATH=.:talis_tic python scripts/run_live_scout_canary.py --n-scouts 100 "
                    "--concurrency 4 --cost-cap-usd 1.00 --provider-timeout-s 45 "
                    f"--prompt-variant {winner['configuration']['prompt_variants'][0]} "
                    f"--max-tool-iterations {max_tool_iterations} {ramp_policy_arg}--allow-live-spend"
                ),
                "promotion_rule": (
                    "Promote to 1,000 only if the 100-scout run keeps provider errors <= "
                    f"{thresholds['max_provider_error_rate']}, success >= {thresholds['min_success_rate']}, "
                    f"and duplicate rate <= {thresholds['max_duplicate_rate']}."
                ),
            }
        ]
    failed = set(winner.get("failed_gates") or [])
    plan: list[dict[str, Any]] = []
    sample = winner.get("sample") if isinstance(winner.get("sample"), dict) else {}
    requested = int(sample.get("requested") or 0)
    ramp_policy_arg = _ramp_policy_command_arg(winner)
    scale_quality_failed = bool({
        "distribution_success_rate_ge_0_90",
        "distribution_structural_flag_rate_le_0_10",
        "scale_success_rate_ge_0_90",
        "scale_structural_flag_rate_le_0_10",
        "scale_information_strings_ge_1000",
    }.intersection(failed))
    market_evolve_failed = any(name.startswith("distribution_market_evolve_") or name.startswith("scale_market_evolve_") for name in failed)
    ramp_policy_rehearsal_failed = any(name.startswith("ramp_policy_rehearsal_") for name in failed)
    tool_creation_failed = any(name.startswith("tool_creation_") for name in failed)
    if ramp_policy_rehearsal_failed:
        policy_path = str(((winner.get("configuration") or {}).get("ramp_policy_path")) or "PROMPT_OUTPUT_DIR/live_scout_ramp_policy.json")
        plan.append({
            "id": "ramp_policy_rehearsal_repair",
            "purpose": (
                "Repair the learned next-run policy before spending on another scale run. "
                "The policy must first prove, without provider calls, that it attaches strict "
                "contracts, repair IDs, watch metrics, refreshed tools, and source-family coverage."
            ),
            "command": (
                "PYTHONPATH=. python scripts/run_live_scout_canary.py --n-scouts 12 "
                "--concurrency 1 --cost-cap-usd 0.01 --provider-timeout-s 45 "
                "--max-tool-iterations 1 "
                f"--ramp-policy {shlex.quote(policy_path)}"
            ),
            "promotion_rule": (
                "Do not run the next paid ramp until live_scout_ramp_policy_rehearsal.json has "
                "status=pass, decision=policy_can_gate_live_spend, tool refresh >= 95%, source "
                "target coverage >= 60%, strict contract attachment at 100%, and zero over-limit cells."
            ),
        })
    if tool_creation_failed:
        plan.append({
            "id": "tool_creation_quality_repair_100",
            "purpose": (
                "Repair the agent-created tool surface before buying a larger scout ramp. "
                "Scouts may request and create tools, but every proposed tool needs an expected "
                "market-map edge, decision-change claim, and deterministic eval plan."
            ),
            "command": (
                "PYTHONPATH=. python scripts/run_scout_system_launch_gate.py --allow-live-spend "
                "--live-scouts 100 --live-cost-cap-usd 1.00 --live-concurrency 4 "
                f"--max-tool-iterations 1 {ramp_policy_arg}".rstrip()
            ),
            "promotion_rule": (
                "Do not promote to 1,000 until analysis_tool_proposals pass quality >= 70%, "
                "eval-plan attachment >= 85%, expected-edge attachment >= 60%, "
                "decision-change attachment >= 60%, and eval/runtime backlog stays bounded."
            ),
        })
    if market_evolve_failed:
        plan.append({
            "id": "market_evolve_proof_repair_100",
            "purpose": (
                "Repair the evolution harness before buying scale: every 100+ run must stamp policy "
                "on seeds, include matched control/candidate slices, and evaluate falsification gates."
            ),
            "command": (
                "PYTHONPATH=.:talis_tic python scripts/run_live_scout_canary.py --n-scouts 100 "
                "--concurrency 4 --cost-cap-usd 1.00 --provider-timeout-s 45 "
                "--prompt-variant flash_temporal_v4 --max-tool-iterations 0 "
                "--market-evolve-pairs 20 --allow-live-spend"
            ),
            "promotion_rule": (
                "Do not promote any 100+ scout distribution unless the tournament sees "
                "MarketEvolve policy applications for every seed, at least 20 matched pairs, "
                "both experiment arms, an evaluated result, and falsification gates."
            ),
        })
    if requested >= int(thresholds["scale_min_scouts"]) and scale_quality_failed:
        plan.append({
            "id": "flash_temporal_v4_repair_200",
            "purpose": "Repair the 1,000-run failure mode before buying another broad ramp: missing top-level hypotheses, empty model strings, and structural metadata gaps.",
            "command": (
                "PYTHONPATH=. python scripts/run_live_scout_canary.py --n-scouts 200 "
                "--concurrency 4 --cost-cap-usd 1.00 --provider-timeout-s 45 "
                "--prompt-variant flash_temporal_v4 --max-tool-iterations 0 "
                "--seed-rng 20260524 --allow-live-spend"
            ),
            "promotion_rule": (
                "Only rerun 1,000 if the 200-scout repair arm achieves success >= "
                f"{thresholds['distribution_min_success_rate']}, structural flag rate <= "
                f"{thresholds['distribution_max_structural_flag_rate']}, provider errors <= "
                f"{thresholds['distribution_max_provider_error_rate']}, duplicate rate <= "
                f"{thresholds['distribution_max_duplicate_rate']}, and preserves string yield."
            ),
        })
        plan.append({
            "id": "seed_routing_gap_audit",
            "purpose": "Audit failed cells by lens/horizon/source family so scarce scouts stop landing on cells whose required evidence route is missing.",
            "command": (
                "python - <<'PY'\n"
                "import json, collections, pathlib\n"
                "p=pathlib.Path('PROMPT_OUTPUT_DIR/live_scout_canary_outputs.json')\n"
                "rows=json.loads(p.read_text())\n"
                "bad=[r for r in rows if any('missing_hypothesis' in str(f) or 'missing_information_strings' in str(f) or 'unresolved_evidence_refs' in str(f) for f in (r.get('quality_flags') or []))]\n"
                "print(collections.Counter((r.get('lens'), r.get('horizon')) for r in bad).most_common(30))\n"
                "PY"
            ),
            "promotion_rule": "The repair prompt is not enough if failures cluster in lenses whose tool routes are missing; patch routing/tool coverage before another 1,000 run.",
        })
    if "provider_error_rate_le_max" in failed or "avg_latency_le_max" in failed:
        plan.append({
            "id": "flash_compact_latency_arm",
            "purpose": "Test whether a genuinely compact Flash prompt removes timeout brittleness.",
            "command": (
                "PYTHONPATH=. python scripts/run_live_scout_canary.py --n-scouts 10 "
                "--concurrency 1 --cost-cap-usd 0.10 --provider-timeout-s 45 "
                "--prompt-variant flash_compact_v2 --max-tool-iterations 0 --allow-live-spend"
            ),
            "promotion_rule": "Provider error rate must be <= 10% with >= 1 string/scout before any 100-scout ramp.",
        })
        plan.append({
            "id": "fallback_primary_latency_arm",
            "purpose": "Separate prompt length from DeepSeek provider reliability by running the same compact contract on the fallback model as primary.",
            "command": (
                "PYTHONPATH=. python scripts/run_live_scout_canary.py --n-scouts 10 "
                "--concurrency 1 --cost-cap-usd 0.20 --provider-timeout-s 45 "
                "--model anthropic:claude-haiku-4-5 --fallback deepseek:v4-flash "
                "--prompt-variant flash_compact_v2 --max-tool-iterations 0 --allow-live-spend"
            ),
            "promotion_rule": "If this passes and DeepSeek fails, route DeepSeek only to cells whose prompt budget fits its latency envelope.",
        })
    if "duplicate_rate_le_max" in failed:
        plan.append({
            "id": "anti_duplicate_route_arm",
            "purpose": "Apply explicit prior-string pressure so scouts extend or contradict the map instead of restating the same claim.",
            "command": (
                "PYTHONPATH=. python scripts/run_live_scout_canary.py --n-scouts 10 "
                "--concurrency 1 --cost-cap-usd 0.10 --provider-timeout-s 45 "
                "--prompt-variant flash_compact_v2 --max-tool-iterations 0 --seed-rng 20260523 --allow-live-spend"
            ),
            "promotion_rule": "Duplicate hypothesis rate must fall below 35% while preserving string yield and evidence support.",
        })
    if (
        "structural_flag_rate_le_max" in failed
        or "distribution_structural_flag_rate_le_0_10" in failed
        or "scale_structural_flag_rate_le_0_10" in failed
        or "avg_prompt_quality_ge_min" in failed
        or "low_prompt_quality_rate_le_max" in failed
    ):
        plan.append({
            "id": "flash_temporal_quality_arm",
            "purpose": "Keep the compact latency envelope while restoring mandatory temporal metadata.",
            "command": (
                "PYTHONPATH=. python scripts/run_live_scout_canary.py --n-scouts 10 "
                "--concurrency 1 --cost-cap-usd 0.10 --provider-timeout-s 45 "
                "--prompt-variant flash_temporal_v4 --max-tool-iterations 0 --allow-live-spend"
            ),
            "promotion_rule": "Temporal/structural quality flags must stay <= 10% while provider errors remain <= 10%.",
        })
    if "sample_size_ge_min" in failed:
        plan.append({
            "id": "complete_10_scout_sample",
            "purpose": "One-scout success is not proof. Finish a full 10-scout sample under the same capture harness.",
            "command": (
                "PYTHONPATH=. python scripts/run_live_scout_canary.py --n-scouts 10 "
                    "--concurrency 1 --cost-cap-usd 0.10 --provider-timeout-s 45 "
                    "--prompt-variant flash_temporal_v3 --max-tool-iterations 0 --allow-live-spend"
                ),
            "promotion_rule": "The 10-scout sample must pass every tournament gate.",
        })
    return plan[:4]


def _system_performance(
    candidates: list[dict[str, Any]],
    *,
    winner: dict[str, Any] | None,
    repeatability: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not candidates:
        return {
            "summary": "No canary evidence was available.",
            "ready_for_full_run": False,
        }
    repeatability = repeatability if isinstance(repeatability, dict) else {}
    decision = decision if isinstance(decision, dict) else {}
    best = winner or candidates[0]
    q = best["quality"]
    m = best["map_effect"]
    sample = best.get("sample") if isinstance(best.get("sample"), dict) else {}
    requested = int(sample.get("requested") or 0)
    if decision.get("ready_for_scheduled_production"):
        summary = (
            "The live policy has now passed two independent 1,000-scout shadow runs "
            "with stable quality, geometry, prompt structure, duplicate rate, and "
            "information-string yield. It is ready for a guarded scheduled shadow "
            "production job, with trade execution still disabled."
        )
    elif best.get("promotion_eligible"):
        if requested >= int(DEFAULT_THRESHOLDS["scale_min_scouts"]):
            summary = (
                "The live 1,000-scout distribution is clean enough for a repeated "
                "shadow-production trial: provider calls completed, strings were stored, "
                "synthesis promoted hypotheses, geometry cells were created, and self-healing "
                "tasks ran at scale, with MarketEvolve proof captured. The remaining blocker "
                "is repeatability across an independent 1,000-scout run before any scheduled "
                "production posture."
            )
        elif requested >= int(DEFAULT_THRESHOLDS["distribution_min_scouts"]):
            summary = (
                "The live 100+ scout distribution is clean enough for a capped 1,000-scout ramp: "
                "provider calls completed, strings were stored, synthesis promoted hypotheses, "
                "geometry cells were created, self-healing tasks ran, and MarketEvolve evaluated "
                "matched control/candidate slices. The remaining blocker is 1,000-scout stability "
                "before any scheduled production posture."
            )
        else:
            summary = (
                "The live 10-scout distribution is clean enough for a 100-scout ramp: "
                "provider calls completed, strings were stored, synthesis promoted hypotheses, "
                "geometry cells were created, and self-healing tasks ran. The remaining blocker "
                "is distributional stability at 100, not the 10-scout gate."
            )
    else:
        if requested >= int(DEFAULT_THRESHOLDS["scale_min_scouts"]):
            summary = (
                "The 1,000-scout map path functioned: strings were stored, synthesis promoted "
                "hypotheses, geometry cells were created, and self-healing tasks ran. The current "
                "blocker is scale-quality: too many scouts left the top-level hypothesis or model "
                "string contract empty, and structural metadata misses exceeded the 10% gate."
            )
        else:
            summary = (
                "The map path is functioning when provider calls complete: strings are stored, "
                "synthesis promotes hypotheses, geometry cells are created, and self-healing tasks run. "
                "The current blocker is provider/prompt reliability, not graph storage."
            )
    return {
        "summary": summary,
        "ready_for_full_run": bool(decision.get("ready_for_scheduled_production")),
        "best_candidate_id": best["candidate_id"],
        "best_score": best["score"],
        "best_success_rate": q["success_rate"],
        "best_provider_error_rate": q["provider_error_rate"],
        "best_strings_per_scout": q["strings_per_scout"],
        "best_duplicate_rate": q["duplicate_hypothesis_rate"],
        "best_geometry_cells": m["geometry_cells"],
        "best_coverage_ratio": m["coverage_ratio"],
        "best_market_evolve": best.get("market_evolve") or {},
        "shadow_repeatability": repeatability,
        "full_run_boundary": (
            "A guarded scheduled shadow-production job is allowed; live trading remains out of scope until verifier/trade-execution gates are separately proven."
            if decision.get("ready_for_scheduled_production") else
            "Scheduled production remains blocked until a repeat 1,000-scout shadow trial passes with stable provider reliability, duplicate rate, prompt structure, geometry, and coverage deltas."
            if best.get("promotion_eligible") and requested >= int(DEFAULT_THRESHOLDS["scale_min_scouts"]) else
            (
                "A 1,000-scout ramp is now allowed under a hard cap, but scheduled production remains blocked until that broader run repeats the same quality curve."
                if best.get("promotion_eligible") and requested >= int(DEFAULT_THRESHOLDS["distribution_min_scouts"]) else
                (
                    "Do not repeat the 1,000-scout ramp until a smaller repair arm proves success >= 90% and structural misses <= 10%."
                    if requested >= int(DEFAULT_THRESHOLDS["scale_min_scouts"]) else
                    "A 100-scout live ramp is blocked unless one candidate passes all hard gates. A 1,000-scout run remains blocked until the 100-scout ramp repeats the same curve."
                )
            )
        ),
    }


def _interpret_candidate(candidate: dict[str, Any]) -> str:
    if candidate["promotion_eligible"]:
        sample = candidate.get("sample") if isinstance(candidate.get("sample"), dict) else {}
        if int(sample.get("requested") or 0) >= DEFAULT_THRESHOLDS["scale_min_scouts"]:
            return "This 1,000-scout distribution is clean enough for a repeat shadow-production trial, but not scheduled production."
        if int(sample.get("requested") or 0) >= DEFAULT_THRESHOLDS["distribution_min_scouts"]:
            return "This 100-scout distribution is clean enough for a capped 1,000-scout ramp, but not scheduled production."
        return "This candidate is clean enough for a 100-scout live ramp, but not for direct 1,000."
    failed = set(candidate.get("failed_gates") or [])
    fragments = []
    if "provider_error_rate_le_max" in failed:
        fragments.append("provider reliability is not stable enough")
    if (
        "tool_error_rate_le_max" in failed
        or "distribution_tool_error_rate_le_0_02" in failed
        or "scale_tool_error_rate_le_0_02" in failed
    ):
        fragments.append("tool/source execution has unresolved errors")
    if "original_canary_status_pass" in failed:
        fragments.append("the source canary verdict was not a clean pass")
    if "structural_flag_rate_le_max" in failed:
        fragments.append("the output contract is missing required temporal/structural fields")
    if "distribution_structural_flag_rate_le_0_10" in failed or "scale_structural_flag_rate_le_0_10" in failed:
        fragments.append("structural misses are above the 10% distribution gate")
    if "distribution_success_rate_ge_0_90" in failed or "scale_success_rate_ge_0_90" in failed:
        fragments.append("useful scout completion is below the 90% scale gate")
    if any(name.startswith("ramp_policy_rehearsal_") for name in failed):
        fragments.append("the learned next-run policy has not passed the no-spend rehearsal gate")
    if any(name.startswith("tool_creation_") for name in failed):
        fragments.append("agent-created tool proposals are not yet evaluator-grade")
    if any(name.startswith("distribution_market_evolve_") or name.startswith("scale_market_evolve_") for name in failed):
        fragments.append("the evolution proof loop did not run cleanly across matched control/candidate slices")
    if "avg_prompt_quality_ge_min" in failed or "low_prompt_quality_rate_le_max" in failed:
        fragments.append("prompt quality is not consistently high enough")
    if "success_rate_ge_min" in failed:
        fragments.append("too few scouts completed")
    if "duplicate_rate_le_max" in failed:
        fragments.append("outputs are still too redundant")
    if "strings_per_scout_ge_min" in failed:
        fragments.append("string yield is too low")
    if "sample_size_ge_min" in failed:
        fragments.append("sample size is not credible")
    if not fragments:
        fragments.append("one or more map-quality gates failed")
    return "Blocked from scale because " + ", ".join(fragments) + "."


def _stage_name(candidate: dict[str, Any]) -> str:
    level = _stage_level(candidate)
    if level >= 3:
        return "1,000-scout"
    if level == 2:
        return "100-scout"
    if level == 1:
        return "10-scout"
    return "canary"


def _prompt_variants(report: dict[str, Any], scouts: dict[str, Any], output_rows: list[Any]) -> list[str]:
    variants: set[str] = set()
    override = str(report.get("prompt_variant_override") or "").strip()
    if override:
        variants.add(override)
    raw = scouts.get("prompt_variants")
    if isinstance(raw, dict):
        variants.update(str(k) for k in raw if str(k).strip())
    elif isinstance(raw, list):
        variants.update(str(x) for x in raw if str(x).strip())
    for row in output_rows:
        if not isinstance(row, dict):
            continue
        for flag in row.get("quality_flags") or []:
            text = str(flag)
            if text.startswith("prompt_variant:"):
                variants.add(text.split(":", 1)[1])
    return sorted(variants) or ["receptive_field_v1"]


def _candidate_id(report: dict[str, Any], prompt_variants: list[str]) -> str:
    raw = "_".join([
        str(prompt_variants[0] if prompt_variants else "unknown"),
        str(report.get("model") or "model"),
        f"n{report.get('n_scouts_requested') or 0}",
        f"t{report.get('provider_timeout_s') or 0}",
        f"c{report.get('concurrency') or 0}",
        f"iter{report.get('max_tool_iterations') if report.get('max_tool_iterations') is not None else 'na'}",
        str(report.get("cycle_id") or "")[-8:],
    ])
    return re.sub(r"[^A-Za-z0-9_]+", "_", raw).strip("_").lower()


def _load_transcript(report_path: Path) -> dict[str, Any]:
    for filename in ("live_scout_transcript.json", "live_scout_canary_transcript_progress.json"):
        raw = _read_sibling_json(report_path, filename)
        if isinstance(raw, dict):
            return raw
    return {"calls": []}


def _transcript_errors(transcript: dict[str, Any], summary: dict[str, Any]) -> list[str]:
    errors = [
        str(call.get("error"))
        for call in transcript.get("calls") or []
        if isinstance(call, dict) and call.get("error")
    ]
    if errors:
        return errors
    return [str(e) for e in summary.get("errors") or [] if str(e)]


def _provider_quality_errors(scouts: dict[str, Any]) -> int:
    flags = scouts.get("top_quality_flags") if isinstance(scouts.get("top_quality_flags"), dict) else {}
    total = 0
    for key, value in flags.items():
        text = str(key).lower()
        if "provider" in text or "json_unparseable" in text or "timeout" in text:
            try:
                total += int(value)
            except Exception:
                total += 1
    return total


def _tool_call_error_metrics(report: dict[str, Any]) -> dict[str, Any]:
    db_path = Path(str(report.get("db_path") or ""))
    if not str(db_path) or not db_path.exists():
        return {
            "tool_call_count": 0,
            "tool_error_count": 0,
            "tool_error_rate": 0.0,
            "tool_error_gate_observed": False,
        }
    try:
        with sqlite3.connect(str(db_path)) as conn:
            total = int(conn.execute("SELECT count(*) FROM tool_call_log").fetchone()[0] or 0)
            errors = int(conn.execute(
                "SELECT count(*) FROM tool_call_log WHERE error IS NOT NULL AND error != ''"
            ).fetchone()[0] or 0)
    except Exception:
        return {
            "tool_call_count": 0,
            "tool_error_count": 1,
            "tool_error_rate": 1.0,
            "tool_error_gate_observed": False,
        }
    return {
        "tool_call_count": total,
        "tool_error_count": errors,
        "tool_error_rate": round(errors / max(total, 1), 6),
        "tool_error_gate_observed": True,
    }


def _structural_quality_flags(scouts: dict[str, Any]) -> int:
    flags = scouts.get("top_quality_flags") if isinstance(scouts.get("top_quality_flags"), dict) else {}
    total = 0
    for key, value in flags.items():
        text = str(key).lower()
        if (
            "missing_temporal_metadata" in text
            or "missing_information_strings" in text
            or "string_missing" in text
            or "rubric_failed" in text
            or "unresolved_evidence_refs" in text
        ):
            try:
                total += int(value)
            except Exception:
                total += 1
    return total


def _prompt_quality_metrics(scouts: dict[str, Any], *, n_requested: int) -> dict[str, Any]:
    flags = scouts.get("top_quality_flags") if isinstance(scouts.get("top_quality_flags"), dict) else {}
    weighted_total = 0.0
    count = 0
    low_count = 0
    for key, value in flags.items():
        text = str(key)
        if not text.startswith("prompt_quality:"):
            continue
        quality = _to_float(text.split(":", 1)[1], default=0.0)
        try:
            n = int(value)
        except Exception:
            n = 1
        weighted_total += quality * n
        count += n
        if quality < 0.70:
            low_count += n
    if count <= 0:
        return {
            "avg_prompt_quality": 0.0,
            "low_prompt_quality_count": max(0, int(n_requested or 0)),
            "low_prompt_quality_rate": 1.0,
        }
    denominator = max(count, int(n_requested or 0), 1)
    return {
        "avg_prompt_quality": round(weighted_total / count, 4),
        "low_prompt_quality_count": low_count,
        "low_prompt_quality_rate": round(low_count / denominator, 4),
    }


def _read_sibling_json(report_path: Path, filename: str) -> Any:
    path = report_path.parent / filename
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _default_output_dir(report_paths: list[Path]) -> Path:
    if report_paths:
        return report_paths[-1].parent
    return Path.cwd()


def _to_float(raw: Any, *, default: float = 0.0) -> float:
    try:
        return float(raw)
    except Exception:
        return default


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return round(ordered[idx], 3)


if __name__ == "__main__":
    raise SystemExit(main())
