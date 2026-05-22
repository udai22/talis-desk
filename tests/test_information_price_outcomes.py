from __future__ import annotations

from talis_desk.information_map import (
    InformationString,
    collect_market_evolve_metrics,
    evaluate_information_price_outcomes,
    load_information_price_outcomes,
    persist_information_strings,
    run_market_evolve_step,
    score_market_evolve_metrics,
    seed_default_market_evolve_program,
)
from talis_desk.store import reset_desk_store_for_test


def test_information_price_outcomes_score_early_repricing_hit(tmp_path):
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    string_ids = persist_information_strings(
        conn=store.conn,
        cycle_id="cycle_price",
        scout_id="scout_vvv",
        seed_id="seed_vvv",
        entity="VVV",
        theme="early_social_alpha",
        horizon="intraday",
        lens="social_alpha",
        bias_mode="frontier",
        strings=[
            InformationString(
                title="VVV early upward repricing pressure",
                thesis="VVV should be priced upwards before broad market attention catches up.",
                mechanism="Fresh social and flow attention imply upside repricing pressure.",
                expected_outcome="VVV trades higher over the next hour.",
                time_horizon="hour",
                observed_at="2026-05-22T10:00:00+00:00",
                conviction=0.90,
                novelty_score=0.85,
                crowdedness=0.15,
                entities_chain=["VVV", "social attention"],
                depth_layers=[{"layer": 1, "claim": "attention"}, {"layer": 2, "claim": "repricing"}],
                evidence_refs=["x://vvv/example"],
                quality_flags=["source_family:grok_x_alpha"],
            )
        ],
    )

    report = evaluate_information_price_outcomes(
        cycle_id="cycle_price",
        price_observations=[
            {"entity": "VVV", "observed_at": "2026-05-22T09:59:00+00:00", "price": 5.00, "source": "fixture"},
            {"entity": "VVV", "observed_at": "2026-05-22T11:05:00+00:00", "price": 5.25, "source": "fixture"},
        ],
        min_move_threshold_pct=0.02,
        conn=store.conn,
    )

    assert report["evaluated_count"] == 1
    assert report["summary"]["outcome_direction_hit_rate"] == 1.0
    assert report["summary"]["outcome_threshold_hit_rate"] == 1.0
    outcomes = load_information_price_outcomes(cycle_id="cycle_price", conn=store.conn)
    assert outcomes[0]["string_id"] == string_ids[0]
    assert outcomes[0]["expected_direction"] == "up"
    assert outcomes[0]["direction_hit"] is True
    assert outcomes[0]["threshold_hit"] is True
    assert outcomes[0]["realized_edge_score"] > 0.9


def test_market_evolve_metrics_include_information_price_outcomes(tmp_path):
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    program = seed_default_market_evolve_program(cycle_id="cycle_price_metric", conn=store.conn)
    persist_information_strings(
        conn=store.conn,
        cycle_id="cycle_price_metric",
        scout_id="scout_price",
        seed_id="seed_price",
        entity="VVV",
        theme="early_social_alpha",
        horizon="intraday",
        lens="social_alpha",
        bias_mode="frontier",
        strings=[
            InformationString(
                title="VVV upward repricing",
                thesis="VVV reprices higher when early social attention is confirmed by flow.",
                mechanism="Attention and flow converge before price fully reflects it.",
                expected_outcome="VVV trades higher in the next hour.",
                time_horizon="hour",
                observed_at="2026-05-22T10:00:00+00:00",
                conviction=0.90,
                novelty_score=0.85,
                crowdedness=0.15,
                entities_chain=["VVV", "attention", "flow"],
                depth_layers=[{"layer": 1, "claim": "attention"}, {"layer": 2, "claim": "flow"}],
                evidence_refs=["fixture://attention"],
            )
        ],
    )
    before = collect_market_evolve_metrics(cycle_id="cycle_price_metric", conn=store.conn)
    before_score = score_market_evolve_metrics(before, program=program)

    evaluate_information_price_outcomes(
        cycle_id="cycle_price_metric",
        price_observations=[
            {"entity": "VVV", "observed_at": "2026-05-22T09:59:00+00:00", "price": 10.0, "source": "fixture"},
            {"entity": "VVV", "observed_at": "2026-05-22T11:10:00+00:00", "price": 10.6, "source": "fixture"},
        ],
        min_move_threshold_pct=0.02,
        conn=store.conn,
    )
    after = collect_market_evolve_metrics(cycle_id="cycle_price_metric", conn=store.conn)
    after_score = score_market_evolve_metrics(after, program=program)

    assert before["outcome_eval_count"] == 0.0
    assert after["outcome_eval_count"] == 1.0
    assert after["outcome_direction_hit_rate"] == 1.0
    assert after["early_repricing_hit_rate"] == 1.0
    assert after["avg_realized_edge_score"] > 0.9
    assert after_score > before_score


def test_market_evolve_exploits_price_feedback_when_strings_beat_price(tmp_path):
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    persist_information_strings(
        conn=store.conn,
        cycle_id="cycle_price_exploit",
        scout_id="scout_price_exploit",
        seed_id="seed_price_exploit",
        entity="VVV",
        theme="early_social_alpha",
        horizon="intraday",
        lens="social_alpha",
        bias_mode="frontier",
        strings=[
            InformationString(
                title="VVV pre-price upward pressure",
                thesis="VVV should be priced upwards as informed attention and flow arrive before consensus.",
                mechanism="Fresh attention plus flow pressure creates upward repricing before broad market recognition.",
                expected_outcome="VVV trades higher inside the next hour.",
                time_horizon="hour",
                observed_at="2026-05-22T10:00:00+00:00",
                conviction=0.90,
                novelty_score=0.85,
                crowdedness=0.15,
                entities_chain=["VVV", "attention", "flow"],
                depth_layers=[{"layer": 1, "claim": "attention"}, {"layer": 2, "claim": "flow"}],
                evidence_refs=["fixture://vvv/attention"],
                quality_flags=[
                    "source_family:our_hl_node",
                    "source_family:market_microstructure",
                    "source_family:web_attention",
                ],
            )
        ],
    )
    evaluate_information_price_outcomes(
        cycle_id="cycle_price_exploit",
        price_observations=[
            {"entity": "VVV", "observed_at": "2026-05-22T09:59:00+00:00", "price": 10.0, "source": "fixture"},
            {"entity": "VVV", "observed_at": "2026-05-22T11:05:00+00:00", "price": 10.8, "source": "fixture"},
        ],
        min_move_threshold_pct=0.02,
        conn=store.conn,
    )

    step = run_market_evolve_step(cycle_id="cycle_price_exploit", conn=store.conn)

    assert step.best_evaluation is not None
    assert step.best_evaluation.metrics["outcome_direction_hit_rate"] == 1.0
    mutation = next(m for m in step.mutations if m.mutation_kind == "exploit_price_feedback_surface")
    proof = mutation.mutation["_evolution_proof"]
    assert "outcome_direction_hit_rate" in proof["target_metrics"]
    assert any(gate["metric"] == "candidate_avg_realized_edge_score" for gate in proof["falsification_gates"])
    child = next(c for c in step.child_programs if c.program_id == mutation.child_program_id)
    assert child.genome["prompt_policy"]["emphasize_price_feedback_refs"] is True
    assert child.genome["tool_request_policy"]["prefer_price_anchor_tools"] is True
    assert child.genome["routing_thresholds"]["price_feedback_exploitation_budget_share"] > 0.02


def test_market_evolve_repairs_contract_when_price_feedback_fails(tmp_path):
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    strings = []
    for i in range(5):
        strings.append(
            InformationString(
                title=f"Failed VVV upside narrative {i}",
                thesis="VVV should be priced upwards, but this thesis needs strict disconfirming price checks.",
                mechanism="The narrative claims upward repricing without enough price-anchor confirmation.",
                expected_outcome="VVV trades higher inside the next hour.",
                time_horizon="hour",
                observed_at="2026-05-22T10:00:00+00:00",
                conviction=0.72,
                novelty_score=0.65,
                crowdedness=0.35,
                entities_chain=["VVV", "attention"],
                depth_layers=[{"layer": 1, "claim": "attention"}, {"layer": 2, "claim": "repricing"}],
                evidence_refs=[f"fixture://vvv/failed/{i}"],
                quality_flags=["source_family:web_attention"],
            )
        )
    persist_information_strings(
        conn=store.conn,
        cycle_id="cycle_price_repair",
        scout_id="scout_price_repair",
        seed_id="seed_price_repair",
        entity="VVV",
        theme="early_social_alpha",
        horizon="intraday",
        lens="social_alpha",
        bias_mode="frontier",
        strings=strings,
    )
    evaluate_information_price_outcomes(
        cycle_id="cycle_price_repair",
        price_observations=[
            {"entity": "VVV", "observed_at": "2026-05-22T09:59:00+00:00", "price": 10.0, "source": "fixture"},
            {"entity": "VVV", "observed_at": "2026-05-22T11:05:00+00:00", "price": 9.6, "source": "fixture"},
        ],
        min_move_threshold_pct=0.02,
        conn=store.conn,
    )

    step = run_market_evolve_step(cycle_id="cycle_price_repair", conn=store.conn)

    assert step.best_evaluation is not None
    assert step.best_evaluation.metrics["outcome_direction_hit_rate"] == 0.0
    mutation = next(m for m in step.mutations if m.mutation_kind == "tighten_price_feedback_contract")
    proof = mutation.mutation["_evolution_proof"]
    assert any(gate["metric"] == "candidate_outcome_direction_hit_rate" for gate in proof["falsification_gates"])
    child = next(c for c in step.child_programs if c.program_id == mutation.child_program_id)
    assert child.genome["prompt_policy"]["emphasize_price_feedback_refs"] is True
    assert child.genome["tool_request_policy"]["require_disconfirming_price_check"] is True
