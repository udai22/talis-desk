from __future__ import annotations

from talis_desk.information_map import (
    InformationString,
    collect_market_evolve_metrics,
    evaluate_information_price_outcomes,
    load_information_price_outcomes,
    persist_information_strings,
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
