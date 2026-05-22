from scripts.run_scout_system_launch_gate import (
    StageResult,
    build_launch_gate_report,
    render_launch_gate_html,
    render_launch_gate_markdown,
    _parse_stdout_paths,
)


def test_launch_gate_preflight_blocks_before_live_spend_but_allows_authorized_canary():
    report = build_launch_gate_report(
        deterministic_report=_deterministic_report(),
        live_report=_live_preflight_report(),
        stages=[
            StageResult(
                name="deterministic_100",
                command=["python", "scripts/run_100_scout_readiness_slice.py"],
                returncode=0,
                elapsed_s=1.0,
                artifacts={"SCOUT100_REPORT_JSON": "/tmp/100.json"},
            ),
            StageResult(
                name="live_canary_or_preflight",
                command=["python", "scripts/run_live_scout_canary.py"],
                returncode=0,
                elapsed_s=0.5,
                artifacts={"LIVE_CANARY_REPORT_JSON": "/tmp/live.json"},
            ),
        ],
        viewer_index="/tmp/viewer/index.html",
        artifact_dir="/tmp/launch",
        allow_live_spend=False,
    )

    decision = report["decision"]
    assert decision["status"] == "ready_for_authorized_live_canary"
    assert decision["human_authorization_required"] is True
    assert decision["exit_ok"] is True
    assert "--allow-live-spend" in decision["next_command"]
    assert report["proof_ladder"][0]["passed"] is True
    assert report["proof_ladder"][1]["passed"] is True
    assert report["proof_ladder"][2]["passed"] is False

    markdown = render_launch_gate_markdown(report)
    assert "ready_for_authorized_live_canary" in markdown
    assert "live_provider_preflight" in markdown

    html = render_launch_gate_html(report)
    assert "Ready, but the spend gate is locked." in html
    assert "337" in html
    assert "86" in html
    assert "First Live Scout Preview" in html
    assert "HYPE / intraday / on_chain / frontier" in html
    assert "Full Canary Slice Preview" in html
    assert "HYPE / intraday / on_chain / frontier" in html
    assert "MarketEvolve: control" in html
    assert "tic://tool/builtin/query_events_recent@v1" in html
    assert "launch_gate_report.json" in html
    assert "Repair Work Orders" in html
    assert "Pre-1000 Watchlist" in html


def test_launch_gate_uses_tournament_as_only_1000_promotion_authority():
    report = build_launch_gate_report(
        deterministic_report=_deterministic_report(),
        live_report=_live_pass_report(n_scouts=100),
        tournament_report={
            "promotion_decision": {
                "decision": "promote_to_1000_scout_ramp",
                "ready_for_live_100": True,
                "ready_for_live_1000": True,
                "ready_for_scheduled_production": False,
                "reason": "100 passed distribution and MarketEvolve gates.",
            }
        },
        allow_live_spend=True,
    )

    decision = report["decision"]
    assert decision["status"] == "ready_for_live_1000_ramp"
    assert decision["allowed_next_step"] == "live_1000_scout_ramp"
    assert decision["human_authorization_required"] is True
    assert report["proof_ladder"][4]["passed"] is True
    assert report["proof_ladder"][5]["passed"] is False


def test_launch_gate_blocks_when_deterministic_market_evolve_readiness_fails():
    deterministic = _deterministic_report()
    deterministic["readiness"]["status"] = "fail"
    deterministic["readiness"]["ready_for_live_1000"] = False
    deterministic["readiness"]["failed_gates"] = ["market_evolve_result_or_low_sample_boundary"]

    report = build_launch_gate_report(
        deterministic_report=deterministic,
        live_report=_live_preflight_report(),
    )

    assert report["decision"]["status"] == "blocked_deterministic_readiness"
    assert report["decision"]["exit_ok"] is False
    assert report["deterministic"]["failed_gates"] == ["market_evolve_result_or_low_sample_boundary"]


def test_parse_stdout_paths_keeps_launch_artifact_locations():
    paths = _parse_stdout_paths(
        "\n".join([
            "noise",
            "SCOUT100_REPORT_JSON=/tmp/a.json",
            "LIVE_CANARY_PROMPT_OUTPUT_DIR=/tmp/live",
            "SCOUT_SYSTEM_LAUNCH_DECISION=ready",
        ])
    )

    assert paths["SCOUT100_REPORT_JSON"] == "/tmp/a.json"
    assert paths["LIVE_CANARY_PROMPT_OUTPUT_DIR"] == "/tmp/live"
    assert paths["SCOUT_SYSTEM_LAUNCH_DECISION"] == "ready"


def _deterministic_report():
    return {
        "cycle_id": "cycle_test_det",
        "readiness": {
            "status": "pass",
            "ready_for_live_1000": True,
            "failed_gates": [],
        },
        "metrics": {
            "seeds": {"effective_unique_cell_ratio": 1.0},
            "scouts": {
                "completed": 100,
                "success_rate": 1.0,
                "avg_information_strings_per_scout": 3.37,
            },
            "information_map": {"string_count": 337},
            "geometry": {"cell_count": 80, "routing_queue_count": 24},
            "market_evolve": {
                "paired_seed_slices": 20,
                "latest_experiment_decision": "reject_candidate",
            },
        },
    }


def _live_preflight_report():
    return {
        "cycle_id": "cycle_test_live_preflight",
        "mode": "preflight_no_live_spend",
        "preflight": {
            "tic_root_ok": True,
            "provider_import_ok": True,
            "tool_atlas_ok": True,
            "market_universe_ok": True,
            "tool_atlas": {"tools": 86, "sources": 89},
            "market_universe": {"entity_count": 265},
        },
        "verdict": {
            "status": "blocked",
            "failed_gates": ["explicit_live_spend_allowed"],
        },
        "scale_decision": {"decision": "do_not_scale_yet"},
        "metrics": {},
        "prompt_preview": {
            "status": "ready",
            "seed": {
                "entity": "HYPE",
                "horizon": "intraday",
                "lens": "on_chain",
                "bias_mode": "frontier",
                "theme": "validator_unstake",
            },
            "prompt_variant": "flash_temporal_v4",
            "prompt_contract_pressure": "raise",
            "minimum_information_strings": 2,
            "market_evolve": {
                "program_name": "rosewood_research_policy_v1",
                "experiment_arm": "control",
                "applied": True,
            },
            "tool_policy": {
                "allowed_tool_candidates": [
                    "tic://tool/builtin/query_events_recent@v1",
                    "tic://source/hl/hl_reject_corpus",
                ],
                "tool_candidate_count": 2,
                "max_evidence_tools": 2,
                "max_tool_iterations": 0,
            },
            "system_prompt": "Return strict JSON only with information strings.",
            "user_prompt": "entity=HYPE\nallowed_tool_candidates:\n  tic://tool/builtin/query_events_recent@v1",
            "system_prompt_chars": 49,
            "user_prompt_chars": 86,
        },
        "slice_preview": {
            "schema_version": "talis_live_scout_slice_preview_v1",
            "status": "ready",
            "n_scouts": 2,
            "unique_cell_count": 2,
            "duplicate_cell_count": 0,
            "tool_candidate_count_stats": {"min": 2, "max": 3, "avg": 2.5},
            "distributions": {
                "asset_class": {"hyperliquid_perp": 2},
                "horizon": {"intraday": 1, "1d": 1},
                "lens": {"on_chain": 1, "microstructure": 1},
                "bias_mode": {"frontier": 2},
                "theme": {"validator_unstake": 1, "node_intelligence": 1},
                "prompt_variant": {"flash_temporal_v4": 2},
                "market_evolve_arm": {"control": 1, "candidate": 1},
                "source_family": {"hydromancer": 2, "our_node": 2},
            },
            "seed_rows": [
                {
                    "index": 0,
                    "seed_id": "seed_live_0",
                    "entity": "HYPE",
                    "asset_class": "hyperliquid_perp",
                    "horizon": "intraday",
                    "lens": "on_chain",
                    "bias_mode": "frontier",
                    "theme": "validator_unstake",
                    "prompt_variant": "flash_temporal_v4",
                    "tool_candidate_count": 2,
                    "source_families": ["hydromancer", "our_node"],
                    "market_evolve": {"experiment_arm": "control"},
                },
                {
                    "index": 1,
                    "seed_id": "seed_live_1",
                    "entity": "kFLOKI",
                    "asset_class": "hyperliquid_perp",
                    "horizon": "1d",
                    "lens": "microstructure",
                    "bias_mode": "frontier",
                    "theme": "node_intelligence",
                    "prompt_variant": "flash_temporal_v4",
                    "tool_candidate_count": 3,
                    "source_families": ["hydromancer", "our_node"],
                    "market_evolve": {"experiment_arm": "candidate"},
                },
            ],
        },
    }


def _live_pass_report(*, n_scouts: int):
    return {
        "cycle_id": f"cycle_test_live_{n_scouts}",
        "mode": "live_provider_cost_capped",
        "preflight": {
            "tic_root_ok": True,
            "provider_import_ok": True,
            "tool_atlas_ok": True,
            "market_universe_ok": True,
        },
        "verdict": {
            "status": "pass",
            "failed_gates": [],
            "ready_for_next_live_100": n_scouts < 100,
            "ready_for_live_1000_tournament": n_scouts >= 100,
        },
        "metrics": {
            "scouts": {
                "completed": n_scouts,
                "success_rate": 0.95,
                "avg_information_strings_per_scout": 2.0,
                "total_cost_usd_estimate": 0.04,
            },
            "information_map": {"string_count": n_scouts * 2},
        },
        "learning_report": {
            "summary": "The live run opened the next ramp while surfacing repair work.",
            "scorecard": {
                "avg_prompt_quality": 0.86,
                "weak_scout_count": 6,
            },
            "failure_modes": [
                {
                    "id": "json_unparseable",
                    "count": 1,
                    "severity": "red",
                    "mitigation": "Run fallback JSON repair before dropping scouts.",
                }
            ],
            "evolution_arms": [
                {
                    "id": "harness_repair_arm",
                    "type": "harness",
                }
            ],
            "repair_work_orders": [
                {
                    "work_order_id": "lso_json_unparseable_scout_harness",
                    "owner": "scout_harness",
                    "priority": "P0",
                    "trigger_count": 1,
                    "metric": "json_unparseable_rate",
                }
            ],
            "pre_1000_gate": {
                "ready_for_authorized_1000": True,
                "scheduled_production_allowed": False,
                "red_failure_modes_from_prior_ramp": ["json_unparseable"],
                "must_watch_metrics": ["json_unparseable_rate"],
            },
            "next_run": {
                "allowed_next_step": "live_1000_scout_ramp",
            },
        },
    }
