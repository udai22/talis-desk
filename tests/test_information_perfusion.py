from __future__ import annotations

from argparse import Namespace

from scripts.run_live_scout_canary import _generate_control_aware_live_seeds
from talis_desk.information_map import (
    InformationString,
    compute_information_perfusion,
    evaluate_information_price_outcomes,
    load_information_perfusion,
    persist_information_strings,
    run_market_evolve_step,
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
    assert cell.metrics["flow_shear"] > 0.50
    assert cell.metrics["perfusion_efficiency"] > 0.60
    assert "information_not_absorbed_by_price" in cell.quality_flags

    persisted = load_information_perfusion(cycle_id="cycle_perf", conn=store.conn)
    assert persisted[0]["route_directive"] == "dilate_scouts"
    assert persisted[0]["metrics"]["pressure_gradient"] == cell.metrics["pressure_gradient"]


def test_information_perfusion_marks_latched_unabsorbed_pressure(tmp_path):
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    persist_information_strings(
        conn=store.conn,
        cycle_id="cycle_perf_latch",
        scout_id="scout_latch",
        seed_id="seed_latch",
        entity="VVV",
        theme="early_social_alpha",
        horizon="intraday",
        lens="social_alpha",
        bias_mode="frontier",
        strings=[
            InformationString(
                title="VVV stale pressure needs unlatching",
                thesis="VVV upward pressure is strong but may be trapped behind stale confirmation and non-absorbed price.",
                mechanism="Several source families agree, but the cell needs a fresh repair pass before widening scout flow.",
                expected_outcome="Fresh sources should either confirm upward repricing pressure or kill the stale chain.",
                time_horizon="hour",
                observed_at="2026-05-22T10:00:00+00:00",
                conviction=0.96,
                novelty_score=0.94,
                crowdedness=0.12,
                entities_chain=["VVV", "attention", "flow"],
                depth_layers=[{"layer": 1, "claim": "attention"}, {"layer": 2, "claim": "flow"}],
                evidence_refs=["fixture://our_hl_node/vvv", "fixture://orderbook/vvv", "fixture://twitter/vvv"],
                quality_flags=[
                    "source_family:our_hl_node",
                    "source_family:market_microstructure",
                    "source_family:grok_x_alpha",
                    "stale_source_needs_refresh",
                ],
            )
        ],
    )
    evaluate_information_price_outcomes(
        cycle_id="cycle_perf_latch",
        price_observations=[
            {"entity": "VVV", "observed_at": "2026-05-22T09:59:00+00:00", "price": 10.0, "source": "fixture"},
            {"entity": "VVV", "observed_at": "2026-05-22T11:05:00+00:00", "price": 10.02, "source": "fixture"},
        ],
        min_move_threshold_pct=0.02,
        conn=store.conn,
    )

    snapshot = compute_information_perfusion(
        cycle_id="cycle_perf_latch",
        scout_budget=4,
        conn=store.conn,
    )

    cell = snapshot.cells[0]
    assert cell.metrics["latch_risk"] >= 0.45
    assert cell.metrics["transport_cost"] > 0.0
    assert snapshot.global_metrics["avg_latch_risk"] == cell.metrics["latch_risk"]
    assert "information_latch_risk" in cell.quality_flags


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


def test_information_perfusion_can_replicate_control_routed_slices(tmp_path):
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    persist_information_strings(
        conn=store.conn,
        cycle_id="cycle_perf_replicate",
        scout_id="scout_hype",
        seed_id="seed_hype",
        entity="HYPE",
        theme="node_intelligence",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        strings=[
            InformationString(
                title="HYPE replicated pressure",
                thesis="HYPE should be repriced upward if node absorption repeats across scout perspectives.",
                mechanism="A control-routed sentinel should spend multiple scouts on the same pressure cell with varied bias.",
                expected_outcome="Replicated scouts confirm or kill the pressure before a broad run.",
                time_horizon="hour",
                observed_at="2026-05-22T10:00:00+00:00",
                expires_at="2099-05-23T10:00:00+00:00",
                conviction=0.90,
                novelty_score=0.86,
                crowdedness=0.14,
                entities_chain=["HYPE", "our_hl_node", "hydromancer"],
                depth_layers=[{"layer": 1, "claim": "node absorption"}, {"layer": 2, "claim": "repricing"}],
                evidence_refs=["fixture://our_hl_node/hype", "fixture://hydromancer/hype"],
                quality_flags=["source_family:our_hl_node", "source_family:hydromancer"],
            )
        ],
    )
    evaluate_information_price_outcomes(
        cycle_id="cycle_perf_replicate",
        price_observations=[
            {"entity": "HYPE", "observed_at": "2026-05-22T09:59:00+00:00", "price": 20.0, "source": "fixture"},
            {"entity": "HYPE", "observed_at": "2026-05-22T11:05:00+00:00", "price": 20.1, "source": "fixture"},
        ],
        min_move_threshold_pct=0.02,
        conn=store.conn,
    )
    compute_information_perfusion(cycle_id="cycle_perf_replicate", scout_budget=3, conn=store.conn)

    seeds = generate_information_perfusion_seeds(
        cycle_id="cycle_next_replicated",
        source_cycle_id="cycle_perf_replicate",
        n_seed_budget=12,
        max_seeds=3,
        replicate_recommended=True,
        route_objective="pressure",
        conn=store.conn,
    )

    assert len(seeds) == 3
    assert [seed.payload["information_perfusion_replicate_index"] for seed in seeds] == [0, 1, 2]
    assert len({seed.bias_mode for seed in seeds}) > 1
    assert all(seed.payload["information_perfusion_route_objective"] == "pressure" for seed in seeds)
    assert all("latch_risk" in seed.payload for seed in seeds)


def test_live_canary_routes_control_decision_into_perfusion_seeds(tmp_path):
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    persist_information_strings(
        conn=store.conn,
        cycle_id="cycle_perf_live_route",
        scout_id="scout_route",
        seed_id="seed_route",
        entity="HYPE",
        theme="node_perfusion",
        horizon="intraday",
        lens="onchain_flow",
        bias_mode="frontier",
        strings=[
            InformationString(
                title="HYPE pressure route",
                thesis="HYPE information pressure needs follow-up before price fully absorbs node demand.",
                mechanism="Node and onchain sources agree but the market has not moved enough.",
                expected_outcome="HYPE reprices higher if new demand keeps absorbing unstake supply.",
                time_horizon="hour",
                observed_at="2026-05-22T10:00:00+00:00",
                expires_at="2099-05-23T10:00:00+00:00",
                conviction=0.91,
                novelty_score=0.84,
                crowdedness=0.18,
                entities_chain=["HYPE", "our_hl_node", "hydromancer"],
                depth_layers=[{"layer": 1, "claim": "node pressure"}, {"layer": 2, "claim": "repricing"}],
                evidence_refs=["fixture://our_hl_node/hype", "fixture://hydromancer/hype"],
                quality_flags=["source_family:our_hl_node", "source_family:hydromancer"],
            )
        ],
    )
    evaluate_information_price_outcomes(
        cycle_id="cycle_perf_live_route",
        price_observations=[
            {"entity": "HYPE", "observed_at": "2026-05-22T10:00:00+00:00", "price": 20.0, "source": "fixture"},
            {"entity": "HYPE", "observed_at": "2026-05-22T11:00:00+00:00", "price": 20.08, "source": "fixture"},
        ],
        min_move_threshold_pct=0.02,
        conn=store.conn,
    )
    compute_information_perfusion(cycle_id="cycle_perf_live_route", scout_budget=3, conn=store.conn)

    args = Namespace(
        control_decision="perfusion_pressure_requests_sentinel",
        control_allowed_next_step="perfusion_pressure_sentinel",
        perfusion_source_cycle_id="cycle_perf_live_route",
        seed_rng=7,
        theme_share=0.0,
        prompt_variant="temporal_pyramid_v1",
        max_tool_iterations=3,
    )
    seeds, report = _generate_control_aware_live_seeds(
        args=args,
        cycle_id="cycle_live_next",
        conn=store.conn,
        universe_entities=["HYPE", "BTC"],
        base_seed_count=3,
    )

    assert report["status"] == "perfusion_routed"
    assert report["route_objective"] == "pressure"
    assert report["perfusion_seed_count"] == 3
    assert report["broad_seed_count"] == 0
    assert all(seed.payload["source"] == "information_perfusion_route" for seed in seeds)
    assert all(seed.payload["prompt_variant"] == "temporal_pyramid_v1" for seed in seeds)
    assert all(seed.payload["max_tool_iterations"] == 3 for seed in seeds)


def test_live_canary_annotates_missing_proof_metric_seed_slice(tmp_path):
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    args = Namespace(
        control_decision="collect_missing_proof_gate_metrics",
        control_allowed_next_step="proof_gate_metric_sentinel",
        control_proof_metrics="candidate_fragile_verify_rate,candidate_avg_realized_edge_score",
        perfusion_source_cycle_id="",
        seed_rng=11,
        theme_share=0.0,
        prompt_variant="",
        max_tool_iterations=1,
    )

    seeds, report = _generate_control_aware_live_seeds(
        args=args,
        cycle_id="cycle_live_proof_gate",
        conn=store.conn,
        universe_entities=["HYPE", "BTC"],
        base_seed_count=3,
    )

    assert report["route_objective"] == "broad_market_grid"
    assert report["proof_metric_seed_count"] == 3
    assert report["control_proof_metrics"] == [
        "candidate_fragile_verify_rate",
        "candidate_avg_realized_edge_score",
    ]
    assert all(seed.payload["control_collects_missing_falsification_gate_metrics"] is True for seed in seeds)
    assert all(seed.payload["control_proof_metrics"] == report["control_proof_metrics"] for seed in seeds)
    assert all(seed.payload["max_tool_iterations"] == 2 for seed in seeds)
    assert all(seed.payload["max_evidence_tools"] >= 3 for seed in seeds)
    assert all(seed.payload["tool_candidate_limit"] >= 12 for seed in seeds)
    assert all("tic://tool/talis_native/plan_alpha_geometry_actions@v1" in seed.payload["tool_candidates"] for seed in seeds)
    assert all("tic://tool/builtin/query_timeseries@v1" in seed.payload["tool_candidates"] for seed in seeds)


def test_market_evolve_exploits_perfusion_pressure_when_price_has_not_absorbed(tmp_path):
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    persist_information_strings(
        conn=store.conn,
        cycle_id="cycle_perf_evolve",
        scout_id="scout_vvv",
        seed_id="seed_vvv",
        entity="VVV",
        theme="early_social_alpha",
        horizon="intraday",
        lens="social_alpha",
        bias_mode="frontier",
        strings=[
            InformationString(
                title="VVV oxygenated pressure",
                thesis="VVV should be priced upwards as node, microstructure, and X attention align before consensus.",
                mechanism="Independent source families align while price has barely moved.",
                expected_outcome="VVV trades higher after repeated pressure confirmation.",
                time_horizon="hour",
                observed_at="2026-05-22T10:00:00+00:00",
                expires_at="2099-05-23T10:00:00+00:00",
                conviction=0.94,
                novelty_score=0.90,
                crowdedness=0.08,
                entities_chain=["VVV", "our_hl_node", "market_microstructure", "grok_x_alpha"],
                depth_layers=[{"layer": 1, "claim": "attention"}, {"layer": 2, "claim": "repricing"}],
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
        cycle_id="cycle_perf_evolve",
        price_observations=[
            {"entity": "VVV", "observed_at": "2026-05-22T09:59:00+00:00", "price": 10.0, "source": "fixture"},
            {"entity": "VVV", "observed_at": "2026-05-22T11:05:00+00:00", "price": 10.05, "source": "fixture"},
        ],
        min_move_threshold_pct=0.02,
        conn=store.conn,
    )
    compute_information_perfusion(cycle_id="cycle_perf_evolve", scout_budget=16, conn=store.conn)

    step = run_market_evolve_step(cycle_id="cycle_perf_evolve", conn=store.conn)

    mutation = next(
        m for m in step.mutations
        if m.mutation_kind == "exploit_information_perfusion_pressure"
    )
    proof = mutation.mutation["_evolution_proof"]
    assert "perfusion_avg_pressure_gradient" in proof["target_metrics"]
    assert any(gate["metric"] == "candidate_perfusion_max_dilation_score" for gate in proof["falsification_gates"])
    child = next(c for c in step.child_programs if c.program_id == mutation.child_program_id)
    assert child.genome["prompt_policy"]["emphasize_perfusion_state"] is True
    assert child.genome["tool_request_policy"]["prefer_information_perfusion_tools"] is True
    assert child.genome["routing_thresholds"]["perfusion_followup_budget_share"] > 0.02


def test_market_evolve_oxygenates_perfusion_sources_when_pressure_is_thin(tmp_path):
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    persist_information_strings(
        conn=store.conn,
        cycle_id="cycle_perf_oxygenate",
        scout_id="scout_hype",
        seed_id="seed_hype",
        entity="HYPE",
        theme="node_intelligence",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        strings=[
            InformationString(
                title="HYPE thin pressure",
                thesis="HYPE should be priced upwards if a single observed node route implies absorption.",
                mechanism="The map has pressure, but source breadth is too thin for broad scale.",
                expected_outcome="HYPE trades higher only if other sources confirm absorption.",
                time_horizon="hour",
                observed_at="2026-05-22T10:00:00+00:00",
                expires_at="2099-05-23T10:00:00+00:00",
                conviction=0.88,
                novelty_score=0.82,
                crowdedness=0.18,
                entities_chain=["HYPE", "our_hl_node"],
                depth_layers=[{"layer": 1, "claim": "node route"}, {"layer": 2, "claim": "absorption"}],
                evidence_refs=[],
                quality_flags=["source_family:our_hl_node"],
            )
        ],
    )
    evaluate_information_price_outcomes(
        cycle_id="cycle_perf_oxygenate",
        price_observations=[
            {"entity": "HYPE", "observed_at": "2026-05-22T09:59:00+00:00", "price": 20.0, "source": "fixture"},
            {"entity": "HYPE", "observed_at": "2026-05-22T11:05:00+00:00", "price": 20.1, "source": "fixture"},
        ],
        min_move_threshold_pct=0.02,
        conn=store.conn,
    )
    perfusion = compute_information_perfusion(cycle_id="cycle_perf_oxygenate", scout_budget=8, conn=store.conn)
    assert perfusion.cells[0].route_directive == "oxygenate_sources"

    step = run_market_evolve_step(cycle_id="cycle_perf_oxygenate", conn=store.conn)

    mutation = next(
        m for m in step.mutations
        if m.mutation_kind == "oxygenate_information_perfusion_sources"
    )
    proof = mutation.mutation["_evolution_proof"]
    assert "perfusion_avg_source_oxygenation" in proof["target_metrics"]
    child = next(c for c in step.child_programs if c.program_id == mutation.child_program_id)
    assert child.genome["tool_request_policy"]["prefer_information_perfusion_tools"] is True
    assert child.genome["tool_request_policy"]["prefer_missing_source_family"] is True
    assert "grok_x_alpha" in child.genome["tool_request_policy"]["source_family_targets"]
