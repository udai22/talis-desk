from __future__ import annotations

import json
from types import SimpleNamespace

from talis_desk.agent_harness import (
    HarnessPolicy,
    dispatch_harness_tool,
    execute_cortex_task_queue,
    filter_fulfilled_tool_requests,
    normalize_tool_requests,
)
from talis_desk.coordination import post_task
from talis_desk.store import DeskStore
from talis_desk.tool_atlas import AgentContext


def test_harness_denies_mutating_tools_by_default():
    context = AgentContext(
        cycle_id="cycle_harness",
        specialist_id="test_agent",
        investigation_id="inv_harness",
    )

    out = dispatch_harness_tool(
        "tic://tool/builtin/request_trade@v1",
        {"symbol": "HYPE"},
        context,
    )

    assert out["ok"] is False
    assert out["error_type"] == "permission_denied"
    assert "approved read-only" in out["recovery_hint"]


def test_harness_normalizes_tool_requests_as_bounded_proposals():
    requests = normalize_tool_requests(
        [
            {
                "tool_uri": "tic://tool/builtin/query_timeseries@v1",
                "args": {"entity_symbol": "HYPE"},
                "why": "Need market-depth bridge.",
                "expected_edge": "event -> depth",
                "expected_info_value": 0.82,
                "would_change_decision": True,
                "fallback_if_denied": "Keep route as unconfirmed map gap.",
                "priority": "high",
            },
            {
                "tool_uri": "tic://tool/builtin/invented@v1",
                "why": "Need unavailable source.",
                "expected_edge": "gap -> proposed_tool",
            },
        ],
        allowed_tools=["tic://tool/builtin/query_timeseries@v1"],
    )

    assert requests[0]["tool_uri"] == "tic://tool/builtin/query_timeseries@v1"
    assert requests[0]["priority"] == "high"
    assert requests[0]["expected_info_value"] == 0.82
    assert requests[0]["would_change_decision"] is True
    assert requests[0]["fallback_if_denied"] == "Keep route as unconfirmed map gap."
    assert requests[1]["tool_uri"] == ""
    assert requests[1]["tool_name"] == "invented"


def test_harness_retries_retryable_read_failures(monkeypatch):
    import talis_desk.tool_atlas as tool_atlas

    calls = 0

    def fake_dispatch(uri, args, context):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(
                ok=False,
                result=None,
                error="timeout fetching source",
                tool_call_log_id="tc_timeout",
                cost_usd=0.0,
            )
        return SimpleNamespace(
            ok=True,
            result={"rows": [{"value": 1}]},
            error=None,
            tool_call_log_id="tc_ok",
            cost_usd=0.0,
        )

    monkeypatch.setattr(tool_atlas, "dispatch_uri", fake_dispatch)
    context = AgentContext(cycle_id="cycle_harness", specialist_id="test_agent")

    out = dispatch_harness_tool(
        "tic://tool/builtin/query_timeseries@v1",
        {"entity_symbol": "HYPE"},
        context,
        policy=HarnessPolicy(max_retries=1, retry_backoff_s=0.0),
    )

    assert calls == 2
    assert out["ok"] is True
    assert out["attempts"] == 2
    assert out["retry_errors"][0]["type"] == "timeout"


def test_harness_preserves_tool_request_lease_metadata(monkeypatch):
    import talis_desk.tool_atlas as tool_atlas

    def fake_dispatch(uri, args, context):
        return SimpleNamespace(
            ok=True,
            result={"rows": [{"value": 1}]},
            error=None,
            tool_call_log_id="tc_ok",
            cost_usd=0.0,
        )

    monkeypatch.setattr(tool_atlas, "dispatch_uri", fake_dispatch)
    context = AgentContext(cycle_id="cycle_harness", specialist_id="test_agent")

    out = dispatch_harness_tool(
        "tic://tool/builtin/query_timeseries@v1",
        {"entity_symbol": "HYPE"},
        context,
        requested_by_model=True,
        request_why="Validate whether the event has market depth support.",
        expected_edge="event -> depth",
        expected_info_value=0.73,
        would_change_decision=True,
        fallback_if_denied="Record a missing-depth edge.",
    )

    assert out["requested_by_model"] is True
    assert out["expected_edge"] == "event -> depth"
    assert out["expected_info_value"] == 0.73
    assert out["would_change_decision"] is True
    assert out["fallback_if_denied"] == "Record a missing-depth edge."


def test_harness_filters_fulfilled_requests():
    remaining = filter_fulfilled_tool_requests(
        [
            {
                "tool_uri": "tic://tool/builtin/query_timeseries@v1",
                "args": {"entity_symbol": "HYPE"},
            },
            {
                "tool_uri": "",
                "tool_name": "new_node_tool",
            },
        ],
        tool_evidence=[
            {
                "uri": "tic://tool/builtin/query_timeseries@v1",
                "args": {"entity_symbol": "HYPE"},
                "ok": True,
            }
        ],
    )

    assert remaining == [{"tool_uri": "", "tool_name": "new_node_tool"}]


def test_cortex_task_worker_claims_executes_and_completes_shape_task(tmp_path, monkeypatch):
    import talis_desk.store as store_mod
    import talis_desk.tool_atlas as tool_atlas

    store = DeskStore(db_path=tmp_path / "desk.db")
    store_mod._STORE = store
    calls: list[tuple[str, dict]] = []

    def fake_dispatch(uri, args, context):
        calls.append((uri, args))
        return SimpleNamespace(
            ok=True,
            result={
                "schema_version": "fake_shape_tool_v1",
                "routing_queue": [{"route_task_id": "shape_route_worker"}],
                "cortex_work_orders": [{"route_task_id": "shape_route_worker"}],
            },
            error=None,
            tool_call_log_id=f"tc_{len(calls)}",
            cost_usd=0.0,
        )

    monkeypatch.setattr(tool_atlas, "dispatch_uri", fake_dispatch)
    task_id = post_task(
        topic="alpha_geometry.route",
        title="Execute shape route",
        cycle_id="cycle_cortex_worker",
        priority=9.0,
        input_schema={
            "required_first_tool": "tic://tool/talis_native/plan_alpha_geometry_actions@v1",
            "tool_sequence": [
                "tic://tool/talis_native/review_alpha_geometry_cortex@v1",
                "tic://tool/builtin/query_events_recent@v1",
            ],
        },
        allowed_tools=[
            "tic://tool/talis_native/plan_alpha_geometry_actions@v1",
            "tic://tool/talis_native/review_alpha_geometry_cortex@v1",
            "tic://tool/builtin/query_events_recent@v1",
        ],
        promotion_criteria={"success_gate": "shape observed"},
        kill_criteria={"stop_condition": "shape reader fails"},
        payload={
            "route_task_id": "shape_route_worker",
            "entity": "HYPE",
            "horizon": "intraday",
            "lens": "on_chain",
            "owner": "seed_router",
            "action": "replicate_thin_cell",
        },
        conn=store.conn,
    )

    batch = execute_cortex_task_queue(
        cycle_id="cycle_cortex_worker",
        conn=store.conn,
        policy=HarnessPolicy(evidence_hard_cap=4, max_retries=0),
    )

    assert batch["schema_version"] == "cortex_task_worker_batch_v1"
    assert batch["claimed_count"] == 1
    assert batch["completed_count"] == 1
    assert batch["failed_count"] == 0
    assert [uri for uri, _ in calls] == [
        "tic://tool/talis_native/plan_alpha_geometry_actions@v1",
        "tic://tool/talis_native/review_alpha_geometry_cortex@v1",
    ]
    row = store.conn.execute(
        "SELECT status, owner_agent_id, owner_specialist_id FROM task_contracts WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["status"] == "completed"
    assert row["owner_agent_id"] == "cortex_task_worker"
    assert row["owner_specialist_id"] == "alpha_geometry_cortex"
    event = store.conn.execute(
        """
        SELECT payload
        FROM blackboard_events
        WHERE task_id = ? AND event_type = 'task.completed'
        ORDER BY occurred_at DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    payload = json.loads(event["payload"])
    assert payload["schema_version"] == "cortex_task_execution_v1"
    assert payload["proof"]["shape_tool_observed"] is True
    assert payload["proof"]["observations_logged"] == 2
    assert payload["deferred_tool_sequence"] == ["tic://tool/builtin/query_events_recent@v1"]

    repeat = execute_cortex_task_queue(cycle_id="cycle_cortex_worker", conn=store.conn)

    assert repeat["task_count"] == 0


def test_cortex_task_worker_executes_bounded_followups_after_shape_read(tmp_path, monkeypatch):
    import talis_desk.store as store_mod
    import talis_desk.tool_atlas as tool_atlas

    store = DeskStore(db_path=tmp_path / "desk.db")
    store_mod._STORE = store
    calls: list[tuple[str, dict]] = []

    def fake_dispatch(uri, args, context):
        calls.append((uri, args))
        if "plan_alpha_geometry_actions" in uri or "review_alpha_geometry_cortex" in uri:
            result = {
                "schema_version": "fake_shape_tool_v1",
                "routing_queue": [{"route_task_id": "shape_route_followup"}],
                "cortex_work_orders": [{"route_task_id": "shape_route_followup"}],
            }
        else:
            result = {
                "schema_version": "fake_followup_v1",
                "rows": [{"entity": args.get("entity"), "edge": "event -> depth"}],
            }
        return SimpleNamespace(
            ok=True,
            result=result,
            error=None,
            tool_call_log_id=f"tc_followup_{len(calls)}",
            cost_usd=0.0,
        )

    monkeypatch.setattr(tool_atlas, "dispatch_uri", fake_dispatch)
    task_id = post_task(
        topic="alpha_geometry.route",
        title="Execute shape route with followups",
        cycle_id="cycle_cortex_followup",
        priority=9.0,
        input_schema={
            "required_first_tool": "tic://tool/talis_native/plan_alpha_geometry_actions@v1",
            "tool_sequence": [
                "tic://tool/talis_native/review_alpha_geometry_cortex@v1",
                "tic://tool/builtin/query_events_recent@v1",
                "tic://tool/builtin/query_timeseries@v1",
            ],
        },
        allowed_tools=[
            "tic://tool/talis_native/plan_alpha_geometry_actions@v1",
            "tic://tool/talis_native/review_alpha_geometry_cortex@v1",
            "tic://tool/builtin/query_events_recent@v1",
            "tic://tool/builtin/query_timeseries@v1",
        ],
        promotion_criteria={"success_gate": "shape and follow-up observed"},
        kill_criteria={"stop_condition": "shape reader fails"},
        payload={
            "route_task_id": "shape_route_followup",
            "entity": "HYPE",
            "horizon": "intraday",
            "lens": "on_chain",
            "owner": "seed_router",
            "action": "replicate_thin_cell",
        },
        conn=store.conn,
    )

    batch = execute_cortex_task_queue(
        cycle_id="cycle_cortex_followup",
        conn=store.conn,
        policy=HarnessPolicy(evidence_hard_cap=3, max_retries=0),
        execute_followup_tools=True,
    )

    assert batch["completed_count"] == 1
    assert [uri for uri, _ in calls] == [
        "tic://tool/talis_native/plan_alpha_geometry_actions@v1",
        "tic://tool/talis_native/review_alpha_geometry_cortex@v1",
        "tic://tool/builtin/query_events_recent@v1",
    ]
    event = store.conn.execute(
        """
        SELECT payload
        FROM blackboard_events
        WHERE task_id = ? AND event_type = 'task.completed'
        ORDER BY occurred_at DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    payload = json.loads(event["payload"])
    assert payload["proof"]["shape_tool_observed"] is True
    assert payload["proof"]["followup_observations_logged"] == 1
    assert payload["proof"]["deferred_tool_count"] == 1
    assert "executed_followup_tools:1" in payload["quality_flags"]
    from talis_desk.information_map import collect_market_evolve_metrics

    metrics = collect_market_evolve_metrics(cycle_id="cycle_cortex_followup", conn=store.conn)
    assert metrics["cortex_followup_execution_rate"] == 1.0
    assert metrics["cortex_followup_observations_per_task"] == 1.0


def test_cortex_task_worker_fails_when_shape_tools_do_not_observe(tmp_path, monkeypatch):
    import talis_desk.store as store_mod
    import talis_desk.tool_atlas as tool_atlas

    store = DeskStore(db_path=tmp_path / "desk.db")
    store_mod._STORE = store

    def fake_dispatch(uri, args, context):
        return SimpleNamespace(
            ok=False,
            result=None,
            error="runtime_error: shape backend offline",
            tool_call_log_id="tc_failed_shape",
            cost_usd=0.0,
        )

    monkeypatch.setattr(tool_atlas, "dispatch_uri", fake_dispatch)
    task_id = post_task(
        topic="market_evolve.frontier",
        title="Inspect frontier",
        cycle_id="cycle_cortex_worker_fail",
        input_schema={"required_first_tool": "tic://tool/talis_native/plan_alpha_geometry_actions@v1"},
        allowed_tools=["tic://tool/talis_native/plan_alpha_geometry_actions@v1"],
        promotion_criteria={"success_gate": "shape observed"},
        kill_criteria={"stop_condition": "shape reader fails"},
        payload={"route_task_id": "frontier_task"},
        conn=store.conn,
    )

    batch = execute_cortex_task_queue(
        cycle_id="cycle_cortex_worker_fail",
        conn=store.conn,
        policy=HarnessPolicy(max_retries=0),
    )

    assert batch["completed_count"] == 0
    assert batch["failed_count"] == 1
    row = store.conn.execute(
        "SELECT status FROM task_contracts WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["status"] == "failed"
    event = store.conn.execute(
        """
        SELECT payload
        FROM blackboard_events
        WHERE task_id = ? AND event_type = 'task.failed'
        ORDER BY occurred_at DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    payload = json.loads(event["payload"])
    assert payload["reason"] == "no_successful_shape_observation"
    assert payload["proof"]["shape_tool_observed"] is False


def test_cortex_task_worker_blocks_followups_when_shape_read_fails(tmp_path, monkeypatch):
    import talis_desk.store as store_mod
    import talis_desk.tool_atlas as tool_atlas

    store = DeskStore(db_path=tmp_path / "desk.db")
    store_mod._STORE = store
    calls: list[str] = []

    def fake_dispatch(uri, args, context):
        calls.append(uri)
        return SimpleNamespace(
            ok=False,
            result=None,
            error="runtime_error: shape backend offline",
            tool_call_log_id=f"tc_blocked_{len(calls)}",
            cost_usd=0.0,
        )

    monkeypatch.setattr(tool_atlas, "dispatch_uri", fake_dispatch)
    task_id = post_task(
        topic="alpha_geometry.route",
        title="Block followups until shape",
        cycle_id="cycle_cortex_followup_block",
        input_schema={
            "required_first_tool": "tic://tool/talis_native/plan_alpha_geometry_actions@v1",
            "tool_sequence": ["tic://tool/builtin/query_events_recent@v1"],
        },
        allowed_tools=[
            "tic://tool/talis_native/plan_alpha_geometry_actions@v1",
            "tic://tool/builtin/query_events_recent@v1",
        ],
        promotion_criteria={"success_gate": "shape observed"},
        kill_criteria={"stop_condition": "shape reader fails"},
        payload={"route_task_id": "frontier_task_block"},
        conn=store.conn,
    )

    batch = execute_cortex_task_queue(
        cycle_id="cycle_cortex_followup_block",
        conn=store.conn,
        policy=HarnessPolicy(evidence_hard_cap=4, max_retries=0),
        execute_followup_tools=True,
    )

    assert batch["failed_count"] == 1
    assert calls == ["tic://tool/talis_native/plan_alpha_geometry_actions@v1"]
    event = store.conn.execute(
        """
        SELECT payload
        FROM blackboard_events
        WHERE task_id = ? AND event_type = 'task.failed'
        ORDER BY occurred_at DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    payload = json.loads(event["payload"])
    assert payload["proof"]["followup_tools_blocked_by_shape"] is True
    assert payload["deferred_tool_sequence"] == ["tic://tool/builtin/query_events_recent@v1"]
    assert "followup_tools_blocked_by_shape_read" in payload["quality_flags"]
    from talis_desk.information_map import collect_market_evolve_metrics

    metrics = collect_market_evolve_metrics(cycle_id="cycle_cortex_followup_block", conn=store.conn)
    assert metrics["cortex_shape_blocked_followup_rate"] == 1.0
