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
    "distribution_min_scouts": 100,
    "distribution_min_success_rate": 0.90,
    "distribution_max_provider_error_rate": 0.02,
    "distribution_max_duplicate_rate": 0.20,
    "distribution_max_structural_flag_rate": 0.10,
    "distribution_min_geometry_cells": 50,
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
        "distribution_min_scouts": args.distribution_min_scouts,
        "distribution_min_success_rate": args.distribution_min_success_rate,
        "distribution_max_provider_error_rate": args.distribution_max_provider_error_rate,
        "distribution_max_duplicate_rate": args.distribution_max_duplicate_rate,
        "distribution_max_structural_flag_rate": args.distribution_max_structural_flag_rate,
        "distribution_min_geometry_cells": args.distribution_min_geometry_cells,
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
    parser.add_argument("--distribution-min-scouts", type=int, default=DEFAULT_THRESHOLDS["distribution_min_scouts"])
    parser.add_argument("--distribution-min-success-rate", type=float, default=DEFAULT_THRESHOLDS["distribution_min_success_rate"])
    parser.add_argument("--distribution-max-provider-error-rate", type=float, default=DEFAULT_THRESHOLDS["distribution_max_provider_error_rate"])
    parser.add_argument("--distribution-max-duplicate-rate", type=float, default=DEFAULT_THRESHOLDS["distribution_max_duplicate_rate"])
    parser.add_argument("--distribution-max-structural-flag-rate", type=float, default=DEFAULT_THRESHOLDS["distribution_max_structural_flag_rate"])
    parser.add_argument("--distribution-min-geometry-cells", type=int, default=DEFAULT_THRESHOLDS["distribution_min_geometry_cells"])
    return parser.parse_args()


def evaluate_live_scout_tournament(
    report_paths: list[Path],
    *,
    thresholds: dict[str, float | int] | None = None,
) -> dict[str, Any]:
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    candidates = [
        evaluate_live_scout_candidate(path, thresholds=thresholds)
        for path in report_paths
        if path.exists()
    ]
    candidates = [c for c in candidates if c]
    candidates.sort(key=lambda row: (_stage_level(row), bool(row["promotion_eligible"]), row["score"]), reverse=True)
    top_stage = _stage_level(candidates[0]) if candidates else 0
    promoted = [c for c in candidates if c["promotion_eligible"] and _stage_level(c) == top_stage]
    winner = promoted[0] if promoted else (candidates[0] if candidates else None)
    decision = _promotion_decision(winner=winner, promoted=promoted, thresholds=thresholds)
    report = {
        "schema_version": "talis_live_scout_tournament_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "purpose": (
            "Decide whether a live prompt/provider policy is good enough to spend on "
            "the next 100-scout ramp."
        ),
        "thresholds": thresholds,
        "input_reports": [str(p) for p in report_paths],
        "promotion_decision": decision,
        "winner": winner,
        "candidates": candidates,
        "system_performance": _system_performance(candidates, winner=winner),
        "next_experiment_plan": _next_experiment_plan(winner=winner, candidates=candidates, thresholds=thresholds),
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
    transcript = _load_transcript(path)
    transcript_summary = report.get("transcript_summary") if isinstance(report.get("transcript_summary"), dict) else {}
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
        "information_strings_created": string_count > 0,
        "synthesis_promoted": int(info.get("promoted_hypotheses") or 0) > 0,
        "geometry_cells_created": int(geometry.get("cell_count") or 0) > 0,
        "self_healing_no_failures": int(self_healing.get("failed_tasks") or 0) == 0,
    }
    if n_requested >= int(thresholds["distribution_min_scouts"]):
        gates.update({
            "distribution_sample_size_ge_100": n_requested >= int(thresholds["distribution_min_scouts"]),
            "distribution_success_rate_ge_0_90": success_rate >= float(thresholds["distribution_min_success_rate"]),
            "distribution_provider_error_rate_le_0_02": provider_error_rate <= float(thresholds["distribution_max_provider_error_rate"]),
            "distribution_duplicate_rate_le_0_20": duplicate_rate <= float(thresholds["distribution_max_duplicate_rate"]),
            "distribution_structural_flag_rate_le_0_10": structural_flag_rate <= float(thresholds["distribution_max_structural_flag_rate"]),
            "distribution_geometry_cells_ge_50": int(geometry.get("cell_count") or 0) >= int(thresholds["distribution_min_geometry_cells"]),
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
        "original_canary_verdict": report.get("verdict") if isinstance(report.get("verdict"), dict) else {},
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


def render_tournament_markdown(report: dict[str, Any]) -> str:
    decision = report.get("promotion_decision") if isinstance(report.get("promotion_decision"), dict) else {}
    lines = [
        "# Live Scout Tournament",
        "",
        f"- decision: `{decision.get('decision')}`",
        f"- ready_for_live_100: `{decision.get('ready_for_live_100')}`",
        f"- ready_for_live_1000: `{decision.get('ready_for_live_1000')}`",
        f"- reason: {decision.get('reason')}",
        "",
        "## Candidates",
        "",
    ]
    for c in report.get("candidates") or []:
        q = c.get("quality") or {}
        sample = c.get("sample") or {}
        lines.extend([
            f"### {c.get('candidate_id')}",
            "",
            f"- score: `{c.get('score')}`",
            f"- promotion_eligible: `{c.get('promotion_eligible')}`",
            f"- scouts: `{sample.get('completed')}/{sample.get('requested')}`",
            f"- success_rate: `{q.get('success_rate')}`",
            f"- provider_error_rate: `{q.get('provider_error_rate')}`",
            f"- strings_per_scout: `{q.get('strings_per_scout')}`",
            f"- duplicate_rate: `{q.get('duplicate_hypothesis_rate')}`",
            f"- avg_latency_s: `{q.get('avg_latency_s')}`",
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
    success = _to_float(q.get("success_rate"))
    strings = min(1.0, _to_float(q.get("strings_per_scout")) / 1.5)
    evidence = _to_float(q.get("evidence_ok_rate"))
    prompt_quality = _to_float(q.get("avg_prompt_quality"), default=0.0)
    geometry = 1.0 if m.get("geometry_cells") else 0.0
    self_healing = 1.0 if not m.get("self_healing_failed") else 0.0
    provider_error = _to_float(q.get("provider_error_rate"))
    duplicate = _to_float(q.get("duplicate_hypothesis_rate"), default=1.0)
    cost_per = _to_float(q.get("cost_per_scout_usd"))
    cost_eff = max(0.0, 1.0 - min(1.0, cost_per / DEFAULT_THRESHOLDS["max_cost_per_scout_usd"]))
    avg_latency = q.get("avg_latency_s")
    latency = max(0.0, 1.0 - (_to_float(avg_latency) / 60.0)) if avg_latency is not None else 0.35
    sample_bonus = min(1.0, int(sample.get("requested") or 0) / DEFAULT_THRESHOLDS["min_scouts"])
    score = (
        0.24 * success
        + 0.18 * strings
        + 0.12 * evidence
        + 0.08 * prompt_quality
        + 0.10 * geometry
        + 0.08 * self_healing
        + 0.08 * cost_eff
        + 0.10 * latency
        + 0.02 * sample_bonus
        - 0.22 * provider_error
        - 0.10 * duplicate
    )
    return round(max(0.0, min(1.0, score)), 4)


def _stage_level(candidate: dict[str, Any]) -> int:
    sample = candidate.get("sample") if isinstance(candidate.get("sample"), dict) else {}
    requested = int(sample.get("requested") or 0)
    if requested >= DEFAULT_THRESHOLDS["distribution_min_scouts"]:
        return 2
    if requested >= DEFAULT_THRESHOLDS["min_scouts"]:
        return 1
    return 0


def _promotion_decision(
    *,
    winner: dict[str, Any] | None,
    promoted: list[dict[str, Any]],
    thresholds: dict[str, float | int],
) -> dict[str, Any]:
    if not winner:
        return {
            "decision": "no_candidates",
            "promoted_candidate_id": "",
            "ready_for_live_100": False,
            "ready_for_live_1000": False,
            "reason": "No readable canary reports were provided.",
        }
    if promoted:
        sample = winner.get("sample") if isinstance(winner.get("sample"), dict) else {}
        if int(sample.get("requested") or 0) >= int(thresholds["distribution_min_scouts"]):
            return {
                "decision": "promote_to_1000_scout_ramp",
                "promoted_candidate_id": winner["candidate_id"],
                "ready_for_live_100": True,
                "ready_for_live_1000": True,
                "reason": (
                    "The 100-scout live distribution passed provider reliability, "
                    "string yield, duplicate, prompt-quality, temporal-structure, "
                    "geometry, and self-healing gates. A 1,000-scout ramp is allowed "
                    "under a hard cap; it is still a ramp, not an always-on production schedule."
                ),
            }
        return {
            "decision": "promote_to_100_scout_ramp",
            "promoted_candidate_id": winner["candidate_id"],
            "ready_for_live_100": True,
            "ready_for_live_1000": False,
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
        "reason": (
            f"The top candidate `{winner['candidate_id']}` still failed: "
            f"{', '.join(winner.get('failed_gates') or [])}. Do not spend on 100 scouts yet."
        ),
    }


def _next_experiment_plan(
    *,
    winner: dict[str, Any] | None,
    candidates: list[dict[str, Any]],
    thresholds: dict[str, float | int],
) -> list[dict[str, Any]]:
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
    if winner.get("promotion_eligible"):
        sample = winner.get("sample") if isinstance(winner.get("sample"), dict) else {}
        if int(sample.get("requested") or 0) >= int(thresholds["distribution_min_scouts"]):
            return [
                {
                    "id": "live_1000_ramp",
                    "purpose": "Validate the winning policy at broad market-sensing scale under a hard cap.",
                    "command": (
                        "PYTHONPATH=. python scripts/run_live_scout_canary.py --n-scouts 1000 "
                        "--concurrency 8 --cost-cap-usd 5.00 --provider-timeout-s 45 "
                        f"--prompt-variant {winner['configuration']['prompt_variants'][0]} "
                        "--max-tool-iterations 0 --allow-live-spend"
                    ),
                    "promotion_rule": (
                        "Promote to scheduled production only if the 1,000-scout run keeps provider errors <= "
                        f"{thresholds['distribution_max_provider_error_rate']}, success >= {thresholds['distribution_min_success_rate']}, "
                        f"duplicate rate <= {thresholds['distribution_max_duplicate_rate']}, and produces usable geometry/coverage deltas."
                    ),
                }
            ]
        return [
            {
                "id": "live_100_ramp",
                "purpose": "Validate the winning policy at distributional scale.",
                "command": (
                    "PYTHONPATH=. python scripts/run_live_scout_canary.py --n-scouts 100 "
                    "--concurrency 4 --cost-cap-usd 1.00 --provider-timeout-s 45 "
                    f"--prompt-variant {winner['configuration']['prompt_variants'][0]} "
                    "--max-tool-iterations 0 --allow-live-spend"
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
    if "structural_flag_rate_le_max" in failed or "avg_prompt_quality_ge_min" in failed or "low_prompt_quality_rate_le_max" in failed:
        plan.append({
            "id": "flash_temporal_quality_arm",
            "purpose": "Keep the compact latency envelope while restoring mandatory temporal metadata.",
            "command": (
                "PYTHONPATH=. python scripts/run_live_scout_canary.py --n-scouts 10 "
                "--concurrency 1 --cost-cap-usd 0.10 --provider-timeout-s 45 "
                "--prompt-variant flash_temporal_v3 --max-tool-iterations 0 --allow-live-spend"
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


def _system_performance(candidates: list[dict[str, Any]], *, winner: dict[str, Any] | None) -> dict[str, Any]:
    if not candidates:
        return {
            "summary": "No canary evidence was available.",
            "ready_for_full_run": False,
        }
    best = winner or candidates[0]
    q = best["quality"]
    m = best["map_effect"]
    sample = best.get("sample") if isinstance(best.get("sample"), dict) else {}
    requested = int(sample.get("requested") or 0)
    if best.get("promotion_eligible"):
        if requested >= int(DEFAULT_THRESHOLDS["distribution_min_scouts"]):
            summary = (
                "The live 100-scout distribution is clean enough for a capped 1,000-scout ramp: "
                "provider calls completed, strings were stored, synthesis promoted hypotheses, "
                "geometry cells were created, and self-healing tasks ran. The remaining blocker "
                "is 1,000-scout stability before any scheduled production posture."
            )
        else:
            summary = (
                "The live 10-scout distribution is clean enough for a 100-scout ramp: "
                "provider calls completed, strings were stored, synthesis promoted hypotheses, "
                "geometry cells were created, and self-healing tasks ran. The remaining blocker "
                "is distributional stability at 100, not the 10-scout gate."
            )
    else:
        summary = (
            "The map path is functioning when provider calls complete: strings are stored, "
            "synthesis promotes hypotheses, geometry cells are created, and self-healing tasks run. "
            "The current blocker is provider/prompt reliability, not graph storage."
        )
    return {
        "summary": summary,
        "ready_for_full_run": False,
        "best_candidate_id": best["candidate_id"],
        "best_score": best["score"],
        "best_success_rate": q["success_rate"],
        "best_provider_error_rate": q["provider_error_rate"],
        "best_strings_per_scout": q["strings_per_scout"],
        "best_duplicate_rate": q["duplicate_hypothesis_rate"],
        "best_geometry_cells": m["geometry_cells"],
        "best_coverage_ratio": m["coverage_ratio"],
        "full_run_boundary": (
            "A 1,000-scout ramp is now allowed under a hard cap, but scheduled production remains blocked until that broader run repeats the same quality curve."
            if best.get("promotion_eligible") and requested >= int(DEFAULT_THRESHOLDS["distribution_min_scouts"]) else
            "A 100-scout live ramp is blocked unless one candidate passes all hard gates. A 1,000-scout run remains blocked until the 100-scout ramp repeats the same curve."
        ),
    }


def _interpret_candidate(candidate: dict[str, Any]) -> str:
    if candidate["promotion_eligible"]:
        sample = candidate.get("sample") if isinstance(candidate.get("sample"), dict) else {}
        if int(sample.get("requested") or 0) >= DEFAULT_THRESHOLDS["distribution_min_scouts"]:
            return "This 100-scout distribution is clean enough for a capped 1,000-scout ramp, but not scheduled production."
        return "This candidate is clean enough for a 100-scout live ramp, but not for direct 1,000."
    failed = set(candidate.get("failed_gates") or [])
    fragments = []
    if "provider_error_rate_le_max" in failed:
        fragments.append("provider reliability is not stable enough")
    if "structural_flag_rate_le_max" in failed:
        fragments.append("the output contract is missing required temporal/structural fields")
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
