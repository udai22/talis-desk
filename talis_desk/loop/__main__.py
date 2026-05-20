"""Phase 4-loop smoke test — end-to-end research cycle.

Run:

    python -m talis_desk.loop

What it does:
  1. Points the desk store at a throwaway DB so it doesn't pollute
     ~/.talis/desk.db.
  2. Insert a synthetic minimal persona (state_kind='persona') for
     specialist_id='macro_regime' if none exists. This is the SAME
     hand-off the specialists/macro_regime build will own once it ships —
     the smoke test just unblocks itself when running solo.
  3. Calls run_research_cycle(specialist_id='macro_regime',
     cycle_id='loop_smoke_01', as_of=now()).
  4. Prints the full ResearchCycleResult: hydration counters,
     n_hypotheses_planned, n_tool_calls, n_trade_ideas_emitted,
     total_cost_usd, kill_switch_triggered.
  5. Verifies a `specialist_states` row was written with
     state_kind='dehydration'.
  6. Verifies idempotency: re-runs with same cycle_id, gets the
     short-circuit path, no new dehydration rows.

Honest gaps:
  - This script uses a synthetic dispatcher so we don't hit live tools
    (would cost real money + need internet). The dispatcher writes
    proper tool_call_log rows so the rest of the pipeline is exercised
    on real DB writes.
  - We DO make real LLM calls via tic.desk.models.chat() — PLAN and
    REFLECT actually call providers. If no API keys are configured
    PLAN will raise (which is the intended "no stub" behavior).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def _isolated_store() -> Path:
    tmpdir = tempfile.mkdtemp(prefix="loop_smoke_")
    return Path(tmpdir) / "desk.db"


def _hr(t: str) -> None:
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


def _insert_synthetic_persona(specialist_id: str) -> str:
    """Insert a minimal persona for `specialist_id` if none exists. Returns
    the persona_state_id."""
    from talis_desk.store import get_desk_store

    conn = get_desk_store().conn
    row = conn.execute(
        "SELECT id FROM specialist_states WHERE specialist_id = ? "
        "AND state_kind = 'persona' AND transaction_to IS NULL LIMIT 1",
        (specialist_id,),
    ).fetchone()
    if row is not None:
        return row["id"]

    # Pull 5 builtin tool URIs from the regenerated atlas.
    from talis_desk.tool_atlas import regenerate_tool_atlas, AgentContext
    snap = regenerate_tool_atlas()
    builtin_uris = [r["tool_uri"] for r in snap.rows
                    if r["kind"] in ("builtin", "hydromancer")][:5]
    if len(builtin_uris) < 5:
        # Pad with whatever rows we have
        builtin_uris = [r["tool_uri"] for r in snap.rows][:5]

    state_json = {
        "system_prompt": (
            "You are macro_regime, a senior macro-systematic specialist on the "
            "Hyperliquid research desk. Your edge is reading the regime: "
            "DXY, Treasury auction stress, funding curves, basis, and "
            "front-month volatility. You think like a JPM ATS rates desk "
            "quant turned crypto macro — you prefer 1-7d horizons and you "
            "are skeptical of single-asset crowded narratives.\n\n"
            "Behavioral defaults: cite at least one contradiction at "
            "confidence>=0.7; bias toward 1d-3d horizons; prefer BTC/ETH/SOL "
            "instruments unless the regime call demands HIP-3 long-tail."
        ),
        "persona_tool_uris": builtin_uris,
        "preferred_models": ["anthropic:claude-opus-4-7"],
    }
    persona_id = "spst_" + uuid4().hex[:24]
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO specialist_states "
        "(id, specialist_id, persona_version, cycle_id, state_kind, "
        " state_json, valid_from, transaction_from) "
        "VALUES (?, ?, 'v0_test', 'persona_bootstrap', 'persona', ?, ?, ?)",
        (persona_id, specialist_id, json.dumps(state_json), now_iso, now_iso),
    )
    conn.commit()
    return persona_id


def _wire_synthetic_dispatcher() -> None:
    """Synthetic dispatcher so we don't hit real HL / network. Each call
    returns a deterministic posterior delta that's enough to exercise
    the BFS hot-branch path."""
    from datetime import datetime, timezone
    from uuid import uuid4
    from talis_desk.exploration.bfs import set_dispatcher_for_test
    from talis_desk.tool_atlas.atlas import ToolResult
    from talis_desk.store import get_desk_store

    schedule = [
        +0.12, +0.18, -0.08, +0.22, +0.05, -0.15, +0.10, +0.08,
        +0.05, -0.04, +0.06, +0.05, +0.04, +0.03, +0.02, +0.02,
    ]
    state = {"i": 0}

    def dispatcher(uri: str, args: dict, ctx) -> ToolResult:
        i = state["i"]
        delta = schedule[i % len(schedule)]
        state["i"] += 1
        tc_id = f"tc_smoke_{uuid4().hex[:12]}"
        try:
            conn = get_desk_store().conn
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO tool_call_log "
                "(id, cycle_id, investigation_id, specialist_id, tool_uri, "
                " tool_version, args_hash, args_json, started_at, valid_from, "
                " transaction_from, cost_usd, duration_ms, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tc_id, ctx.cycle_id, getattr(ctx, "investigation_id", None),
                    ctx.specialist_id, uri, "v1",
                    "ah_smoke", json.dumps(args or {}),
                    now_iso, now_iso, now_iso, 0.001, 1, now_iso,
                ),
            )
            conn.commit()
        except Exception as e:
            print(f"    [warn] tool_call_log insert failed: {e}")
        return ToolResult(
            ok=True,
            uri=uri,
            args_hash="ah_smoke",
            result_hash="rh_smoke",
            result={
                "_posterior_delta": delta,
                "_surprise": min(1.0, abs(delta) * 2),
                "_novelty": 0.5 + abs(delta),
            },
            duration_ms=1,
            cost_usd=0.001,
            tool_call_log_id=tc_id,
            error=None,
        )

    set_dispatcher_for_test(dispatcher)


def _print_result(r) -> None:
    print(f"  specialist_id: {r.specialist_id}")
    print(f"  cycle_id: {r.cycle_id}")
    print(f"  persona_version: {r.persona_version}")
    print(f"  idempotent_short_circuit: {r.idempotent_short_circuit}")
    print()
    print("  -- HYDRATE --")
    print(f"    persona_state_id: {r.hydration.persona_state_id}")
    print(f"    persona_prompt: {len(r.hydration.persona_prompt)} chars, "
          f"{len(r.hydration.persona_tool_uris)} uris")
    print(f"    atlas: {r.hydration.tool_atlas.n_tools} tools, "
          f"{r.hydration.tool_atlas.n_skills} skills, "
          f"{r.hydration.tool_atlas.n_sources} sources "
          f"(pinned={r.hydration.atlas_pinned})")
    print(f"    unread_messages: {len(r.hydration.unread_messages)}")
    print(f"    recent_brier_outcomes: {len(r.hydration.recent_brier_outcomes)}")
    print(f"    open_hypotheses_carryover: {len(r.hydration.open_hypotheses)}")
    print()
    print("  -- PLAN --")
    print(f"    model_used: {r.plan.model_used} (fallback={r.plan.fallback_used})")
    print(f"    n_hypotheses_planned: {len(r.plan.hypotheses)}")
    for i, h in enumerate(r.plan.hypotheses):
        print(f"      [{i}] {h.title[:80]}  uris={len(h.tool_uris)}  "
              f"prob={h.initial_prob:.2f}")
    print(f"    llm_cost_usd: ${r.plan.llm_cost_usd:.4f}")
    print()
    print("  -- EXPLORE --")
    print(f"    exploration_trace_id: {r.exploration_trace_id}")
    print(f"    n_traces: {len(r.exploration_traces)}")
    total_steps = sum(t.n_calls for t in r.exploration_traces)
    total_hot = sum(t.n_hot_branches for t in r.exploration_traces)
    print(f"    total_tool_calls: {total_steps}  hot_branches: {total_hot}")
    for t in r.exploration_traces:
        print(f"      - inv={t.investigation_id}: calls={t.n_calls} "
              f"hot={t.n_hot_branches} contradiction_share={t.contradiction_share:.2f} "
              f"final_posterior={t.final_posterior:.2f} cost=${t.total_cost_usd:.4f}")
    print()
    print("  -- SYNTHESIZE --")
    print(f"    n_trade_ideas_emitted: {len(r.synthesis.new_trade_ideas)}")
    for i in r.synthesis.new_trade_ideas[:3]:
        print(f"      - {i.id} {i.direction} {i.instrument} "
              f"conf={i.confidence:.2f} status={i.status}")
    print(f"    n_resolved_hypotheses: {len(r.synthesis.resolved_hypotheses)}")
    print(f"    n_updated_hypotheses: {len(r.synthesis.updated_hypotheses)}")
    print(f"    n_peer_messages: {len(r.synthesis.peer_messages)}")
    triggered = [d for d in r.synthesis.debate_triggers if d.should_trigger]
    print(f"    n_debate_triggers: {len(r.synthesis.debate_triggers)} "
          f"(of which fired: {len(triggered)})")
    print()
    print("  -- REFLECT --")
    print(f"    model_used: {r.reflection.model_used}")
    print(f"    llm_cost_usd: ${r.reflection.llm_cost_usd:.4f}")
    print(f"    notes_to_self: {r.reflection.notes_to_self[:160]}")
    print(f"    n_posterior_adjustments: {len(r.reflection.posterior_adjustments)}")
    print(f"    n_tool_affinity_delta: {len(r.reflection.tool_affinity_delta)}")
    print(f"    n_redundant_tool_calls: {len(r.reflection.redundant_tool_calls)}")
    print()
    print("  -- DEHYDRATE --")
    print(f"    next_state_id: {r.next_state_id}")
    print()
    print("  -- TOTALS --")
    print(f"    total_cost_usd: ${r.total_cost_usd:.4f}")
    print(f"    total_tool_calls: {r.total_tool_calls}")
    print(f"    elapsed_seconds: {r.elapsed_seconds:.2f}")
    print(f"    kill_switch_triggered: {r.kill_switch_triggered}")
    print(f"    paper_only: {r.paper_only}")
    print(f"    quality_flags: {r.quality_flags}")


def main() -> int:
    # The atlas regen scans tic.desk.tools.TOOLS — make sure the sibling
    # repo is on sys.path before we touch the atlas. The loop runner's
    # chat() bridge does this too, but we do it eagerly so the synthetic
    # persona builder works as well.
    _tic_sibling = "/Users/udaikhattar/jarvis-ios/docs/research/brief_experiments"
    if _tic_sibling not in sys.path:
        sys.path.insert(0, _tic_sibling)

    db_path = _isolated_store()
    print(f"smoke db: {db_path}")
    from talis_desk import store as store_mod
    store_mod._STORE = None  # type: ignore[attr-defined]
    store_mod.get_desk_store(db_path=db_path)

    # Reset debate cooldowns (process-local module state).
    from talis_desk.exploration.bfs import reset_debate_cooldowns
    reset_debate_cooldowns()

    # Insert synthetic persona + wire dispatcher
    persona_id = _insert_synthetic_persona("macro_regime")
    _wire_synthetic_dispatcher()
    print(f"persona_state_id: {persona_id}")

    # ----- 1st run -----
    _hr("[1] First cycle run")
    from talis_desk.loop import run_research_cycle, LoopConfig

    try:
        result1 = run_research_cycle(
            specialist_id="macro_regime",
            cycle_id="loop_smoke_01",
            as_of=datetime.now(timezone.utc),
            loop_config=LoopConfig(
                max_calls=24,
                max_cost_usd=5.0,
                per_hypothesis_max_calls=10,
                paper_only=True,  # smoke test stays in paper mode
            ),
        )
    except Exception as e:
        print(f"  [FAIL] first cycle raised: {e}")
        traceback.print_exc()
        return 1
    _print_result(result1)

    # ----- Verify dehydration row exists -----
    _hr("[2] Verify dehydration row in specialist_states")
    from talis_desk.store import get_desk_store
    conn = get_desk_store().conn
    rows = conn.execute(
        "SELECT id, state_kind, persona_version, cycle_id, transaction_from "
        "FROM specialist_states "
        "WHERE specialist_id = ? AND cycle_id = ? AND state_kind = 'dehydration'",
        ("macro_regime", "loop_smoke_01"),
    ).fetchall()
    print(f"  dehydration_rows: {len(rows)}")
    for r in rows:
        d = dict(r)
        print(f"    - {d['id']} kind={d['state_kind']} "
              f"persona_version={d['persona_version']} "
              f"cycle_id={d['cycle_id']} txn_from={d['transaction_from']}")
    if len(rows) != 1:
        print(f"  [FAIL] expected exactly 1 dehydration row, got {len(rows)}")
        return 2

    # ----- 2nd run (idempotency) -----
    _hr("[3] Second cycle run with same cycle_id (expect idempotent short-circuit)")
    try:
        result2 = run_research_cycle(
            specialist_id="macro_regime",
            cycle_id="loop_smoke_01",
            as_of=datetime.now(timezone.utc),
            loop_config=LoopConfig(paper_only=True),
        )
    except Exception as e:
        print(f"  [FAIL] second cycle raised: {e}")
        traceback.print_exc()
        return 3
    print(f"  idempotent_short_circuit: {result2.idempotent_short_circuit}")
    print(f"  next_state_id: {result2.next_state_id}")
    print(f"  total_cost_usd: ${result2.total_cost_usd:.4f}")

    rows2 = conn.execute(
        "SELECT count(*) AS n FROM specialist_states "
        "WHERE specialist_id = ? AND cycle_id = ? AND state_kind = 'dehydration'",
        ("macro_regime", "loop_smoke_01"),
    ).fetchone()
    n_after = rows2["n"]
    print(f"  dehydration_rows after re-run: {n_after}")
    if n_after != 1:
        print(f"  [FAIL] expected still 1 dehydration row after re-run, got {n_after}")
        return 4

    # ----- Kill switch ack (artificial) -----
    _hr("[4] Kill switch check (artificial: simulate >10 debate fires)")
    # We can't easily inject 10+ legit debate triggers without 10 high-conf
    # hypotheses. We instead poke the in-memory debate cycle_counts and
    # call maybe_trigger_debate one more time to confirm the kill bit lights.
    from talis_desk.exploration.bfs import _DEBATE_STATE, maybe_trigger_debate
    from talis_desk.tool_atlas import AgentContext
    _DEBATE_STATE["cycle_counts"]["loop_smoke_kill"] = 10
    ctx = AgentContext(cycle_id="loop_smoke_kill", specialist_id="macro_regime")
    decision = maybe_trigger_debate(
        {"posterior_prob": 0.9, "confidence": 0.9}, ctx,
    )
    print(f"  decision.kill_switch={decision.kill_switch} "
          f"reason={decision.reason}")
    if not decision.kill_switch:
        print("  [FAIL] expected kill_switch=True after 10 debates")
        return 5

    # ----- Acceptance gates -----
    _hr("[5] Acceptance criteria")
    checks = []
    checks.append(("AC1: cycle completed end-to-end",
                    not result1.idempotent_short_circuit and result1.next_state_id))
    checks.append(("AC2: ResearchCycleResult populated",
                    result1.synthesis is not None and result1.reflection is not None))
    checks.append(("AC3: cost under cap",
                    result1.total_cost_usd <= 10.0))  # extension cap
    checks.append(("AC4: tool_call_log written with cycle_id",
                    conn.execute(
                        "SELECT count(*) AS n FROM tool_call_log "
                        "WHERE cycle_id = ?", ("loop_smoke_01",)
                    ).fetchone()["n"] > 0))
    checks.append(("AC5: idempotent",
                    result2.idempotent_short_circuit))
    checks.append(("AC6: kill switch fires on >10 debates",
                    decision.kill_switch is True))
    checks.append(("AC7: dehydration row bitemporal",
                    bool(rows[0]["transaction_from"])))
    # AC8: chat() with real fallback chain — verified by absence of stub
    # text in plan.model_used. The string "(idempotent_short_circuit)" is
    # ok; what we don't want is "stub_*".
    checks.append(("AC8: no stubs (model_used is a real provider tag)",
                    ":" in (result1.plan.model_used or "")))

    n_pass = 0
    for label, ok in checks:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {label}")
        if ok:
            n_pass += 1
    print()
    print(f"  {n_pass}/{len(checks)} acceptance criteria passed")
    if n_pass != len(checks):
        return 6

    print()
    print("PHASE 4 LOOP ORCHESTRATOR — READY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
