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


def test_live_scout_tournament_promotes_clean_hundred_scout_distribution_to_1000(tmp_path):
    report_path = _write_canary(
        tmp_path,
        n_requested=100,
        success_rate=0.93,
        transcript_errors=0,
        duplicate_rate=0.06,
        completed=93,
        geometry_cells=100,
    )

    tournament = evaluate_live_scout_tournament([report_path])

    assert tournament["promotion_decision"]["decision"] == "promote_to_1000_scout_ramp"
    assert tournament["promotion_decision"]["ready_for_live_1000"] is True
    assert tournament["winner"]["promotion_eligible"] is True
    assert "distribution_success_rate_ge_0_90" in tournament["winner"]["gates"]
    assert tournament["next_experiment_plan"][0]["id"] == "live_1000_ramp"


def test_live_scout_tournament_promotes_clean_thousand_scout_distribution_to_shadow_trial(tmp_path):
    report_path = _write_canary(
        tmp_path,
        n_requested=1000,
        success_rate=0.93,
        transcript_errors=0,
        duplicate_rate=0.06,
        completed=930,
        geometry_cells=1000,
        string_count=2870,
    )

    tournament = evaluate_live_scout_tournament([report_path])

    assert tournament["promotion_decision"]["decision"] == "promote_to_shadow_production_trial"
    assert tournament["promotion_decision"]["ready_for_live_1000"] is True
    assert tournament["promotion_decision"]["ready_for_scheduled_production"] is False
    assert tournament["winner"]["promotion_eligible"] is True
    assert "scale_success_rate_ge_0_90" in tournament["winner"]["gates"]
    assert tournament["next_experiment_plan"][0]["id"] == "repeat_1000_shadow_trial"


def test_live_scout_tournament_routes_failed_thousand_to_repair_arm(tmp_path):
    report_path = _write_canary(
        tmp_path,
        n_requested=1000,
        success_rate=0.896,
        transcript_errors=1,
        duplicate_rate=0.103,
        completed=896,
        structural_flags=149,
        geometry_cells=982,
        string_count=2799,
    )

    tournament = evaluate_live_scout_tournament([report_path])

    assert tournament["promotion_decision"]["decision"] == "no_promotion"
    assert tournament["promotion_decision"]["ready_for_live_1000"] is False
    assert "Do not promote the 1,000-scout stage yet" in tournament["promotion_decision"]["reason"]
    assert "scale_success_rate_ge_0_90" in tournament["winner"]["failed_gates"]
    assert tournament["next_experiment_plan"][0]["id"] == "flash_temporal_v4_repair_200"


def test_live_scout_tournament_uses_later_repair_arm_after_failed_thousand(tmp_path):
    failed_path = _write_canary(
        tmp_path / "failed",
        n_requested=1000,
        success_rate=0.896,
        transcript_errors=1,
        duplicate_rate=0.103,
        completed=896,
        structural_flags=149,
        geometry_cells=982,
        string_count=2799,
        prompt_variant="flash_temporal_v3",
    )
    repair_path = _write_canary(
        tmp_path / "repair",
        n_requested=200,
        success_rate=0.975,
        transcript_errors=0,
        duplicate_rate=0.02,
        completed=195,
        structural_flags=8,
        geometry_cells=199,
        string_count=592,
        prompt_variant="flash_temporal_v4",
    )

    tournament = evaluate_live_scout_tournament([failed_path, repair_path])

    assert tournament["promotion_decision"]["decision"] == "promote_to_1000_scout_ramp"
    assert tournament["winner"]["candidate_id"].startswith("flash_temporal_v4")
    assert tournament["promotion_decision"]["ready_for_live_1000"] is True
    assert tournament["next_experiment_plan"][0]["id"] == "live_1000_ramp"


def test_live_scout_tournament_blocks_temporal_contract_regression(tmp_path):
    report_path = _write_canary(
        tmp_path,
        success_rate=1.0,
        transcript_errors=0,
        duplicate_rate=0.0,
        completed=10,
        structural_flags=10,
    )

    tournament = evaluate_live_scout_tournament([report_path])

    assert tournament["promotion_decision"]["decision"] == "no_promotion"
    winner = tournament["winner"]
    assert winner["promotion_eligible"] is False
    assert "structural_flag_rate_le_max" in winner["failed_gates"]
    assert any(item["id"] == "flash_temporal_quality_arm" for item in tournament["next_experiment_plan"])


def _write_canary(
    tmp_path: Path,
    *,
    n_requested: int = 10,
    success_rate: float,
    transcript_errors: int,
    duplicate_rate: float,
    completed: int,
    structural_flags: int = 0,
    geometry_cells: int = 6,
    string_count: int | None = None,
    prompt_variant: str = "flash_compact_v2",
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    report_path = tmp_path / "live_scout_canary_report.json"
    report = {
        "schema_version": "talis_live_scout_canary_v1",
        "cycle_id": "cycle_test_live_canary",
        "model": "deepseek:v4-flash",
        "fallback": "anthropic:claude-haiku-4-5",
        "n_scouts_requested": n_requested,
        "provider_timeout_s": 45,
        "concurrency": 1,
        "seed_rng": 1,
        "prompt_variant_override": prompt_variant,
        "max_tool_iterations": 0,
        "metrics": {
            "scouts": {
                "completed": completed,
                "errored": n_requested - completed,
                "success_rate": success_rate,
                "avg_information_strings_per_scout": 1.2,
                "evidence_ok_rate": 0.9,
                "duplicate_hypothesis_rate": duplicate_rate,
                "total_cost_usd_estimate": 0.04,
                "top_quality_flags": {
                    "scout_provider_unavailable": transcript_errors,
                    "prompt_string_missing_temporal_metadata": structural_flags,
                    "prompt_quality:0.90": n_requested,
                },
            },
            "information_map": {
                "string_count": string_count if string_count is not None else max(12, completed),
                "cells_with_strings": max(8, completed),
                "confluences": 4,
                "tensions": 1,
                "promoted_hypotheses": 4,
            },
            "geometry": {
                "cell_count": geometry_cells,
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
            "call_count": n_requested,
            "errors": ["TimeoutError: "] * transcript_errors,
            "prompt_chars": 60000,
            "response_chars": 12000,
        },
        "verdict": {"status": "pass" if transcript_errors == 0 else "fail"},
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    calls = []
    for i in range(n_requested):
        call = {"elapsed_s": 4.0, "model": "deepseek:v4-flash", "text": "{}"}
        if i < transcript_errors:
            call["error"] = "TimeoutError: "
        calls.append(call)
    (tmp_path / "live_scout_transcript.json").write_text(json.dumps({"calls": calls}), encoding="utf-8")
    outputs = [
        {"quality_flags": [f"prompt_variant:{prompt_variant}"]}
        for _ in range(n_requested)
    ]
    (tmp_path / "live_scout_canary_outputs.json").write_text(json.dumps(outputs), encoding="utf-8")
    return report_path
