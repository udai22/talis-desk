from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from talis_desk.information_map import (
    DEPTH_LADDER,
    DEFAULT_MARKET_EVOLVE_GENOME,
    AlphaGeometryCell,
    AlphaGeometrySnapshot,
    ActorProfile,
    EventDataPoint,
    EventScenario,
    InformationString,
    InformationSynthesis,
    MarketEventIntelligenceBundle,
    MarketEvolveEvaluation,
    NodeObservation,
    WatchTrigger,
    apply_market_evolve_policy_to_seeds,
    apply_adversarial_review,
    alpha_geometry_seed_directives,
    build_alpha_geometry_cortex_review,
    build_market_evolve_lineage,
    collect_market_evolve_metrics,
    compute_alpha_geometry,
    evaluate_market_evolve_experiments,
    evaluate_prompt_scale_gate,
    event_intelligence_from_tool_evidence,
    event_intelligence_to_information_string,
    load_promoted_candidates,
    load_alpha_geometry,
    load_market_evolve_experiment_results,
    load_market_evolve_experiments,
    load_market_evolve_policy_applications,
    load_market_evolve_programs,
    prepare_market_evolve_experiment_seed_pairs,
    plan_alpha_geometry_actions,
    post_evolution_control_work_orders,
    persist_alpha_geometry,
    run_market_evolve_step,
    node_intelligence_from_tool_evidence,
    node_intelligence_to_information_string,
    persist_node_intelligence,
    persist_event_intelligence,
    persist_information_strings,
    recent_information_strings,
    run_information_synthesis,
    select_information_context,
    review_information_string,
    score_event_intelligence,
    score_information_string,
    score_node_intelligence,
    data_substrate_to_information_string,
    summarize_data_substrate,
)
from talis_desk.information_map.deep_scout_prompt import (
    build_deep_scout_system_prompt,
    score_deep_scout_output,
)
from talis_desk.information_map.market_evolve import (
    _experiment_decision,
    propose_market_evolve_mutation,
    propose_market_evolve_mutations,
    seed_default_market_evolve_program,
)
from talis_desk.coordination import claim_task, complete_task, fail_task, post_task, start_task
from talis_desk.schema.migrations import get_schema_version
from talis_desk.store import DeskStore, reset_desk_store_for_test
from talis_desk.tool_atlas.discovery import (
    AnalysisToolProposal,
    evaluate_analysis_tool_proposal,
    iterate_tool_proposal,
    load_analysis_tool_proposals,
    persist_analysis_tool_proposals,
    propose_tools_from_quality_flags,
)
from talis_desk.tool_atlas import (
    AgentContext,
    dispatch_uri,
    regenerate_tool_atlas,
    mark_runtime_adapter_ready,
    promote_analysis_tool_proposal,
)
from talis_desk.swarm.information_bridge import promoted_scouts_from_synthesis
from talis_desk.swarm.scout_runner import (
    _apply_prompt_contract_pressure,
    _build_user_prompt,
    _event_bundles_for_scout,
    _evaluate_route_contract_alignment,
    _extract_first_json,
    _expanded_tool_candidate_rows,
    _infer_tool_args,
    _infer_tool_args_for_candidate,
    _normalize_tool_requests,
    _node_snapshots_for_scout,
    _successful_tool_call_ids,
)
from talis_desk.swarm.seed_generator import (
    SeedCell,
    generate_alpha_geometry_route_seeds,
    generate_seeds,
    narrow_tools_for_seed,
)


def test_schema_v24_information_map_tables(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    conn = store.conn
    assert get_schema_version(conn) >= 24
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "information_strings" in tables
    assert "information_map_nodes" in tables
    assert "information_map_edges" in tables
    assert "information_syntheses" in tables
    assert "information_synthesis_items" in tables
    assert "promoted_candidates" in tables
    assert "information_artifact_edges" in tables
    assert "information_string_evidence" in tables
    assert "market_event_intelligence" in tables
    assert "market_event_data_points" in tables
    assert "market_event_watch_triggers" in tables
    assert "node_intelligence_snapshots" in tables
    assert "node_intelligence_observations" in tables
    assert "analysis_tool_proposals" in tables
    assert "information_geometry_snapshots" in tables
    assert "market_evolve_programs" in tables
    assert "market_evolve_evaluations" in tables
    assert "market_evolve_mutations" in tables
    assert "market_evolve_policy_applications" in tables
    assert "market_evolve_experiments" in tables
    assert "market_evolve_experiment_results" in tables
    app_cols = {
        r[1]
        for r in conn.execute("PRAGMA table_info(market_evolve_policy_applications)").fetchall()
    }
    assert "experiment_id" in app_cols
    assert "experiment_arm" in app_cols
    cols = {
        r[1]
        for r in conn.execute("PRAGMA table_info(information_strings)").fetchall()
    }
    assert "attention_score" in cols
    assert "coverage_cell_key" in cols
    assert "time_scale" in cols
    assert "event_time_start" in cols
    assert "rollup_parent_ids" in cols


def test_persist_information_strings_updates_graph(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    ids = persist_information_strings(
        conn=store.conn,
        cycle_id="cycle_a",
        scout_id="scout_a",
        seed_id="seed_a",
        entity="NVDA",
        theme="ai_capex",
        horizon="1w",
        lens="catalyst",
        bias_mode="frontier",
        coverage_cell_key="NVDA|1w|catalyst|frontier|ai_capex",
        strings=[
            InformationString(
                title="AI capex digestion",
                thesis="NVDA reprices if hyperscaler capex guidance shifts second-order supplier expectations.",
                mechanism="Guidance changes propagate from hyperscalers to GPU supplier estimates.",
                expected_outcome="Estimate revisions and semis basket relative strength move together.",
                time_horizon="1w",
                time_scale="1w",
                source_time_basis="event_time",
                kill_signal="Hyperscaler guide is unchanged and semis breadth fails to confirm.",
                extends_or_contradicts="new",
                would_change_decision=True,
                expires_at="after the next hyperscaler capex guide",
                crowdedness=0.6,
                conviction=0.72,
                novelty_score=0.68,
                entities_chain=["NVDA", "hyperscaler capex", "semis basket"],
                depth_layers=[{"layer": 1, "claim": "capex guide"}, {"layer": 2, "claim": "supplier revisions"}],
                evidence_refs=["tool_call_1"],
            )
        ],
    )
    assert len(ids) == 1
    rows = recent_information_strings(cycle_id="cycle_a", conn=store.conn)
    assert rows[0]["entity"] == "NVDA"
    assert rows[0]["entities_chain"][0] == "NVDA"
    assert rows[0]["coverage_cell_key"] == "NVDA|1w|catalyst|frontier|ai_capex"
    assert rows[0]["time_scale"] == "1w"
    assert rows[0]["attention_score"] > 0
    context = select_information_context(
        entity="NVDA",
        theme="ai_capex",
        lens="catalyst",
        horizon="1w",
        conn=store.conn,
    )
    assert context[0]["id"] == ids[0]
    assert store.conn.execute("SELECT COUNT(*) FROM information_map_nodes").fetchone()[0] >= 3
    assert store.conn.execute("SELECT COUNT(*) FROM information_map_edges").fetchone()[0] >= 2
    assert store.conn.execute("SELECT COUNT(*) FROM information_string_evidence").fetchone()[0] == 1


def test_information_synthesis_promotes_high_signal_strings(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    for i, entity in enumerate(["NVDA", "AVGO", "TSM"]):
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_b",
            scout_id=f"scout_{i}",
            seed_id=f"seed_{i}",
            entity=entity,
            theme="ai_capex",
            horizon="1w",
            lens="catalyst",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"{entity} AI capex",
                    thesis=f"{entity} has a second-order AI capex repricing string.",
                    mechanism="Shared capex driver.",
                    expected_outcome="Relative strength confirms.",
                    kill_signal="Capex guide weakens.",
                    conviction=0.75 - i * 0.05,
                    novelty_score=0.7,
                    crowdedness=0.3,
                    entities_chain=[entity, "ai_capex"],
                    depth_layers=[{"layer": 1, "claim": "direct"}, {"layer": 2, "claim": "second-order"}],
                )
            ],
        )
    result = run_information_synthesis(cycle_id="cycle_b", use_llm=False)
    assert result.promoted_hypotheses
    assert result.confluences
    assert result.synthesis_item_ids
    assert result.candidate_ids
    assert len(result.promoted_hypotheses[0]["source_string_ids"]) >= 2
    assert result.promoted_hypotheses[0]["status"] == "queued_verifier"
    assert "adversarial_promotion_gate" in result.promoted_hypotheses[0]["quality_flags"]
    assert store.conn.execute("SELECT COUNT(*) FROM information_syntheses").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM information_synthesis_items").fetchone()[0] >= 1
    assert store.conn.execute("SELECT COUNT(*) FROM promoted_candidates").fetchone()[0] >= 1
    assert store.conn.execute("SELECT COUNT(*) FROM information_artifact_edges").fetchone()[0] >= 3
    geometry_rows = load_alpha_geometry(cycle_id="cycle_b", conn=store.conn)
    assert geometry_rows
    assert "trade_scream_score" in geometry_rows[0]
    candidates = load_promoted_candidates(synthesis_id=result.synthesis_id, conn=store.conn)
    assert candidates[0]["source_string_ids"]
    scouts = promoted_scouts_from_synthesis(result, existing_scouts=[], max_items=3)
    assert scouts[0].hypothesis_id == candidates[0]["id"]
    assert "queryable_promoted_candidate" in scouts[0].quality_flags


def test_promoted_synthesis_wraps_as_scout_outputs():
    synthesis = InformationSynthesis(
        synthesis_id="isyn_test",
        cycle_id="cycle_c",
        summary="Two strings converge on AI capex.",
        promoted_hypotheses=[
            {
                "hypothesis": "NVDA reprices if AI capex guide pulls semis breadth higher inside 1w.",
                "entity": "NVDA",
                "horizon": "1w",
                "lens": "catalyst",
                "confidence": 0.67,
                "rationale_brief": "Cross-string confluence across capex and breadth.",
                "source_string_ids": ["istr_a", "istr_b"],
            }
        ],
    )
    scouts = promoted_scouts_from_synthesis(synthesis, existing_scouts=[], max_items=3)
    assert len(scouts) == 1
    assert scouts[0].information_string_ids == ["istr_a", "istr_b"]
    assert "promoted_from_information_synthesis" in scouts[0].quality_flags


def test_deep_scout_prompt_quality_gate():
    prompt = build_deep_scout_system_prompt("skeptical_operator_v1")
    assert "information_strings" in prompt
    assert "would_change_decision" in prompt
    assert "Depth ladder" in prompt
    assert "human specialization" in prompt
    assert "<prompt_metadata>" in prompt
    assert "<verify_before_output>" in prompt
    assert "Do not summarize a headline" in prompt
    assert "two desks would each miss half the chain" in build_deep_scout_system_prompt("seam_hunter_v1")
    assert "climb all five depth layers" in build_deep_scout_system_prompt("depth_ladder_v1")
    assert "pre-obvious" in build_deep_scout_system_prompt("early_alpha_v1")
    assert "Hard blockers" in build_deep_scout_system_prompt("concise_contract_v1")
    assert "Keep JSON small" in build_deep_scout_system_prompt("flash_compact_v2")
    assert "temporal_confidence" in build_deep_scout_system_prompt("flash_temporal_v3")
    assert "scale repair arm" in build_deep_scout_system_prompt("flash_temporal_v4")
    assert "Do not leave `hypothesis` empty" in build_deep_scout_system_prompt("flash_temporal_v4")
    assert len(build_deep_scout_system_prompt("flash_temporal_v3")) < len(build_deep_scout_system_prompt("concise_contract_v1"))
    assert len(build_deep_scout_system_prompt("flash_compact_v2")) < len(build_deep_scout_system_prompt("concise_contract_v1"))
    assert "event_intelligence" in build_deep_scout_system_prompt("mycelial_network_v1")
    assert "node_intelligence" in build_deep_scout_system_prompt("mycelial_network_v1")
    assert len(DEPTH_LADDER) == 5
    parsed = {
        "hypothesis": "NVDA reprices higher if hyperscaler capex guides up and semis breadth confirms within 1w.",
        "confidence": 0.63,
        "rationale_brief": "Capex guide drives supplier revisions and breadth confirmation.",
        "suggested_tools": ["tic://tool/builtin/query_events_recent@v1", "tic://tool/builtin/query_timeseries@v1"],
        "information_strings": [
            {
                "title": "AI capex string",
                "thesis": "Hyperscaler capex raises supplier revision probability.",
                "entities_chain": ["NVDA", "hyperscaler capex", "semis breadth"],
                "mechanism": "Guidance changes expectations before estimates fully update.",
                "depth_layers": [
                    {"layer": 1, "claim": "capex"},
                    {"layer": 2, "claim": "supplier revisions"},
                    {"layer": 3, "claim": "semis breadth"},
                    {"layer": 4, "claim": "positioning and vol amplify"},
                    {"layer": 5, "claim": "kill if breadth diverges"},
                ],
                "expected_outcome": "NVDA and semis breadth outperform.",
                "time_horizon": "1w",
                "kill_signal": "Capex guide disappoints or breadth diverges.",
                "extends_or_contradicts": "new",
                "would_change_decision": True,
                "expires_at": "after the next hyperscaler capex guide",
                "crowdedness": 0.6,
                "conviction": 0.68,
                "novelty_score": 0.62,
                "evidence_refs": ["tool_call_1"],
                "prior_thread_refs": [],
            }
        ],
    }
    q = score_deep_scout_output(
        parsed,
        allowed_tools=["tic://tool/builtin/query_events_recent@v1", "tic://tool/builtin/query_timeseries@v1"],
    )
    assert q.passed
    assert q.n_strings == 1


def test_string_rubric_rejects_shallow_summary_and_scale_gate():
    shallow = {
        "thesis": "NVDA is an AI winner.",
        "crowdedness": 0.95,
        "novelty_score": 0.05,
        "conviction": 0.8,
    }
    result = score_information_string(shallow)
    assert not result.passed
    assert "missing_mechanism" in result.flags
    assert "missing_evidence_refs" in result.flags

    deep = {
        "thesis": "A hyperscaler capex guide changes supplier revision odds before consensus estimates update.",
        "mechanism": "Capex budget revisions flow first through GPU purchase expectations, then through semis breadth.",
        "expected_outcome": "NVDA and supplier basket relative strength confirm inside one week.",
        "time_horizon": "1w",
        "kill_signal": "Capex guide is unchanged or semis breadth diverges.",
        "extends_or_contradicts": "new",
        "would_change_decision": True,
        "expires_at": "after next hyperscaler guide",
        "crowdedness": 0.45,
        "conviction": 0.72,
        "novelty_score": 0.66,
        "entities_chain": ["NVDA", "hyperscaler capex", "supplier basket"],
        "depth_layers": [{"layer": i, "claim": f"layer {i}"} for i in range(1, 6)],
        "evidence_refs": ["tool_call_1", "tool_call_2"],
    }
    result = score_information_string(deep)
    assert result.passed
    gate = evaluate_prompt_scale_gate([
        {"score": result.score, "passed": True, "flags": []},
        {"score": result.score, "passed": True, "flags": []},
        {"score": 0.76, "passed": True, "flags": []},
    ])
    assert gate.passed


def test_market_event_intelligence_persists_all_data(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    bundle = MarketEventIntelligenceBundle(
        cycle_id="cycle_hype",
        entity="HYPE",
        event_type="unstake",
        asset="HYPE",
        protocol="Hyperliquid",
        event_time="2026-05-21T11:30:00Z",
        amount=558_100,
        amount_unit="HYPE",
        notional_usd=19_500_000,
        actor=ActorProfile(
            label="aHYPE",
            address="0xabc",
            actor_type="protocol_wrapper",
            confidence=0.74,
            prior_behavior=["prior unstake mostly restaked within 2h"],
            source_refs=["tool_actor_profile"],
        ),
        summary="HYPE unstake matters only if the actor routes tokens to sellable liquidity.",
        base_case="Base case is watch-and-confirm: prior behavior leans restake/hold, not immediate sell.",
        bear_case="Bearish if tokens move to CEX while funding remains crowded long.",
        bull_case="Bullish if no transfer appears by T+2h and perps de-risk first.",
        kill_signal="No transfer, CEX deposit, or sellable route by T+2h after unstake.",
        directional_bias="mixed",
        liquidity_context=[
            EventDataPoint(
                category="liquidity_context",
                label="amount_vs_24h_volume",
                value="558.1K HYPE is 7.8% of trailing 24h volume",
                numeric_value=0.078,
                unit="ratio",
                source_ref="tool_volume",
                confidence=0.82,
            ),
            EventDataPoint(
                category="liquidity_context",
                label="amount_vs_orderbook_depth",
                value="2.4x top-of-book 1% depth",
                numeric_value=2.4,
                unit="ratio",
                source_ref="tool_depth",
                confidence=0.79,
            ),
        ],
        derivatives_context=[
            EventDataPoint(
                category="derivatives_context",
                label="funding_and_oi",
                value="Funding positive while OI elevated",
                numeric_value=0.63,
                unit="crowding_score",
                source_ref="tool_derivatives",
                confidence=0.7,
            )
        ],
        historical_analogs=[
            EventDataPoint(
                "historical_analog",
                "prior_aHYPE_unstake",
                "restaked within 2h",
                source_ref="tool_actor_profile",
                confidence=0.74,
            )
        ],
        scenarios=[
            EventScenario(
                "restake_hold",
                0.55,
                "Actor repeats benign prior behavior.",
                "No sell pressure.",
                "No transfer by T+2h",
                source_refs=["tool_actor_profile"],
            ),
            EventScenario(
                "cex_sell",
                0.30,
                "Tokens route to exchange while perps are long.",
                "Spot pressure and perp de-risking.",
                "CEX deposit within 90m",
                "No transfer by T+2h",
                ["tool_depth"],
            ),
        ],
        watch_triggers=[
            WatchTrigger(
                "cex_deposit",
                "Alert if actor sends HYPE to a CEX-labeled address.",
                "T+90m",
                "bearish",
                "red",
                ["tool_actor_profile"],
            ),
            WatchTrigger(
                "no_transfer",
                "Alert if tokens stay idle/restake by T+2h.",
                "T+2h",
                "bullish",
                "green",
                ["tool_actor_profile"],
            ),
        ],
        source_refs=["tool_actor_profile", "tool_volume", "tool_depth", "tool_derivatives"],
    )
    quality = score_event_intelligence(bundle)
    assert quality.passed
    bundle_id = persist_event_intelligence(bundle, conn=store.conn)
    assert bundle_id.startswith("mev_")
    assert store.conn.execute("SELECT COUNT(*) FROM market_event_intelligence").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM market_event_data_points").fetchone()[0] >= 12
    assert store.conn.execute("SELECT COUNT(*) FROM market_event_watch_triggers").fetchone()[0] == 2
    info = event_intelligence_to_information_string(bundle)
    assert "from_market_event_intelligence" in info.quality_flags
    assert "tool_depth" in info.evidence_refs
    assert info.would_change_decision


def test_event_intelligence_fallback_recovers_tool_evidence(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    evidence = [
        {
            "uri": "tic://tool/builtin/query_events_recent@v1",
            "ok": True,
            "tool_call_log_id": "tc_events",
            "result": {
                "events": [
                    {
                        "event_type": "unstake",
                        "headline": "Upcoming unstaking aHYPE 558.1K HYPE in 3h.",
                        "source": "hyperview",
                        "event_time": "2026-05-21T11:30:00Z",
                        "metadata": {
                            "label": "aHYPE",
                            "address": "0xabc",
                            "actor_type": "protocol_wrapper",
                            "depth_1pct_ratio": 2.4,
                            "funding_crowding_score": 0.63,
                        },
                    }
                ]
            },
        },
        {
            "uri": "tic://tool/builtin/query_timeseries@v1",
            "ok": True,
            "tool_call_log_id": "tc_ts",
            "result": {
                "points": [
                    {"ts": "2026-05-21T10:00:00Z", "metric": "spot_volume_24h", "value": 7_150_000},
                    {"ts": "2026-05-21T10:00:00Z", "metric": "perp_open_interest", "value": 0.71},
                    {"ts": "2026-05-20T10:00:00Z", "metric": "prior_unstake_return_2h", "value": -0.012},
                ]
            },
        },
    ]
    bundles = event_intelligence_from_tool_evidence(
        cycle_id="cycle_hype",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        tool_evidence=evidence,
    )
    assert len(bundles) == 1
    bundle = bundles[0]
    assert bundle.amount == 558_100
    assert bundle.actor.label == "aHYPE"
    assert "tc_events" in bundle.source_refs
    assert bundle.liquidity_context
    assert bundle.derivatives_context
    assert bundle.historical_analogs
    assert score_event_intelligence(bundle).passed
    bundle_id = persist_event_intelligence(bundle, conn=store.conn)
    info = event_intelligence_to_information_string(bundle)
    assert bundle_id in info.evidence_refs
    assert store.conn.execute("SELECT COUNT(*) FROM market_event_data_points").fetchone()[0] >= 10


def test_scout_event_bundle_wiring_uses_model_or_tool_evidence():
    seed = SeedCell(
        seed_id="seed_hype",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        theme="staking_unstake",
    )
    evidence = [
        {
            "uri": "tic://tool/builtin/query_events_recent@v1",
            "ok": True,
            "tool_call_log_id": "tc_events",
            "result": {
                "events": [
                    {
                        "event_type": "unstake",
                        "headline": "Upcoming unstaking Insilico Terminal 512K HYPE in 4h.",
                        "event_time": "2026-05-21T12:30:00Z",
                        "metadata": {
                            "label": "Insilico Terminal",
                            "depth_1pct_ratio": 1.8,
                            "funding_crowding_score": 0.57,
                        },
                    }
                ]
            },
        }
    ]
    parsed = {
        "hypothesis": "HYPE reprices only if the unstake routes to sellable liquidity.",
        "confidence": 0.61,
        "rationale_brief": "The event row needs actor-route and liquidity absorption checks.",
        "suggested_tools": ["tic://tool/builtin/query_events_recent@v1"],
        "information_strings": [],
    }
    bundles = _event_bundles_for_scout(
        parsed=parsed,
        seed=seed,
        cycle_id="cycle_hype",
        tool_evidence=evidence,
    )
    assert len(bundles) == 1
    assert bundles[0].event_type == "unstake"
    assert bundles[0].amount == 512_000
    assert bundles[0].raw_sources
    assert "from_tool_evidence_fallback" in bundles[0].quality_flags


def test_node_intelligence_persists_hydromancer_and_node_evidence(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    evidence = [
        {
            "uri": "tic://tool/hydromancer/get_hl_pnl_leaderboard@v1",
            "ok": True,
            "tool_call_log_id": "tc_hydro_leaders",
            "result": {
                "leaders": [
                    {
                        "rank": 1,
                        "wallet": "0xabc",
                        "realized_pnl_usd": 1_250_000,
                        "win_rate_pct": 68.2,
                        "volume_usd": 25_000_000,
                        "n_completed_trades": 144,
                    }
                ],
                "headline": "Top trader 0xabc leads HYPE tape.",
            },
        },
        {
            "uri": "tic://tool/hydromancer/get_builder_fills@v1",
            "ok": True,
            "tool_call_log_id": "tc_builder",
            "result": {
                "builder": "0xaf99",
                "total_volume_usd": 3_500_000,
                "total_builder_fee_usd": 420,
                "unique_users": 7,
                "top_coins": [["HYPE", 19]],
                "top_users_by_volume": [["0xabc", 2_100_000]],
                "fills_sample": [{"coin": "HYPE", "user": "0xabc", "px": 35, "sz": 1000}],
            },
        },
        {
            "uri": "tic://tool/builtin/hl_reject_corpus@v1",
            "ok": True,
            "tool_call_log_id": "tc_node_rejects",
            "result": {
                "wallet": "0xabc",
                "reject_rate_pct": 1.7,
                "status_counts": {"filled": 116, "rejected": 2},
                "top_reject_reasons": [["insufficient_margin", 2]],
            },
        },
        {
            "uri": "tic://tool/builtin/query_timeseries@v1",
            "ok": True,
            "tool_call_log_id": "tc_market_state",
            "result": {
                "points": [
                    {"ts": "2026-05-21T10:00:00Z", "metric": "HYPE_perp_open_interest", "value": 910_000_000},
                    {"ts": "2026-05-21T10:00:00Z", "metric": "HYPE_orderbook_depth_1pct", "value": 8_000_000},
                ]
            },
        },
    ]
    snapshot = node_intelligence_from_tool_evidence(
        cycle_id="cycle_hype",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        tool_evidence=evidence,
    )
    assert snapshot is not None
    quality = score_node_intelligence(snapshot)
    assert quality.passed
    assert "hydromancer" in quality.source_families
    assert "our_hl_node" in quality.source_families
    snapshot_id = persist_node_intelligence(snapshot, conn=store.conn)
    info = node_intelligence_to_information_string(snapshot)
    assert snapshot_id in info.evidence_refs
    assert "from_node_intelligence" in info.quality_flags
    assert store.conn.execute("SELECT COUNT(*) FROM node_intelligence_observations").fetchone()[0] >= 4


def test_data_substrate_summarizes_touched_surfaces_and_expansion_plan():
    evidence = [
        {
            "uri": "tic://tool/builtin/query_events_recent@v1",
            "ok": True,
            "tool_call_log_id": "tc_events",
            "result": {"events": [{"entity": "HYPE", "event_type": "unstake"}]},
        },
        {
            "uri": "tic://tool/hydromancer/get_hl_pnl_leaderboard@v1",
            "ok": True,
            "tool_call_log_id": "tc_hydro",
            "result": {"leaders": [{"wallet": "0xabc", "realized_pnl_usd": 1000}]},
        },
        {
            "uri": "tic://tool/builtin/hl_reject_corpus@v1",
            "ok": True,
            "tool_call_log_id": "tc_node",
            "result": {"source": "our_hl_node", "reject_rate_pct": 1.7},
        },
        {
            "uri": "tic://tool/builtin/query_timeseries@v1",
            "ok": True,
            "tool_call_log_id": "tc_market",
            "result": {"points": [{"metric": "HYPE_orderbook_depth_1pct", "value": 8_000_000}]},
        },
    ]
    summary = summarize_data_substrate(
        evidence,
        allowed_tools=[item["uri"] for item in evidence],
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
    )
    touched = {row.surface.key for row in summary.touched if row.touched}
    assert {"event_feed", "market_state", "hydromancer_actor_graph", "our_hl_node"} <= touched
    assert "event -> HYPE market_state" in summary.connection_edges
    assert "wallet -> node_order_quality" in summary.connection_edges
    expansion_titles = {row.title for row in summary.expansions}
    assert "Resolve actor route state" in expansion_titles
    assert "Watch pending intent" in expansion_titles
    assert summary.coverage_score > 0
    assert summary.active_receipts >= 4


def test_tool_source_receipts_become_map_strings(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    evidence = [
        {
            "uri": "tic://tool/builtin/query_events_recent@v1",
            "ok": True,
            "tool_call_log_id": "tc_events",
            "result": {"events": [{"entity": "HYPE", "event_type": "unstake"}]},
        },
        {
            "uri": "tic://source/hl/l4_micro",
            "ok": True,
            "tool_call_log_id": "tc_source_l4",
            "result": {"source": "hl_l4_micro", "orderbook": {"coin": "HYPE"}},
        },
        {
            "uri": "tic://tool/hydromancer/get_builder_fills@v1",
            "ok": True,
            "tool_call_log_id": "tc_builder",
            "result": {"builder": "0xaf99", "top_coins": [["HYPE", 7]]},
        },
    ]
    info = data_substrate_to_information_string(
        cycle_id="cycle_tools_to_map",
        scout_id="scout_tools_to_map",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        tool_evidence=evidence,
        allowed_tools=[item["uri"] for item in evidence],
    )
    assert info is not None
    assert "from_tool_source_expansion" in info.quality_flags
    assert {"tc_events", "tc_source_l4", "tc_builder"} <= set(info.evidence_refs)
    assert "information_map" in info.entities_chain
    assert any("Missing edges" in row["claim"] for row in info.depth_layers)
    ids = persist_information_strings(
        conn=store.conn,
        cycle_id="cycle_tools_to_map",
        scout_id="scout_tools_to_map",
        seed_id="seed_tools_to_map",
        entity="HYPE",
        theme="node_intelligence",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        strings=[info],
    )
    assert ids
    assert store.conn.execute("SELECT COUNT(*) FROM information_string_evidence").fetchone()[0] >= 3
    assert store.conn.execute("SELECT COUNT(*) FROM information_map_edges").fetchone()[0] >= 2


def test_data_substrate_names_broader_source_estate():
    evidence = [
        {
            "uri": "tic://tool/parallel/parallel_search@v1",
            "ok": True,
            "tool_call_log_id": "tc_parallel",
            "result": {"results": [{"url": "https://example.com", "text": "fresh source"}]},
        },
        {
            "uri": "tic://source/misc/astro_cycles",
            "ok": True,
            "tool_call_log_id": "tc_astro",
            "result": {"sunspot_number": 121, "lunar_phase": "waxing"},
        },
        {
            "uri": "tic://source/macro/fred_macro",
            "ok": True,
            "tool_call_log_id": "tc_fred",
            "result": {"series": [{"id": "DGS10", "value": 4.2}]},
        },
    ]
    summary = summarize_data_substrate(
        evidence,
        allowed_tools=[item["uri"] for item in evidence],
        entity="SPY",
        horizon="structural",
        lens="anomaly",
    )
    touched = {row.surface.key for row in summary.touched if row.touched}
    assert {"parallel_web_attention", "celestial_cycles", "macro_official"} <= touched
    assert "cycle_prior -> macro_confounder_test" in summary.connection_edges


def test_scout_can_expand_to_full_readonly_atlas(monkeypatch):
    import talis_desk.tool_atlas as tool_atlas

    seed = SeedCell(
        seed_id="seed_atlas",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        theme="validator_unstake",
        payload={"tool_candidates": ["tic://tool/builtin/query_events_recent@v1"]},
    )
    rows = [
        {
            "tool_uri": "tic://tool/hydromancer/get_builder_fills@v1",
            "tool_name": "get_builder_fills",
            "kind": "hydromancer",
            "provider": "hydromancer",
            "description": "builder fills, users, coins and routed flow",
            "source_dependencies": ["hl_builder_fills"],
            "permission_scope": "read_only",
            "status": "active",
            "schema_json": {"type": "object", "properties": {}},
        },
        {
            "tool_uri": "tic://source/hl/l4_micro",
            "tool_name": "hl_l4_micro",
            "kind": "source",
            "provider": "hl",
            "description": "Hyperliquid L4 microstructure source",
            "source_dependencies": ["hl_l4_micro"],
            "permission_scope": "read_only",
            "status": "active",
            "schema_json": {"type": "object", "properties": {}},
        },
        {
            "tool_uri": "tic://tool/admin/delete_positions@v1",
            "tool_name": "delete_positions",
            "kind": "admin",
            "provider": "internal",
            "description": "unsafe write",
            "permission_scope": "write",
            "status": "active",
        },
    ]
    monkeypatch.setattr(
        tool_atlas,
        "get_atlas_snapshot_for_cycle",
        lambda cycle_id: SimpleNamespace(rows=rows),
    )

    candidates = _expanded_tool_candidate_rows(
        seed=seed,
        cycle_id="cycle_atlas",
        explicit_candidates=seed.payload["tool_candidates"],
    )
    uris = [row["tool_uri"] for row in candidates]
    assert uris[0] == "tic://tool/builtin/query_events_recent@v1"
    assert "tic://tool/hydromancer/get_builder_fills@v1" in uris
    assert "tic://source/hl/l4_micro" in uris
    assert "tic://tool/admin/delete_positions@v1" not in uris
    source_row = next(row for row in candidates if row["tool_uri"] == "tic://source/hl/l4_micro")
    assert _infer_tool_args_for_candidate(source_row["tool_uri"], seed, source_row) == {"coins": ["HYPE"]}


def test_prompt_exposes_evidence_expanded_tool_subset():
    seed = SeedCell(
        seed_id="seed_prompt",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        theme="validator_unstake",
        payload={"tool_candidates": ["tic://tool/builtin/query_events_recent@v1"]},
    )
    user_prompt = _build_user_prompt(
        seed,
        tool_evidence=[
            {
                "uri": "tic://source/hl/l4_micro",
                "ok": True,
                "tool_call_log_id": "tc_source_l4",
                "summary": "HL L4 source receipt",
            }
        ],
    )
    assert "atlas_policy" in user_prompt
    assert "tic://tool/builtin/query_events_recent@v1" in user_prompt
    assert "tic://source/hl/l4_micro" in user_prompt


def test_scout_infers_source_and_web_args_for_full_data_estate():
    hype_seed = SeedCell(
        seed_id="seed_source_args",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        theme="validator_unstake",
    )
    assert _infer_tool_args_for_candidate(
        "tic://source/hl/l4_micro",
        hype_seed,
        {"tool_name": "l4_micro", "kind": "source"},
    ) == {"coins": ["HYPE"]}
    assert _infer_tool_args_for_candidate(
        "tic://source/asksurf/asksurf_news",
        hype_seed,
        {"tool_name": "asksurf_news", "kind": "source"},
    ) == {"tickers": ["HYPE"], "feed_limit": 25, "per_ticker_limit": 8}
    assert _infer_tool_args_for_candidate(
        "tic://source/polymarket/polymarket_search",
        hype_seed,
        {"tool_name": "polymarket_search", "kind": "source"},
    ) == {"query": "HYPE validator_unstake on_chain intraday", "max_markets": 10}

    nvda_seed = SeedCell(
        seed_id="seed_equity_source_args",
        entity="NVDA",
        horizon="1w",
        lens="catalyst",
        bias_mode="frontier",
        theme="ai_capex",
    )
    assert _infer_tool_args_for_candidate(
        "tic://source/misc/news_tickers",
        nvda_seed,
        {"tool_name": "news_tickers", "kind": "source"},
    ) == {"tickers": ["NVDA"], "lookback_hours": 72}
    assert _infer_tool_args_for_candidate(
        "tic://source/misc/sec_edgar_search",
        nvda_seed,
        {"tool_name": "sec_edgar_search", "kind": "source"},
    )["query"] == "NVDA ai_capex catalyst 1w"

    parallel_row = {
        "tool_uri": "tic://tool/parallel/parallel_search@v1",
        "tool_name": "parallel_search",
        "kind": "external",
        "provider": "parallel.ai",
        "schema_json": {
            "type": "object",
            "properties": {
                "objective": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["objective"],
        },
    }
    parallel_args = _infer_tool_args_for_candidate(
        parallel_row["tool_uri"],
        nvda_seed,
        parallel_row,
    )
    assert parallel_args is not None
    assert parallel_args["max_results"] == 10
    assert "NVDA ai_capex catalyst 1w" in parallel_args["objective"]

    assert _infer_tool_args_for_candidate(
        "tic://source/misc/astro_cycles",
        SeedCell(
            seed_id="seed_astro",
            entity="SPY",
            horizon="structural",
            lens="anomaly",
            bias_mode="frontier",
            theme="celestial_regime",
        ),
        {"tool_name": "astro_cycles", "kind": "source", "description": "sunspots lunar planetary cycles"},
    ) == {}


def test_scout_tool_requests_are_atlas_bounded():
    allowed = ["tic://source/hl/l4_micro", "tic://tool/builtin/web_search@v1"]
    requests = _normalize_tool_requests(
        [
            {
                "tool_uri": "tic://source/hl/l4_micro",
                "args": {"coins": ["HYPE"]},
                "why": "Need order-book evidence.",
                "expected_edge": "claim -> depth",
                "priority": "high",
            },
            {
                "tool_uri": "tic://tool/fake/not_real@v1",
                "tool_name": "mempool_actor_watch",
                "args": {"asset": "HYPE"},
                "why": "Need pending intent.",
            },
            {
                "tool_uri": "tic://tool/fake/not_real@v1",
                "tool_name": "mempool_actor_watch",
                "args": {"asset": "HYPE"},
                "why": "Need pending intent.",
            },
        ],
        allowed_tools=allowed,
    )
    assert len(requests) == 2
    assert requests[0]["tool_uri"] == "tic://source/hl/l4_micro"
    assert requests[0]["priority"] == "high"
    assert requests[1]["tool_uri"] == ""
    assert requests[1]["tool_name"] == "mempool_actor_watch"
    assert requests[1]["priority"] == "low"
    assert "low_ev_missing_expected_edge" in requests[1]["why"]


def test_adversarial_review_strips_failed_refs_and_calibrates_scores():
    info = InformationString(
        title="HYPE unstake route",
        thesis="HYPE reprices if unstake liquidity reaches sellable depth before informed wallets absorb it.",
        mechanism="Unstake supply needs route, liquidity, and actor-quality confirmation.",
        expected_outcome="Sellable depth expands before absorption appears in wallet flow.",
        time_horizon="intraday",
        kill_signal="No transfer, benign restake, or strong informed-wallet absorption by T+2h.",
        crowdedness=0.35,
        conviction=0.95,
        novelty_score=0.9,
        entities_chain=["HYPE", "aHYPE", "sellable liquidity"],
        depth_layers=[{"layer": 1, "claim": "unstake supply"}, {"layer": 2, "claim": "wallet absorption"}],
        evidence_refs=["tc_ok", "tc_bad"],
    )
    tool_evidence = [
        {
            "uri": "tic://source/hl/l4_micro",
            "ok": True,
            "tool_call_log_id": "tc_ok",
            "result": {"orderbook": {"coin": "HYPE"}},
        },
        {
            "uri": "tic://tool/builtin/web_search@v1",
            "ok": False,
            "tool_call_log_id": "tc_bad",
            "error": "timeout",
        },
    ]

    review = review_information_string(info, tool_evidence=tool_evidence)
    assert review.decision == "downgrade"
    assert "failed_call_as_evidence" in review.flags
    assert _successful_tool_call_ids(tool_evidence) == ["tc_ok"]

    reviewed = apply_adversarial_review(info, review)
    assert "tc_bad" not in reviewed.evidence_refs
    assert reviewed.conviction < 0.95
    assert "adversarial_reviewed" in reviewed.quality_flags


def test_non_queued_synthesis_promotions_do_not_spend_verifier_budget():
    synthesis = InformationSynthesis(
        synthesis_id="isyn_weak",
        cycle_id="cycle_weak",
        summary="Single string only.",
        promoted_hypotheses=[
            {
                "hypothesis": "HYPE single scout idea needs another independent string.",
                "entity": "HYPE",
                "horizon": "intraday",
                "lens": "on_chain",
                "confidence": 0.4,
                "source_string_ids": ["istr_single"],
                "status": "needs_cross_string_support",
            }
        ],
    )

    assert promoted_scouts_from_synthesis(synthesis, existing_scouts=[], max_items=3) == []


def test_alpha_geometry_scores_trade_scream_and_routes_next_step(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    families = ["hydromancer", "market_microstructure", "our_hl_node"]
    for i, family in enumerate(families):
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_geo",
            scout_id=f"scout_geo_{i}",
            seed_id=f"seed_geo_{i}",
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"HYPE route {family}",
                    thesis="HYPE reprices if unstake liquidity reaches sellable depth before informed wallets absorb it.",
                    mechanism="Route quality plus actor quality determines whether supply becomes market pressure.",
                    expected_outcome="Depth and wallet absorption diverge in the next intraday window.",
                    kill_signal="No transfer, benign restake, or strong informed-wallet absorption.",
                    conviction=0.82,
                    novelty_score=0.78,
                    crowdedness=0.25,
                    entities_chain=["HYPE", "aHYPE", "sellable liquidity"],
                    depth_layers=[{"layer": 1, "claim": "unstake"}, {"layer": 2, "claim": "route quality"}],
                    evidence_refs=[f"tic://source/{family}/sample"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            coverage_cell_key="HYPE|intraday|on_chain|frontier|validator_unstake",
            source_tool_call_ids=[f"tc_geo_{i}"],
        )

    snapshot = compute_alpha_geometry(cycle_id="cycle_geo", conn=store.conn)
    assert snapshot.cells
    top = snapshot.cells[0]
    assert top.route_directive == "verify_now"
    assert top.metrics["source_independence"] > 0.9
    assert top.metrics["verifier_readiness"] > 0.7
    assert top.metrics["trade_scream_score"] > 0.6
    assert "frontier_trade_candidate" in top.quality_flags
    directives = alpha_geometry_seed_directives(snapshot)
    assert directives[0]["route_directive"] == "verify_now"
    loaded = load_alpha_geometry(cycle_id="cycle_geo", conn=store.conn)
    assert loaded[0]["coordinates"]["x_source_independence"] > 0.9


def test_alpha_geometry_action_plan_turns_shape_into_cortex_agenda(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    families = ["hydromancer", "market_microstructure", "our_hl_node"]
    for i, family in enumerate(families):
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_geo_actions",
            scout_id=f"scout_geo_actions_{i}",
            seed_id=f"seed_geo_actions_{i}",
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"HYPE action plan {family}",
                    thesis="HYPE route intelligence should go to verification when the field is source-diverse and supported.",
                    mechanism="The geometry combines source independence, support mass, and verifier readiness into route action.",
                    expected_outcome="The cortex sees a verify-now agenda from the map shape.",
                    kill_signal="The action planner ignores a verifier-ready geometry cell.",
                    conviction=0.86,
                    novelty_score=0.78,
                    crowdedness=0.20,
                    entities_chain=["HYPE", "alpha geometry", family],
                    depth_layers=[{"layer": 1, "claim": "shape"}, {"layer": 2, "claim": "action"}],
                    evidence_refs=[f"tic://source/{family}/shape_action"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            coverage_cell_key="HYPE|intraday|on_chain|frontier|validator_unstake",
            source_tool_call_ids=[f"tc_geo_actions_{i}_{family}"],
        )
    compute_alpha_geometry(cycle_id="cycle_geo_actions", conn=store.conn)

    plan = plan_alpha_geometry_actions(cycle_id="cycle_geo_actions", conn=store.conn)

    assert plan["schema_version"] == "alpha_geometry_action_plan_v1"
    assert plan["global_shape"]["route_directive_counts"]["verify_now"] >= 1
    assert plan["actions"][0]["action"] == "send_to_verifier_council"
    assert plan["actions"][0]["owner"] == "verifier"
    assert plan["actions"][0]["route_task_id"].startswith("shape_task_")
    assert "claim -> verifier_decision" in plan["actions"][0]["missing_edges"]
    assert plan["routing_queue"]
    assert plan["routing_queue"][0]["route_task_id"] == plan["actions"][0]["route_task_id"]
    assert plan["routing_queue"][0]["must_call_first"] == "tic://tool/talis_native/plan_alpha_geometry_actions@v1"
    assert plan["routing_queue"][0]["map_update_rule"]
    assert plan["cortex_next_step"]["status"] == "ready"
    assert plan["cortex_next_step"]["primary_action"] == "send_to_verifier_council"
    assert plan["cortex_next_step"]["route_task_id"] == plan["actions"][0]["route_task_id"]
    assert plan["cortex_next_step"]["seed_template"]["must_call_first"] == (
        "tic://tool/talis_native/plan_alpha_geometry_actions@v1"
    )
    assert plan["cortex_toolkit"][0]["tool_uri"] == "tic://tool/talis_native/plan_alpha_geometry_actions@v1"
    assert plan["tool_requests"]


def test_alpha_geometry_action_plan_is_callable_through_tool_atlas(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    for i, family in enumerate(["hydromancer", "market_microstructure", "our_hl_node"]):
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_geo_tool",
            scout_id=f"scout_geo_tool_{i}",
            seed_id=f"seed_geo_tool_{i}",
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"HYPE tool-plan {family}",
                    thesis="The cortex should be able to call a tool that reads the geometry field and returns next actions.",
                    mechanism="A callable shape planner lets agents inspect route directives through the harness.",
                    expected_outcome="Tool dispatch returns a verifier-ready action plan with audit log.",
                    kill_signal="The planner is absent from the atlas or cannot dispatch.",
                    conviction=0.86,
                    novelty_score=0.78,
                    crowdedness=0.20,
                    entities_chain=["HYPE", "geometry tool", family],
                    depth_layers=[{"layer": 1, "claim": "geometry"}, {"layer": 2, "claim": "tool call"}],
                    evidence_refs=[f"tic://source/{family}/shape_tool"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            coverage_cell_key="HYPE|intraday|on_chain|frontier|validator_unstake",
            source_tool_call_ids=[f"tc_geo_tool_{i}_{family}"],
        )
    compute_alpha_geometry(cycle_id="cycle_geo_tool", conn=store.conn)

    atlas = regenerate_tool_atlas()
    uris = {row["tool_uri"] for row in atlas.rows}
    assert "tic://tool/talis_native/plan_alpha_geometry_actions@v1" in uris

    result = dispatch_uri(
        "tic://tool/talis_native/plan_alpha_geometry_actions@v1",
        {"cycle_id": "cycle_geo_tool", "limit": 16},
        AgentContext(cycle_id="cycle_geo_tool", specialist_id="geometry_cortex_test"),
    )

    assert result.ok, result.error
    assert result.result["schema_version"] == "alpha_geometry_action_plan_v1"
    assert result.result["actions"][0]["action"] == "send_to_verifier_council"
    assert result.result["cortex_next_step"]["primary_owner"] == "verifier"
    row = store.conn.execute(
        "SELECT tool_uri, error FROM tool_call_log WHERE id = ?",
        (result.tool_call_log_id,),
    ).fetchone()
    assert row["tool_uri"] == "tic://tool/talis_native/plan_alpha_geometry_actions@v1"
    assert row["error"] is None


def test_run_swarm_manifest_exports_evolution_control_payload(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    for i, family in enumerate(["hydromancer", "market_microstructure", "our_hl_node"]):
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_swarm_control",
            scout_id=f"scout_swarm_control_{i}",
            seed_id=f"seed_swarm_control_{i}",
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"HYPE swarm control {family}",
                    thesis="The normal swarm manifest should expose the same geometry and evolution control plane as the smoke viewer.",
                    mechanism="Production operators need the action plan, cortex review, and lineage frontier after a run.",
                    expected_outcome="A cycle manifest contains auditable next-route and evolution-control state.",
                    kill_signal="The control payload is only present in smoke artifacts.",
                    conviction=0.86,
                    novelty_score=0.78,
                    crowdedness=0.20,
                    entities_chain=["HYPE", "swarm manifest", family],
                    depth_layers=[{"layer": 1, "claim": "geometry"}, {"layer": 2, "claim": "manifest"}],
                    evidence_refs=[f"tic://source/{family}/swarm_control"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            coverage_cell_key="HYPE|intraday|on_chain|frontier|validator_unstake",
            source_tool_call_ids=[f"tc_swarm_control_{i}_{family}"],
        )
    compute_alpha_geometry(cycle_id="cycle_swarm_control", conn=store.conn)
    step = run_market_evolve_step(cycle_id="cycle_swarm_control", conn=store.conn)

    import run_swarm

    payload = run_swarm._build_evolution_control_payload(
        cycle_id="cycle_swarm_control",
        active_program=step.programs[0],
        market_evolve_step=step,
        conn=store.conn,
    )

    assert payload["schema_version"] == "swarm_evolution_control_v1"
    assert payload["source"] == "swarm_manifest"
    assert payload["alpha_geometry_action_plan"]["schema_version"] == "alpha_geometry_action_plan_v1"
    assert payload["alpha_geometry_action_plan"]["cortex_next_step"]["status"] == "ready"
    assert payload["geometry_cortex_review"]["schema_version"] == "alpha_geometry_cortex_review_v1"
    assert payload["market_evolve_lineage"]["schema_version"] == "market_evolve_lineage_v1"
    assert payload["proof"]["action_plan_ready"] is True
    assert payload["proof"]["cortex_review_ready"] is True
    assert payload["proof"]["lineage_frontier_count"] >= 1

    dispatch = post_evolution_control_work_orders(
        cycle_id="cycle_swarm_control",
        evolution_control=payload,
        conn=store.conn,
        source="test_run_swarm_manifest",
    )
    regenerate_tool_atlas()
    worker = run_swarm._run_cortex_task_worker_feedback(
        cycle_id="cycle_swarm_control",
        active_program=step.programs[0],
        conn=store.conn,
    )
    payload["task_dispatch"] = dispatch
    payload["task_execution"] = worker["task_execution"]
    payload["task_feedback"] = worker["task_feedback"]

    assert payload["task_dispatch"]["task_count"] >= 1
    assert payload["task_execution"]["completed_count"] >= 1
    assert payload["task_feedback"]["schema_version"] == "cortex_task_feedback_v1"
    assert payload["task_feedback"]["proof"]["evaluator_saw_worker_tasks"] is True
    assert payload["task_feedback"]["proof"]["worker_completion_is_rewarded"] is True
    assert payload["task_feedback"]["metrics"]["cortex_task_completion_rate"] > 0.0


def test_monitor_research_evolution_exposes_control_plane_from_live_data(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    for i, family in enumerate(["hydromancer", "our_hl_node", "market_microstructure"]):
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_monitor_control",
            scout_id=f"scout_monitor_control_{i}",
            seed_id=f"seed_monitor_control_{i}",
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"HYPE monitor control {family}",
                    thesis="The monitor should show how the shape routes the next research move.",
                    mechanism="The same persisted strings support geometry, cortex review, and MarketEvolve lineage.",
                    expected_outcome="The research evolution API exposes a swarm_evolution_control_v1 payload.",
                    kill_signal="The monitor only shows flat evaluator rows without shape supervision.",
                    conviction=0.84,
                    novelty_score=0.76,
                    crowdedness=0.22,
                    entities_chain=["HYPE", "monitor", family],
                    depth_layers=[{"layer": 1, "claim": "shape"}, {"layer": 2, "claim": "cortex"}],
                    evidence_refs=[f"tic://source/{family}/monitor_control"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            coverage_cell_key="HYPE|intraday|on_chain|frontier|validator_unstake",
            source_tool_call_ids=[f"tc_monitor_control_{i}_{family}"],
        )
    compute_alpha_geometry(cycle_id="cycle_monitor_control", conn=store.conn)
    run_market_evolve_step(cycle_id="cycle_monitor_control", conn=store.conn)

    from talis_desk.monitor.server import _build_research_evolution_payload

    payload = _build_research_evolution_payload(
        store.conn,
        manifest={},
        cycle_id="cycle_monitor_control",
        limit=12,
    )

    control = payload["evolution_control"]
    assert control["schema_version"] == "swarm_evolution_control_v1"
    assert control["source"] == "monitor_live_rebuild"
    assert control["alpha_geometry_action_plan"]["schema_version"] == "alpha_geometry_action_plan_v1"
    assert control["geometry_cortex_review"]["schema_version"] == "alpha_geometry_cortex_review_v1"
    assert control["market_evolve_lineage"]["schema_version"] == "market_evolve_lineage_v1"
    assert payload["summary"]["evolution_control_source"] == "monitor_live_rebuild"
    assert payload["summary"]["lineage_frontier_count"] >= 1


def test_monitor_research_evolution_reuses_manifest_control_payload(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")

    from talis_desk.monitor.server import _build_research_evolution_payload

    payload = _build_research_evolution_payload(
        store.conn,
        manifest={
            "cycle_id": "cycle_manifest_control",
            "evolution_control": {
                "schema_version": "swarm_evolution_control_v1",
                "cycle_id": "cycle_manifest_control",
                "proof": {
                    "shape_can_direct_next": True,
                    "diagnostic_codes": ["manifest_control_seen"],
                    "lineage_frontier_count": 2,
                    "mutation_kind_hint": "tighten_shape_route_contract",
                },
            },
        },
        cycle_id="cycle_manifest_control",
        limit=12,
    )

    assert payload["evolution_control"]["source"] == "manifest"
    assert payload["summary"]["shape_can_direct_next"] is True
    assert payload["summary"]["cortex_diagnostic_codes"] == ["manifest_control_seen"]
    assert payload["summary"]["cortex_mutation_hint"] == "tighten_shape_route_contract"
    assert payload["summary"]["lineage_frontier_count"] == 2


def test_evolution_control_posts_dispatchable_shape_tasks_idempotently(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    for i, family in enumerate(["hydromancer", "our_hl_node", "market_microstructure"]):
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_control_dispatch",
            scout_id=f"scout_control_dispatch_{i}",
            seed_id=f"seed_control_dispatch_{i}",
            entity="HYPE",
            theme="node_intelligence",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"Dispatchable shape task {family}",
                    thesis="The cortex-selected shape task should become schedulable worker work.",
                    mechanism="A route directive with missing edges carries enough owner, tool, and success-gate data to post a task contract.",
                    expected_outcome="task_contracts contains idempotent alpha-geometry route work with allowed tools.",
                    kill_signal="The shape work only exists in an unclaimed JSON artifact.",
                    conviction=0.88,
                    novelty_score=0.80,
                    crowdedness=0.18,
                    entities_chain=["HYPE", "alpha geometry", family],
                    depth_layers=[{"layer": 1, "claim": "route"}, {"layer": 2, "claim": "dispatch"}],
                    evidence_refs=[f"tic://source/{family}/dispatch"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            coverage_cell_key="HYPE|intraday|on_chain|frontier|node_intelligence",
            source_tool_call_ids=[f"tc_control_dispatch_{i}_{family}"],
        )
    compute_alpha_geometry(cycle_id="cycle_control_dispatch", conn=store.conn)
    step = run_market_evolve_step(cycle_id="cycle_control_dispatch", conn=store.conn)

    from talis_desk.information_map import build_evolution_control_payload

    control = build_evolution_control_payload(
        cycle_id="cycle_control_dispatch",
        active_program=step.programs[0],
        market_evolve_step=step,
        conn=store.conn,
        source="test_control_dispatch",
    )
    first = post_evolution_control_work_orders(
        cycle_id="cycle_control_dispatch",
        evolution_control=control,
        conn=store.conn,
        source="test_control_dispatch",
    )
    second = post_evolution_control_work_orders(
        cycle_id="cycle_control_dispatch",
        evolution_control=control,
        conn=store.conn,
        source="test_control_dispatch",
    )

    assert first["schema_version"] == "evolution_control_task_dispatch_v1"
    assert first["posted_count"] >= 1
    assert second["posted_count"] == 0
    assert second["existing_count"] == first["posted_count"]
    rows = store.conn.execute(
        """
        SELECT topic, title, allowed_tools_json, evidence_requirements_json,
               promotion_criteria_json, kill_criteria_json, payload
        FROM task_contracts
        WHERE cycle_id = ?
        ORDER BY priority DESC
        """,
        ("cycle_control_dispatch",),
    ).fetchall()
    assert len(rows) == first["task_count"]
    payload = json.loads(rows[0]["payload"])
    assert payload["dedupe_key"]
    assert payload["route_task_id"]
    assert rows[0]["topic"] in {"alpha_geometry.route", "market_evolve.frontier"}
    assert "tic://tool/talis_native/plan_alpha_geometry_actions@v1" in json.loads(rows[0]["allowed_tools_json"])
    assert json.loads(rows[0]["promotion_criteria_json"])["success_gate"]
    assert json.loads(rows[0]["kill_criteria_json"])["stop_condition"]
    assert store.conn.execute(
        "SELECT COUNT(*) FROM blackboard_events WHERE cycle_id = ? AND event_type = 'task.posted'",
        ("cycle_control_dispatch",),
    ).fetchone()[0] == first["posted_count"]


def test_evolution_control_posts_market_evolve_experiment_arm_tasks(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    step = run_market_evolve_step(cycle_id="cycle_cortex_ab_plan", conn=store.conn)
    assert step.experiment_plans
    experiment = step.experiment_plans[0]

    control = {
        "schema_version": "swarm_evolution_control_v1",
        "cycle_id": "cycle_cortex_ab_dispatch",
        "alpha_geometry_action_plan": {
            "routing_queue": [
                {
                    "route_task_id": "shape_task_cortex_policy_ab",
                    "owner": "seed_router",
                    "action": "replicate_with_independent_scouts",
                    "route_directive": "widen_scouts",
                    "priority_score": 0.91,
                    "cell_key": "HYPE|intraday|on_chain|frontier|node_intelligence",
                    "entity": "HYPE",
                    "horizon": "intraday",
                    "lens": "on_chain",
                    "theme": "node_intelligence",
                    "must_call_first": "tic://tool/talis_native/plan_alpha_geometry_actions@v1",
                    "tool_sequence": ["tic://tool/builtin/query_events_recent@v1"],
                    "missing_edges": ["thin_cell -> independent_replication"],
                    "success_gate": "candidate and control both observe the same shape edge",
                }
            ],
        },
        "geometry_cortex_review": {"cortex_work_orders": []},
    }
    dispatch = post_evolution_control_work_orders(
        cycle_id="cycle_cortex_ab_dispatch",
        evolution_control=control,
        conn=store.conn,
        source="test_cortex_ab_dispatch",
        max_experiment_pairs=1,
        limit=6,
    )

    assert dispatch["posted_count"] == 3
    rows = store.conn.execute(
        """
        SELECT input_schema_json, payload
        FROM task_contracts
        WHERE cycle_id = ?
        ORDER BY posted_at ASC
        """,
        ("cycle_cortex_ab_dispatch",),
    ).fetchall()
    payloads = [json.loads(row["payload"]) for row in rows]
    arm_payloads = [
        payload for payload in payloads
        if payload.get("market_evolve_experiment_id") == experiment["id"]
    ]
    assert {payload["market_evolve_experiment_arm"] for payload in arm_payloads} == {"control", "candidate"}
    assert {
        payload["market_evolve_program_id"]
        for payload in arm_payloads
    } == {experiment["parent_program_id"], experiment["candidate_program_id"]}
    assert all(isinstance(payload.get("market_evolve_cortex_policy"), dict) for payload in arm_payloads)
    assert all(payload.get("base_route_task_id") == "shape_task_cortex_policy_ab" for payload in arm_payloads)

    schemas = [json.loads(row["input_schema_json"]) for row in rows]
    arm_schemas = [
        schema for schema in schemas
        if schema.get("market_evolve_experiment_id") == experiment["id"]
    ]
    assert {schema["market_evolve_experiment_arm"] for schema in arm_schemas} == {"control", "candidate"}

    again = post_evolution_control_work_orders(
        cycle_id="cycle_cortex_ab_dispatch",
        evolution_control=control,
        conn=store.conn,
        source="test_cortex_ab_dispatch",
        max_experiment_pairs=1,
        limit=6,
    )
    assert again["posted_count"] == 0
    assert again["existing_count"] == dispatch["posted_count"]


def test_geometry_cortex_reviews_shape_and_proposes_policy_pressure(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    for i, family in enumerate(["hydromancer", "market_microstructure", "our_hl_node"]):
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_geo_cortex",
            scout_id=f"scout_geo_cortex_{i}",
            seed_id=f"seed_geo_cortex_{i}",
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"HYPE cortex review {family}",
                    thesis="The cortex should inspect not just a route, but whether geometry pressure itself is healthy.",
                    mechanism="A shape health review can convert fragile verifier leakage into a testable policy patch.",
                    expected_outcome="The geometry cortex proposes source repair pressure before more verifier spend.",
                    kill_signal="The cortex can see a bad shape metric but produces no auditable patch.",
                    conviction=0.86,
                    novelty_score=0.78,
                    crowdedness=0.20,
                    entities_chain=["HYPE", "geometry cortex", family],
                    depth_layers=[{"layer": 1, "claim": "shape"}, {"layer": 2, "claim": "policy"}],
                    evidence_refs=[f"tic://source/{family}/shape_cortex"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            coverage_cell_key="HYPE|intraday|on_chain|frontier|validator_unstake",
            source_tool_call_ids=[f"tc_geo_cortex_{i}_{family}"],
        )
    compute_alpha_geometry(cycle_id="cycle_geo_cortex", conn=store.conn)
    action_plan = plan_alpha_geometry_actions(cycle_id="cycle_geo_cortex", conn=store.conn)

    review = build_alpha_geometry_cortex_review(
        cycle_id="cycle_geo_cortex",
        conn=store.conn,
        action_plan=action_plan,
        metrics={
            "geometry_cell_count": 3.0,
            "geometry_route_action_rate": 0.40,
            "geometry_route_entropy": 0.60,
            "fragile_verify_rate": 0.42,
            "high_signal_observe_rate": 0.0,
            "route_contract_success_rate": 1.0,
            "route_contract_eval_count": 3.0,
            "avg_fragility": 0.58,
            "avg_source_independence": 0.30,
        },
    )

    assert review["schema_version"] == "alpha_geometry_cortex_review_v1"
    assert review["shape_can_direct_next"] is True
    assert any(d["code"] == "fragile_verify_leak" for d in review["diagnostics"])
    patch = review["proposed_geometry_policy"]["policy_patch"]
    assert patch["geometry_weights"]["fragility_penalty"] >= 0.34
    assert patch["routing_thresholds"]["verify_allow_fragility_max"] <= 0.35
    assert review["cortex_work_orders"][0]["route_task_id"] == action_plan["routing_queue"][0]["route_task_id"]
    assert any(order["owner"] == "market_evolve" for order in review["cortex_work_orders"])
    assert review["proposed_geometry_policy"]["mutation_kind_hint"] == "retune_geometry_repair_before_verify"
    assert review["llm_cortex"]["enabled"] is False


def test_geometry_cortex_review_is_callable_through_tool_atlas(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    for i, family in enumerate(["hydromancer", "market_microstructure", "our_hl_node"]):
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_geo_cortex_tool",
            scout_id=f"scout_geo_cortex_tool_{i}",
            seed_id=f"seed_geo_cortex_tool_{i}",
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"HYPE cortex tool {family}",
                    thesis="A native cortex review tool lets agents inspect the map shape and its policy frontier.",
                    mechanism="The harness exposes geometry health, work orders, and evolution frontier in one callable packet.",
                    expected_outcome="Tool dispatch returns a cortex review and records the call.",
                    kill_signal="The review tool is absent from the atlas or cannot dispatch.",
                    conviction=0.86,
                    novelty_score=0.78,
                    crowdedness=0.20,
                    entities_chain=["HYPE", "geometry cortex tool", family],
                    depth_layers=[{"layer": 1, "claim": "tool"}, {"layer": 2, "claim": "review"}],
                    evidence_refs=[f"tic://source/{family}/shape_cortex_tool"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            coverage_cell_key="HYPE|intraday|on_chain|frontier|validator_unstake",
            source_tool_call_ids=[f"tc_geo_cortex_tool_{i}_{family}"],
        )
    compute_alpha_geometry(cycle_id="cycle_geo_cortex_tool", conn=store.conn)

    atlas = regenerate_tool_atlas()
    uris = {row["tool_uri"] for row in atlas.rows}
    assert "tic://tool/talis_native/review_alpha_geometry_cortex@v1" in uris

    result = dispatch_uri(
        "tic://tool/talis_native/review_alpha_geometry_cortex@v1",
        {"cycle_id": "cycle_geo_cortex_tool", "limit": 16, "use_llm": False},
        AgentContext(cycle_id="cycle_geo_cortex_tool", specialist_id="geometry_cortex_review_test"),
    )

    assert result.ok, result.error
    assert result.result["schema_version"] == "alpha_geometry_cortex_review_v1"
    assert result.result["shape_health"]["routing_queue_length"] >= 1
    assert result.result["cortex_work_orders"]
    assert "context_packet" in result.result
    row = store.conn.execute(
        "SELECT tool_uri, error FROM tool_call_log WHERE id = ?",
        (result.tool_call_log_id,),
    ).fetchone()
    assert row["tool_uri"] == "tic://tool/talis_native/review_alpha_geometry_cortex@v1"
    assert row["error"] is None


def test_shape_guided_seeds_receive_shape_reader_tool(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    regenerate_tool_atlas()

    seed = SeedCell(
        seed_id="seed_shape_reader",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        theme="alpha_geometry_verify_now",
        payload={
            "source": "alpha_geometry_route",
            "alpha_geometry_route_directive": "verify_now",
        },
    )

    tools = narrow_tools_for_seed(seed, k=8)

    assert tools[0] == "tic://tool/talis_native/plan_alpha_geometry_actions@v1"


def test_shape_reader_tool_args_point_at_source_geometry_cycle():
    seed = SeedCell(
        seed_id="seed_shape_reader_args",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        theme="alpha_geometry_verify_now",
        payload={
            "source": "alpha_geometry_route",
            "alpha_geometry_source_cycle_id": "cycle_shape_source",
            "alpha_geometry_route_directive": "verify_now",
            "shape_reader_limit": 17,
        },
    )

    args = _infer_tool_args("tic://tool/talis_native/plan_alpha_geometry_actions@v1", seed)

    assert args == {"cycle_id": "cycle_shape_source", "limit": 17}


def test_shape_routed_seed_prompt_exposes_cortex_contract():
    seed = SeedCell(
        seed_id="seed_shape_prompt",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        theme="node_intelligence",
        payload={
            "source": "alpha_geometry_route",
            "alpha_geometry_source_cycle_id": "cycle_prior",
            "alpha_geometry_cell_key": "HYPE|intraday|on_chain|frontier|node_intelligence",
            "alpha_geometry_route_directive": "widen_scouts",
            "alpha_geometry_action": "replicate_with_independent_scouts",
            "alpha_geometry_action_owner": "seed_router",
            "alpha_geometry_action_reason": "The cell is novel but thin.",
            "alpha_geometry_missing_edges": [
                "frontier_cell -> next_receptive_field",
                "thin_cell -> independent_replication",
            ],
            "alpha_geometry_success_gate": "second scout confirms, contradicts, or kills the string",
            "alpha_geometry_suggested_next_tools": [
                "tic://tool/builtin/query_events_recent@v1",
                "tic://tool/builtin/query_timeseries@v1",
            ],
            "tool_candidates": [
                "tic://tool/talis_native/plan_alpha_geometry_actions@v1",
                "tic://tool/builtin/query_events_recent@v1",
            ],
        },
    )

    prompt = _build_user_prompt(seed, tool_evidence=[])

    assert "alpha_geometry_route_contract:" in prompt
    assert "shape_route_rules:" in prompt
    assert "replicate_with_independent_scouts" in prompt
    assert "seed_router" in prompt
    assert "frontier_cell -> next_receptive_field" in prompt
    assert "second scout confirms, contradicts, or kills the string" in prompt
    assert "plan_alpha_geometry_actions@v1" in prompt
    assert "suggested_tools must copy from it exactly" in prompt


def test_route_contract_alignment_scores_edge_and_gate():
    seed = SeedCell(
        seed_id="seed_route_alignment",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        theme="node_intelligence",
        payload={
            "source": "alpha_geometry_route",
            "alpha_geometry_route_directive": "widen_scouts",
            "alpha_geometry_action": "replicate_with_independent_scouts",
            "alpha_geometry_missing_edges": ["thin_cell -> independent_replication"],
            "alpha_geometry_success_gate": "second scout confirms, contradicts, or kills the string",
        },
    )
    info = InformationString(
        title="Independent replication moved the thin cell",
        thesis="A second scout confirms the HYPE route claim and pins independent replication to the thin cell.",
        mechanism="The new source independently checks whether the node route is real rather than repeating the original path.",
        expected_outcome="The scout either confirms, contradicts, or kills the route string before verifier spend.",
        kill_signal="Independent replication cannot reproduce the route signal.",
        conviction=0.82,
        novelty_score=0.74,
        crowdedness=0.30,
        entities_chain=["HYPE", "thin cell", "independent replication"],
        depth_layers=[
            {"layer": 1, "claim": "thin cell gets an independent replication edge"},
            {"layer": 2, "claim": "second scout confirms or kills the original string"},
        ],
    )

    alignment = _evaluate_route_contract_alignment(
        seed=seed,
        parsed={
            "hypothesis": "HYPE route quality improves if the independent replication edge confirms.",
            "rationale_brief": "The missing edge is now directly tested.",
        },
        information_strings=[info],
        tool_requests=[],
    )

    assert alignment.passed
    assert alignment.score >= 0.60
    assert alignment.addressed_edges == ["thin_cell -> independent_replication"]
    assert "route_contract_all_edges_addressed" in alignment.flags
    assert "route_contract_success_gate_addressed" in alignment.flags


def test_route_contract_alignment_fails_vague_output():
    seed = SeedCell(
        seed_id="seed_route_alignment_fail",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        theme="node_intelligence",
        payload={
            "source": "alpha_geometry_route",
            "alpha_geometry_route_directive": "widen_scouts",
            "alpha_geometry_action": "replicate_with_independent_scouts",
            "alpha_geometry_missing_edges": ["thin_cell -> independent_replication"],
            "alpha_geometry_success_gate": "second scout confirms, contradicts, or kills the string",
        },
    )
    vague = InformationString(
        title="HYPE looks interesting",
        thesis="HYPE may move if market attention increases.",
        mechanism="Market participants may watch headlines.",
        expected_outcome="Price may react.",
        kill_signal="No reaction.",
        conviction=0.55,
        novelty_score=0.30,
        crowdedness=0.70,
        entities_chain=["HYPE"],
        depth_layers=[{"layer": 1, "claim": "generic market attention"}],
    )

    alignment = _evaluate_route_contract_alignment(
        seed=seed,
        parsed={"hypothesis": "HYPE might move.", "rationale_brief": "Generic attention."},
        information_strings=[vague],
        tool_requests=[],
    )

    assert not alignment.passed
    assert alignment.missed_edges == ["thin_cell -> independent_replication"]
    assert "route_contract_no_edge_moved" in alignment.flags
    assert "route_contract_success_gate_missing" in alignment.flags


def test_alpha_geometry_uses_evolved_geometry_weights(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    for i, family in enumerate(["hydromancer", "market_microstructure", "our_hl_node"]):
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_geo_weights",
            scout_id=f"scout_geo_weights_{i}",
            seed_id=f"seed_geo_weights_{i}",
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"Geometry policy {family}",
                    thesis="HYPE route intelligence is most useful when geometry can learn which dimensions matter.",
                    mechanism="Changing source, frontier, support, and fragility weights should change the map score.",
                    expected_outcome="A source-heavy geometry policy produces a different trade-scream score.",
                    kill_signal="Weights do not alter the persisted geometry score.",
                    conviction=0.76,
                    novelty_score=0.70,
                    crowdedness=0.28,
                    entities_chain=["HYPE", "geometry policy", family],
                    depth_layers=[{"layer": 1, "claim": "source"}, {"layer": 2, "claim": "weight"}],
                    evidence_refs=[f"tic://source/{family}/geometry_policy"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            coverage_cell_key="HYPE|intraday|on_chain|frontier|validator_unstake",
            source_tool_call_ids=[f"tc_geo_weights_{i}_{family}"],
        )

    default_snapshot = compute_alpha_geometry(cycle_id="cycle_geo_weights", conn=store.conn, persist=False)
    source_heavy = compute_alpha_geometry(
        cycle_id="cycle_geo_weights",
        conn=store.conn,
        persist=False,
        geometry_weights={
            "frontier_pressure": 0.0,
            "tension": 0.0,
            "verifier_readiness": 0.0,
            "support_mass": 0.0,
            "source_independence": 1.0,
            "fragility_penalty": 0.0,
        },
    )

    assert "policy_weighted_geometry" in source_heavy.quality_flags
    assert source_heavy.cells[0].trade_scream_score != default_snapshot.cells[0].trade_scream_score
    assert source_heavy.cells[0].metrics["geometry_weight_source_independence"] == 1.0
    assert source_heavy.cells[0].metrics["geometry_weight_frontier_pressure"] == 0.0
    scoped_metrics = collect_market_evolve_metrics(
        cycle_id="cycle_geo_weights",
        seed_ids=[f"seed_geo_weights_{i}" for i in range(3)],
        geometry_weights={
            "frontier_pressure": 0.0,
            "tension": 0.0,
            "verifier_readiness": 0.0,
            "support_mass": 0.0,
            "source_independence": 1.0,
            "fragility_penalty": 0.0,
        },
        conn=store.conn,
    )
    assert scoped_metrics["avg_trade_scream_score"] == source_heavy.cells[0].trade_scream_score


def test_alpha_geometry_uses_evolved_routing_thresholds(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    persist_information_strings(
        conn=store.conn,
        cycle_id="cycle_geo_routes",
        scout_id="scout_geo_routes",
        seed_id="seed_geo_routes",
        entity="HYPE",
        theme="validator_unstake",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        strings=[
            InformationString(
                title="Route policy thin-cell scout widening",
                thesis="A thin but source-diverse cell should stay observable by default and widen scouts under exploratory policy.",
                mechanism="The evidence is real and source-diverse, but a single scout makes it an exploration-routing problem.",
                expected_outcome="Lowering the evolved novelty threshold routes the cell to widen_scouts.",
                kill_signal="The route does not change when the routing threshold changes.",
                conviction=0.25,
                novelty_score=0.55,
                crowdedness=0.78,
                entities_chain=["HYPE", "routing policy", "coverage geometry"],
                depth_layers=[{"layer": 1, "claim": "single observation"}, {"layer": 2, "claim": "explore"}],
                evidence_refs=[
                    "tic://source/hydromancer/route_policy",
                    "tic://source/orderbook/route_policy",
                    "tic://source/news/route_policy",
                ],
                quality_flags=[
                    "source_family:hydromancer",
                    "source_family:market_microstructure",
                    "source_family:web_attention",
                    "adversarial_decision:allow",
                ],
            )
        ],
        coverage_cell_key="HYPE|intraday|on_chain|frontier|validator_unstake",
        source_tool_call_ids=["tc_geo_route_hydromancer", "tc_geo_route_orderbook", "tc_geo_route_news"],
    )

    default_snapshot = compute_alpha_geometry(cycle_id="cycle_geo_routes", conn=store.conn, persist=False)
    routed_snapshot = compute_alpha_geometry(
        cycle_id="cycle_geo_routes",
        conn=store.conn,
        persist=False,
        routing_thresholds={"widen_scouts_novelty_min": 0.10},
    )

    assert default_snapshot.cells[0].route_directive == "observe"
    assert routed_snapshot.cells[0].route_directive == "widen_scouts"
    assert "policy_routed_geometry" in routed_snapshot.quality_flags
    assert routed_snapshot.cells[0].metrics["routing_threshold_widen_scouts_novelty_min"] == 0.10
    default_metrics = collect_market_evolve_metrics(
        cycle_id="cycle_geo_routes",
        seed_ids=["seed_geo_routes"],
        conn=store.conn,
    )
    policy_metrics = collect_market_evolve_metrics(
        cycle_id="cycle_geo_routes",
        seed_ids=["seed_geo_routes"],
        routing_thresholds={"verify_trade_scream_min": 0.40, "verify_readiness_min": 0.50},
        conn=store.conn,
    )
    assert default_metrics["frontier_candidate_rate"] == 0.0
    assert policy_metrics["frontier_candidate_rate"] == 1.0
    assert policy_metrics["routing_threshold_verify_trade_scream_min"] == 0.4


def test_alpha_geometry_routes_multi_string_single_scout_cells_to_replication(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    persist_information_strings(
        conn=store.conn,
        cycle_id="cycle_geo_single_scout",
        scout_id="scout_one_voice",
        seed_id="seed_one_voice",
        entity="HYPE",
        theme="validator_unstake",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        strings=[
            InformationString(
                title="Single scout first string",
                thesis="HYPE route quality is interesting but only one scout has observed it.",
                mechanism="One scout emitted a plausible source-diverse route, but independence requires a second scout.",
                expected_outcome="The map should replicate this cell before verifier spend.",
                kill_signal="Independent scout fails to confirm route quality.",
                conviction=0.78,
                novelty_score=0.82,
                crowdedness=0.20,
                entities_chain=["HYPE", "route quality"],
                depth_layers=[
                    {"layer": 1, "claim": "route quality"},
                    {"layer": 2, "claim": "replication needed"},
                ],
                evidence_refs=[
                    "tic://source/hydromancer/single_scout",
                    "tic://source/news/single_scout",
                ],
                quality_flags=[
                    "source_family:hydromancer",
                    "source_family:web_attention",
                    "adversarial_decision:allow",
                ],
            ),
            InformationString(
                title="Single scout second string",
                thesis="The same scout also found a second mechanism, which is support but not independent scout coverage.",
                mechanism="Multiple strings from one scout should raise attention, not count as independent scout support.",
                expected_outcome="Geometry routes to widen_scouts.",
                kill_signal="Second scout contradicts the route.",
                conviction=0.74,
                novelty_score=0.80,
                crowdedness=0.18,
                entities_chain=["HYPE", "independent replication"],
                depth_layers=[
                    {"layer": 1, "claim": "same scout"},
                    {"layer": 2, "claim": "needs independent scout"},
                ],
                evidence_refs=[
                    "tic://source/orderbook/single_scout",
                    "tic://source/news/single_scout",
                ],
                quality_flags=[
                    "source_family:market_microstructure",
                    "source_family:web_attention",
                    "adversarial_decision:allow",
                ],
            ),
        ],
        coverage_cell_key="HYPE|intraday|on_chain|frontier|validator_unstake",
        source_tool_call_ids=["tc_single_hydro", "tc_single_orderbook", "tc_single_news"],
    )

    snapshot = compute_alpha_geometry(cycle_id="cycle_geo_single_scout", conn=store.conn, persist=True)
    assert snapshot.cells[0].route_directive == "widen_scouts"
    assert "multi_string_single_scout_cell" in snapshot.cells[0].quality_flags

    action_plan = plan_alpha_geometry_actions(cycle_id="cycle_geo_single_scout", conn=store.conn)
    action = action_plan["actions"][0]
    assert action["action"] == "replicate_with_independent_scouts"
    assert "multi_string_single_scout_cell" in action["quality_flags"]
    assert "single_scout_cell -> independent_replication" in action["missing_edges"]


def test_alpha_geometry_policy_can_block_fragile_verify_route(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    for i, family in enumerate(["hydromancer", "our_hl_node"]):
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_fragile_verify_policy",
            scout_id=f"scout_fragile_verify_{i}",
            seed_id=f"seed_fragile_verify_{i}",
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"Fragile high-scream route {family}",
                    thesis="HYPE looks verifier-ready, but the source surface is flagged as brittle.",
                    mechanism="A high-support route can still be unsafe if the source layer is carrying failed-evidence flags.",
                    expected_outcome="Default geometry may verify; evolved geometry should repair first.",
                    kill_signal="Independent source repair clears the fragility before verifier spend.",
                    conviction=0.96,
                    novelty_score=0.92,
                    crowdedness=0.10,
                    entities_chain=["HYPE", "aHYPE", family],
                    depth_layers=[
                        {"layer": 1, "claim": "route signal"},
                        {"layer": 2, "claim": "source fragility"},
                    ],
                    evidence_refs=[f"tic://source/{family}/fragile_verify"],
                    quality_flags=[
                        f"source_family:{family}",
                        "failed_call_as_evidence",
                        "missing_mechanism",
                    ],
                )
            ],
            coverage_cell_key="HYPE|intraday|on_chain|frontier|validator_unstake",
            source_tool_call_ids=[f"tc_fragile_verify_{family}"],
        )

    default_shape = compute_alpha_geometry(
        cycle_id="cycle_fragile_verify_policy",
        conn=store.conn,
        persist=False,
    )
    constrained_shape = compute_alpha_geometry(
        cycle_id="cycle_fragile_verify_policy",
        conn=store.conn,
        persist=False,
        routing_thresholds={
            "verify_allow_fragility_max": 0.20,
            "verify_source_independence_min": 0.45,
            "repair_fragility_min": 0.20,
        },
    )

    assert default_shape.cells[0].route_directive == "verify_now"
    assert constrained_shape.cells[0].metrics["fragility"] > 0.20
    assert constrained_shape.cells[0].route_directive == "repair_sources"


def test_market_evolve_policy_applies_prompt_and_tool_budget_to_seeds(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    seeds = [
        SeedCell(
            seed_id="seed_policy_1",
            entity="HYPE",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            theme="validator_unstake",
            payload={
                "tool_candidates": [
                    f"tic://tool/test/tool_{i}@v1" for i in range(20)
                ]
            },
        )
    ]

    program = apply_market_evolve_policy_to_seeds(
        seeds,
        cycle_id="cycle_policy",
        conn=store.conn,
    )

    payload = seeds[0].payload
    assert payload["market_evolve_program_id"] == program.program_id
    assert payload["prompt_variant"] == "temporal_pyramid_v1"
    assert payload["tool_candidate_limit"] == 10
    assert payload["max_evidence_tools"] == 2
    assert len(payload["tool_candidates"]) == 10
    rows = load_market_evolve_policy_applications(cycle_id="cycle_policy", conn=store.conn)
    assert rows[0]["program_id"] == program.program_id
    assert rows[0]["prompt_variant"] == "temporal_pyramid_v1"
    assert rows[0]["tool_candidate_limit"] == 10


def test_market_evolve_scores_governor_routed_gap_seeds_and_mutates(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    seeds = [
        SeedCell(
            seed_id=f"seed_governor_{i}",
            entity="BTC",
            horizon="intraday",
            lens="microstructure",
            bias_mode="frontier",
            theme="market_map_gap_repair",
            payload={
                "source": "market_map_governor",
                "gap_id": f"gap_governor_{i}",
                "missing_surfaces": [
                    "market_state",
                    "our_hl_node",
                    "hydromancer_actor_graph",
                ],
                "expected_edges": [
                    "entity -> depth",
                    "wallet -> node_order_quality",
                ],
                "market_map_completion_pressure": "bootstrap_unmapped_market",
                "market_map_valid_cell_count": 132048,
                "tool_candidates": [
                    "tic://tool/builtin/query_timeseries@v1",
                    "tic://tool/builtin/hl_reject_corpus@v1",
                    "tic://tool/hydromancer/get_hl_pnl_leaderboard@v1",
                ],
            },
        )
        for i in range(4)
    ]
    program = apply_market_evolve_policy_to_seeds(
        seeds,
        cycle_id="cycle_governor_evolve",
        conn=store.conn,
    )
    families = ["market_microstructure", "our_hl_node", "hydromancer"]
    for i, family in enumerate(families):
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_governor_evolve",
            scout_id=f"scout_governor_{i}",
            seed_id=seeds[i].seed_id,
            entity="BTC",
            theme="market_map_gap_repair",
            horizon="intraday",
            lens="microstructure",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"Governor routed BTC gap {family}",
                    thesis="BTC intraday microstructure gap is decision-changing when depth, node quality, and actor behavior converge.",
                    mechanism="The governor routed a missing lattice cell into a source-diverse evidence packet.",
                    expected_outcome="Depth, node rejects, and Hydromancer actor quality decide whether the gap graduates.",
                    kill_signal="The missing source edge does not change depth, actor quality, or route confidence.",
                    conviction=0.84,
                    novelty_score=0.77,
                    crowdedness=0.22,
                    entities_chain=["BTC", "depth", "node quality", family],
                    depth_layers=[
                        {"layer": 1, "claim": "missing cell sampled"},
                        {"layer": 2, "claim": "source edge added"},
                    ],
                    evidence_refs=[f"tic://source/{family}/governor_gap"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            coverage_cell_key=f"BTC|intraday|microstructure|frontier|gap_{i}",
            source_tool_call_ids=[f"tc_governor_{family}"],
        )

    apps = load_market_evolve_policy_applications(
        cycle_id="cycle_governor_evolve",
        conn=store.conn,
        limit=20,
    )
    assert {row["applied"]["seed_source"] for row in apps} == {"market_map_governor"}
    assert {row["applied"]["gap_id"] for row in apps} == {f"gap_governor_{i}" for i in range(4)}

    metrics = collect_market_evolve_metrics(cycle_id="cycle_governor_evolve", conn=store.conn)
    assert metrics["governor_seed_count"] == 4.0
    assert metrics["governor_string_count"] == 3.0
    assert metrics["governor_string_yield_per_seed"] == 0.75
    assert metrics["governor_gap_repair_rate"] == 0.75
    assert metrics["governor_valid_string_rate"] == 1.0
    assert metrics["governor_avg_source_independence"] >= 0.9

    step = run_market_evolve_step(cycle_id="cycle_governor_evolve", conn=store.conn)
    assert any(m.mutation_kind == "exploit_market_map_governor" for m in step.mutations)
    child = next(
        c for c in step.child_programs
        if "mutation:exploit_market_map_governor" in c.quality_flags
    )
    assert child.genome["routing_thresholds"]["coverage_gap_budget_share"] > program.genome["routing_thresholds"]["coverage_gap_budget_share"]
    assert child.genome["tool_request_policy"]["prefer_missing_source_family"] is True


def test_market_evolve_prompt_contract_pressure_reaches_scout_prompt():
    seed = SeedCell(
        seed_id="seed_prompt_pressure",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        payload={
            "prompt_contract_pressure": "raise",
            "prompt_min_information_strings": 2,
            "prompt_require_mechanism": True,
            "prompt_require_kill_signal": True,
            "prompt_require_evidence_refs": True,
        },
    )

    prompt = _apply_prompt_contract_pressure("BASE_PROMPT", seed)

    assert "<market_evolve_prompt_contract>" in prompt
    assert "minimum_information_strings: 2" in prompt
    assert "Reject your own string if mechanism is missing" in prompt


def test_market_evolve_scores_prompt_quality_and_raises_contract_pressure(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    families = ["hydromancer", "market_microstructure", "our_hl_node"]
    now = "2026-05-21T12:00:00+00:00"
    for i, family in enumerate(families):
        seed_id = f"seed_prompt_quality_{i}"
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_prompt_quality",
            scout_id=f"scout_prompt_quality_{i}",
            seed_id=seed_id,
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"Prompt quality still emitted {family}",
                    thesis="HYPE source-diverse route evidence exists, but prompt structure is too weak to scale.",
                    mechanism="The information map can be valid while the prompt quality gate remains below scale threshold.",
                    expected_outcome="MarketEvolve should raise contract pressure before expanding Flash calls.",
                    kill_signal="Prompt-quality metrics are ignored by the evaluator.",
                    conviction=0.82,
                    novelty_score=0.74,
                    crowdedness=0.24,
                    entities_chain=["HYPE", "prompt policy", family],
                    depth_layers=[{"layer": 1, "claim": "string valid"}, {"layer": 2, "claim": "prompt weak"}],
                    evidence_refs=[f"tic://source/{family}/prompt_quality"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            coverage_cell_key="HYPE|intraday|on_chain|frontier|validator_unstake",
            source_tool_call_ids=[f"tc_prompt_quality_{i}_{family}"],
        )
        payload = {
            "tier": "scout",
            "seed_id": seed_id,
            "prompt_variant": "temporal_pyramid_v1",
            "quality_flags": [
                "prompt_variant:temporal_pyramid_v1",
                "prompt_quality:0.45",
                "prompt_too_few_valid_tools",
            ],
        }
        store.conn.execute(
            """
            INSERT OR REPLACE INTO hypotheses (
                id, cycle_id, specialist_id, title, hypothesis_text,
                status, payload, valid_from, transaction_from
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"hyp_prompt_quality_{i}",
                "cycle_prompt_quality",
                "tier1_scout",
                "Prompt quality weak",
                "Prompt quality should influence MarketEvolve.",
                "active",
                json.dumps(payload),
                now,
                now,
            ),
        )
    store.conn.commit()

    metrics = collect_market_evolve_metrics(cycle_id="cycle_prompt_quality", conn=store.conn)
    step = run_market_evolve_step(cycle_id="cycle_prompt_quality", conn=store.conn)

    assert metrics["prompt_eval_count"] == 3.0
    assert metrics["avg_prompt_quality"] == 0.45
    assert metrics["prompt_pass_rate"] == 0.0
    assert any(m.mutation_kind == "tighten_prompt_contract" for m in step.mutations)
    child = next(
        c for c in step.child_programs
        if "mutation:tighten_prompt_contract" in c.quality_flags
    )
    prompt_policy = child.genome["prompt_policy"]
    assert prompt_policy["contract_pressure"] == "raise"
    assert prompt_policy["min_information_strings"] >= 2


def test_market_evolve_tightens_shape_route_contract_when_geometry_route_fails(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    families = ["hydromancer", "market_microstructure", "our_hl_node"]
    now = "2026-05-21T12:00:00+00:00"
    for i, family in enumerate(families):
        seed_id = f"seed_route_contract_{i}"
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_route_contract",
            scout_id=f"scout_route_contract_{i}",
            seed_id=seed_id,
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"Valid but misrouted route output {family}",
                    thesis="HYPE node route evidence is structurally valid but did not move the geometry missing edge.",
                    mechanism="This keeps prompt quality high while isolating the route-contract failure mode.",
                    expected_outcome="MarketEvolve should tighten the shape route contract, not only the generic prompt.",
                    kill_signal="The evaluator ignores route_contract_failed flags.",
                    conviction=0.82,
                    novelty_score=0.74,
                    crowdedness=0.24,
                    entities_chain=["HYPE", "route contract", family],
                    depth_layers=[
                        {"layer": 1, "claim": "valid scout string"},
                        {"layer": 2, "claim": "route contract failure is tracked separately"},
                    ],
                    evidence_refs=[f"tic://source/{family}/route_contract"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            coverage_cell_key="HYPE|intraday|on_chain|frontier|validator_unstake",
            source_tool_call_ids=[f"tc_route_contract_{i}_{family}"],
        )
        payload = {
            "tier": "scout",
            "seed_id": seed_id,
            "prompt_variant": "temporal_pyramid_v1",
            "quality_flags": [
                "prompt_variant:temporal_pyramid_v1",
                "prompt_quality:0.82",
                "route_contract_alignment:0.20",
                "route_contract_failed",
                "route_contract_no_edge_moved",
                "route_contract_success_gate_missing",
            ],
        }
        store.conn.execute(
            """
            INSERT OR REPLACE INTO hypotheses (
                id, cycle_id, specialist_id, title, hypothesis_text,
                status, payload, valid_from, transaction_from
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"hyp_route_contract_{i}",
                "cycle_route_contract",
                "tier1_scout",
                "Route contract failed",
                "Route contract should influence MarketEvolve.",
                "active",
                json.dumps(payload),
                now,
                now,
            ),
        )
    store.conn.commit()

    metrics = collect_market_evolve_metrics(cycle_id="cycle_route_contract", conn=store.conn)
    step = run_market_evolve_step(cycle_id="cycle_route_contract", conn=store.conn)

    assert metrics["route_contract_eval_count"] == 3.0
    assert metrics["route_contract_success_rate"] == 0.0
    assert metrics["route_contract_failure_rate"] == 1.0
    assert any(m.mutation_kind == "tighten_shape_route_contract" for m in step.mutations)
    mutation = next(m for m in step.mutations if m.mutation_kind == "tighten_shape_route_contract")
    assert mutation.mutation["_geometry_cortex_review"]["source"] == "alpha_geometry_cortex_review"
    assert "route_contract_not_moving_edges" in mutation.mutation["_geometry_cortex_review"]["diagnostic_codes"]
    assert mutation.mutation["_evolution_proof"]["mutation_source"] == "alpha_geometry_cortex_review"
    assert mutation.mutation["_evolution_proof"]["source_work_order_ids"]
    child = next(
        c for c in step.child_programs
        if "mutation:tighten_shape_route_contract" in c.quality_flags
    )
    assert child.genome["prompt_policy"]["route_contract_pressure"] == "strict"
    assert child.genome["tool_request_policy"]["require_expected_edge"] is True
    assert child.genome["routing_thresholds"]["route_contract_min_success_rate"] >= 0.70


def test_market_evolve_scores_cortex_task_execution_feedback(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    completed = post_task(
        topic="alpha_geometry.route",
        title="Complete shape route",
        cycle_id="cycle_cortex_feedback",
        allowed_tools=["tic://tool/talis_native/plan_alpha_geometry_actions@v1"],
        payload={"route_task_id": "shape_task_feedback_ok"},
        conn=store.conn,
    )
    claim_task(completed, agent_id="cortex_task_worker", specialist_id="alpha_geometry_cortex", conn=store.conn)
    start_task(completed, agent_id="cortex_task_worker", specialist_id="alpha_geometry_cortex", conn=store.conn)
    complete_task(
        completed,
        agent_id="cortex_task_worker",
        specialist_id="alpha_geometry_cortex",
        payload={
            "schema_version": "cortex_task_execution_v1",
            "proof": {
                "shape_tool_observed": True,
                "observations_logged": 2,
                "deferred_tool_count": 1,
            },
            "observations": [
                {"uri": "tic://tool/talis_native/plan_alpha_geometry_actions@v1", "ok": True},
                {"uri": "tic://tool/talis_native/review_alpha_geometry_cortex@v1", "ok": True},
            ],
            "deferred_tool_sequence": ["tic://tool/builtin/query_events_recent@v1"],
        },
        conn=store.conn,
    )
    failed = post_task(
        topic="market_evolve.frontier",
        title="Failed frontier task",
        cycle_id="cycle_cortex_feedback",
        allowed_tools=["tic://tool/talis_native/plan_alpha_geometry_actions@v1"],
        payload={"route_task_id": "shape_task_feedback_fail"},
        conn=store.conn,
    )
    claim_task(failed, agent_id="cortex_task_worker", specialist_id="alpha_geometry_cortex", conn=store.conn)
    start_task(failed, agent_id="cortex_task_worker", specialist_id="alpha_geometry_cortex", conn=store.conn)
    fail_task(
        failed,
        agent_id="cortex_task_worker",
        specialist_id="alpha_geometry_cortex",
        reason="shape_backend_offline",
        payload={
            "schema_version": "cortex_task_execution_v1",
            "proof": {
                "shape_tool_observed": False,
                "observations_logged": 1,
            },
        },
        conn=store.conn,
    )

    metrics = collect_market_evolve_metrics(cycle_id="cycle_cortex_feedback", conn=store.conn)

    assert metrics["cortex_task_count"] == 2.0
    assert metrics["cortex_task_completed_count"] == 1.0
    assert metrics["cortex_task_failed_count"] == 1.0
    assert metrics["cortex_task_completion_rate"] == 0.5
    assert metrics["cortex_task_failure_rate"] == 0.5
    assert metrics["cortex_shape_observation_rate"] == 0.5
    assert metrics["cortex_deferred_followup_rate"] == 0.5


def test_market_evolve_mutates_when_cortex_task_harness_fails(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    for i in range(3):
        task_id = post_task(
            topic="alpha_geometry.route",
            title=f"Bad cortex task {i}",
            cycle_id="cycle_cortex_harness_mutation",
            allowed_tools=["tic://tool/talis_native/plan_alpha_geometry_actions@v1"],
            payload={"route_task_id": f"shape_task_bad_{i}"},
            conn=store.conn,
        )
        claim_task(task_id, agent_id="cortex_task_worker", specialist_id="alpha_geometry_cortex", conn=store.conn)
        start_task(task_id, agent_id="cortex_task_worker", specialist_id="alpha_geometry_cortex", conn=store.conn)
        fail_task(
            task_id,
            agent_id="cortex_task_worker",
            specialist_id="alpha_geometry_cortex",
            reason="shape_backend_offline",
            payload={
                "schema_version": "cortex_task_execution_v1",
                "proof": {
                    "shape_tool_observed": False,
                    "observations_logged": 1,
                },
            },
            conn=store.conn,
        )

    metrics = collect_market_evolve_metrics(cycle_id="cycle_cortex_harness_mutation", conn=store.conn)
    step = run_market_evolve_step(cycle_id="cycle_cortex_harness_mutation", conn=store.conn)

    assert metrics["cortex_task_count"] == 3.0
    assert metrics["cortex_task_completion_rate"] == 0.0
    assert metrics["cortex_task_failure_rate"] == 1.0
    assert any(m.mutation_kind == "tighten_cortex_task_harness" for m in step.mutations)
    mutation = next(m for m in step.mutations if m.mutation_kind == "tighten_cortex_task_harness")
    proof = mutation.mutation["_evolution_proof"]
    assert proof["mutation_kind"] == "tighten_cortex_task_harness"
    assert "cortex_task_completion_rate" in proof["target_metrics"]
    assert any(
        gate["metric"] == "candidate_cortex_task_completion_rate"
        for gate in proof["falsification_gates"]
    )
    child = next(
        c for c in step.child_programs
        if "mutation:tighten_cortex_task_harness" in c.quality_flags
    )
    assert child.genome["cortex_policy"]["min_task_completion_rate"] >= 0.90
    assert child.genome["tool_request_policy"]["prefer_native_shape_tools"] is True


def test_market_evolve_allows_bounded_diverse_open_experiments(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    program = seed_default_market_evolve_program(
        cycle_id="cycle_population_evolve",
        conn=store.conn,
    )
    prompt_eval = MarketEvolveEvaluation(
        evaluation_id="meval_population_prompt",
        program_id=program.program_id,
        cycle_id="cycle_population_evolve",
        evaluator_version="test",
        score=0.20,
        metrics={
            "prompt_eval_count": 3.0,
            "avg_prompt_quality": 0.20,
            "prompt_pass_rate": 0.20,
            "prompt_contract_failure_rate": 0.80,
            "valid_string_rate": 1.0,
            "avg_source_independence": 0.80,
            "avg_fragility": 0.20,
        },
        baseline_metrics={},
        passed=False,
        rationale="prompt repair should be tested",
        quality_flags=["test_prompt_failure"],
    )
    prompt_child = propose_market_evolve_mutation(
        program=program,
        evaluation=prompt_eval,
        cycle_id="cycle_population_evolve",
        conn=store.conn,
    )
    assert prompt_child is not None

    cortex_eval = MarketEvolveEvaluation(
        evaluation_id="meval_population_cortex",
        program_id=program.program_id,
        cycle_id="cycle_population_evolve",
        evaluator_version="test",
        score=0.10,
        metrics={
            "valid_string_rate": 1.0,
            "avg_source_independence": 0.80,
            "avg_fragility": 0.20,
            "cortex_task_count": 3.0,
            "cortex_task_completion_rate": 0.0,
            "cortex_shape_observation_rate": 0.0,
            "cortex_task_failure_rate": 1.0,
        },
        baseline_metrics={},
        passed=False,
        rationale="cortex worker repair should be tested in parallel",
        quality_flags=["test_cortex_failure"],
    )
    cortex_child = propose_market_evolve_mutation(
        program=program,
        evaluation=cortex_eval,
        cycle_id="cycle_population_evolve",
        conn=store.conn,
    )
    duplicate_cortex_child = propose_market_evolve_mutation(
        program=program,
        evaluation=cortex_eval,
        cycle_id="cycle_population_evolve",
        conn=store.conn,
    )

    experiments = load_market_evolve_experiments(
        cycle_id="cycle_population_evolve",
        conn=store.conn,
    )
    kinds = {
        row["arms"][1].get("mutation_kind")
        for row in experiments
        if row.get("arms")
    }
    assert cortex_child is not None
    assert duplicate_cortex_child is None
    assert kinds == {"tighten_prompt_contract", "tighten_cortex_task_harness"}
    assert len(experiments) == 2
    cortex_mutation = next(
        row for row in store.conn.execute(
            """
            SELECT mutation_json
            FROM market_evolve_mutations
            WHERE child_program_id = ?
            """,
            (cortex_child.program_id,),
        ).fetchall()
    )
    proof = json.loads(cortex_mutation["mutation_json"])["_evolution_proof"]
    assert proof["population_gate"]["allowed"] is True
    assert proof["population_gate"]["open_experiment_count"] == 1
    assert proof["population_gate"]["population_mode"] == "bounded_parallel"


def test_market_evolve_proposes_diverse_candidate_batch_from_one_evaluation(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    program = seed_default_market_evolve_program(
        cycle_id="cycle_candidate_batch",
        conn=store.conn,
    )
    evaluation = MarketEvolveEvaluation(
        evaluation_id="meval_candidate_batch",
        program_id=program.program_id,
        cycle_id="cycle_candidate_batch",
        evaluator_version="test",
        score=0.10,
        metrics={
            "valid_string_rate": 1.0,
            "avg_source_independence": 0.80,
            "avg_fragility": 0.20,
            "prompt_eval_count": 3.0,
            "avg_prompt_quality": 0.20,
            "prompt_pass_rate": 0.20,
            "prompt_contract_failure_rate": 0.80,
            "cortex_task_count": 3.0,
            "cortex_task_completion_rate": 0.0,
            "cortex_shape_observation_rate": 0.0,
            "cortex_task_failure_rate": 1.0,
        },
        baseline_metrics={},
        passed=False,
        rationale="multiple independent repair signals",
        quality_flags=["test_multi_signal"],
    )

    children = propose_market_evolve_mutations(
        program=program,
        evaluation=evaluation,
        cycle_id="cycle_candidate_batch",
        max_children=2,
        conn=store.conn,
    )
    mutations = [
        json.loads(row["mutation_json"])
        for row in store.conn.execute(
            """
            SELECT mutation_kind, mutation_json
            FROM market_evolve_mutations
            WHERE parent_program_id = ?
            ORDER BY created_at ASC
            """,
            (program.program_id,),
        ).fetchall()
    ]
    kinds = {
        m["_evolution_proof"]["mutation_kind"]
        for m in mutations
    }

    assert len(children) == 2
    assert kinds == {"tighten_cortex_task_harness", "tighten_prompt_contract"}
    assert store.conn.execute(
        """
        SELECT COUNT(*)
        FROM market_evolve_experiments
        WHERE parent_program_id = ?
          AND status = 'planned'
        """,
        (program.program_id,),
    ).fetchone()[0] == 2
    assert any(
        m["_evolution_proof"]["population_gate"]["open_experiment_count"] == 1
        for m in mutations
    )
    lineage = build_market_evolve_lineage(conn=store.conn)
    node_by_id = {node["program_id"]: node for node in lineage["nodes"]}
    for child in children:
        node = node_by_id[child.program_id]
        assert node["mutation_source"] == "market_evolve_metric_heuristic"
        assert node["mutation_hypothesis"]
        assert node["kill_signal"]
        assert node["promotion_evidence_required"]
        assert node["falsification_gate_count"] >= 1
        assert node["population_gate"]["allowed"] is True
    edge_by_child = {
        edge["to_program_id"]: edge
        for edge in lineage["edges"]
        if edge["to_program_id"] in node_by_id
    }
    for child in children:
        edge = edge_by_child[child.program_id]
        assert edge["kill_signal"]
        assert edge["promotion_evidence_required"]
        assert edge["population_gate"]["schema_version"] == "market_evolve_population_gate_v1"
    frontier_by_id = {row["program_id"]: row for row in lineage["frontier"]}
    assert all(
        frontier_by_id[child.program_id]["kill_signal"]
        for child in children
        if child.program_id in frontier_by_id
    )


def test_market_evolve_retunes_geometry_when_fragile_cells_are_verified(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    cells = []
    for i in range(3):
        metrics = {
            "source_independence": 0.22,
            "frontier_pressure": 0.86,
            "verifier_readiness": 0.84,
            "fragility": 0.68,
            "support_mass": 0.91,
            "tension": 0.05,
            "trade_scream_score": 0.82,
        }
        cells.append(AlphaGeometryCell(
            cell_key=f"HYPE|intraday|on_chain|frontier|fragile_{i}",
            cycle_id="cycle_fragile_geometry",
            entity="HYPE",
            horizon="intraday",
            lens="on_chain",
            theme=f"fragile_{i}",
            string_count=3,
            scout_count=2,
            evidence_ref_count=2,
            source_families=["hydromancer"],
            coordinates={
                "x_source_independence": 0.22,
                "y_frontier_pressure": 0.86,
                "z_tension": 0.05,
                "color_fragility": 0.68,
                "size_support_mass": 0.91,
            },
            metrics=metrics,
            route_directive="verify_now",
            quality_flags=["frontier_trade_candidate", "fragile_geometry", "low_source_entropy"],
        ))
    persist_alpha_geometry(
        AlphaGeometrySnapshot(
            cycle_id="cycle_fragile_geometry",
            created_at="2026-05-21T12:00:00+00:00",
            cells=cells,
            global_metrics={},
            quality_flags=["alpha_geometry_v1"],
        ),
        conn=store.conn,
    )

    metrics = collect_market_evolve_metrics(cycle_id="cycle_fragile_geometry", conn=store.conn)
    step = run_market_evolve_step(cycle_id="cycle_fragile_geometry", conn=store.conn)

    assert metrics["geometry_cell_count"] == 3.0
    assert metrics["fragile_verify_rate"] == 1.0
    assert any(m.mutation_kind == "retune_geometry_repair_before_verify" for m in step.mutations)
    mutation = next(
        m for m in step.mutations
        if m.mutation_kind == "retune_geometry_repair_before_verify"
    )
    proof = mutation.mutation["_evolution_proof"]
    assert proof["schema_version"] == "market_evolve_mutation_proof_v1"
    assert proof["target_metrics"]["fragile_verify_rate"] == 1.0
    changed_paths = {row["path"] for row in proof["changed_paths"]}
    assert "routing_thresholds.verify_allow_fragility_max" in changed_paths
    assert "geometry_weights.fragility_penalty" in changed_paths
    assert proof["falsification_gates"]
    child = next(
        c for c in step.child_programs
        if "mutation:retune_geometry_repair_before_verify" in c.quality_flags
    )
    assert child.genome["routing_thresholds"]["verify_allow_fragility_max"] <= 0.55
    assert child.genome["routing_thresholds"]["verify_source_independence_min"] >= 0.45
    assert child.genome["geometry_weights"]["fragility_penalty"] > DEFAULT_MARKET_EVOLVE_GENOME["geometry_weights"]["fragility_penalty"]
    assert child.genome["tool_request_policy"]["prefer_missing_source_family"] is True


def test_seed_generation_is_replayable_with_rng(tmp_path):
    reset_desk_store_for_test(tmp_path / "desk.db")
    kwargs = dict(
        n_seeds=12,
        cycle_id="cycle_replay",
        entities=["HYPE", "NVDA", "SPX"],
        themes=["node_flow", "liquidity_route"],
        rng_seed=424242,
    )

    first = generate_seeds(**kwargs)
    second = generate_seeds(**kwargs)

    assert [s.to_payload() for s in first] == [s.to_payload() for s in second]
    assert {s.payload["source"] for s in first} == {"theme_injection", "stratified"}


def test_alpha_geometry_routes_become_next_cycle_seed_allocation(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    families = ["hydromancer", "market_microstructure", "our_hl_node"]
    for i, family in enumerate(families):
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_geo_prior",
            scout_id=f"scout_geo_prior_{i}",
            seed_id=f"seed_geo_prior_{i}",
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"Prior geometry route {family}",
                    thesis="Prior HYPE geometry is source-diverse enough to deserve next-cycle confirmation.",
                    mechanism="Source-family breadth and conviction make the cell verifier-ready.",
                    expected_outcome="The next cycle should pin a scout to the same geometry cell.",
                    kill_signal="Next-cycle seed allocation ignores the geometry directive.",
                    conviction=0.88,
                    novelty_score=0.82,
                    crowdedness=0.18,
                    entities_chain=["HYPE", "aHYPE", "route quality", family],
                    depth_layers=[{"layer": 1, "claim": "route"}, {"layer": 2, "claim": "confirmation"}],
                    evidence_refs=[f"tic://source/{family}/prior_geometry"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            coverage_cell_key="HYPE|intraday|on_chain|frontier|validator_unstake",
            source_tool_call_ids=[f"tc_geo_prior_{i}_{family}"],
        )
    prior_snapshot = compute_alpha_geometry(cycle_id="cycle_geo_prior", conn=store.conn)
    assert prior_snapshot.cells[0].route_directive == "verify_now"

    program = SimpleNamespace(
        genome={
            "routing_thresholds": {
                "frontier_exploitation_budget_share": 0.10,
                "coverage_exploration_budget_share": 0.0,
            }
        }
    )
    route_seeds = generate_alpha_geometry_route_seeds(
        cycle_id="cycle_geo_next",
        n_seed_budget=20,
        program=program,
        conn=store.conn,
    )
    assert route_seeds
    assert route_seeds[0].payload["source"] == "alpha_geometry_route"
    assert route_seeds[0].payload["alpha_geometry_source_cycle_id"] == "cycle_geo_prior"
    assert route_seeds[0].payload["alpha_geometry_route_directive"] == "verify_now"
    assert route_seeds[0].payload["alpha_geometry_route_task_id"].startswith("shape_task_")
    assert route_seeds[0].payload["alpha_geometry_action"] == "send_to_verifier_council"
    assert route_seeds[0].payload["alpha_geometry_action_owner"] == "verifier"
    assert "claim -> verifier_decision" in route_seeds[0].payload["alpha_geometry_missing_edges"]
    assert route_seeds[0].payload["alpha_geometry_success_gate"] == "2_of_3 verifier majority with independent source receipts"
    assert "tic://tool/builtin/query_source_health@v1" in route_seeds[0].payload["tool_candidates"]
    assert route_seeds[0].entity == "HYPE"
    assert route_seeds[0].lens == "on_chain"
    assert route_seeds[0].bias_mode == "consensus_confirm"

    base_seeds = generate_seeds(
        n_seeds=20 - len(route_seeds),
        cycle_id="cycle_geo_next",
        entities=["HYPE", "NVDA"],
        rng_seed=123,
    )
    next_seeds = route_seeds + base_seeds
    apply_market_evolve_policy_to_seeds(next_seeds, cycle_id="cycle_geo_next", conn=store.conn)

    assert len(next_seeds) == 20
    assert next_seeds[0].payload["market_evolve_applied"] is True
    assert next_seeds[0].payload["alpha_geometry_route_directive"] == "verify_now"
    applications = load_market_evolve_policy_applications(
        cycle_id="cycle_geo_next",
        conn=store.conn,
        limit=40,
    )
    assert len(applications) == 20
    assert any(row["seed_id"] == next_seeds[0].seed_id for row in applications)


def test_market_evolve_assigns_experiment_arms_and_promotes_after_repeated_wins(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    # First cycle creates an active parent plus a candidate mutation and planned experiment.
    planning_step = run_market_evolve_step(cycle_id="cycle_plan", conn=store.conn)
    assert planning_step.experiment_plans
    experiment_id = planning_step.experiment_plans[0]["id"]
    parent_program_id = planning_step.experiment_plans[0]["parent_program_id"]
    candidate_program_id = planning_step.experiment_plans[0]["candidate_program_id"]

    seeds = [
        SeedCell(
            seed_id=f"seed_ab_{i:02d}",
            entity="HYPE",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            theme="validator_unstake",
            payload={"tool_candidates": [f"tic://tool/test/tool_{j}@v1" for j in range(12)]},
        )
        for i in range(80)
    ]
    apply_market_evolve_policy_to_seeds(
        seeds,
        cycle_id="cycle_ab",
        conn=store.conn,
    )
    apps = load_market_evolve_policy_applications(cycle_id="cycle_ab", conn=store.conn, limit=200)
    arms = {row["experiment_arm"] for row in apps if row.get("experiment_id") == experiment_id}
    assert {"control", "candidate"}.issubset(arms)

    candidate_apps = [row for row in apps if row.get("experiment_arm") == "candidate"][:6]
    families = ["hydromancer", "market_microstructure", "our_hl_node"]
    for i, app in enumerate(candidate_apps):
        family = families[i % len(families)]
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_ab",
            scout_id=f"scout_ab_candidate_{i}",
            seed_id=app["seed_id"],
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
                    depth_layers=[{"layer": 1, "claim": "route"}, {"layer": 2, "claim": "actor quality"}],
                    evidence_refs=[f"tic://source/{family}/candidate"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            source_tool_call_ids=[f"tc_ab_candidate_{i}_{family}"],
        )

    step = run_market_evolve_step(cycle_id="cycle_ab", conn=store.conn)
    results = load_market_evolve_experiment_results(cycle_id="cycle_ab", conn=store.conn)

    assert step.experiment_results
    result = next(row for row in results if row["experiment_id"] == experiment_id)
    assert result["decision"] == "continue_candidate"
    assert result["score_delta"] > 0
    assert result["falsification_gate_results"]
    assert any(
        flag.startswith("proof_gate_passed:")
        for flag in result["quality_flags"]
    )
    programs = {
        p.program_id: p
        for p in load_market_evolve_programs(status="", limit=100, conn=store.conn)
    }
    assert programs[parent_program_id].status == "active"
    assert programs[candidate_program_id].status == "candidate"
    assert "multi_cycle_promotion_pending" in result["quality_flags"]
    assert store.conn.execute(
        "SELECT status FROM market_evolve_experiments WHERE id = ?",
        (experiment_id,),
    ).fetchone()[0] == "running"
    mutation_status = store.conn.execute(
        "SELECT status FROM market_evolve_mutations WHERE child_program_id = ?",
        (candidate_program_id,),
    ).fetchone()[0]
    assert mutation_status == "needs_more_data"
    open_experiments = [
        row for row in load_market_evolve_experiments(status="", limit=100, conn=store.conn)
        if row["parent_program_id"] == parent_program_id
        and row["status"] in {"planned", "running", "insufficient_sample"}
    ]
    assert len(open_experiments) <= DEFAULT_MARKET_EVOLVE_GENOME["evolution_policy"]["max_open_experiments_per_parent"]

    next_seeds = [
        SeedCell(
            seed_id=f"seed_ab_next_{i:02d}",
            entity="HYPE",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            theme="validator_unstake",
            payload={"tool_candidates": [f"tic://tool/test/tool_{j}@v1" for j in range(12)]},
        )
        for i in range(80)
    ]
    apply_market_evolve_policy_to_seeds(
        next_seeds,
        cycle_id="cycle_ab_next",
        conn=store.conn,
    )
    next_apps = load_market_evolve_policy_applications(cycle_id="cycle_ab_next", conn=store.conn, limit=200)
    next_candidate_apps = [
        row for row in next_apps
        if row.get("experiment_id") == experiment_id and row.get("experiment_arm") == "candidate"
    ][:6]
    for i, app in enumerate(next_candidate_apps):
        family = families[i % len(families)]
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_ab_next",
            scout_id=f"scout_ab_next_candidate_{i}",
            seed_id=app["seed_id"],
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"Candidate policy repeated {family}",
                    thesis="Candidate policy repeats source-diverse HYPE route intelligence out of sample.",
                    mechanism="Repeated source-family breadth gives verifier-ready route context.",
                    expected_outcome="Liquidity absorption or sellable route becomes observable intraday.",
                    kill_signal="No movement to sellable liquidity or strong informed absorption.",
                    conviction=0.88,
                    novelty_score=0.82,
                    crowdedness=0.18,
                    entities_chain=["HYPE", "aHYPE", "route quality"],
                    depth_layers=[{"layer": 1, "claim": "route"}, {"layer": 2, "claim": "actor quality"}],
                    evidence_refs=[f"tic://source/{family}/candidate_next"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            source_tool_call_ids=[f"tc_ab_next_candidate_{i}_{family}"],
        )

    step = run_market_evolve_step(cycle_id="cycle_ab_next", conn=store.conn)
    results = load_market_evolve_experiment_results(cycle_id="cycle_ab_next", conn=store.conn)
    assert step.experiment_results
    result = next(row for row in results if row["experiment_id"] == experiment_id)
    assert result["decision"] == "promote_candidate"
    assert "multi_cycle_promotion_gate_passed" in result["quality_flags"]
    assert result["falsification_gate_results"]
    programs = {
        p.program_id: p
        for p in load_market_evolve_programs(status="", limit=100, conn=store.conn)
    }
    assert programs[parent_program_id].status == "superseded"
    assert programs[candidate_program_id].status == "active"
    assert programs[candidate_program_id].generation == 1
    assert "promoted_by_market_evolve_experiment" in programs[candidate_program_id].quality_flags
    mutation_status = store.conn.execute(
        "SELECT status FROM market_evolve_mutations WHERE child_program_id = ?",
        (candidate_program_id,),
    ).fetchone()[0]
    assert mutation_status == "promoted"
    assert any(
        child.generation == 2 and child.parent_program_ids == [candidate_program_id]
        for child in step.child_programs
    )
    candidate = [p for p in load_market_evolve_programs(status="active", conn=store.conn) if p.program_id == candidate_program_id]
    assert candidate
    reused = evaluate_market_evolve_experiments(cycle_id="cycle_ab_next", conn=store.conn)
    reused_by_experiment = {row["experiment_id"]: row for row in reused}
    assert reused_by_experiment[experiment_id]["id"] == result["id"]
    assert "existing_experiment_result_reused" in reused_by_experiment[experiment_id]["quality_flags"]
    assert store.conn.execute(
        "SELECT COUNT(*) FROM market_evolve_experiment_results WHERE experiment_id = ? AND cycle_id = ?",
        (experiment_id, "cycle_ab_next"),
    ).fetchone()[0] == 1
    lineage = build_market_evolve_lineage(conn=store.conn)
    assert lineage["schema_version"] == "market_evolve_lineage_v1"
    node_by_id = {node["program_id"]: node for node in lineage["nodes"]}
    assert node_by_id[candidate_program_id]["status"] == "active"
    assert node_by_id[candidate_program_id]["latest_decision"] == "promote_candidate"
    assert node_by_id[candidate_program_id]["proof_gate_summary"]["evaluated"] >= 1
    assert node_by_id[candidate_program_id]["diversity_signature"]
    assert any(
        edge["from_program_id"] == parent_program_id
        and edge["to_program_id"] == candidate_program_id
        and edge["decision"] == "promote_candidate"
        for edge in lineage["edges"]
    )
    assert lineage["frontier"][0]["program_id"] == candidate_program_id
    assert lineage["frontier"][0]["next_action"] == "mutate_active_policy"


def test_market_evolve_experiment_uses_mutation_falsification_gates():
    experiment = {
        "matched_slice": {"min_seeds_per_arm": 20},
        "success_criteria": {
            "min_score_delta": 0.05,
            "min_valid_string_rate": 0.60,
            "min_source_independence": 0.45,
            "max_avg_fragility": 0.65,
            "max_low_ev_tool_rate": 0.25,
            "max_tool_eval_failed_rate": 0.50,
            "falsification_gates": [
                {
                    "metric": "candidate_fragile_verify_rate",
                    "operator": ">=",
                    "threshold": 0.20,
                    "decision": "reject_candidate",
                }
            ],
        },
    }
    control_metrics = {
        "scout_count": 20.0,
        "valid_string_rate": 1.0,
        "avg_source_independence": 0.72,
        "avg_fragility": 0.18,
        "low_ev_tool_rate": 0.0,
        "tool_eval_failed_rate": 0.0,
        "policy_cost_usd": 0.01,
        "geometry_cell_count": 1.0,
    }
    candidate_metrics = {
        **control_metrics,
        "avg_source_independence": 0.80,
        "avg_fragility": 0.22,
        "fragile_verify_rate": 0.75,
        "policy_cost_usd": 0.01,
    }

    result = _experiment_decision(
        experiment=experiment,
        control_metrics=control_metrics,
        candidate_metrics=candidate_metrics,
        control_score=0.55,
        candidate_score=0.72,
    )

    assert result["decision"] == "reject_candidate"
    assert result["score_delta"] == 0.17
    assert "proof_gate_triggered:candidate_fragile_verify_rate" in result["quality_flags"]
    assert "proof_gate_failed:candidate_fragile_verify_rate" in result["quality_flags"]
    assert result["falsification_gate_results"][0]["triggered"] is True
    assert result["falsification_gate_results"][0]["observed"] == 0.75


def test_market_evolve_prepares_paired_experiment_seed_slices(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    planning_step = run_market_evolve_step(cycle_id="cycle_pair_plan", conn=store.conn)
    experiment_id = planning_step.experiment_plans[0]["id"]
    seeds = [
        SeedCell(
            seed_id=f"seed_pair_{i:02d}",
            entity="HYPE" if i % 2 == 0 else "BTC",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            theme=f"paired_slice_{i}",
            payload={"tool_candidates": [f"tic://tool/test/tool_{j}@v1" for j in range(12)]},
        )
        for i in range(12)
    ]

    n_pairs = prepare_market_evolve_experiment_seed_pairs(
        seeds,
        cycle_id="cycle_pair_ab",
        conn=store.conn,
        max_pairs=4,
    )
    assert n_pairs == 4
    assert len(seeds) == 16
    pair_payloads = [
        s.payload for s in seeds
        if s.payload.get("market_evolve_pair_id")
    ]
    assert len(pair_payloads) == 8
    pair_ids = sorted({p["market_evolve_pair_id"] for p in pair_payloads})
    assert len(pair_ids) == 4
    for pair_id in pair_ids:
        arms = {
            p["market_evolve_forced_experiment_arm"]
            for p in pair_payloads
            if p["market_evolve_pair_id"] == pair_id
        }
        units = {
            p["market_evolve_pair_unit_key"]
            for p in pair_payloads
            if p["market_evolve_pair_id"] == pair_id
        }
        assert arms == {"control", "candidate"}
        assert len(units) == 1

    apply_market_evolve_policy_to_seeds(seeds, cycle_id="cycle_pair_ab", conn=store.conn)
    apps = load_market_evolve_policy_applications(cycle_id="cycle_pair_ab", conn=store.conn, limit=40)
    paired_apps = [
        row for row in apps
        if row.get("experiment_id") == experiment_id
        and row.get("seed_id") in {s.seed_id for s in seeds if s.payload.get("market_evolve_pair_id")}
    ]
    assert len(paired_apps) == 8
    for pair_id in pair_ids:
        paired_seed_ids = {
            s.seed_id for s in seeds
            if s.payload.get("market_evolve_pair_id") == pair_id
        }
        arms = {
            row["experiment_arm"]
            for row in paired_apps
            if row["seed_id"] in paired_seed_ids
        }
        assert arms == {"control", "candidate"}


def test_market_evolve_keeps_insufficient_sample_experiments_assignable(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    planning_step = run_market_evolve_step(cycle_id="cycle_more_data_plan", conn=store.conn)
    experiment = planning_step.experiment_plans[0]
    experiment_id = experiment["id"]
    candidate_program_id = experiment["candidate_program_id"]
    seeds = [
        SeedCell(
            seed_id=f"seed_more_data_{i:02d}",
            entity="HYPE",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            theme="validator_unstake",
            payload={"tool_candidates": [f"tic://tool/test/tool_{j}@v1" for j in range(12)]},
        )
        for i in range(8)
    ]
    apply_market_evolve_policy_to_seeds(seeds, cycle_id="cycle_more_data", conn=store.conn)

    step = run_market_evolve_step(cycle_id="cycle_more_data", conn=store.conn)
    assert step.experiment_results
    assert step.experiment_results[0]["decision"] == "insufficient_sample"
    assert store.conn.execute(
        "SELECT status FROM market_evolve_experiments WHERE id = ?",
        (experiment_id,),
    ).fetchone()[0] == "running"
    assert store.conn.execute(
        "SELECT status FROM market_evolve_mutations WHERE child_program_id = ?",
        (candidate_program_id,),
    ).fetchone()[0] == "needs_more_data"

    next_seeds = [
        SeedCell(
            seed_id=f"seed_more_data_next_{i:02d}",
            entity="HYPE",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            theme="validator_unstake",
            payload={"tool_candidates": [f"tic://tool/test/tool_{j}@v1" for j in range(12)]},
        )
        for i in range(80)
    ]
    apply_market_evolve_policy_to_seeds(next_seeds, cycle_id="cycle_more_data_next", conn=store.conn)
    apps = load_market_evolve_policy_applications(cycle_id="cycle_more_data_next", conn=store.conn, limit=200)
    arms = {row["experiment_arm"] for row in apps if row.get("experiment_id") == experiment_id}

    assert {"control", "candidate"}.issubset(arms)


def test_market_evolve_rejects_losing_candidate_without_resurrecting_lineage(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    planning_step = run_market_evolve_step(cycle_id="cycle_reject_plan", conn=store.conn)
    experiment = planning_step.experiment_plans[0]
    experiment_id = experiment["id"]
    parent_program_id = experiment["parent_program_id"]
    candidate_program_id = experiment["candidate_program_id"]
    seeds = [
        SeedCell(
            seed_id=f"seed_reject_{i:02d}",
            entity="HYPE",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            theme="validator_unstake",
            payload={"tool_candidates": [f"tic://tool/test/tool_{j}@v1" for j in range(12)]},
        )
        for i in range(80)
    ]
    apply_market_evolve_policy_to_seeds(seeds, cycle_id="cycle_reject", conn=store.conn)
    apps = load_market_evolve_policy_applications(cycle_id="cycle_reject", conn=store.conn, limit=200)
    control_apps = [
        row for row in apps
        if row.get("experiment_id") == experiment_id and row.get("experiment_arm") == "control"
    ][:6]
    candidate_apps = [
        row for row in apps
        if row.get("experiment_id") == experiment_id and row.get("experiment_arm") == "candidate"
    ]
    assert len(control_apps) >= 3
    assert len(candidate_apps) >= 20
    families = ["hydromancer", "market_microstructure", "our_hl_node"]
    for i, app in enumerate(control_apps):
        family = families[i % len(families)]
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_reject",
            scout_id=f"scout_reject_control_{i}",
            seed_id=app["seed_id"],
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"Control policy found {family}",
                    thesis="Control policy finds source-diverse HYPE route intelligence before candidate.",
                    mechanism="Source-family breadth gives verifier-ready route context.",
                    expected_outcome="Liquidity absorption or sellable route becomes observable intraday.",
                    kill_signal="No movement to sellable liquidity or strong informed absorption.",
                    conviction=0.86,
                    novelty_score=0.80,
                    crowdedness=0.20,
                    entities_chain=["HYPE", "aHYPE", "route quality"],
                    depth_layers=[{"layer": 1, "claim": "route"}, {"layer": 2, "claim": "actor quality"}],
                    evidence_refs=[f"tic://source/{family}/control"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            source_tool_call_ids=[f"tc_reject_control_{i}_{family}"],
        )

    step = run_market_evolve_step(cycle_id="cycle_reject", conn=store.conn)
    results = load_market_evolve_experiment_results(cycle_id="cycle_reject", conn=store.conn)

    assert step.experiment_results
    result = next(row for row in results if row["experiment_id"] == experiment_id)
    assert result["decision"] == "reject_candidate"
    assert result["score_delta"] < 0
    assert result["falsification_gate_results"]
    assert any(
        flag.startswith("proof_gate_triggered:")
        for flag in result["quality_flags"]
    )
    programs = {
        p.program_id: p
        for p in load_market_evolve_programs(status="", limit=100, conn=store.conn)
    }
    assert programs[parent_program_id].status == "active"
    assert programs[candidate_program_id].status == "rejected"
    assert "rejected_by_market_evolve_experiment" in programs[candidate_program_id].quality_flags
    mutation_status = store.conn.execute(
        "SELECT status FROM market_evolve_mutations WHERE child_program_id = ?",
        (candidate_program_id,),
    ).fetchone()[0]
    assert mutation_status == "rejected"
    assert all(child.program_id != candidate_program_id for child in step.child_programs)
    reused = evaluate_market_evolve_experiments(cycle_id="cycle_reject", conn=store.conn)
    reused_by_experiment = {row["experiment_id"]: row for row in reused}
    assert reused_by_experiment[experiment_id]["id"] == result["id"]
    assert "existing_experiment_result_reused" in reused_by_experiment[experiment_id]["quality_flags"]


def test_market_evolve_rewards_learned_tool_usage_and_mutates_tool_surface(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    families = ["hydromancer", "market_microstructure", "our_hl_node"]
    for i, family in enumerate(families):
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_tool_surface",
            scout_id=f"scout_tool_surface_{i}",
            seed_id=f"seed_tool_surface_{i}",
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"Learned tool surface found {family}",
                    thesis="Learned node and market tools improve HYPE route intelligence.",
                    mechanism="Learned tools convert source gaps into direct evidence calls.",
                    expected_outcome="Route state and actor quality become observable intraday.",
                    kill_signal="No route movement or no actor-quality confirmation.",
                    conviction=0.86,
                    novelty_score=0.80,
                    crowdedness=0.20,
                    entities_chain=["HYPE", "aHYPE", "learned tool surface"],
                    depth_layers=[{"layer": 1, "claim": "route"}, {"layer": 2, "claim": "actor quality"}],
                    evidence_refs=[f"tic://source/{family}/learned"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            coverage_cell_key="HYPE|intraday|on_chain|frontier|validator_unstake",
            source_tool_call_ids=[f"tc_tool_surface_{i}_{family}"],
        )
        now = "2026-05-21T12:00:00+00:00"
        store.conn.execute(
            """
            INSERT OR REPLACE INTO tool_call_log (
                id, cycle_id, investigation_id, specialist_id, tool_uri,
                tool_version, args_hash, args_json, result_hash,
                result_summary, error, started_at, finished_at, duration_ms,
                cost_usd, quality_flags, valid_from, transaction_from
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"tc_learned_surface_{i}",
                "cycle_tool_surface",
                f"inv_tool_surface_{i}",
                "tier1_scout",
                "tic://tool/learned/hl_node_stream_reader@v1",
                "v1",
                f"args_{i}",
                json.dumps({"coin": "HYPE"}),
                f"result_{i}",
                json.dumps({"status": "ok", "n_observations": 2}),
                None,
                now,
                now,
                4,
                0.0001,
                json.dumps([]),
                now,
                now,
            ),
        )
    proposals = propose_tools_from_quality_flags(
        cycle_id="cycle_tool_surface",
        artifact_kind="node_intelligence",
        artifact_id="nint_tool_surface",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        quality_flags=["missing_our_hl_node"],
    )
    ids = persist_analysis_tool_proposals(proposals, conn=store.conn)
    store.conn.execute(
        "UPDATE analysis_tool_proposals SET status = 'active' WHERE id = ?",
        (ids[0],),
    )
    store.conn.commit()

    step = run_market_evolve_step(cycle_id="cycle_tool_surface", conn=store.conn)

    assert step.best_evaluation is not None
    assert step.best_evaluation.metrics["tool_activation_rate"] == 1.0
    assert step.best_evaluation.metrics["learned_tool_success_rate"] == 1.0
    assert any(m.mutation_kind == "exploit_learned_tool_surface" for m in step.mutations)
    child = next(
        c for c in step.child_programs
        if "mutation:exploit_learned_tool_surface" in c.quality_flags
    )
    policy = child.genome["tool_request_policy"]
    assert policy["prefer_learned_tools"] is True
    assert policy["learned_tool_priority_boost"] >= 1.0


def test_market_evolve_raises_tool_promotion_discipline_when_tool_backlog_stalls(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    families = ["hydromancer", "market_microstructure", "our_hl_node"]
    for i, family in enumerate(families):
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_tool_backlog",
            scout_id=f"scout_tool_backlog_{i}",
            seed_id=f"seed_tool_backlog_{i}",
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"Tool backlog string {family}",
                    thesis="HYPE route intelligence needs several new source surfaces.",
                    mechanism="The map is good enough to identify gaps, but proposals are stuck before active-tool status.",
                    expected_outcome="Tool promotion discipline should increase before more broad scout spend.",
                    kill_signal="Pending proposals activate or prove low value.",
                    conviction=0.80,
                    novelty_score=0.76,
                    crowdedness=0.25,
                    entities_chain=["HYPE", "tool backlog", "source gaps"],
                    depth_layers=[{"layer": 1, "claim": "gap"}, {"layer": 2, "claim": "tool promotion"}],
                    evidence_refs=[f"tic://source/{family}/backlog"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            coverage_cell_key="HYPE|intraday|on_chain|frontier|validator_unstake",
            source_tool_call_ids=[f"tc_tool_backlog_{i}_{family}"],
        )
    proposals = []
    for flags in (
        ["missing_our_hl_node"],
        ["missing_hydromancer"],
        ["missing_liquidity_context"],
    ):
        proposals.extend(propose_tools_from_quality_flags(
            cycle_id="cycle_tool_backlog",
            artifact_kind="node_intelligence",
            artifact_id=f"nint_tool_backlog_{len(proposals)}",
            entity="HYPE",
            horizon="intraday",
            lens="on_chain",
            quality_flags=flags,
        ))
    persist_analysis_tool_proposals(proposals, conn=store.conn)

    step = run_market_evolve_step(cycle_id="cycle_tool_backlog", conn=store.conn)

    assert step.best_evaluation is not None
    assert step.best_evaluation.metrics["tool_proposal_count"] >= 3
    assert step.best_evaluation.metrics["tool_activation_rate"] == 0.0
    mutation = next(m for m in step.mutations if m.mutation_kind == "raise_tool_promotion_discipline")
    assert mutation.mutation["tool_request_policy"]["auto_promote_high_priority_tools"] is True
    child = next(c for c in step.child_programs if c.program_id == mutation.child_program_id)
    assert child.genome["tool_request_policy"]["max_tool_promotions_per_cycle"] >= 4


def test_market_evolve_routes_runtime_adapter_backlog_to_adapter_builds(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    families = ["hydromancer", "market_microstructure", "our_hl_node"]
    for i, family in enumerate(families):
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_runtime_adapter_backlog",
            scout_id=f"scout_adapter_backlog_{i}",
            seed_id=f"seed_adapter_backlog_{i}",
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"Adapter backlog string {family}",
                    thesis="HYPE node intelligence has a useful tool idea that needs a runtime adapter before it can enter the atlas.",
                    mechanism="The market map found the edge, but the learned-tool lifecycle needs executable source access and fixtures.",
                    expected_outcome="MarketEvolve should prioritize adapter construction rather than widen scouts blindly.",
                    kill_signal="The tool idea fails EV checks or an existing adapter covers the source.",
                    conviction=0.84,
                    novelty_score=0.78,
                    crowdedness=0.22,
                    entities_chain=["HYPE", "runtime adapter", "learned tool surface"],
                    depth_layers=[{"layer": 1, "claim": "tool gap"}, {"layer": 2, "claim": "adapter build"}],
                    evidence_refs=[f"tic://source/{family}/adapter"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            coverage_cell_key="HYPE|intraday|on_chain|frontier|validator_unstake",
            source_tool_call_ids=[f"tc_adapter_backlog_{i}_{family}"],
        )
    proposal = AnalysisToolProposal(
        cycle_id="cycle_runtime_adapter_backlog",
        artifact_kind="information_synthesis",
        artifact_id="isyn_adapter_gap",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        proposal_kind="runtime_adapter",
        tool_name="blank_alpha_scryer",
        purpose="Build a real runtime adapter for a high-EV source edge the map cannot currently execute.",
        source_family="node_discovery",
        trigger="runtime_adapter_missing",
        input_shape={"asset": "HYPE", "source_refs": ["tc_..."]},
        promotion_gate={
            "expected_edge": "node_discovery -> causal market map edge",
            "expected_info_value": 0.84,
            "would_change_decision": True,
            "runtime_adapter_exists": True,
        },
        eval_plan={"fixtures": ["adapter_fixture"], "min_pass_rate": 0.85},
        priority="high",
        status="needs_runtime_adapter",
        quality_flags=["runtime_adapter_missing"],
    )
    persist_analysis_tool_proposals([proposal], conn=store.conn)

    step = run_market_evolve_step(cycle_id="cycle_runtime_adapter_backlog", conn=store.conn)

    assert step.best_evaluation is not None
    assert step.best_evaluation.metrics["runtime_adapter_backlog_count"] == 1.0
    assert step.best_evaluation.metrics["runtime_adapter_backlog_rate"] == 1.0
    mutation = next(m for m in step.mutations if m.mutation_kind == "build_runtime_adapter_surface")
    policy = mutation.mutation["tool_request_policy"]
    assert policy["runtime_adapter_backlog_priority"] == "high"
    assert policy["require_runtime_adapter_eval_fixtures"] is True
    child = next(c for c in step.child_programs if c.program_id == mutation.child_program_id)
    assert child.genome["tool_request_policy"]["max_runtime_adapter_builds_per_cycle"] == 1


def test_market_evolve_penalizes_low_value_harness_tool_leases(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    families = ["hydromancer", "market_microstructure", "our_hl_node"]
    for i, family in enumerate(families):
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_low_ev_lease",
            scout_id=f"scout_low_ev_lease_{i}",
            seed_id=f"seed_low_ev_lease_{i}",
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"Lease quality string {family}",
                    thesis="HYPE route intelligence is source-diverse, but weak tool leases should be punished.",
                    mechanism="The map has enough evidence to see whether requested tools are decision-changing.",
                    expected_outcome="Low-value tool requests tighten the EV gate before more tool creation.",
                    kill_signal="Tool requests supply a real edge and credible eval plan.",
                    conviction=0.82,
                    novelty_score=0.76,
                    crowdedness=0.24,
                    entities_chain=["HYPE", "tool lease", "source gaps"],
                    depth_layers=[{"layer": 1, "claim": "lease"}, {"layer": 2, "claim": "tool EV"}],
                    evidence_refs=[f"tic://source/{family}/lease"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            coverage_cell_key="HYPE|intraday|on_chain|frontier|validator_unstake",
            source_tool_call_ids=[f"tc_low_ev_lease_{i}_{family}"],
        )
    weak = AnalysisToolProposal(
        cycle_id="cycle_low_ev_lease",
        artifact_kind="scout_output",
        artifact_id="scout_low_ev_lease_tool_request",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        proposal_kind="next_tool_call",
        tool_name="extra_unstake_context",
        purpose="Maybe collect more context.",
        source_family="hydromancer",
        trigger="scout_tool_request",
        input_shape={"asset": "HYPE"},
        promotion_gate={
            "expected_edge": "",
            "expected_info_value": 0.10,
            "would_change_decision": False,
        },
        eval_plan={},
        priority="high",
    )
    assert not evaluate_analysis_tool_proposal(weak).passed
    persist_analysis_tool_proposals([weak], conn=store.conn)

    step = run_market_evolve_step(cycle_id="cycle_low_ev_lease", conn=store.conn)

    assert step.best_evaluation is not None
    assert step.best_evaluation.metrics["low_ev_tool_rate"] == 1.0
    assert any(m.mutation_kind == "tighten_tool_ev" for m in step.mutations)
    rows = load_analysis_tool_proposals(cycle_id="cycle_low_ev_lease", conn=store.conn)
    flags = rows[0]["quality_flags"]
    assert "tool_proposal_low_expected_info_value" in flags
    assert "tool_proposal_would_not_change_decision" in flags


def test_market_evolve_scores_cycle_and_proposes_policy_mutation(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    import talis_desk.store as store_mod

    store_mod._STORE = store
    families = ["hydromancer", "market_microstructure", "our_hl_node"]
    for i, family in enumerate(families):
        persist_information_strings(
            conn=store.conn,
            cycle_id="cycle_evolve",
            scout_id=f"scout_evolve_{i}",
            seed_id=f"seed_evolve_{i}",
            entity="HYPE",
            theme="validator_unstake",
            horizon="intraday",
            lens="on_chain",
            bias_mode="frontier",
            strings=[
                InformationString(
                    title=f"HYPE node route {family}",
                    thesis="HYPE reprices if unstake flow reaches sellable liquidity before quality actors absorb it.",
                    mechanism="Node, route, and wallet-quality evidence combine into a tradable supply-pressure test.",
                    expected_outcome="Liquidity path and informed-wallet absorption decide intraday pressure.",
                    kill_signal="No transfer, benign restake, or strong absorption by known profitable actors.",
                    conviction=0.84,
                    novelty_score=0.78,
                    crowdedness=0.22,
                    entities_chain=["HYPE", "aHYPE", "node route", "sellable liquidity"],
                    depth_layers=[{"layer": 1, "claim": "unstake"}, {"layer": 2, "claim": "route quality"}],
                    evidence_refs=[f"tic://source/{family}/sample"],
                    quality_flags=[f"source_family:{family}", "adversarial_decision:allow"],
                )
            ],
            coverage_cell_key="HYPE|intraday|on_chain|frontier|validator_unstake",
            source_tool_call_ids=[f"tc_evolve_{i}"],
        )
        now = "2026-05-21T12:00:00+00:00"
        store.conn.execute(
            """
            INSERT OR REPLACE INTO hypotheses (
                id, cycle_id, specialist_id, title, hypothesis_text,
                status, valid_from, transaction_from
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"hyp_evolve_{i}",
                "cycle_evolve",
                "scout",
                "HYPE route",
                "HYPE reprices on sellable unstake liquidity.",
                "active",
                now,
                now,
            ),
        )
    compute_alpha_geometry(cycle_id="cycle_evolve", conn=store.conn)
    store.conn.execute(
        """
        INSERT OR REPLACE INTO claim_votes (
            id, claim_id, cycle_id, verifier_agent_id, vote, confidence,
            rationale, voted_at, valid_from, transaction_from
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "vote_evolve_1",
            "claim_evolve_1",
            "cycle_evolve",
            "verifier_alpha",
            "pass",
            0.82,
            "Independent source families support the route thesis.",
            "2026-05-21T12:02:00+00:00",
            "2026-05-21T12:02:00+00:00",
            "2026-05-21T12:02:00+00:00",
        ),
    )

    step = run_market_evolve_step(cycle_id="cycle_evolve", conn=store.conn)

    assert step.evaluations
    assert step.best_evaluation is not None
    assert step.best_evaluation.score > 0.4
    assert step.best_evaluation.metrics["valid_string_rate"] == 1.0
    assert step.mutations
    assert step.child_programs
    assert step.experiment_plans
    experiments = load_market_evolve_experiments(cycle_id="cycle_evolve", conn=store.conn)
    assert experiments[0]["status"] == "planned"
    assert experiments[0]["experiment_kind"] == "matched_policy_ab"
    assert experiments[0]["success_criteria"]["primary_metric"] == "accepted_unique_high_quality_coverage_per_dollar"
    assert experiments[0]["success_criteria"]["mutation_intent"]["schema_version"] == "market_evolve_mutation_proof_v1"
    assert experiments[0]["success_criteria"]["mutation_target_metrics"]
    assert experiments[0]["success_criteria"]["falsification_gates"]
    assert "evolution_proof_required" in experiments[0]["success_criteria"]["promotion_requires"]
    assert load_market_evolve_programs(status="active", conn=store.conn)
    assert store.conn.execute("SELECT COUNT(*) FROM market_evolve_programs").fetchone()[0] >= 2
    assert store.conn.execute("SELECT COUNT(*) FROM market_evolve_evaluations").fetchone()[0] >= 1
    assert store.conn.execute("SELECT COUNT(*) FROM market_evolve_mutations").fetchone()[0] >= 1
    assert store.conn.execute("SELECT COUNT(*) FROM market_evolve_experiments").fetchone()[0] >= 1


def test_scout_node_intelligence_wiring_and_hydromancer_args():
    seed = SeedCell(
        seed_id="seed_node",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        theme="validator_unstake",
    )
    leaderboard_args = _infer_tool_args("tic://tool/hydromancer/get_hl_pnl_leaderboard@v1", seed)
    builder_args = _infer_tool_args("tic://tool/hydromancer/get_builder_fills@v1", seed)
    assert leaderboard_args["top_n"] == 25
    assert builder_args["lookback_hours"] == 24
    parsed = {
        "hypothesis": "HYPE node intelligence changes if informed wallets absorb the unstake.",
        "confidence": 0.64,
        "rationale_brief": "Hydromancer plus node rejects separate good actors from noisy flow.",
        "suggested_tools": ["tic://tool/hydromancer/get_hl_pnl_leaderboard@v1"],
        "information_strings": [],
    }
    snapshots = _node_snapshots_for_scout(
        parsed=parsed,
        seed=seed,
        cycle_id="cycle_hype",
        tool_evidence=[
            {
                "uri": "tic://tool/hydromancer/get_hl_pnl_leaderboard@v1",
                "ok": True,
                "tool_call_log_id": "tc_hydro",
                "result": {"leaders": [{"rank": 1, "wallet": "0xabc", "realized_pnl_usd": 1000, "volume_usd": 100000}]},
            },
            {
                "uri": "tic://tool/builtin/hl_reject_corpus@v1",
                "ok": True,
                "tool_call_log_id": "tc_node",
                "result": {"wallet": "0xabc", "reject_rate_pct": 2.0, "status_counts": {"filled": 10}},
            },
        ],
    )
    assert len(snapshots) == 1
    assert "from_tool_evidence_node_intelligence" in snapshots[0].quality_flags
    assert any(obs.category == "hydromancer_leaderboard" for obs in snapshots[0].observations)
    assert snapshots[0].coverage.get("tool_proposals")


def test_analysis_tool_proposals_apply_to_all_analysis_and_iterate(tmp_path):
    store = DeskStore(db_path=tmp_path / "desk.db")
    proposals = propose_tools_from_quality_flags(
        cycle_id="cycle_tools",
        artifact_kind="research_report",
        artifact_id="report_1",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        quality_flags=[
            "missing_liquidity_context",
            "missing_source_refs",
            "missing_hydromancer",
        ],
    )
    names = {p.tool_name for p in proposals}
    assert "liquidity_absorption_context" in names
    assert "evidence_ref_resolver" in names
    assert "hydromancer_actor_quality_bulk" in names
    ids = persist_analysis_tool_proposals(proposals, conn=store.conn)
    assert len(ids) == len(proposals)
    parent = proposals[0]
    parent.proposal_id = ids[0]
    improved = iterate_tool_proposal(
        parent,
        critique_flags=["needs_block_height_or_raw_offset"],
        improvement_note="Add raw source offsets and before/after sample windows.",
        promotion_gate_delta={"requires_raw_offsets": True},
    )
    improved_ids = persist_analysis_tool_proposals([improved], conn=store.conn)
    rows = load_analysis_tool_proposals(cycle_id="cycle_tools", conn=store.conn)
    assert improved_ids[0] in {r["id"] for r in rows}
    assert any(r["parent_proposal_id"] == ids[0] and r["iteration"] == 1 for r in rows)
    assert store.conn.execute("SELECT COUNT(*) FROM analysis_tool_proposals").fetchone()[0] == len(proposals) + 1


def test_analysis_tool_proposal_promotes_learned_tool_and_dispatches(monkeypatch, tmp_path):
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    monkeypatch.setenv("TALIS_LEARNED_TOOLS_DIR", str(tmp_path / "learned_tools"))
    proposals = propose_tools_from_quality_flags(
        cycle_id="cycle_learned_tool",
        artifact_kind="node_intelligence",
        artifact_id="nint_gap",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        quality_flags=["missing_our_hl_node"],
    )
    ids = persist_analysis_tool_proposals(proposals, conn=store.conn)
    proposal_id = next(
        pid for pid, proposal in zip(ids, proposals)
        if proposal.tool_name == "hl_node_stream_reader"
    )

    promotion = promote_analysis_tool_proposal(proposal_id, conn=store.conn)

    assert promotion.passed
    assert promotion.tool_uri == "tic://tool/learned/hl_node_stream_reader@v1"
    assert (tmp_path / "learned_tools" / "hl_node_stream_reader" / "manifest.json").exists()
    assert store.conn.execute(
        "SELECT status FROM analysis_tool_proposals WHERE id = ?",
        (proposal_id,),
    ).fetchone()[0] == "active"
    assert store.conn.execute(
        "SELECT COUNT(*) FROM tool_atlas WHERE tool_uri = ? AND status = 'active' AND transaction_to IS NULL",
        (promotion.tool_uri,),
    ).fetchone()[0] == 1

    result = dispatch_uri(
        promotion.tool_uri,
        {"coin": "HYPE", "wallets": ["0xabc"], "lookback_minutes": 90},
        AgentContext(
            cycle_id="cycle_learned_tool",
            specialist_id="learned_tool_test",
            investigation_id="inv_learned_tool",
        ),
    )
    assert result.ok
    assert result.result["source_family"] == "our_hl_node"
    assert result.result["n_observations"] >= 1
    assert result.result["has_raw_offsets"]


def test_learned_tool_preference_reenters_tier0_tool_menu(monkeypatch, tmp_path):
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    monkeypatch.setenv("TALIS_LEARNED_TOOLS_DIR", str(tmp_path / "learned_tools"))
    proposals = propose_tools_from_quality_flags(
        cycle_id="cycle_learned_menu",
        artifact_kind="node_intelligence",
        artifact_id="nint_gap",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        quality_flags=["missing_our_hl_node"],
    )
    ids = persist_analysis_tool_proposals(proposals, conn=store.conn)
    proposal_id = next(
        pid for pid, proposal in zip(ids, proposals)
        if proposal.tool_name == "hl_node_stream_reader"
    )
    promotion = promote_analysis_tool_proposal(proposal_id, conn=store.conn)
    assert promotion.passed

    seed = SeedCell(
        seed_id="seed_learned_menu",
        entity="NVDA",
        horizon="1w",
        lens="filing",
        bias_mode="frontier",
        theme="earnings footnotes",
        payload={
            "prefer_learned_tools": True,
            "learned_tool_priority_boost": 3.0,
            "source_family_targets": ["our_hl_node"],
            "tool_candidate_limit": 4,
        },
    )
    assert promotion.tool_uri in narrow_tools_for_seed(seed)


def test_unknown_learned_tool_runtime_does_not_promote_as_generic_echo(monkeypatch, tmp_path):
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    monkeypatch.setenv("TALIS_LEARNED_TOOLS_DIR", str(tmp_path / "learned_tools"))
    proposals = [
        AnalysisToolProposal(
            cycle_id="cycle_no_generic_learned_tool",
            artifact_kind="information_synthesis",
            artifact_id="isyn_gap",
            entity="HYPE",
            horizon="intraday",
            lens="on_chain",
            tool_name="blank_alpha_scryer",
            purpose="Resolve a proposed edge only if the learned runtime has a real adapter.",
            source_family="unknown_source_family",
            trigger="adversarial_no_stub_audit",
            input_shape={"asset": "string"},
            promotion_gate={
                "expected_edge": "unknown_source_family -> causal market map edge",
                "expected_info_value": 0.9,
                "would_change_decision": True,
            },
            eval_plan={"fixture": "must dispatch through a specialized adapter"},
            priority="high",
        )
    ]
    ids = persist_analysis_tool_proposals(proposals, conn=store.conn)

    promotion = promote_analysis_tool_proposal(ids[0], conn=store.conn)

    assert not promotion.passed
    assert promotion.status == "eval_failed"
    assert promotion.eval_report["dispatch_ok"] is False
    assert "runtime_adapter_missing" in promotion.quality_flags
    assert promotion.eval_report["error"] == "learned_runtime_adapter_missing:blank_alpha_scryer"
    assert promotion.eval_report["next_action"] == "iterate_tool_proposal_with_runtime_adapter"
    assert promotion.iteration_proposal_id
    rows = load_analysis_tool_proposals(cycle_id="cycle_no_generic_learned_tool", conn=store.conn)
    child = next(row for row in rows if row["id"] == promotion.iteration_proposal_id)
    assert child["parent_proposal_id"] == ids[0]
    assert child["proposal_kind"] == "runtime_adapter"
    assert child["status"] == "needs_runtime_adapter"
    assert child["iteration"] == 1
    assert child["eval_plan_json"]["required_runtime_adapter"] == "blank_alpha_scryer"
    assert any("runtime_adapter_missing" in flag for flag in child["quality_flags"])
    assert load_analysis_tool_proposals(
        cycle_id="cycle_no_generic_learned_tool",
        status="proposed",
        conn=store.conn,
    ) == []
    assert store.conn.execute(
        "SELECT COUNT(*) FROM tool_atlas WHERE tool_uri = ? AND status = 'active' AND transaction_to IS NULL",
        (promotion.tool_uri,),
    ).fetchone()[0] == 0

    import run_swarm

    orders = run_swarm._consume_runtime_adapter_backlog(
        tool_policy={
            "runtime_adapter_backlog_priority": "high",
            "max_runtime_adapter_builds_per_cycle": 1,
        },
        cycle_id="cycle_no_generic_learned_tool",
        conn=store.conn,
    )
    assert len(orders) == 1
    assert orders[0].proposal_id == promotion.iteration_proposal_id
    work_order = json.loads(Path(orders[0].work_order_path).read_text())
    assert work_order["adapter"]["runtime"] == "blank_alpha_scryer"
    assert work_order["adapter"]["target_runtime_file"] == "talis_desk/tool_atlas/learned_runtime.py"
    assert "Add runtime to SUPPORTED_LEARNED_RUNTIMES." in work_order["acceptance_checks"]
    updated_child = load_analysis_tool_proposals(
        cycle_id="cycle_no_generic_learned_tool",
        status="adapter_requested",
        conn=store.conn,
    )[0]
    assert updated_child["id"] == promotion.iteration_proposal_id
    assert "runtime_adapter_work_order_created" in updated_child["quality_flags"]

    not_ready = mark_runtime_adapter_ready(
        work_order_path=orders[0].work_order_path,
        conn=store.conn,
    )
    assert not_ready.ready is False
    assert not_ready.status == "adapter_requested"
    assert "runtime_adapter_not_ready" in not_ready.quality_flags
    still_requested = load_analysis_tool_proposals(
        cycle_id="cycle_no_generic_learned_tool",
        status="adapter_requested",
        conn=store.conn,
    )[0]
    assert still_requested["id"] == promotion.iteration_proposal_id

    ready_proposal = AnalysisToolProposal(
        cycle_id="cycle_adapter_ready",
        artifact_kind="node_intelligence",
        artifact_id="nint_adapter_ready",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        proposal_kind="runtime_adapter",
        tool_name="liquidity_absorption_context",
        purpose="Adapter exists and should be routed back to learned-tool eval.",
        source_family="market_microstructure",
        trigger="runtime_adapter_ready",
        input_shape={"asset": "HYPE", "amount": 558100, "depth_1pct_usd": 8000000},
        promotion_gate={
            "expected_edge": "event_amount -> depth absorption",
            "expected_info_value": 0.8,
            "would_change_decision": True,
            "runtime_adapter_exists": True,
        },
        eval_plan={"required_runtime_adapter": "liquidity_absorption_context"},
        priority="high",
        status="needs_runtime_adapter",
    )
    ready_id = persist_analysis_tool_proposals([ready_proposal], conn=store.conn)[0]
    ready_orders = run_swarm._consume_runtime_adapter_backlog(
        tool_policy={
            "runtime_adapter_backlog_priority": "high",
            "max_runtime_adapter_builds_per_cycle": 2,
        },
        cycle_id="cycle_adapter_ready",
        conn=store.conn,
    )
    assert len(ready_orders) == 1
    ready = mark_runtime_adapter_ready(
        work_order_path=ready_orders[0].work_order_path,
        conn=store.conn,
    )
    assert ready.ready is True
    assert ready.status == "proposed"
    proposed = load_analysis_tool_proposals(
        cycle_id="cycle_adapter_ready",
        status="proposed",
        conn=store.conn,
    )[0]
    assert proposed["id"] == ready_id
    assert "runtime_adapter_ready_for_eval" in proposed["quality_flags"]


def test_mempool_tool_proposal_promotes_and_reconciles_fixture(monkeypatch, tmp_path):
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    monkeypatch.setenv("TALIS_LEARNED_TOOLS_DIR", str(tmp_path / "learned_tools"))
    proposals = propose_tools_from_quality_flags(
        cycle_id="cycle_mempool_tool",
        artifact_kind="node_intelligence",
        artifact_id="nint_mempool_gap",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        quality_flags=["missing_mempool"],
    )
    assert any(p.tool_name == "hyperevm_mempool_actor_watch" for p in proposals)
    ids = persist_analysis_tool_proposals(proposals, conn=store.conn)
    proposal_id = next(
        pid for pid, proposal in zip(ids, proposals)
        if proposal.tool_name == "hyperevm_mempool_actor_watch"
    )

    promotion = promote_analysis_tool_proposal(proposal_id, conn=store.conn)
    assert promotion.passed
    result = dispatch_uri(
        promotion.tool_uri,
        {
            "addresses": ["0xabc"],
            "contracts": ["0xrouter"],
            "asset": "HYPE",
            "fixture_events": [
                {
                    "tx_hash": "0xhash",
                    "actor": "0xabc",
                    "contract": "0xrouter",
                    "method": "transfer",
                    "asset": "HYPE",
                    "seen_at": "2026-05-21T10:00:00Z",
                    "settled_event_ref": "tc_settled",
                }
            ],
        },
        AgentContext("cycle_mempool_tool", "learned_tool_test", "inv_mempool_tool"),
    )
    assert result.ok
    assert result.result["source_family"] == "mempool"
    assert result.result["n_pending"] == 1
    assert result.result["settlement_reconciliation"]


def test_node_intelligence_rejects_failed_or_untrusted_node_shapes():
    failed = node_intelligence_from_tool_evidence(
        cycle_id="cycle_bad_node",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        tool_evidence=[
            {
                "uri": "tic://tool/hydromancer/get_hl_pnl_leaderboard@v1",
                "ok": False,
                "tool_call_log_id": "tc_failed_hydro",
                "result": {
                    "leaders": [{"rank": 1, "wallet": "0xabc", "realized_pnl_usd": 1_000_000}],
                },
            }
        ],
    )
    assert failed is None

    untrusted_reject_shape = node_intelligence_from_tool_evidence(
        cycle_id="cycle_bad_node",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        tool_evidence=[
            {
                "uri": "tic://tool/builtin/query_events_recent@v1",
                "ok": True,
                "tool_call_log_id": "tc_social_reject_words",
                "result": {
                    "wallet": "0xabc",
                    "reject_rate_pct": 1.2,
                    "status_counts": {"filled": 20, "rejected": 1},
                    "top_reject_reasons": [["insufficient_margin", 1]],
                },
            }
        ],
    )
    assert untrusted_reject_shape is not None
    assert not score_node_intelligence(untrusted_reject_shape).passed
    assert "missing_our_hl_node" in score_node_intelligence(untrusted_reject_shape).flags
    assert not any(
        obs.category == "node_reject_corpus"
        for obs in untrusted_reject_shape.observations
    )


def test_model_node_intelligence_requires_resolved_source_refs():
    seed = SeedCell(
        seed_id="seed_model_node",
        entity="HYPE",
        horizon="intraday",
        lens="macro",
        bias_mode="frontier",
    )
    snapshots = _node_snapshots_for_scout(
        parsed={
            "node_intelligence": {
                "entity": "HYPE",
                "source_families": ["hydromancer", "our_hl_node"],
                "source_refs": ["fake_model_source"],
                "summary": "Model claims strong node intelligence without evidence.",
                "edge_summary": "This should not become sourced truth.",
                "observations": [
                    {
                        "category": "hydromancer_leaderboard",
                        "label": "leader_rank_1",
                        "actor": "0xabc",
                        "value": "top wallet",
                        "source_ref": "fake_model_source",
                        "source_family": "hydromancer",
                        "confidence": 0.9,
                    },
                    {
                        "category": "node_reject_corpus",
                        "label": "reject_profile",
                        "actor": "0xabc",
                        "value": "clean",
                        "source_ref": "fake_model_source",
                        "source_family": "our_hl_node",
                        "confidence": 0.9,
                    },
                    {
                        "category": "market_state",
                        "label": "HYPE_open_interest",
                        "value": 123,
                        "source_ref": "fake_model_source",
                        "source_family": "our_hl_node",
                        "confidence": 0.9,
                    },
                ],
            }
        },
        seed=seed,
        cycle_id="cycle_model_node",
        tool_evidence=[
            {
                "uri": "tic://tool/hydromancer/get_hl_pnl_leaderboard@v1",
                "ok": True,
                "tool_call_log_id": "tc_real_hydro",
                "result": {"leaders": []},
            }
        ],
    )
    assert snapshots == []


def test_scout_executes_model_requested_tool_iteration(monkeypatch, tmp_path):
    reset_desk_store_for_test(tmp_path / "desk.db")
    cycle_id = "cycle_iterative_scout"

    import talis_desk.tool_atlas as tool_atlas
    from talis_desk.swarm import scout_runner

    dispatched: list[str] = []

    def fake_dispatch(uri, args, context):
        dispatched.append(uri)
        if "query_events_recent" in uri:
            return SimpleNamespace(
                ok=True,
                result={"events": [{"entity": "HYPE", "headline": "HYPE unlock watch"}]},
                error=None,
                tool_call_log_id="tc_seed",
                cost_usd=0.0,
            )
        return SimpleNamespace(
            ok=True,
            result={"points": [{"metric": "HYPE_depth_1pct", "value": 8_500_000}]},
            error=None,
            tool_call_log_id="tc_follow",
            cost_usd=0.0,
        )

    chat_users: list[str] = []

    async def fake_chat(model, system, user, *, max_tokens, fallback=None):
        chat_users.append(user)
        if len(chat_users) == 1:
            return {
                "text": json.dumps({
                    "hypothesis": "HYPE unlock needs market-depth confirmation before verifier spend.",
                    "confidence": 0.52,
                    "rationale_brief": "Event evidence exists, but depth is the missing edge.",
                    "suggested_tools": [
                        "tic://tool/builtin/query_events_recent@v1",
                        "tic://tool/builtin/query_timeseries@v1",
                    ],
                    "tool_requests": [
                        {
                            "tool_uri": "tic://tool/builtin/query_timeseries@v1",
                            "args": {
                                "entity_symbol": "HYPE",
                                "metric_prefix": "depth",
                                "lookback_hours": 24,
                                "limit": 20,
                            },
                            "why": "Confirm whether the unlock is large relative to immediate depth.",
                            "expected_edge": "event_supply -> market_depth_absorption",
                            "priority": "high",
                        }
                    ],
                    "information_strings": [],
                }),
                "model_used": model,
                "provider": "fake",
            }
        return {
            "text": json.dumps({
                "hypothesis": "HYPE only reprices if unlock supply exceeds immediate depth and informed wallets do not absorb.",
                "confidence": 0.68,
                "rationale_brief": "The second tool closes the event-to-depth edge.",
                "suggested_tools": [
                    "tic://tool/builtin/query_events_recent@v1",
                    "tic://tool/builtin/query_timeseries@v1",
                ],
                "tool_requests": [],
                "information_strings": [
                    {
                        "title": "HYPE unlock depth bridge",
                        "thesis": "HYPE sell-pressure risk depends on unlock supply crossing available depth before informed-wallet absorption.",
                        "mechanism": "The event only matters if it can route into sellable liquidity larger than near-touch depth.",
                        "expected_outcome": "Verifier should compare unlock flow, depth, funding, and wallet absorption over the next intraday window.",
                        "time_horizon": "intraday",
                        "time_scale": "intraday",
                        "event_time_start": "2099-05-21T11:30:00Z",
                        "event_time_end": "2099-05-21T13:30:00Z",
                        "observed_at": "2099-05-21T10:00:00Z",
                        "source_time_basis": "event_time",
                        "kill_signal": "Depth remains ample or wallets absorb before route-to-market.",
                        "extends_or_contradicts": "new",
                        "would_change_decision": True,
                        "expires_at": "2099-05-21T13:30:00Z",
                        "crowdedness": 0.42,
                        "conviction": 0.76,
                        "novelty_score": 0.72,
                        "entities_chain": ["HYPE", "unlock supply", "market depth", "informed wallets"],
                        "depth_layers": [
                            {"layer": 1, "claim": "event creates possible supply"},
                            {"layer": 2, "claim": "depth decides whether it can move price"},
                        ],
                        "evidence_refs": ["tc_seed", "tc_follow"],
                        "temporal_confidence": 0.78,
                    }
                ],
            }),
            "model_used": model,
            "provider": "fake",
        }

    tic = types.ModuleType("tic")
    desk = types.ModuleType("tic.desk")
    models = types.ModuleType("tic.desk.models")
    models.chat = fake_chat
    desk.models = models
    tic.desk = desk
    monkeypatch.setitem(sys.modules, "tic", tic)
    monkeypatch.setitem(sys.modules, "tic.desk", desk)
    monkeypatch.setitem(sys.modules, "tic.desk.models", models)
    monkeypatch.setattr(tool_atlas, "dispatch_uri", fake_dispatch)

    seed = SeedCell(
        seed_id="seed_iterative_scout",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        theme="unlock_depth",
        payload={
            "disable_atlas_expansion": True,
            "tool_iteration_limit": 1,
            "tool_candidates": ["tic://tool/builtin/query_events_recent@v1"],
            "expanded_tool_candidates": ["tic://tool/builtin/query_timeseries@v1"],
        },
    )

    out = asyncio.run(scout_runner._run_one_scout(
        seed,
        cycle_id=cycle_id,
        model="deepseek:v4-flash",
        fallback="anthropic:claude-haiku-4-5",
        cost_counter={},
        cost_cap=1.0,
    ))

    assert dispatched == [
        "tic://tool/builtin/query_events_recent@v1",
        "tic://tool/builtin/query_timeseries@v1",
    ]
    assert len(chat_users) == 2
    assert "scout_tool_iteration:" in " ".join(out.quality_flags)
    assert out.tool_iteration_count == 1
    assert any(ev.get("phase") == "tool_request_iteration" for ev in out.tool_evidence)
    assert out.information_string_ids
    assert out.tool_requests == []


def test_scout_tool_dispatch_retries_retryable_read_failure(monkeypatch, tmp_path):
    reset_desk_store_for_test(tmp_path / "desk.db")

    import talis_desk.tool_atlas as tool_atlas
    from talis_desk.swarm import scout_runner

    calls = 0

    def fake_dispatch(uri, args, context):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(
                ok=False,
                result=None,
                error="rate limit 429",
                tool_call_log_id="tc_retry_1",
                cost_usd=0.0,
            )
        return SimpleNamespace(
            ok=True,
            result={"points": [{"metric": "depth", "value": 1}]},
            error=None,
            tool_call_log_id="tc_retry_2",
            cost_usd=0.0,
        )

    monkeypatch.setattr(tool_atlas, "dispatch_uri", fake_dispatch)
    context = tool_atlas.AgentContext(
        cycle_id="cycle_retry",
        specialist_id="tier1_scout",
        investigation_id="scout_retry",
    )

    ev = scout_runner._dispatch_scout_tool(
        "tic://tool/builtin/query_timeseries@v1",
        {"entity_symbol": "HYPE"},
        context,
    )

    assert calls == 2
    assert ev["ok"] is True
    assert ev["attempts"] == 2
    assert ev["retry_errors"][0]["type"] == "rate_limited"


def test_live_scout_smoke_persists_and_monitor_attaches_by_id(monkeypatch, tmp_path):
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    cycle_id = "cycle_live_scout_smoke"

    import talis_desk.tool_atlas as tool_atlas
    from talis_desk.monitor.server import _fetch_scout_rows
    from talis_desk.swarm import scout_runner

    def fake_dispatch(uri, args, context):
        if "query_events_recent" in uri:
            result = {
                "events": [
                    {
                        "entity": "HYPE",
                        "event_type": "unstake",
                        "headline": "Upcoming unstaking aHYPE 558.1K HYPE in 3h.",
                        "event_time": "2026-05-21T11:30:00Z",
                        "metadata": {
                            "label": "aHYPE",
                            "address": "0xabc0000000000000000000000000000000000000",
                            "depth_1pct_ratio": 2.4,
                            "funding_crowding_score": 0.63,
                        },
                    }
                ]
            }
            ref = "tc_events"
        elif "get_hl_pnl_leaderboard" in uri:
            result = {
                "leaders": [
                    {
                        "rank": 1,
                        "wallet": "0xabc0000000000000000000000000000000000000",
                        "realized_pnl_usd": 1_250_000,
                        "win_rate_pct": 68.2,
                        "volume_usd": 25_000_000,
                    }
                ]
            }
            ref = "tc_hydro"
        elif "hl_reject_corpus" in uri:
            result = {
                "wallet": "0xabc0000000000000000000000000000000000000",
                "reject_rate_pct": 1.7,
                "status_counts": {"filled": 116, "rejected": 2},
                "top_reject_reasons": [["insufficient_margin", 2]],
                "source": "our_hl_node",
            }
            ref = "tc_node"
        else:
            result = {
                "points": [
                    {"ts": "2026-05-21T10:00:00Z", "metric": "HYPE_perp_open_interest", "value": 910_000_000},
                    {"ts": "2026-05-21T10:00:00Z", "metric": "HYPE_orderbook_depth_1pct", "value": 8_000_000},
                ]
            }
            ref = "tc_market"
        return SimpleNamespace(ok=True, result=result, error=None, tool_call_log_id=ref, cost_usd=0.0)

    async def fake_chat(model, system, user, *, max_tokens, fallback=None):
        return {
            "text": json.dumps({
                "hypothesis": "HYPE reprices if aHYPE unstake routes to sellable liquidity before informed wallets absorb.",
                "confidence": 0.64,
                "rationale_brief": "Event row plus Hydromancer actor quality and our-node reject state route verifier spend.",
                "suggested_tools": [
                    "tic://tool/builtin/query_events_recent@v1",
                    "tic://tool/hydromancer/get_hl_pnl_leaderboard@v1",
                ],
                "information_strings": [
                    {
                        "title": "HYPE unstake route",
                        "thesis": "HYPE risk rises only if the aHYPE unstake becomes sellable before high-quality node actors absorb it.",
                        "mechanism": "Unlock supply needs route, liquidity, and actor-quality confirmation before it is price-impacting.",
                        "expected_outcome": "Verifier should watch CEX route, order-book absorption, funding, and informed wallet behavior.",
                        "time_horizon": "intraday",
                        "time_scale": "intraday",
                        "event_time_start": "2099-05-21T11:30:00Z",
                        "event_time_end": "2099-05-21T13:30:00Z",
                        "observed_at": "2099-05-21T10:00:00Z",
                        "source_time_basis": "event_time",
                        "kill_signal": "No transfer, benign restake, or strong informed-wallet absorption by T+2h.",
                        "extends_or_contradicts": "new",
                        "would_change_decision": True,
                        "expires_at": "2099-05-21T13:30:00Z",
                        "crowdedness": 0.35,
                        "conviction": 0.74,
                        "novelty_score": 0.78,
                        "entities_chain": ["HYPE", "aHYPE", "sellable liquidity", "informed wallets"],
                        "depth_layers": [
                            {"layer": 1, "claim": "unstake creates potential supply"},
                            {"layer": 2, "claim": "route decides whether supply can hit market"},
                            {"layer": 3, "claim": "node actor quality decides absorption"},
                        ],
                        "evidence_refs": ["tc_events", "tc_hydro", "tc_node", "tc_market"],
                        "temporal_confidence": 0.78,
                    }
                ],
            }),
            "model_used": model,
            "provider": "fake",
        }

    tic = types.ModuleType("tic")
    desk = types.ModuleType("tic.desk")
    models = types.ModuleType("tic.desk.models")
    models.chat = fake_chat
    desk.models = models
    tic.desk = desk
    monkeypatch.setitem(sys.modules, "tic", tic)
    monkeypatch.setitem(sys.modules, "tic.desk", desk)
    monkeypatch.setitem(sys.modules, "tic.desk.models", models)
    monkeypatch.setattr(tool_atlas, "dispatch_uri", fake_dispatch)

    seed = SeedCell(
        seed_id="seed_live_scout",
        entity="HYPE",
        horizon="intraday",
        lens="on_chain",
        bias_mode="frontier",
        theme="validator_unstake",
        payload={
            "tool_candidates": [
                "tic://tool/builtin/query_events_recent@v1",
                "tic://tool/hydromancer/get_hl_pnl_leaderboard@v1",
                "tic://tool/builtin/hl_reject_corpus@v1",
                "tic://tool/builtin/query_timeseries@v1",
            ],
        },
    )
    out = asyncio.run(scout_runner._run_one_scout(
        seed,
        cycle_id=cycle_id,
        model="deepseek:v4-flash",
        fallback="anthropic:claude-haiku-4-5",
        cost_counter={},
        cost_cap=1.0,
    ))
    assert out.hypothesis_id
    assert out.information_string_ids
    assert out.event_intelligence_ids
    assert out.node_intelligence_ids
    assert "node_intelligence_not_promoted" not in out.quality_flags

    rows = _fetch_scout_rows(
        store.conn,
        cycle_id=cycle_id,
        strings=[],
        events=[],
        node_intel=[],
        tool_proposals=[],
        limit=1,
    )
    assert len(rows) == 1
    assert rows[0]["information_strings"]
    assert rows[0]["event_intelligence"]
    assert rows[0]["node_intelligence"]


def test_json_extractor_handles_fences_and_braces_inside_strings():
    text = '''```json
{
  "hypothesis": "A string with literal braces like {quoted} should parse.",
  "confidence": 0.61,
  "information_strings": [{"thesis": "ok", "mechanism": "x"}]
}
```'''
    parsed = _extract_first_json(text)
    assert parsed["confidence"] == 0.61
    assert "{quoted}" in parsed["hypothesis"]
