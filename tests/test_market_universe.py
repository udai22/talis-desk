from __future__ import annotations

import json
import sqlite3
import sys

from talis_desk.market_map.coverage_audit import build_coverage_gap_manifest
from talis_desk.market_map.governor import build_market_map_governor_plan
from talis_desk.market_map.self_healing import (
    execute_market_map_self_healing_tasks,
    post_market_map_self_healing_work_orders,
)
from talis_desk.market_map.universe import build_market_universe
from talis_desk.schema.migrations import get_schema_version
from talis_desk.store import reset_desk_store_for_test
from talis_desk.swarm import seed_generator
import talis_desk.execution.hl_catalog as hl_catalog


def _reset_hl_catalog() -> None:
    with hl_catalog._STATE.lock:
        hl_catalog._STATE.specs = {}
        hl_catalog._STATE.source = "uninitialized"
        hl_catalog._STATE.last_fetch_at = 0.0


def test_market_universe_includes_hyperliquid_snapshot_when_live_meta_disabled(monkeypatch) -> None:
    monkeypatch.setenv("TALIS_DISABLE_HL_META", "1")
    _reset_hl_catalog()

    manifest = build_market_universe(default_entities=["NVDA", "BTC"])
    symbols = set(manifest.entity_symbols())

    assert "NVDA" in symbols
    assert "BTC" in symbols
    assert "ETH" in symbols
    assert manifest.source_counts["hyperliquid_info_meta"] >= 4
    assert manifest.source_quality == "snapshot_only"


def test_seed_generator_uses_tradeable_hyperliquid_universe(monkeypatch) -> None:
    monkeypatch.setenv("TALIS_DISABLE_HL_META", "1")
    _reset_hl_catalog()

    seed_generator._HL_PERP_ENTITY_CACHE = None
    pool = seed_generator._resolve_seed_entity_pool()

    assert {"BTC", "ETH", "SOL", "HYPE"} <= {x.upper() for x in pool}
    seeds = seed_generator.generate_seeds(
        n_seeds=4,
        cycle_id="cycle_universe_test",
        entities=["BTC"],
        rng_seed=7,
    )
    assert all(seed.to_payload()["asset_class"] == "hyperliquid_perp" for seed in seeds)
    assert "smart_money" in seed_generator.valid_lenses_for_entity("BTC")


def test_coverage_gap_manifest_compares_known_lattice_to_covered_cells(monkeypatch) -> None:
    monkeypatch.setenv("TALIS_DISABLE_HL_META", "1")
    _reset_hl_catalog()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE coverage_cells (
            cell_key TEXT PRIMARY KEY,
            entity TEXT,
            horizon TEXT,
            lens TEXT,
            source TEXT,
            bias_mode TEXT,
            theme TEXT,
            n_samples INTEGER,
            n_promoted INTEGER,
            n_killed INTEGER,
            novelty_score REAL,
            density_score REAL,
            expected_value_usd REAL,
            last_sampled_at TEXT,
            last_promoted_at TEXT,
            payload TEXT,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO coverage_cells (
            cell_key, entity, horizon, lens, source, bias_mode, n_samples,
            n_promoted, n_killed, last_sampled_at, payload, transaction_to
        ) VALUES (
            'cell_btc', 'BTC', 'intraday', 'on_chain', 'tier0_seed',
            'frontier', 1, 0, 0, '2026-05-22T00:00:00+00:00', '{}', NULL
        )
        """
    )

    manifest = build_coverage_gap_manifest(cycle_id="cycle_test", conn=conn)

    assert manifest["grid"]["valid_cell_count"] > 0
    assert manifest["coverage"]["covered_count"] == 1
    assert manifest["coverage"]["missing_count"] == manifest["grid"]["valid_cell_count"] - 1
    assert manifest["gap_examples"]


def test_market_map_governor_ranks_gaps_and_builds_llm_context(monkeypatch) -> None:
    monkeypatch.setenv("TALIS_DISABLE_HL_META", "1")
    _reset_hl_catalog()
    seed_generator._HL_PERP_ENTITY_CACHE = None
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE coverage_cells (
            cell_key TEXT PRIMARY KEY,
            entity TEXT,
            horizon TEXT,
            lens TEXT,
            source TEXT,
            bias_mode TEXT,
            theme TEXT,
            n_samples INTEGER,
            n_promoted INTEGER,
            n_killed INTEGER,
            novelty_score REAL,
            density_score REAL,
            expected_value_usd REAL,
            last_sampled_at TEXT,
            last_promoted_at TEXT,
            payload TEXT,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO coverage_cells (
            cell_key, entity, horizon, lens, source, bias_mode, n_samples,
            n_promoted, n_killed, last_sampled_at, payload, transaction_to
        ) VALUES (
            'cell_hype_covered', 'HYPE', 'intraday', 'on_chain', 'tier0_seed',
            'frontier', 1, 0, 0, '2026-05-22T00:00:00+00:00', '{}', NULL
        )
        """
    )
    coverage = build_coverage_gap_manifest(cycle_id="cycle_governor", conn=conn)

    plan = build_market_map_governor_plan(
        cycle_id="cycle_governor",
        conn=conn,
        coverage_manifest=coverage,
        scout_budget=100,
        max_ranked_gaps=16,
        max_seed_cells=8,
    )

    assert plan["schema_version"] == "market_map_governor_v1"
    assert plan["status"] == "ready"
    assert plan["full_market_definition"]["valid_cell_count"] == coverage["grid"]["valid_cell_count"]
    assert plan["coverage_state"]["missing_count"] == coverage["coverage"]["missing_count"]
    assert plan["completion_pressure"] == "bootstrap_unmapped_market"
    assert len(plan["ranked_gaps"]) == 16
    assert len(plan["suggested_seed_cells"]) == 8
    assert sum(lane["scout_count"] for lane in plan["budget_lanes"]) == 100
    assert "context_packet" in plan["llm_governor"]["prompt"]
    assert any(
        "our_hl_node" in gap["missing_surfaces"] or "hydromancer_actor_graph" in gap["missing_surfaces"]
        for gap in plan["ranked_gaps"]
    )
    assert all(
        not (
            seed["entity"] == "HYPE"
            and seed["horizon"] == "intraday"
            and seed["lens"] == "on_chain"
            and seed["bias_mode"] == "frontier"
        )
        for seed in plan["suggested_seed_cells"]
    )


def test_market_map_governor_handles_empty_alpha_geometry_context(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TALIS_DISABLE_HL_META", "1")
    _reset_hl_catalog()
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    get_schema_version(store.conn)

    plan = build_market_map_governor_plan(
        cycle_id="cycle_empty_geometry_governor",
        conn=store.conn,
        scout_budget=4,
        max_ranked_gaps=4,
        max_seed_cells=2,
        use_llm=False,
    )

    geometry_context = plan["alpha_geometry_context"]
    assert geometry_context["status"] in {"empty", "ready"}
    assert isinstance(geometry_context["route_directives"], list)
    assert isinstance(geometry_context["action_plan"].get("actions"), list)


def test_frontier_llm_governor_sees_geometry_and_promotes_gated_seed(monkeypatch) -> None:
    monkeypatch.setenv("TALIS_DISABLE_HL_META", "1")
    _reset_hl_catalog()
    seed_generator._HL_PERP_ENTITY_CACHE = None
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE coverage_cells (
            cell_key TEXT PRIMARY KEY,
            entity TEXT,
            horizon TEXT,
            lens TEXT,
            source TEXT,
            bias_mode TEXT,
            theme TEXT,
            n_samples INTEGER,
            n_promoted INTEGER,
            n_killed INTEGER,
            novelty_score REAL,
            density_score REAL,
            expected_value_usd REAL,
            last_sampled_at TEXT,
            last_promoted_at TEXT,
            payload TEXT,
            transaction_to TEXT
        )
        """
    )

    seen_prompts: list[str] = []

    async def fake_chat(model, system, user, *, max_tokens, fallback):
        seen_prompts.append(user)
        assert "alpha_geometry_state" in user
        assert "action_plan" in user
        return {
            "text": json.dumps({
                "market_state_assessment": {
                    "coverage_posture": "bootstrap",
                    "frontier_thesis": "Node-native HYPE cells should be repaired first.",
                    "highest_value_blind_zones": ["HYPE on-chain actor route"],
                    "why_this_is_not_complete": "Coverage is missing source-family edges.",
                },
                "routing_adjustments": [
                    {
                        "cell": {
                            "entity": "HYPE",
                            "horizon": "intraday",
                            "lens": "smart_money",
                            "bias_mode": "frontier",
                        },
                        "reason": "The map shape points to actor-quality and source-family uncertainty.",
                        "scout_count": 3,
                        "required_surfaces": ["hydromancer_actor_graph", "our_hl_node"],
                        "expected_edges": ["wallet -> pnl", "node -> source_ref"],
                        "stop_condition": "Stop once actor quality and node state agree or contradict.",
                    }
                ],
                "universe_expansions": [],
                "tool_requests": [],
                "strategic_questions": [],
            }),
            "model_used": model,
            "provider": "fake",
        }

    fake = type(sys)("tic.desk.models")  # type: ignore
    fake.chat = fake_chat
    monkeypatch.setitem(sys.modules, "tic.desk.models", fake)

    plan = build_market_map_governor_plan(
        cycle_id="cycle_frontier_llm_governor",
        conn=conn,
        scout_budget=100,
        max_ranked_gaps=16,
        max_seed_cells=8,
        use_llm=True,
        model="anthropic:claude-opus-4-7",
    )

    promoted = [
        seed for seed in plan["suggested_seed_cells"]
        if (seed.get("payload") or {}).get("source") == "frontier_llm_governor"
    ]
    assert seen_prompts
    assert plan["llm_governor"]["promoted_seed_count"] == 1
    assert promoted
    assert promoted[0]["entity"] == "HYPE"
    assert promoted[0]["lens"] == "smart_money"
    assert promoted[0]["payload"]["frontier_llm_requested_scout_count"] == 3
    assert "hydromancer_actor_graph" in promoted[0]["payload"]["missing_surfaces"]
    assert "action_plan" in plan["alpha_geometry_context"]
    assert isinstance(plan["alpha_geometry_context"]["action_plan"].get("actions"), list)


def test_governor_gap_plan_becomes_replayable_seed_cells(monkeypatch) -> None:
    monkeypatch.setenv("TALIS_DISABLE_HL_META", "1")
    _reset_hl_catalog()
    seed_generator._HL_PERP_ENTITY_CACHE = None
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE coverage_cells (
            cell_key TEXT PRIMARY KEY,
            entity TEXT,
            horizon TEXT,
            lens TEXT,
            source TEXT,
            bias_mode TEXT,
            theme TEXT,
            n_samples INTEGER,
            n_promoted INTEGER,
            n_killed INTEGER,
            novelty_score REAL,
            density_score REAL,
            expected_value_usd REAL,
            last_sampled_at TEXT,
            last_promoted_at TEXT,
            payload TEXT,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO coverage_cells (
            cell_key, entity, horizon, lens, source, bias_mode, n_samples,
            n_promoted, n_killed, last_sampled_at, payload, transaction_to
        ) VALUES (
            'cell_hype_covered', 'HYPE', 'intraday', 'on_chain', 'tier0_seed',
            'frontier', 1, 0, 0, '2026-05-22T00:00:00+00:00', '{}', NULL
        )
        """
    )

    seeds = seed_generator.generate_market_map_governor_seeds(
        cycle_id="cycle_governor_seed",
        n_seed_budget=100,
        max_seeds=6,
        conn=conn,
    )

    assert len(seeds) == 6
    assert all(seed.payload["source"] == "market_map_governor" for seed in seeds)
    assert all(seed.payload.get("gap_id") for seed in seeds)
    assert all(seed.payload.get("tool_candidates") for seed in seeds)
    assert all(seed.payload.get("market_map_valid_cell_count") for seed in seeds)
    assert all(seed.theme == "market_map_gap_repair" for seed in seeds)
    assert all(seed.to_payload()["asset_class"] == "hyperliquid_perp" for seed in seeds)
    assert all(
        not (
            seed.entity == "HYPE"
            and seed.horizon == "intraday"
            and seed.lens == "on_chain"
            and seed.bias_mode == "frontier"
        )
        for seed in seeds
    )


def test_self_healing_orders_post_idempotent_task_contracts(tmp_path) -> None:
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    get_schema_version(store.conn)
    plan = {
        "schema_version": "market_map_self_healing_v1",
        "cycle_id": "cycle_self_heal_dispatch",
        "status": "ready",
        "work_orders": [
            {
                "order_id": "mwo_route_followup",
                "owner": "seed_router",
                "priority": "high",
                "action": "allocate_widen_scouts",
                "reason": "Alpha geometry requested an independent scout route.",
                "input_refs": ["istr_route_1"],
                "expected_output": "Follow-up SeedCell batch with geometry source refs.",
                "success_criteria": [
                    "Every generated seed points back to the source geometry cell.",
                    "The follow-up changes a specific geometry coordinate or expires the route.",
                ],
                "prompt_hint": "Move the named route coordinate only.",
                "payload": {
                    "route_directive": "widen_scouts",
                    "cell": {
                        "entity": "HYPE",
                        "horizon": "intraday",
                        "lens": "on_chain",
                        "bias_mode": "frontier",
                    },
                },
            },
            {
                "order_id": "mwo_surface_mempool_pending_intent",
                "owner": "tool_builder",
                "priority": "high",
                "action": "build_or_validate_source_adapter",
                "reason": "Mempool pending intent is not yet first-class.",
                "input_refs": ["tc_node_1"],
                "expected_output": "Promotable read-only tool proposal with fixture coverage.",
                "success_criteria": [
                    "Tool returns typed observations with source timestamps.",
                    "Failed or stale data emits a repair flag instead of fake coverage.",
                ],
                "prompt_hint": "Build the smallest read-only source adapter.",
                "payload": {
                    "surface": {
                        "key": "mempool_pending_intent",
                        "title": "Mempool pending intent",
                        "example_tools": [
                            "tic://tool/learned/hyperevm_mempool_actor_watch@v1",
                        ],
                    },
                    "stop_condition": "Stop if no read-only fixture can be produced.",
                },
            },
        ],
    }

    first = post_market_map_self_healing_work_orders(
        plan,
        cycle_id="cycle_self_heal_dispatch",
        conn=store.conn,
    )
    second = post_market_map_self_healing_work_orders(
        plan,
        cycle_id="cycle_self_heal_dispatch",
        conn=store.conn,
    )

    assert first["posted_count"] == 2
    assert first["proof"]["orders_became_task_contracts"] is True
    assert first["proof"]["all_tasks_have_success_gates"] is True
    assert second["posted_count"] == 0
    assert second["existing_count"] == 2
    rows = store.conn.execute(
        """
        SELECT topic, priority, input_schema_json, allowed_tools_json,
               promotion_criteria_json, kill_criteria_json, coverage_cell_key,
               payload
        FROM task_contracts
        WHERE cycle_id = ? AND topic LIKE 'market_map.self_heal.%'
        ORDER BY priority DESC
        """,
        ("cycle_self_heal_dispatch",),
    ).fetchall()
    assert len(rows) == 2
    route = next(row for row in rows if row["coverage_cell_key"] == "HYPE|intraday|on_chain|frontier")
    route_tools = json.loads(route["allowed_tools_json"])
    assert "tic://tool/talis_native/plan_alpha_geometry_actions@v1" in route_tools
    assert json.loads(route["input_schema_json"])["requires_output"]
    assert json.loads(route["promotion_criteria_json"])["success_gate"]
    tool_task = next(row for row in rows if json.loads(row["payload"])["market_map_work_order_id"].startswith("mwo_surface"))
    tool_tools = json.loads(tool_task["allowed_tools_json"])
    assert "tic://tool/learned/hyperevm_mempool_actor_watch@v1" in tool_tools
    assert json.loads(tool_task["kill_criteria_json"])["stop_condition"] == "Stop if no read-only fixture can be produced."
    posted_events = store.conn.execute(
        "SELECT COUNT(*) FROM blackboard_events WHERE cycle_id = ? AND event_type = 'task.posted'",
        ("cycle_self_heal_dispatch",),
    ).fetchone()[0]
    assert posted_events == 2


def test_self_healing_worker_executes_routes_and_creates_tool_proposals(tmp_path, monkeypatch) -> None:
    store = reset_desk_store_for_test(tmp_path / "desk.db")
    get_schema_version(store.conn)
    import talis_desk.tool_atlas as tool_atlas

    calls: list[str] = []

    def fake_dispatch(uri, args, context):
        calls.append(uri)
        return type("ToolResult", (), {
            "ok": True,
            "result": {
                "schema_version": "shape_reader_fixture_v1",
                "routing_queue": [{"route_task_id": "route_self_heal"}],
                "actions": [{"action": "widen_scouts"}],
            },
            "error": None,
            "tool_call_log_id": f"tc_self_heal_{len(calls)}",
            "cost_usd": 0.0,
        })()

    monkeypatch.setattr(tool_atlas, "dispatch_uri", fake_dispatch)
    plan = {
        "schema_version": "market_map_self_healing_v1",
        "cycle_id": "cycle_self_heal_worker",
        "status": "ready",
        "work_orders": [
            {
                "order_id": "mwo_route_followup",
                "owner": "seed_router",
                "priority": "high",
                "action": "allocate_widen_scouts",
                "reason": "Alpha geometry requested an independent scout route.",
                "input_refs": ["istr_route_1"],
                "expected_output": "Follow-up SeedCell batch with geometry source refs.",
                "success_criteria": ["Route work cites the shape reader."],
                "payload": {
                    "route_directive": "widen_scouts",
                    "cell": {
                        "entity": "HYPE",
                        "horizon": "intraday",
                        "lens": "on_chain",
                        "bias_mode": "frontier",
                    },
                },
            },
            {
                "order_id": "mwo_surface_mempool_pending_intent",
                "owner": "tool_builder",
                "priority": "high",
                "action": "build_or_validate_source_adapter",
                "reason": "Mempool pending intent is not yet first-class.",
                "input_refs": ["tc_node_1"],
                "expected_output": "Promotable read-only tool proposal with fixture coverage.",
                "success_criteria": ["Tool returns typed observations with source timestamps."],
                "payload": {
                    "surface": {
                        "key": "mempool_pending_intent",
                        "title": "Mempool pending intent",
                        "example_tools": [
                            "tic://tool/learned/hyperevm_mempool_actor_watch@v1",
                        ],
                    }
                },
            },
        ],
    }
    post_market_map_self_healing_work_orders(
        plan,
        cycle_id="cycle_self_heal_worker",
        conn=store.conn,
    )

    batch = execute_market_map_self_healing_tasks(
        cycle_id="cycle_self_heal_worker",
        conn=store.conn,
    )

    assert batch["schema_version"] == "market_map_self_healing_worker_batch_v1"
    assert batch["claimed_count"] == 2
    assert batch["completed_count"] == 2
    assert batch["failed_count"] == 0
    assert batch["tool_proposal_count"] == 1
    assert "tic://tool/talis_native/plan_alpha_geometry_actions@v1" in calls
    proposal = store.conn.execute(
        """
        SELECT tool_name, artifact_kind, artifact_id, source_family,
               promotion_gate_json, status
        FROM analysis_tool_proposals
        WHERE cycle_id = ?
        """,
        ("cycle_self_heal_worker",),
    ).fetchone()
    assert proposal["tool_name"] == "hyperevm_mempool_actor_watch"
    assert proposal["artifact_kind"] == "market_map_self_healing_task"
    assert proposal["status"] == "proposed"
    gate = json.loads(proposal["promotion_gate_json"])
    assert gate["would_change_decision"] is True
    assert "mempool_pending_intent" in gate["expected_edge"]
    completed = store.conn.execute(
        """
        SELECT COUNT(*)
        FROM blackboard_events
        WHERE cycle_id = ? AND event_type = 'task.completed'
        """,
        ("cycle_self_heal_worker",),
    ).fetchone()[0]
    assert completed == 2
    repeat = execute_market_map_self_healing_tasks(
        cycle_id="cycle_self_heal_worker",
        conn=store.conn,
    )
    assert repeat["task_count"] == 0
