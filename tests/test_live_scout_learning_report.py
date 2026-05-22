import json

from scripts.analyze_live_scout_run import (
    build_live_scout_learning_report,
    write_learning_report_artifacts,
)


def test_live_scout_learning_report_extracts_repair_modes_and_next_gate(tmp_path):
    live_report = {
        "cycle_id": "cycle_learning",
        "n_scouts_requested": 2,
        "transcript_summary": {"call_count": 2},
        "metrics": {
            "scouts": {
                "completed": 1,
                "success_rate": 0.5,
                "total_cost_usd_estimate": 0.01,
                "duplicate_hypothesis_rate": 0.0,
            },
            "information_map": {"string_count": 2},
            "geometry": {
                "cell_count": 1,
                "routing_queue_count": 1,
                "top_actions": [
                    {
                        "cell_key": "HYPE|intraday|on_chain|node",
                        "route_directive": "widen_scouts",
                        "metrics": {"trade_scream_score": 0.7},
                    }
                ],
            },
            "market_evolve": {
                "paired_seed_slices": 20,
                "arm_counts": {"control": 50, "candidate": 50},
                "latest_experiment_decision": "reject_candidate",
            },
        },
    }
    outputs = [
        {
            "seed_id": "seed_bad",
            "scout_id": "scout_bad",
            "entity": "HYPE",
            "horizon": "intraday",
            "lens": "on_chain",
            "bias_mode": "frontier",
            "error": "scout_json_unparseable",
            "quality_flags": ["prompt_variant:flash_temporal_v4", "scout_json_unparseable"],
            "information_string_ids": [],
            "tool_evidence": [],
            "suggested_tools": [],
            "tool_requests": [],
        },
        {
            "seed_id": "seed_ok",
            "scout_id": "scout_ok",
            "entity": "HYPE",
            "horizon": "intraday",
            "lens": "on_chain",
            "bias_mode": "frontier",
            "hypothesis_text": "HYPE needs source repair before verifier spend.",
            "quality_flags": [
                "prompt_variant:flash_temporal_v4",
                "prompt_quality:0.90",
                "node_intelligence_not_promoted",
            ],
            "information_string_ids": ["istr_1", "istr_2"],
            "information_strings": [
                {"quality_flags": ["source_family:our_node"]},
                {"quality_flags": ["source_family:hydromancer"]},
            ],
            "tool_evidence": [{"ok": True}],
            "suggested_tools": ["tic://tool/builtin/query_timeseries@v1"],
            "tool_requests": [],
        },
    ]
    tournament = {
        "promotion_decision": {
            "decision": "promote_to_1000_scout_ramp",
            "ready_for_live_1000": True,
            "ready_for_scheduled_production": False,
        },
        "winner": {"quality": {"tool_error_rate": 0.0}},
    }
    live_path = tmp_path / "live_scout_canary_report.json"
    live_path.write_text(json.dumps(live_report), encoding="utf-8")
    (tmp_path / "live_scout_canary_outputs.json").write_text(json.dumps(outputs), encoding="utf-8")
    (tmp_path / "live_scout_transcript.json").write_text(json.dumps({"calls": []}), encoding="utf-8")
    (tmp_path / "live_scout_slice_preview.json").write_text(
        json.dumps({"distributions": {"source_family": {"market_timeseries": 2, "our_node": 1}}}),
        encoding="utf-8",
    )
    tournament_path = tmp_path / "live_scout_tournament_report.json"
    tournament_path.write_text(json.dumps(tournament), encoding="utf-8")

    report = build_live_scout_learning_report(live_path, tournament_report_path=tournament_path)

    assert report["schema_version"] == "talis_live_scout_learning_report_v1"
    assert report["next_run"]["allowed_next_step"] == "live_1000_scout_ramp"
    assert any(mode["id"] == "json_unparseable" and mode["count"] == 1 for mode in report["failure_modes"])
    assert any(arm["id"] == "harness_repair_arm" for arm in report["evolution_arms"])
    assert any(arm["id"] == "geometry_replication_arm" for arm in report["evolution_arms"])
    assert report["pre_1000_gate"]["ready_for_authorized_1000"] is True
    assert report["pre_1000_gate"]["scheduled_production_allowed"] is False
    assert any(order["owner"] == "scout_harness" for order in report["repair_work_orders"])
    assert any(order["work_order_id"] == "lso_geometry_replication" for order in report["repair_work_orders"])
    assert "json_unparseable_rate" in report["next_run"]["pre_1000_gate"]["must_watch_metrics"]

    json_path, md_path = write_learning_report_artifacts(report, output_dir=tmp_path / "out")
    assert json_path.exists()
    assert "Live Scout Learning Report" in md_path.read_text(encoding="utf-8")
