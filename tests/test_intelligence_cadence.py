from __future__ import annotations

import json
import sys
from pathlib import Path

from talis_desk.cadence import (
    CadenceCommand,
    CadenceRunPlan,
    build_cadence_control_decision,
    build_followup_plan_from_report,
    build_followup_plan_from_scoreboard,
    build_intelligence_cadence_plan,
    execute_cadence_plan,
    write_cadence_plan,
)


def test_sentinel_plan_is_always_on_flash_without_live_spend_by_default(tmp_path: Path) -> None:
    plan = build_intelligence_cadence_plan(mode="sentinel", artifact_dir=tmp_path, cycle_id="cycle_sentinel")

    assert plan.mode == "sentinel_tick"
    assert plan.allow_live_spend is False
    assert plan.cadence_policy["always_on_flash"]["mode"] == "continuous_sentinel"
    assert [cmd.name for cmd in plan.commands] == [
        "sentinel_live_canary",
        "sentinel_market_evolve_scoreboard",
        "sentinel_agent_graph_export",
    ]

    live_cmd = plan.commands[0].command
    assert "scripts/run_live_scout_canary.py" in live_cmd
    assert "--allow-live-spend" not in live_cmd
    assert _arg_after(live_cmd, "--n-scouts") == "24"
    assert _arg_after(live_cmd, "--cost-cap-usd") == "0.26"
    assert _arg_after(live_cmd, "--max-tool-iterations") == "1"
    assert "--preserve-db" in live_cmd
    assert "--collect-price-observations" in live_cmd
    scoreboard_cmd = plan.commands[1].command
    assert _arg_after(scoreboard_cmd, "--db").endswith("live_canary/desk-live-canary.db")
    assert _arg_after(scoreboard_cmd, "--cadence-mode") == "sentinel_tick"
    assert any(gate["id"] == "no_direct_trade_publication" for gate in plan.gates)
    assert any(gate["id"] == "information_price_loop" for gate in plan.gates)


def test_sentinel_plan_can_open_explicit_live_spend_gate(tmp_path: Path) -> None:
    plan = build_intelligence_cadence_plan(
        mode="always_on",
        artifact_dir=tmp_path,
        cycle_id="cycle_sentinel_live",
        scout_count=12,
        allow_live_spend=True,
        live_cost_cap_usd=0.2,
        concurrency=3,
    )

    live_cmd = plan.commands[0].command
    scoreboard_cmd = plan.commands[1].command
    assert "--allow-live-spend" in live_cmd
    assert "--allow-live-spend" in scoreboard_cmd
    assert _arg_after(live_cmd, "--n-scouts") == "12"
    assert _arg_after(live_cmd, "--concurrency") == "3"
    assert _arg_after(live_cmd, "--cost-cap-usd") == "0.20"
    assert next(g for g in plan.gates if g["id"] == "explicit_spend_gate")["status"] == "open"


def test_full_pipeline_plan_wires_launch_gate_and_daily_brief(tmp_path: Path) -> None:
    plan = build_intelligence_cadence_plan(
        mode="full",
        artifact_dir=tmp_path,
        cycle_id="cycle_full",
        ramp_policy="/tmp/policy.json",
    )

    assert plan.mode == "full_pipeline"
    assert [cmd.name for cmd in plan.commands] == [
        "full_launch_gate",
        "full_market_evolve_scoreboard",
        "daily_brief_composition",
    ]
    launch_cmd = plan.commands[0].command
    scoreboard_cmd = plan.commands[1].command
    brief_cmd = plan.commands[2].command
    assert "scripts/run_scout_system_launch_gate.py" in launch_cmd
    assert "scripts/export_market_evolve_scoreboard.py" in scoreboard_cmd
    assert _arg_after(scoreboard_cmd, "--cycle-id") == "cycle_full_deterministic_100"
    assert _arg_after(scoreboard_cmd, "--db").endswith("deterministic_100/desk-100-scout.db")
    assert _arg_after(scoreboard_cmd, "--cadence-mode") == "full_pipeline"
    assert _arg_after(launch_cmd, "--live-scouts") == "1000"
    assert _arg_after(launch_cmd, "--live-cost-cap-usd") == "5.00"
    assert _arg_after(launch_cmd, "--ramp-policy") == "/tmp/policy.json"
    assert "--collect-price-observations" in launch_cmd
    assert "--allow-live-spend" not in launch_cmd
    assert "run_full_desk.py" in brief_cmd
    assert _arg_after(brief_cmd, "--cycle-id") == "cycle_full_brief"
    assert any(gate["id"] == "repeatability_before_schedule" and gate["status"] == "blocked" for gate in plan.gates)


def test_write_cadence_plan_persists_executable_commands(tmp_path: Path) -> None:
    plan = build_intelligence_cadence_plan(mode="sentinel_tick", artifact_dir=tmp_path, cycle_id="cycle_write")
    path = write_cadence_plan(plan)
    payload = json.loads(path.read_text())

    assert payload["schema_version"] == "talis_intelligence_cadence_run_plan_v1"
    assert payload["mode"] == "sentinel_tick"
    assert "scripts/run_live_scout_canary.py" in payload["commands"][0]["shell"]
    assert "scripts/export_market_evolve_scoreboard.py" in payload["commands"][1]["shell"]
    assert payload["cadence_policy"]["daily_brief_contract"]["brief_reads"]


def test_cadence_control_decision_turns_open_experiments_into_paired_scout_step() -> None:
    decision = build_cadence_control_decision(
        mode="sentinel_tick",
        allow_live_spend=False,
        scoreboard={
            "id": "score_open",
            "status": "experiment_running",
            "counts": {
                "open_experiments": 2,
                "candidate_programs": 2,
                "result_window": 0,
            },
            "evolution_memory": {"best_score_delta_recent": 0.0},
            "hard_experiment_gate_summary": {"triggered": 0},
            "next_actions": [{"action": "run_scouts_before_deciding_experiment"}],
        },
    )

    assert decision["decision"] == "collect_experiment_evidence"
    assert decision["allowed_next_step"] == "paired_evolution_sentinel"
    assert decision["recommended_next_run"]["scouts"] == 32
    assert decision["recommended_next_run"]["requires_allow_live_spend"] is True
    assert "explicit_live_spend_gate_required_for_recommended_run" in decision["quality_flags"]
    assert "open_experiment_without_result_window" in decision["quality_flags"]


def test_cadence_control_decision_routes_missing_proof_gate_metrics() -> None:
    decision = build_cadence_control_decision(
        mode="sentinel_tick",
        allow_live_spend=False,
        scoreboard={
            "id": "score_proof_pending",
            "status": "experiment_running",
            "counts": {
                "open_experiments": 1,
                "candidate_programs": 1,
                "result_window": 1,
            },
            "evolution_memory": {"best_score_delta_recent": 0.17},
            "hard_experiment_gate_summary": {
                "triggered": 0,
                "not_observed": 1,
                "not_observed_metrics": ["candidate_fragile_verify_rate"],
            },
            "next_actions": [
                {
                    "action": "collect_missing_falsification_gate_metrics",
                    "metrics": ["candidate_fragile_verify_rate", "candidate_avg_realized_edge_score"],
                }
            ],
        },
    )

    assert decision["decision"] == "collect_missing_proof_gate_metrics"
    assert decision["allowed_next_step"] == "proof_gate_metric_sentinel"
    assert decision["recommended_next_run"]["scouts"] == 16
    assert decision["recommended_next_run"]["requires_allow_live_spend"] is True
    assert decision["missing_proof_metrics"] == [
        "candidate_fragile_verify_rate",
        "candidate_avg_realized_edge_score",
    ]
    assert "hard_experiment_proof_gate_metrics_missing" in decision["quality_flags"]


def test_cadence_control_decision_blocks_scale_on_repair_state() -> None:
    decision = build_cadence_control_decision(
        mode="full",
        allow_live_spend=True,
        scoreboard={
            "id": "score_repair",
            "status": "repair_needed",
            "counts": {"open_experiments": 0, "candidate_programs": 1, "result_window": 1},
            "hard_experiment_gate_summary": {"triggered": 1},
            "evolution_memory": {"best_score_delta_recent": -0.1},
        },
    )

    assert decision["decision"] == "repair_before_scale"
    assert decision["blocks_wider_spend"] is True
    assert decision["spend_gate"] == "closed"


def test_cadence_control_decision_recommends_shadow_for_promoted_policy() -> None:
    decision = build_cadence_control_decision(
        mode="sentinel_tick",
        allow_live_spend=False,
        scoreboard={
            "id": "score_promoted",
            "status": "evolving_promoted_policy",
            "counts": {"open_experiments": 0, "candidate_programs": 0, "result_window": 2},
            "cadence_readiness": {"eligible_for_shadow_schedule_review": False},
            "hard_experiment_gate_summary": {"triggered": 0},
            "evolution_memory": {"best_score_delta_recent": 0.18},
        },
    )

    assert decision["decision"] == "widen_shadow_evaluation"
    assert decision["recommended_next_run"]["mode"] == "full_pipeline"
    assert decision["recommended_next_run"]["scouts"] == 1000
    assert decision["recommended_next_run"]["requires_allow_live_spend"] is True
    assert "explicit_live_spend_gate_required_for_recommended_run" in decision["quality_flags"]


def test_cadence_control_decision_uses_price_loop_hits_as_evolution_signal() -> None:
    decision = build_cadence_control_decision(
        mode="sentinel_tick",
        allow_live_spend=False,
        scoreboard={
            "id": "score_price_loop",
            "status": "baseline_active",
            "counts": {"open_experiments": 0, "candidate_programs": 0, "result_window": 3},
            "hard_experiment_gate_summary": {"triggered": 0},
            "evolution_memory": {"evolves": True, "best_score_delta_recent": 0.0},
        },
        information_price_loop={
            "status": "scored",
            "outcome_eval_count": 3,
            "outcome_observed_rate": 1.0,
            "outcome_direction_hit_rate": 0.67,
            "outcome_threshold_hit_rate": 0.67,
            "avg_realized_edge_score": 0.82,
            "early_repricing_hit_rate": 0.67,
        },
    )

    assert decision["decision"] == "price_loop_confirms_signal"
    assert decision["allowed_next_step"] == "paired_evolution_sentinel"
    assert decision["recommended_next_run"]["scouts"] == 32
    assert decision["recommended_next_run"]["requires_allow_live_spend"] is True
    assert decision["information_price_loop"]["avg_realized_edge_score"] == 0.82
    assert "information_price_loop_positive_edge" in decision["quality_flags"]


def test_cadence_control_decision_uses_perfusion_pressure_as_routing_signal() -> None:
    decision = build_cadence_control_decision(
        mode="sentinel_tick",
        allow_live_spend=False,
        scoreboard={
            "id": "score_perfusion",
            "status": "baseline_active",
            "counts": {"open_experiments": 0, "candidate_programs": 0, "result_window": 0},
            "hard_experiment_gate_summary": {"triggered": 0},
            "evolution_memory": {"evolves": True, "best_score_delta_recent": 0.0},
        },
        information_perfusion={
            "status": "ready",
            "cell_count": 2.0,
            "routed_cell_count": 1.0,
            "avg_information_pressure": 0.72,
            "avg_pressure_gradient": 0.62,
            "avg_source_oxygenation": 0.80,
            "max_dilation_score": 0.74,
            "high_pressure_unabsorbed_rate": 0.50,
            "top_cells": [{"entity": "VVV", "route_directive": "dilate_scouts"}],
        },
    )

    assert decision["decision"] == "perfusion_pressure_requests_sentinel"
    assert decision["allowed_next_step"] == "perfusion_pressure_sentinel"
    assert decision["recommended_next_run"]["scouts"] == 32
    assert decision["recommended_next_run"]["requires_allow_live_spend"] is True
    assert decision["information_perfusion"]["max_dilation_score"] == 0.74
    assert decision["information_perfusion"]["top_cells"][0]["entity"] == "VVV"
    assert "information_perfusion_positive_pressure" in decision["quality_flags"]


def test_cadence_control_decision_repairs_latched_perfusion_before_widening() -> None:
    decision = build_cadence_control_decision(
        mode="sentinel_tick",
        allow_live_spend=False,
        scoreboard={
            "id": "score_latch",
            "status": "baseline_active",
            "counts": {"open_experiments": 0, "candidate_programs": 0, "result_window": 0},
            "hard_experiment_gate_summary": {"triggered": 0},
            "evolution_memory": {"evolves": True, "best_score_delta_recent": 0.0},
        },
        information_perfusion={
            "status": "ready",
            "cell_count": 2.0,
            "routed_cell_count": 1.0,
            "avg_pressure_gradient": 0.50,
            "avg_latch_risk": 0.52,
            "high_latch_risk_rate": 0.50,
            "max_dilation_score": 0.74,
            "high_pressure_unabsorbed_rate": 0.50,
        },
    )

    assert decision["decision"] == "perfusion_latch_repair_sentinel"
    assert decision["allowed_next_step"] == "perfusion_latch_repair"
    assert decision["recommended_next_run"]["scouts"] == 24
    assert decision["recommended_next_run"]["requires_allow_live_spend"] is True
    assert decision["information_perfusion"]["avg_latch_risk"] == 0.52
    assert "information_perfusion_latch_risk" in decision["quality_flags"]


def test_execute_cadence_report_summarizes_information_price_loop(tmp_path: Path) -> None:
    prompt = tmp_path / "live_canary" / "prompt_outputs"
    prompt.mkdir(parents=True)
    (prompt / "live_price_observations_start.json").write_text(json.dumps({
        "status": "collected",
        "source": "fixture",
        "observed_count": 1,
        "persisted_count": 1,
        "observations": [{"entity": "VVV", "price": 5.0}],
    }))
    (prompt / "live_price_observations_final.json").write_text(json.dumps({
        "status": "collected",
        "source": "fixture",
        "observed_count": 1,
        "persisted_count": 1,
        "observations": [{"entity": "VVV", "price": 5.4}],
    }))
    (prompt / "information_price_outcomes.json").write_text(json.dumps({
        "schema_version": "information_price_outcome_report_v1",
        "cycle_id": "cycle_price",
        "evaluated_count": 1,
        "summary": {
            "outcome_eval_count": 1.0,
            "outcome_observed_count": 1.0,
            "outcome_observed_rate": 1.0,
            "outcome_direction_hit_rate": 1.0,
            "outcome_threshold_hit_rate": 1.0,
            "avg_realized_edge_score": 0.95,
            "early_repricing_hit_rate": 1.0,
        },
        "outcomes": [
            {
                "id": "iout_vvv",
                "string_id": "istr_vvv",
                "entity": "VVV",
                "expected_direction": "up",
                "direction_hit": True,
                "threshold_hit": True,
                "realized_edge_score": 0.95,
                "signed_return_pct": 0.08,
            }
        ],
    }))
    (prompt / "information_perfusion.json").write_text(json.dumps({
        "schema_version": "information_perfusion_export_v1",
        "cycle_id": "cycle_price",
        "global_metrics": {
            "cell_count": 1.0,
            "routed_cell_count": 1.0,
            "avg_information_pressure": 0.72,
            "avg_pressure_gradient": 0.62,
            "avg_source_oxygenation": 0.80,
            "avg_resistance": 0.22,
            "avg_latch_risk": 0.49,
            "avg_flow_shear": 0.55,
            "avg_transport_cost": 0.13,
            "avg_perfusion_efficiency": 0.72,
            "max_dilation_score": 0.74,
            "recommended_scouts": 6.0,
        },
        "quality_flags": ["has_dilation_candidates"],
        "cells": [
            {
                "cell_key": "VVV|intraday|node|fresh_social_alpha",
                "entity": "VVV",
                "theme": "fresh_social_alpha",
                "horizon": "intraday",
                "lens": "node",
                "metrics": {
                    "information_pressure": 0.72,
                    "price_absorption": 0.20,
                    "pressure_gradient": 0.62,
                    "source_oxygenation": 0.80,
                    "resistance": 0.22,
                    "dilation_score": 0.74,
                    "latch_risk": 0.49,
                    "flow_shear": 0.55,
                    "transport_cost": 0.13,
                    "perfusion_efficiency": 0.72,
                },
                "route_directive": "dilate_scouts",
                "recommended_scouts": 6,
                "quality_flags": ["information_not_absorbed_by_price", "information_latch_risk"],
            }
        ],
    }))
    plan = CadenceRunPlan(
        plan_id="icp_price",
        mode="sentinel_tick",
        cycle_id="cycle_price",
        artifact_dir=str(tmp_path),
        generated_at="2026-05-22T00:00:00+00:00",
        allow_live_spend=False,
        cadence_policy={},
        commands=[
            CadenceCommand(
                name="noop",
                command=[sys.executable, "-c", "print('ok')"],
                purpose="test command",
            )
        ],
        gates=[],
    )

    report = execute_cadence_plan(plan, repo_root=tmp_path)

    assert report["status"] == "pass"
    assert report["information_price_loop"]["status"] == "scored"
    assert report["information_price_loop"]["outcome_eval_count"] == 1.0
    assert report["information_price_loop"]["start_observations"]["observed_count"] == 1
    assert report["information_price_loop"]["top_outcomes"][0]["entity"] == "VVV"
    assert report["control_decision"]["information_price_loop"]["avg_realized_edge_score"] == 0.95
    assert report["information_perfusion"]["status"] == "ready"
    assert report["information_perfusion"]["high_pressure_unabsorbed_rate"] == 1.0
    assert report["information_perfusion"]["high_latch_risk_rate"] == 1.0
    assert report["information_perfusion"]["top_cells"][0]["route_directive"] == "dilate_scouts"
    assert report["control_decision"]["information_perfusion"]["max_dilation_score"] == 0.74
    assert report["control_decision"]["information_perfusion"]["avg_latch_risk"] == 0.49


def test_followup_plan_compiles_prior_report_control_decision_without_opening_spend_gate(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({
        "control_decision": {
            "schema_version": "talis_cadence_control_decision_v1",
            "decision": "collect_experiment_evidence",
            "allowed_next_step": "paired_evolution_sentinel",
            "source_scoreboard_id": "score_open",
            "blocks_wider_spend": False,
            "recommended_next_run": {
                "mode": "sentinel_tick",
                "scouts": 32,
                "requires_allow_live_spend": True,
                "primary_market_evolve_action": "run_scouts_before_deciding_experiment",
            },
        },
    }))

    plan = build_followup_plan_from_report(
        report_path=report_path,
        artifact_dir=tmp_path / "next",
        cycle_id="cycle_follow",
        allow_live_spend=False,
    )

    assert plan.mode == "sentinel_tick"
    assert plan.cycle_id == "cycle_follow"
    assert plan.allow_live_spend is False
    assert _arg_after(plan.commands[0].command, "--n-scouts") == "32"
    assert "--allow-live-spend" not in plan.commands[0].command
    assert any("control_decision=collect_experiment_evidence" == note for note in plan.notes)


def test_followup_plan_wires_perfusion_control_into_next_scout_slice(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({
        "control_decision": {
            "schema_version": "talis_cadence_control_decision_v1",
            "decision": "perfusion_latch_repair_sentinel",
            "allowed_next_step": "perfusion_latch_repair",
            "source_scoreboard_id": "score_latch",
            "blocks_wider_spend": False,
            "information_perfusion": {
                "cycle_id": "cycle_prior_perfusion",
                "avg_latch_risk": 0.52,
            },
            "recommended_next_run": {
                "mode": "sentinel_tick",
                "scouts": 24,
                "requires_allow_live_spend": True,
            },
        },
    }))

    plan = build_followup_plan_from_report(
        report_path=report_path,
        artifact_dir=tmp_path / "next",
        cycle_id="cycle_follow_latch",
        allow_live_spend=False,
    )

    command = plan.commands[0].command
    assert _arg_after(command, "--control-decision") == "perfusion_latch_repair_sentinel"
    assert _arg_after(command, "--control-allowed-next-step") == "perfusion_latch_repair"
    assert _arg_after(command, "--perfusion-source-cycle-id") == "cycle_prior_perfusion"
    assert plan.commands[0].expected_artifacts["control_seed_routing"].endswith(
        "live_scout_control_seed_routing.json"
    )
    assert "seed_routing=control_decision" in plan.notes


def test_followup_plan_wires_missing_proof_metrics_into_next_scout_slice(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({
        "control_decision": {
            "schema_version": "talis_cadence_control_decision_v1",
            "decision": "collect_missing_proof_gate_metrics",
            "allowed_next_step": "proof_gate_metric_sentinel",
            "source_scoreboard_id": "score_proof_pending",
            "blocks_wider_spend": False,
            "missing_proof_metrics": [
                "candidate_fragile_verify_rate",
                "candidate_avg_realized_edge_score",
            ],
            "recommended_next_run": {
                "mode": "sentinel_tick",
                "scouts": 16,
                "requires_allow_live_spend": True,
            },
        },
    }))

    plan = build_followup_plan_from_report(
        report_path=report_path,
        artifact_dir=tmp_path / "next",
        cycle_id="cycle_follow_proof",
        allow_live_spend=False,
    )

    command = plan.commands[0].command
    assert _arg_after(command, "--control-decision") == "collect_missing_proof_gate_metrics"
    assert _arg_after(command, "--control-allowed-next-step") == "proof_gate_metric_sentinel"
    assert _arg_after(command, "--control-proof-metrics") == (
        "candidate_fragile_verify_rate,candidate_avg_realized_edge_score"
    )
    assert plan.commands[0].expected_artifacts["control_seed_routing"].endswith(
        "live_scout_control_seed_routing.json"
    )
    assert "proof_gate_metrics=candidate_fragile_verify_rate,candidate_avg_realized_edge_score" in plan.notes


def test_followup_plan_respects_control_block_even_if_spend_requested(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({
        "control_decision": {
            "schema_version": "talis_cadence_control_decision_v1",
            "decision": "repair_before_scale",
            "allowed_next_step": "tool_prompt_route_repair",
            "source_scoreboard_id": "score_repair",
            "blocks_wider_spend": True,
            "recommended_next_run": {"mode": "sentinel_tick", "scouts": 8},
        },
    }))

    plan = build_followup_plan_from_report(
        report_path=report_path,
        artifact_dir=tmp_path / "next",
        allow_live_spend=True,
    )

    assert plan.allow_live_spend is False
    assert _arg_after(plan.commands[0].command, "--n-scouts") == "8"
    assert "--allow-live-spend" not in plan.commands[0].command
    assert any(gate["id"] == "control_blocks_wider_spend" for gate in plan.gates)


def test_followup_plan_from_scoreboard_builds_control_when_missing(tmp_path: Path) -> None:
    scoreboard_path = tmp_path / "scoreboard.json"
    scoreboard_path.write_text(json.dumps({
        "id": "score_promoted",
        "status": "evolving_promoted_policy",
        "counts": {"open_experiments": 0, "candidate_programs": 0, "result_window": 2},
        "cadence_readiness": {"eligible_for_shadow_schedule_review": False},
        "hard_experiment_gate_summary": {"triggered": 0},
        "evolution_memory": {"best_score_delta_recent": 0.18},
    }))

    plan = build_followup_plan_from_scoreboard(
        scoreboard_path=scoreboard_path,
        artifact_dir=tmp_path / "shadow",
        allow_live_spend=False,
    )

    assert plan.mode == "full_pipeline"
    assert plan.allow_live_spend is False
    assert _arg_after(plan.commands[0].command, "--live-scouts") == "1000"
    assert "--allow-live-spend" not in plan.commands[0].command


def _arg_after(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]
