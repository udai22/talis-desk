import json
from pathlib import Path

from scripts.evaluate_live_scout_tournament import evaluate_live_scout_tournament


def test_live_scout_tournament_blocks_failed_provider_candidate(tmp_path):
    report_path = _write_canary(
        tmp_path,
        success_rate=0.5,
        transcript_errors=5,
        duplicate_rate=0.4,
        completed=5,
    )

    tournament = evaluate_live_scout_tournament([report_path])

    assert tournament["promotion_decision"]["decision"] == "no_promotion"
    assert tournament["promotion_decision"]["ready_for_live_100"] is False
    winner = tournament["winner"]
    assert winner["promotion_eligible"] is False
    assert "provider_error_rate_le_max" in winner["failed_gates"]
    assert "duplicate_rate_le_max" in winner["failed_gates"]
    assert any(item["id"] == "flash_compact_latency_arm" for item in tournament["next_experiment_plan"])


def test_live_scout_tournament_promotes_clean_ten_scout_candidate(tmp_path):
    report_path = _write_canary(
        tmp_path,
        success_rate=0.8,
        transcript_errors=0,
        duplicate_rate=0.2,
        completed=8,
    )

    tournament = evaluate_live_scout_tournament([report_path])

    assert tournament["promotion_decision"]["decision"] == "promote_to_100_scout_ramp"
    assert tournament["promotion_decision"]["ready_for_live_100"] is True
    assert tournament["promotion_decision"]["ready_for_live_1000"] is False
    assert tournament["winner"]["promotion_eligible"] is True
    assert not tournament["winner"]["failed_gates"]


def _write_canary(
    tmp_path: Path,
    *,
    success_rate: float,
    transcript_errors: int,
    duplicate_rate: float,
    completed: int,
) -> Path:
    report_path = tmp_path / "live_scout_canary_report.json"
    report = {
        "schema_version": "talis_live_scout_canary_v1",
        "cycle_id": "cycle_test_live_canary",
        "model": "deepseek:v4-flash",
        "fallback": "anthropic:claude-haiku-4-5",
        "n_scouts_requested": 10,
        "provider_timeout_s": 45,
        "concurrency": 1,
        "seed_rng": 1,
        "prompt_variant_override": "flash_compact_v2",
        "max_tool_iterations": 0,
        "metrics": {
            "scouts": {
                "completed": completed,
                "errored": 10 - completed,
                "success_rate": success_rate,
                "avg_information_strings_per_scout": 1.2,
                "evidence_ok_rate": 0.9,
                "duplicate_hypothesis_rate": duplicate_rate,
                "total_cost_usd_estimate": 0.04,
                "top_quality_flags": {"scout_provider_unavailable": transcript_errors},
            },
            "information_map": {
                "string_count": 12,
                "cells_with_strings": 8,
                "confluences": 4,
                "tensions": 1,
                "promoted_hypotheses": 4,
            },
            "geometry": {
                "cell_count": 6,
                "routing_queue_count": 4,
            },
            "coverage": {
                "coverage_ratio": 0.01,
                "covered_count": 6,
                "valid_cell_count": 600,
            },
            "self_healing": {
                "completed_tasks": 4,
                "failed_tasks": 0,
                "tool_proposals": 2,
            },
        },
        "transcript_summary": {
            "call_count": 10,
            "errors": ["TimeoutError: "] * transcript_errors,
            "prompt_chars": 60000,
            "response_chars": 12000,
        },
        "verdict": {"status": "pass" if transcript_errors == 0 else "fail"},
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    calls = []
    for i in range(10):
        call = {"elapsed_s": 4.0, "model": "deepseek:v4-flash", "text": "{}"}
        if i < transcript_errors:
            call["error"] = "TimeoutError: "
        calls.append(call)
    (tmp_path / "live_scout_transcript.json").write_text(json.dumps({"calls": calls}), encoding="utf-8")
    outputs = [
        {"quality_flags": ["prompt_variant:flash_compact_v2"]}
        for _ in range(10)
    ]
    (tmp_path / "live_scout_canary_outputs.json").write_text(json.dumps(outputs), encoding="utf-8")
    return report_path
