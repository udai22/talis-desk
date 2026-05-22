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
    assert scoreboard["evolution_memory"]["best_score_delta_window"] > 0
    assert any(a["action"] == "continue_matched_experiment" for a in scoreboard["next_actions"])
