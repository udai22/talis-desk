from __future__ import annotations

from pathlib import Path

from talis_desk.information_map import (
    InformationString,
    apply_market_evolve_policy_to_seeds,
    build_market_evolve_scoreboard,
    load_market_evolve_scoreboards,
    persist_information_strings,
    run_market_evolve_step,
)
from talis_desk.information_map.market_evolve import (
    _scoreboard_next_actions,
    build_market_evolve_experiment_attribution,
)
from talis_desk.store import reset_desk_store_for_test
from talis_desk.swarm.seed_generator import SeedCell


def test_market_evolve_scoreboard_persists_learning_state(tmp_path: Path) -> None:
    store = reset_desk_store_for_test(tmp_path / "desk.db")

    planning_step = run_market_evolve_step(cycle_id="cycle_score_plan", conn=store.conn)
    assert planning_step.experiment_plans

    scoreboard = build_market_evolve_scoreboard(cycle_id="cycle_score_plan", conn=store.conn)

    assert scoreboard["schema_version"] == "market_evolve_scoreboard_v1"
    assert scoreboard["counts"]["active_programs"] >= 1
    assert scoreboard["counts"]["open_experiments"] >= 1
    assert scoreboard["evolution_memory"]["evolves"] is True
    assert scoreboard["next_actions"]
    assert scoreboard["cadence_readiness"]["scheduled_production_allowed"] is False
    persisted = load_market_evolve_scoreboards(cycle_id="cycle_score_plan", conn=store.conn)
    assert persisted
    assert persisted[0]["id"] == scoreboard["id"]


def test_market_evolve_scoreboard_surfaces_candidate_experiment_evidence(tmp_path: Path) -> None:
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    planning_step = run_market_evolve_step(cycle_id="cycle_score_plan", conn=store.conn)
    experiment_id = planning_step.experiment_plans[0]["id"]

    seeds = [
        SeedCell(
            seed_id=f"seed_score_{i:02d}",
            entity="HYPE",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            theme="validator_unstake",
            payload={"tool_candidates": [f"tic://tool/test/tool_{j}@v1" for j in range(12)]},
        )
        for i in range(80)
    ]
    apply_market_evolve_policy_to_seeds(seeds, cycle_id="cycle_score_ab", conn=store.conn)
    apps = store.conn.execute(
        """
        SELECT seed_id
        FROM market_evolve_policy_applications
        WHERE cycle_id = ? AND experiment_id = ? AND experiment_arm = 'candidate'
        LIMIT 8
        """,
        ("cycle_score_ab", experiment_id),
    ).fetchall()
    assert apps
    families = ["hydromancer", "market_microstructure", "our_hl_node", "grok_x_social_alpha"]
    for i, row in enumerate(apps):
        family = families[i % len(families)]
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_score_ab",
            scout_id=f"scout_score_candidate_{i}",
            seed_id=str(row["seed_id"]),
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"Candidate policy found {family}",
                    thesis="Candidate policy finds source-diverse HYPE route intelligence before control.",
                    mechanism="Source-family breadth gives verifier-ready route context.",
                    expected_outcome="Liquidity absorption or sellable route becomes observable intraday.",
                    kill_signal="No movement to sellable liquidity or strong informed absorption.",
                    conviction=0.86,
                    novelty_score=0.80,
                    crowdedness=0.20,
                    entities_chain=["HYPE", "aHYPE", "route quality"],
                    depth_layers=[
                        {"layer": 1, "claim": "route"},
                        {"layer": 2, "claim": "actor quality"},
                    ],
                    evidence_refs=[f"tic://source/{family}/candidate"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            source_tool_call_ids=[f"tc_score_candidate_{i}_{family}"],
        )

    run_market_evolve_step(cycle_id="cycle_score_ab", conn=store.conn)
    scoreboard = build_market_evolve_scoreboard(cycle_id="cycle_score_ab", conn=store.conn)

    assert scoreboard["decision_counts"]["continue_candidate"] == 1
    assert scoreboard["continuation_candidates"]
    assert scoreboard["hard_experiment_gate_summary"]["evaluated"] > 0
    attribution = scoreboard["hard_experiment_attribution"]
    assert attribution["schema_version"] == "market_evolve_experiment_attribution_v1"
    assert attribution["latest"]
    latest = attribution["latest"][0]
    assert latest["learning_signal"] == "candidate_edge_observed_continue_matched_test"
    assert latest["top_positive_metric_deltas"]
    assert any(
        row["metric"] in {"valid_string_rate", "avg_source_independence"}
        and row["winner"] == "candidate"
        for row in latest["top_positive_metric_deltas"]
    )
    assert latest["proof_metric_deltas"]
    assert scoreboard["evolution_memory"]["best_score_delta_window"] > 0
    assert any(a["action"] == "continue_matched_experiment" for a in scoreboard["next_actions"])


def test_market_evolve_experiment_attribution_respects_metric_direction() -> None:
    attribution = build_market_evolve_experiment_attribution([
        {
            "id": "mres_direction",
            "experiment_id": "mexp_direction",
            "cycle_id": "cycle_direction",
            "decision": "continue_candidate",
            "score_delta": 0.14,
            "control_score": 0.51,
            "candidate_score": 0.65,
            "control_metrics": {
                "valid_string_rate": 0.62,
                "avg_fragility": 0.44,
                "tool_eval_failed_rate": 0.20,
            },
            "candidate_metrics": {
                "valid_string_rate": 0.78,
                "avg_fragility": 0.31,
                "tool_eval_failed_rate": 0.06,
            },
            "falsification_gate_results": [
                {
                    "metric": "candidate_avg_fragility",
                    "operator": "<=",
                    "threshold": 0.40,
                    "observed": 0.31,
                    "triggered": False,
                    "decision": "reject_candidate",
                    "status": "passed",
                }
            ],
        }
    ])

    latest = attribution["latest"][0]
    fragility = next(
        row for row in latest["proof_metric_deltas"]
        if row["gate_metric"] == "candidate_avg_fragility"
    )
    assert fragility["direction"] == "lower_is_better"
    assert fragility["delta"] == -0.13
    assert fragility["improvement"] == 0.13
    assert fragility["winner"] == "candidate"


def test_market_evolve_scoreboard_turns_failed_attribution_into_repair_action() -> None:
    result = {
        "id": "mres_repair",
        "experiment_id": "mexp_repair",
        "cycle_id": "cycle_repair",
        "decision": "reject_candidate",
        "score_delta": -0.09,
        "control_score": 0.62,
        "candidate_score": 0.53,
        "control_metrics": {
            "valid_string_rate": 0.82,
            "avg_source_independence": 0.70,
            "avg_fragility": 0.24,
            "tool_eval_failed_rate": 0.04,
        },
        "candidate_metrics": {
            "valid_string_rate": 0.55,
            "avg_source_independence": 0.38,
            "avg_fragility": 0.58,
            "tool_eval_failed_rate": 0.22,
        },
        "falsification_gate_results": [
            {
                "metric": "candidate_avg_source_independence",
                "operator": ">=",
                "threshold": 0.45,
                "observed": 0.38,
                "triggered": True,
                "decision": "reject_candidate",
                "status": "triggered",
            }
        ],
    }
    attribution = build_market_evolve_experiment_attribution([result])
    actions = _scoreboard_next_actions(
        status="repair_needed",
        frontier=[],
        open_experiments=[],
        applications=[],
        cycle_id="cycle_repair",
        result_window=[result],
        attribution=attribution,
    )

    repair = next(a for a in actions if a["action"] == "repair_experiment_metric_regressions")
    assert repair["experiment_id"] == "mexp_repair"
    assert repair["learning_signal"] == "candidate_failed_hard_proof_gate"
    assert "candidate_avg_source_independence" in repair["metrics"]
    assert "candidate_avg_fragility" in repair["metrics"]
