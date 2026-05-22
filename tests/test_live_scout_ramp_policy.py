from talis_desk.information_map.live_ramp_policy import apply_live_scout_ramp_policy_to_seeds
from talis_desk.swarm.seed_generator import SeedCell


def test_live_scout_ramp_policy_monotonically_patches_seed_payload():
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

    result = apply_live_scout_ramp_policy_to_seeds([seed], policy)

    assert result["status"] == "applied"
    assert result["geometry_annotated_seed_count"] == 1
    assert seed.payload["learning_policy_id"] == "lrp_test"
    assert seed.payload["prompt_contract_pressure"] == "strict"
    assert seed.payload["prompt_min_information_strings"] == 2
    assert seed.payload["max_tool_iterations"] == 2
    assert seed.payload["max_evidence_tools"] == 6
    assert seed.payload["tool_candidate_limit"] == 12
    assert seed.payload["tool_candidates"] == ["tic://tool/existing@v1"]
    assert seed.payload["source_family_targets"] == ["market_timeseries", "hydromancer", "our_node"]
    assert seed.payload["learning_geometry_replication_targets"][0]["cell_key"] == "HYPE|intraday|on_chain|frontier|node_flow"

