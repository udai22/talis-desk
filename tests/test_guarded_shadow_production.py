import json
import subprocess
import sys
from pathlib import Path

from scripts.run_guarded_shadow_production import (
    SAFETY_POLICY,
    build_guarded_shadow_plan,
    preflight_from_plan,
)


def test_guarded_shadow_plan_requires_scheduled_tournament(tmp_path):
    plan = build_guarded_shadow_plan(
        tournament={"promotion_decision": {"decision": "promote_to_shadow_production_trial"}},
        n_scouts=1000,
        concurrency=8,
        cost_cap_usd=5.0,
        provider_timeout_s=45.0,
        prompt_variant="flash_temporal_v4",
        max_tool_iterations=1,
        seed_rng=20260524,
        model="deepseek:v4-flash",
        fallback="anthropic:claude-haiku-4-5",
        artifact_dir=tmp_path / "artifacts",
        prompt_output_dir=tmp_path / "artifacts" / "prompt_outputs",
        allow_live_spend=True,
        dry_run=False,
        max_cost_cap_usd=5.0,
        max_scouts=1000,
    )

    assert plan["allowed_to_start"] is False
    assert "prior_tournament_ready_for_scheduled_production" in plan["failed_gates"]
    assert "decision_is_scheduled_candidate" in plan["failed_gates"]


def test_guarded_shadow_plan_is_shadow_only_and_builds_canary_command(tmp_path):
    plan = build_guarded_shadow_plan(
        tournament=_scheduled_tournament(),
        n_scouts=1000,
        concurrency=8,
        cost_cap_usd=5.0,
        provider_timeout_s=45.0,
        prompt_variant="",
        max_tool_iterations=-1,
        seed_rng=20260524,
        model="deepseek:v4-flash",
        fallback="anthropic:claude-haiku-4-5",
        artifact_dir=tmp_path / "artifacts",
        prompt_output_dir=tmp_path / "artifacts" / "prompt_outputs",
        allow_live_spend=True,
        dry_run=False,
        max_cost_cap_usd=5.0,
        max_scouts=1000,
    )
    preflight = preflight_from_plan(plan)

    assert plan["allowed_to_start"] is True
    assert plan["prompt_variant"] == "flash_temporal_v4"
    assert plan["max_tool_iterations"] == 1
    assert "--allow-live-spend" in plan["command"]
    assert "scripts/run_live_scout_canary.py" in plan["command"]
    assert SAFETY_POLICY["trade_execution_enabled"] is False
    assert preflight["trade_execution_enabled"] is False
    assert preflight["shadow_only"] is True


def test_guarded_shadow_cli_dry_run_writes_auditable_report(tmp_path):
    tournament_path = tmp_path / "live_scout_tournament_report.json"
    tournament_path.write_text(json.dumps(_scheduled_tournament()), encoding="utf-8")
    artifact_dir = tmp_path / "shadow"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/run_guarded_shadow_production.py",
            "--tournament-report",
            str(tournament_path),
            "--artifact-dir",
            str(artifact_dir),
            "--n-scouts",
            "10",
            "--cost-cap-usd",
            "0.05",
            "--dry-run",
        ],
        cwd="/Users/udaikhattar/talis-desk",
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report_path = artifact_dir / "prompt_outputs" / "guarded_shadow_production_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "dry_run"
    assert report["plan"]["shadow_scope"] == "scheduled_probe"
    assert report["plan"]["gates"]["trade_execution_disabled"] is True
    assert report["safety_policy"]["trade_execution_enabled"] is False
    assert "live_scout_canary_report.json" not in report


def _scheduled_tournament() -> dict:
    return {
        "schema_version": "talis_live_scout_tournament_v1",
        "input_reports": [
            "/tmp/first/live_scout_canary_report.json",
            "/tmp/second/live_scout_canary_report.json",
        ],
        "promotion_decision": {
            "decision": "promote_to_scheduled_production_candidate",
            "ready_for_live_100": True,
            "ready_for_live_1000": True,
            "ready_for_scheduled_production": True,
            "promoted_candidate_ids": ["candidate_a", "candidate_b"],
        },
        "shadow_repeatability": {
            "ready_for_scheduled_production": True,
            "shadow_run_count": 2,
            "policy_signature": "deepseek:v4-flash::anthropic:claude-haiku-4-5::flash_temporal_v4::1::45.0",
            "stability_gates": {
                "production_shadow_runs_ge_required": True,
                "production_independent_seed_rng": True,
            },
        },
        "winner": {
            "candidate_id": "candidate_a",
            "configuration": {
                "prompt_variants": ["flash_temporal_v4"],
                "max_tool_iterations": 1,
            },
        },
    }
