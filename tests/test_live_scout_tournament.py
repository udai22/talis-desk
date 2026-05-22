import json
import sqlite3
from pathlib import Path

from scripts.evaluate_live_scout_tournament import evaluate_live_scout_tournament
from talis_desk.tool_atlas import repair_low_quality_analysis_tool_proposals


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
    assert "ramp_policy_rehearsal_status_pass" in tournament["winner"]["gates"]
    assert "--ramp-policy" in tournament["next_experiment_plan"][0]["command"]
    assert tournament["next_experiment_plan"][0]["id"] == "live_1000_ramp"


def test_live_scout_tournament_blocks_hundred_scout_distribution_without_policy_rehearsal(tmp_path):
    report_path = _write_canary(
        tmp_path,
        n_requested=100,
        success_rate=0.93,
        transcript_errors=0,
        duplicate_rate=0.06,
        completed=93,
        geometry_cells=100,
        include_ramp_policy_rehearsal=False,
    )

    tournament = evaluate_live_scout_tournament([report_path])

    assert tournament["promotion_decision"]["decision"] == "no_promotion"
    assert tournament["promotion_decision"]["ready_for_live_1000"] is False
    assert tournament["winner"]["promotion_eligible"] is False
    assert "ramp_policy_rehearsal_observed" in tournament["winner"]["failed_gates"]
    assert tournament["next_experiment_plan"][0]["id"] == "ramp_policy_rehearsal_repair"


def test_live_scout_tournament_blocks_failed_policy_rehearsal(tmp_path):
    report_path = _write_canary(
        tmp_path,
        n_requested=100,
        success_rate=0.93,
        transcript_errors=0,
        duplicate_rate=0.06,
        completed=93,
        geometry_cells=100,
        ramp_policy_rehearsal_status="fail",
        ramp_policy_rehearsal_metrics={
            "tool_candidate_refresh_rate": 0.50,
            "source_target_coverage_rate": 0.25,
            "policy_attached_rate": 1.0,
            "repair_ids_attached_rate": 1.0,
            "watch_metrics_attached_rate": 1.0,
            "strict_contract_rate": 1.0,
            "over_limit_count": 2,
        },
    )

    tournament = evaluate_live_scout_tournament([report_path])

    assert tournament["promotion_decision"]["decision"] == "no_promotion"
    assert "ramp_policy_rehearsal_status_pass" in tournament["winner"]["failed_gates"]
    assert "ramp_policy_rehearsal_tool_refresh_rate_ge_0_95" in tournament["winner"]["failed_gates"]
    assert "ramp_policy_rehearsal_over_limit_count_eq_0" in tournament["winner"]["failed_gates"]


def test_live_scout_tournament_blocks_hundred_scout_distribution_without_market_evolve_proof(tmp_path):
    report_path = _write_canary(
        tmp_path,
        n_requested=100,
        success_rate=0.93,
        transcript_errors=0,
        duplicate_rate=0.06,
        completed=93,
        geometry_cells=100,
        include_market_evolve=False,
    )

    tournament = evaluate_live_scout_tournament([report_path])

    assert tournament["promotion_decision"]["decision"] == "no_promotion"
    assert tournament["winner"]["promotion_eligible"] is False
    assert "distribution_market_evolve_policy_applied" in tournament["winner"]["failed_gates"]
    assert "distribution_market_evolve_falsification_gates_evaluated" in tournament["winner"]["failed_gates"]
    assert tournament["next_experiment_plan"][0]["id"] == "market_evolve_proof_repair_100"


def test_live_scout_tournament_blocks_low_quality_tool_creation_before_1000(tmp_path):
    report_path = _write_canary(
        tmp_path,
        n_requested=100,
        success_rate=0.93,
        transcript_errors=0,
        duplicate_rate=0.06,
        completed=93,
        geometry_cells=100,
        low_quality_tool_creation=True,
    )

    tournament = evaluate_live_scout_tournament([report_path])

    assert tournament["promotion_decision"]["decision"] == "no_promotion"
    assert tournament["winner"]["promotion_eligible"] is False
    assert "tool_creation_eval_plan_rate_ge_0_85" in tournament["winner"]["failed_gates"]
    assert "tool_creation_expected_edge_rate_ge_0_60" in tournament["winner"]["failed_gates"]
    assert tournament["winner"]["tool_creation_evolution"]["metrics"]["quality_pass_rate"] < 0.70
    assert tournament["next_experiment_plan"][0]["id"] == "tool_creation_quality_repair_100"


def test_live_scout_tournament_scores_repaired_tool_creation_frontier(tmp_path):
    report_path = _write_canary(
        tmp_path,
        n_requested=100,
        success_rate=0.93,
        transcript_errors=0,
        duplicate_rate=0.06,
        completed=93,
        geometry_cells=100,
        low_quality_tool_creation=True,
    )
    report = json.loads(report_path.read_text())
    with sqlite3.connect(report["db_path"]) as conn:
        conn.row_factory = sqlite3.Row
        repairs = repair_low_quality_analysis_tool_proposals(
            cycle_id=report["cycle_id"],
            limit=10,
            conn=conn,
        )

    tournament = evaluate_live_scout_tournament([report_path])

    assert len(repairs) == 3
    tool_creation = tournament["winner"]["tool_creation_evolution"]
    assert tool_creation["proposal_count"] == 6
    assert tool_creation["frontier_proposal_count"] == 3
    assert tool_creation["metrics"]["quality_pass_rate"] == 1.0
    assert "tool_creation_quality_pass_rate_ge_0_70" not in tournament["winner"]["failed_gates"]
    assert "tool_creation_expected_edge_rate_ge_0_60" not in tournament["winner"]["failed_gates"]


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


def test_live_scout_tournament_promotes_two_stable_thousand_runs_to_scheduled_candidate(tmp_path):
    first = _write_canary(
        tmp_path / "first",
        n_requested=1000,
        success_rate=0.983,
        transcript_errors=0,
        duplicate_rate=0.016,
        completed=983,
        structural_flags=52,
        geometry_cells=983,
        string_count=2949,
        prompt_variant="flash_temporal_v4",
        cycle_id="cycle_shadow_first",
        seed_rng=20260525,
    )
    second = _write_canary(
        tmp_path / "second",
        n_requested=1000,
        success_rate=0.971,
        transcript_errors=0,
        duplicate_rate=0.019,
        completed=971,
        structural_flags=61,
        geometry_cells=960,
        string_count=2875,
        prompt_variant="flash_temporal_v4",
        cycle_id="cycle_shadow_second",
        seed_rng=20260523,
    )

    tournament = evaluate_live_scout_tournament([first, second])

    decision = tournament["promotion_decision"]
    assert decision["decision"] == "promote_to_scheduled_production_candidate"
    assert decision["ready_for_scheduled_production"] is True
    repeatability = tournament["shadow_repeatability"]
    assert repeatability["ready_for_scheduled_production"] is True
    assert repeatability["shadow_run_count"] == 2
    assert repeatability["stability_gates"]["production_independent_seed_rng"] is True
    assert tournament["system_performance"]["ready_for_full_run"] is True
    assert tournament["next_experiment_plan"][0]["id"] == "schedule_guarded_shadow_production"


def test_live_scout_tournament_blocks_scheduled_candidate_when_repeat_is_unstable(tmp_path):
    first = _write_canary(
        tmp_path / "first",
        n_requested=1000,
        success_rate=0.98,
        transcript_errors=0,
        duplicate_rate=0.02,
        completed=980,
        structural_flags=40,
        geometry_cells=1000,
        string_count=3000,
        prompt_variant="flash_temporal_v4",
        cycle_id="cycle_shadow_first",
        seed_rng=20260525,
    )
    unstable = _write_canary(
        tmp_path / "unstable",
        n_requested=1000,
        success_rate=0.91,
        transcript_errors=0,
        duplicate_rate=0.18,
        completed=910,
        structural_flags=95,
        geometry_cells=610,
        string_count=1200,
        prompt_variant="flash_temporal_v4",
        cycle_id="cycle_shadow_unstable",
        seed_rng=20260523,
    )

    tournament = evaluate_live_scout_tournament([first, unstable])

    assert tournament["promotion_decision"]["decision"] == "promote_to_shadow_production_trial"
    assert tournament["promotion_decision"]["ready_for_scheduled_production"] is False
    repeatability = tournament["shadow_repeatability"]
    assert repeatability["ready_for_scheduled_production"] is False
    assert "production_success_rate_delta_le_max" in repeatability["failed_gates"]
    assert "production_information_string_ratio_ge_min" in repeatability["failed_gates"]
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


def test_live_scout_tournament_blocks_tool_source_errors_even_when_model_output_is_clean(tmp_path):
    report_path = _write_canary(
        tmp_path,
        n_requested=1000,
        success_rate=0.98,
        transcript_errors=0,
        duplicate_rate=0.01,
        completed=980,
        geometry_cells=980,
        string_count=2940,
        tool_call_count=3000,
        tool_error_count=167,
    )

    tournament = evaluate_live_scout_tournament([report_path])

    assert tournament["promotion_decision"]["decision"] == "no_promotion"
    assert tournament["winner"]["promotion_eligible"] is False
    assert "scale_tool_error_rate_le_0_02" in tournament["winner"]["failed_gates"]
    assert tournament["winner"]["quality"]["tool_error_count"] == 167


def test_live_scout_tournament_blocks_non_passing_canary_verdict(tmp_path):
    report_path = _write_canary(
        tmp_path,
        n_requested=100,
        success_rate=0.97,
        transcript_errors=0,
        duplicate_rate=0.02,
        completed=97,
        geometry_cells=98,
        string_count=290,
        verdict_status="warn",
    )

    tournament = evaluate_live_scout_tournament([report_path])

    assert tournament["promotion_decision"]["decision"] == "no_promotion"
    assert "original_canary_status_pass" in tournament["winner"]["failed_gates"]


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
    cycle_id: str = "cycle_test_live_canary",
    seed_rng: int = 1,
    tool_call_count: int = 10,
    tool_error_count: int = 0,
    verdict_status: str | None = None,
    include_market_evolve: bool = True,
    include_ramp_policy_rehearsal: bool | None = None,
    ramp_policy_rehearsal_status: str = "pass",
    ramp_policy_rehearsal_metrics: dict | None = None,
    include_tool_creation_evolution: bool | None = None,
    low_quality_tool_creation: bool = False,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    report_path = tmp_path / "live_scout_canary_report.json"
    db_path = tmp_path / "desk-live-canary.db"
    _write_tool_log_db(db_path, tool_call_count=tool_call_count, tool_error_count=tool_error_count)
    report = {
        "schema_version": "talis_live_scout_canary_v1",
        "cycle_id": cycle_id,
        "model": "deepseek:v4-flash",
        "fallback": "anthropic:claude-haiku-4-5",
        "n_scouts_requested": n_requested,
        "provider_timeout_s": 45,
        "concurrency": 1,
        "seed_rng": seed_rng,
        "db_path": str(db_path),
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
                "tool_proposals": 3 if n_requested >= 100 else 0,
            },
        },
        "transcript_summary": {
            "call_count": n_requested,
            "errors": ["TimeoutError: "] * transcript_errors,
            "prompt_chars": 60000,
            "response_chars": 12000,
        },
        "verdict": {"status": verdict_status or ("pass" if transcript_errors == 0 else "fail")},
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
    if include_market_evolve:
        paired_seed_slices = 20 if n_requested >= 100 else 0
        control = n_requested // 2 if paired_seed_slices else 0
        candidate = n_requested - control if paired_seed_slices else 0
        arm_counts = (
            {"control": control, "candidate": candidate}
            if paired_seed_slices else
            {"active": n_requested}
        )
        report["metrics"]["market_evolve"] = {
            "planning_experiment_count": 1 if paired_seed_slices else 0,
            "policy_application_count": n_requested,
            "paired_seed_slices": paired_seed_slices,
            "arm_counts": arm_counts,
            "final_score": 0.58,
            "final_passed": True,
            "mutation_count": 1,
            "child_program_count": 1,
            "experiment_plan_count": 1 if paired_seed_slices else 0,
            "experiment_result_count": 1 if paired_seed_slices else 0,
            "latest_experiment_decision": "reject_candidate" if paired_seed_slices else None,
            "lineage_nodes": 2,
            "lineage_edges": 1,
            "frontier_count": 1,
        }
        report_path.write_text(json.dumps(report), encoding="utf-8")
        (tmp_path / "market_evolve_hard_experiment.json").write_text(
            json.dumps({
                "schema_version": "market_evolve_hard_experiment_episode_v1",
                "status": "evaluated" if paired_seed_slices else "not_planned",
                "paired_seed_slices": paired_seed_slices,
                "policy_application_count": n_requested,
                "arm_counts": arm_counts,
                "plans": [{"experiment_kind": "matched_policy_ab"}] if paired_seed_slices else [],
                "results": [
                    {
                        "decision": "reject_candidate",
                        "score_delta": -0.001,
                        "falsification_gate_results": {"score_delta_positive": False},
                    }
                ] if paired_seed_slices else [],
                "final_decision": "reject_candidate" if paired_seed_slices else "pending",
                "final_score_delta": -0.001 if paired_seed_slices else None,
                "proof": {
                    "policy_stamped_on_seeds": n_requested > 0,
                    "matched_seed_pairs_present": paired_seed_slices > 0,
                    "control_arm_present": control > 0,
                    "candidate_arm_present": candidate > 0,
                    "experiment_result_evaluated": paired_seed_slices > 0,
                    "falsification_gates_evaluated": paired_seed_slices > 0,
                    "candidate_promoted_or_continued": False,
                    "quality_flags": [],
                },
            }),
            encoding="utf-8",
        )
    if include_ramp_policy_rehearsal is None:
        include_ramp_policy_rehearsal = n_requested >= 100
    if include_ramp_policy_rehearsal:
        policy_path = tmp_path / "live_scout_ramp_policy.json"
        policy_path.write_text(
            json.dumps({
                "schema_version": "talis_live_scout_ramp_policy_v1",
                "policy_id": "policy_test",
                "seed_payload_patch": {"prompt_contract_pressure": "strict"},
                "repair_work_order_ids": ["lso_json_unparseable_scout_harness"],
            }),
            encoding="utf-8",
        )
        metrics = {
            "tool_candidate_refresh_rate": 1.0,
            "source_target_coverage_rate": 1.0,
            "policy_attached_rate": 1.0,
            "repair_ids_attached_rate": 1.0,
            "watch_metrics_attached_rate": 1.0,
            "strict_contract_rate": 1.0,
            "over_limit_count": 0,
            "tool_candidate_added_count": 10,
            "candidate_tool_delta_avg": 1.0,
        }
        if ramp_policy_rehearsal_metrics:
            metrics.update(ramp_policy_rehearsal_metrics)
        (tmp_path / "live_scout_ramp_policy_rehearsal.json").write_text(
            json.dumps({
                "schema_version": "live_scout_ramp_policy_rehearsal_v1",
                "status": ramp_policy_rehearsal_status,
                "decision": (
                    "policy_can_gate_live_spend"
                    if ramp_policy_rehearsal_status == "pass"
                    else "repair_policy_before_live_spend"
                ),
                "score": 1.0 if ramp_policy_rehearsal_status == "pass" else 0.4,
                "metrics": metrics,
                "target_source_family_hits": {
                    "hydromancer": 5,
                    "our_node": 6,
                    "parallel_web": 4,
                },
            }),
            encoding="utf-8",
        )
    if include_tool_creation_evolution is None:
        include_tool_creation_evolution = n_requested >= 100
    if include_tool_creation_evolution:
        _write_analysis_tool_proposals(
            db_path,
            cycle_id=cycle_id,
            low_quality=low_quality_tool_creation,
        )
    return report_path


def _write_tool_log_db(path: Path, *, tool_call_count: int, tool_error_count: int) -> None:
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE tool_call_log (error TEXT)")
        for i in range(tool_call_count):
            conn.execute(
                "INSERT INTO tool_call_log (error) VALUES (?)",
                ("resolve_error" if i < tool_error_count else "",),
            )
        conn.commit()


def _write_analysis_tool_proposals(path: Path, *, cycle_id: str, low_quality: bool) -> None:
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_tool_proposals (
                id TEXT PRIMARY KEY,
                cycle_id TEXT,
                artifact_kind TEXT,
                artifact_id TEXT,
                entity TEXT,
                horizon TEXT,
                lens TEXT,
                proposal_kind TEXT,
                tool_name TEXT,
                purpose TEXT,
                source_family TEXT,
                trigger TEXT,
                input_shape_json TEXT,
                promotion_gate_json TEXT,
                eval_plan_json TEXT,
                priority TEXT,
                status TEXT,
                parent_proposal_id TEXT,
                iteration INTEGER,
                created_by TEXT,
                quality_flags TEXT,
                created_at TEXT,
                valid_from TEXT,
                transaction_from TEXT,
                transaction_to TEXT
            )
            """
        )
        rows = [
            ("hl_node_stream_reader", "our_hl_node"),
            ("hydromancer_actor_quality_bulk", "hydromancer"),
            ("hyperevm_mempool_actor_watch", "mempool"),
        ]
        for i, (tool_name, source_family) in enumerate(rows):
            eval_plan = (
                {}
                if low_quality else
                {"fixtures": ["source_event", "rejection_path"], "min_pass_rate": 0.8}
            )
            promotion_gate = (
                {"has_source": True}
                if low_quality else
                {
                    "expected_edge": f"{source_family} -> HYPE/intraday/on_chain edge",
                    "expected_info_value": 0.78,
                    "would_change_decision": True,
                    "must_emit_source_timestamp_or_rejection": True,
                }
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO analysis_tool_proposals (
                    id, cycle_id, artifact_kind, artifact_id, entity, horizon,
                    lens, proposal_kind, tool_name, purpose, source_family,
                    trigger, input_shape_json, promotion_gate_json, eval_plan_json,
                    priority, status, parent_proposal_id, iteration, created_by,
                    quality_flags, created_at, valid_from, transaction_from,
                    transaction_to
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    f"atp_test_{i}",
                    cycle_id,
                    "node_intelligence",
                    f"nint_{i}",
                    "HYPE",
                    "intraday",
                    "on_chain",
                    "new_tool",
                    tool_name,
                    "Create a sourced read-only analysis tool for a missing market-map edge.",
                    source_family,
                    "node_intelligence_coverage_gap",
                    json.dumps({"entity": "HYPE", "source_family": source_family}),
                    json.dumps(promotion_gate),
                    json.dumps(eval_plan),
                    "high",
                    "proposed",
                    "",
                    0,
                    "test",
                    json.dumps([]),
                    "2026-05-22T00:00:00+00:00",
                    "2026-05-22T00:00:00+00:00",
                    "2026-05-22T00:00:00+00:00",
                ),
            )
        conn.commit()
