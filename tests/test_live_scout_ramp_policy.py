import copy

from talis_desk.information_map.live_ramp_policy import (
    apply_live_scout_ramp_policy_to_seeds,
    build_live_scout_ramp_policy_rehearsal,
)
from talis_desk.swarm.seed_generator import SeedCell


def test_live_scout_ramp_policy_monotonically_patches_seed_payload(monkeypatch):
    def fake_narrow_tools_for_seed(seed, k=None):
        assert seed.payload["source_family_targets"] == ["market_timeseries", "hydromancer", "our_node"]
        return [
            "tic://tool/hydromancer/get_hl_pnl_leaderboard@v1",
            "tic://source/hl/hl_reject_corpus",
        ]

    monkeypatch.setattr(
        "talis_desk.swarm.seed_generator.narrow_tools_for_seed",
        fake_narrow_tools_for_seed,
    )
    seed = SeedCell(
        seed_id="seed_policy",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        theme="node_flow",
        payload={
            "tool_candidates": ["tic://tool/existing@v1"],
            "max_tool_iterations": 1,
            "source_family_targets": ["market_timeseries"],
        },
    )
    policy = {
        "schema_version": "talis_live_scout_ramp_policy_v1",
        "policy_id": "lrp_test",
        "source": "live_scout_learning_report",
        "watch_metrics": ["json_unparseable_rate", "node_promoted_string_rate"],
        "repair_work_order_ids": ["lso_json_unparseable_scout_harness", "lso_geometry_replication"],
        "prompt_repair_modes": ["node_contract_upgrade"],
        "seed_payload_patch": {
            "prompt_contract_pressure": "strict",
            "prompt_min_information_strings": 2,
            "max_tool_iterations": 2,
            "max_evidence_tools_min": 6,
            "tool_candidate_limit_min": 12,
            "prompt_require_evidence_refs": True,
            "suggested_tool_allowlist_only": True,
            "source_family_targets_append": ["hydromancer", "our_node"],
        },
        "geometry_replication_targets": [
            {
                "cell_key": "HYPE|intraday|on_chain|frontier|node_flow",
                "route_directive": "widen_scouts",
                "trade_scream_score": 0.81,
                "source_work_order_id": "lso_geometry_replication",
            }
        ],
    }

    baseline = copy.deepcopy([seed])
    result = apply_live_scout_ramp_policy_to_seeds([seed], policy)
    rehearsal = build_live_scout_ramp_policy_rehearsal(
        baseline_seeds=baseline,
        candidate_seeds=[seed],
        policy=policy,
        application=result,
    )

    assert result["status"] == "applied"
    assert result["geometry_annotated_seed_count"] == 1
    assert result["tool_candidate_refreshed_seed_count"] == 1
    assert result["tool_candidate_added_count"] == 2
    assert seed.payload["learning_policy_id"] == "lrp_test"
    assert seed.payload["prompt_contract_pressure"] == "strict"
    assert seed.payload["prompt_min_information_strings"] == 2
    assert seed.payload["max_tool_iterations"] == 2
    assert seed.payload["max_evidence_tools"] == 6
    assert seed.payload["tool_candidate_limit"] == 12
    assert seed.payload["tool_candidates"] == [
        "tic://tool/existing@v1",
        "tic://tool/hydromancer/get_hl_pnl_leaderboard@v1",
        "tic://source/hl/hl_reject_corpus",
    ]
    assert seed.payload["learning_tool_candidate_refresh"]["after_count"] == 3
    assert seed.payload["source_family_targets"] == ["market_timeseries", "hydromancer", "our_node"]
    assert seed.payload["learning_geometry_replication_targets"][0]["cell_key"] == "HYPE|intraday|on_chain|frontier|node_flow"
    assert rehearsal["schema_version"] == "live_scout_ramp_policy_rehearsal_v1"
    assert rehearsal["status"] == "pass"
    assert rehearsal["decision"] == "policy_can_gate_live_spend"
    assert rehearsal["metrics"]["tool_candidate_refresh_rate"] == 1.0
    assert rehearsal["metrics"]["tool_candidate_added_count"] == 2
    assert rehearsal["metrics"]["source_target_coverage_rate"] >= 0.6
    assert rehearsal["gates"]["candidate_menu_not_smaller"] is True
