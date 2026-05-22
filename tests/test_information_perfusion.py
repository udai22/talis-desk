from __future__ import annotations

from talis_desk.information_map import (
    InformationString,
    compute_information_perfusion,
    evaluate_information_price_outcomes,
    load_information_perfusion,
    persist_information_strings,
)
from talis_desk.store import reset_desk_store_for_test
from talis_desk.swarm.seed_generator import generate_information_perfusion_seeds


def test_information_perfusion_dilates_unabsorbed_information_pressure(tmp_path):
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    persist_information_strings(
        conn=store.conn,
        cycle_id="cycle_perf",
        scout_id="scout_vvv",
        seed_id="seed_vvv",
        entity="VVV",
        theme="early_social_alpha",
        horizon="intraday",
        lens="social_alpha",
        bias_mode="frontier",
        strings=[
            InformationString(
                title="VVV upward pressure not absorbed",
                thesis="VVV should be priced upwards before consensus sees the social and flow shift.",
                mechanism="Fresh attention plus flow creates pressure before broad recognition.",
                expected_outcome="VVV trades higher over the next hour.",
                time_horizon="hour",
                observed_at="2026-05-22T10:00:00+00:00",
                expires_at="2099-05-23T10:00:00+00:00",
                conviction=0.95,
                novelty_score=0.90,
                crowdedness=0.05,
                entities_chain=["VVV", "attention", "flow"],
                depth_layers=[{"layer": 1, "claim": "attention"}, {"layer": 2, "claim": "flow"}],
                evidence_refs=["fixture://our_hl_node/vvv", "fixture://orderbook/vvv", "fixture://twitter/vvv"],
                quality_flags=[
                    "source_family:our_hl_node",
                    "source_family:market_microstructure",
                    "source_family:grok_x_alpha",
                ],
            )
        ],
    )
    evaluate_information_price_outcomes(
        cycle_id="cycle_perf",
        price_observations=[
            {"entity": "VVV", "observed_at": "2026-05-22T09:59:00+00:00", "price": 10.0, "source": "fixture"},
            {"entity": "VVV", "observed_at": "2026-05-22T11:05:00+00:00", "price": 10.05, "source": "fixture"},
        ],
        min_move_threshold_pct=0.02,
        conn=store.conn,
    )

    snapshot = compute_information_perfusion(
        cycle_id="cycle_perf",
        scout_budget=12,
        conn=store.conn,
    )

    assert snapshot.global_metrics["routed_cell_count"] == 1.0
    cell = snapshot.cells[0]
    assert cell.route_directive == "dilate_scouts"
    assert cell.recommended_scouts == 12
    assert cell.metrics["information_pressure"] > 0.85
    assert cell.metrics["price_absorption"] < 0.40
    assert cell.metrics["pressure_gradient"] > 0.55
    assert cell.metrics["source_oxygenation"] > 0.85
    assert "information_not_absorbed_by_price" in cell.quality_flags

    persisted = load_information_perfusion(cycle_id="cycle_perf", conn=store.conn)
    assert persisted[0]["route_directive"] == "dilate_scouts"
    assert persisted[0]["metrics"]["pressure_gradient"] == cell.metrics["pressure_gradient"]


def test_information_perfusion_routes_next_scout_seed_from_pressure_matrix(tmp_path):
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    persist_information_strings(
        conn=store.conn,
        cycle_id="cycle_perf",
        scout_id="scout_hype",
        seed_id="seed_hype",
        entity="HYPE",
        theme="node_intelligence",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        strings=[
            InformationString(
                title="HYPE node pressure",
                thesis="HYPE reprices upward if high-quality node actors absorb supply before liquid sell flow appears.",
                mechanism="Node absorption turns possible supply into scarcity and demand confirmation.",
                expected_outcome="HYPE trades higher before the next full run.",
                time_horizon="hour",
                observed_at="2026-05-22T10:00:00+00:00",
                expires_at="2099-05-23T10:00:00+00:00",
                conviction=0.88,
                novelty_score=0.82,
                crowdedness=0.18,
                entities_chain=["HYPE", "our_hl_node", "hydromancer"],
                depth_layers=[{"layer": 1, "claim": "node absorption"}, {"layer": 2, "claim": "scarcity"}],
                evidence_refs=["fixture://our_hl_node/hype", "fixture://hydromancer/hype"],
                quality_flags=["source_family:our_hl_node", "source_family:hydromancer"],
            )
        ],
    )
    evaluate_information_price_outcomes(
        cycle_id="cycle_perf",
        price_observations=[
            {"entity": "HYPE", "observed_at": "2026-05-22T09:59:00+00:00", "price": 20.0, "source": "fixture"},
            {"entity": "HYPE", "observed_at": "2026-05-22T11:05:00+00:00", "price": 20.1, "source": "fixture"},
        ],
        min_move_threshold_pct=0.02,
        conn=store.conn,
    )
    compute_information_perfusion(cycle_id="cycle_perf", scout_budget=6, conn=store.conn)

    seeds = generate_information_perfusion_seeds(
        cycle_id="cycle_next",
        source_cycle_id="cycle_perf",
        n_seed_budget=100,
        max_seeds=4,
        conn=store.conn,
    )

    assert len(seeds) == 1
    seed = seeds[0]
    assert seed.payload["source"] == "information_perfusion_route"
    assert seed.payload["information_perfusion_route_directive"] == "dilate_scouts"
    assert seed.payload["pressure_gradient"] > 0.50
    assert "tic://tool/talis_native/compute_information_perfusion@v1" in seed.payload["tool_candidates"]
    assert seed.payload["why_this_seed_exists"].startswith("Information pressure is high")
