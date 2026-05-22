from __future__ import annotations

import json
from pathlib import Path

from talis_desk.cadence import build_intelligence_cadence_plan, write_cadence_plan


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
    assert "--allow-live-spend" in live_cmd
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
    assert _arg_after(scoreboard_cmd, "--db").endswith("deterministic_100/desk-100-scout.db")
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


def _arg_after(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]
