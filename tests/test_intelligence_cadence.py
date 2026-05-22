from __future__ import annotations

import json
from pathlib import Path

from talis_desk.cadence import (
    build_cadence_control_decision,
    build_followup_plan_from_report,
    build_followup_plan_from_scoreboard,
    build_intelligence_cadence_plan,
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
    scoreboard_cmd = plan.commands[1].command
    assert _arg_after(scoreboard_cmd, "--db").endswith("live_canary/desk-live-canary.db")
    assert _arg_after(scoreboard_cmd, "--cadence-mode") == "sentinel_tick"
    assert any(gate["id"] == "no_direct_trade_publication" for gate in plan.gates)


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
