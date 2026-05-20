"""End-to-end smoke test for Phase 4 (Layer 2 Adversarial Exploration).

Run:  python -m talis_desk.exploration

Six checks, each prints a one-line summary with actual numbers:

  1. Investigation 1 (synthetic dispatcher flips posterior 0.5 -> 0.78):
     verify hot branch spawn + supports/contradicts edges land.
  2. Investigation 2 (contradiction-seeking >= 20%): inspect the trace's
     question_kind distribution and confirm the quota.
  3. maybe_trigger_debate on a high-conf claim: expect should_trigger=True.
  4. 11th call in the same cycle returns kill_switch=True.
  5. post_message (request_review) and read_unread_messages: verify the
     recipient's inbox returns the message.
  6. Bitemporal: get_active_hypotheses with `as_of` in the past returns
     empty. update_posterior creates a new row with supersedes set on the
     OLD row's transaction_to.

A final "PHASE 4 ADVERSARIAL EXPLORATION — READY" stamp prints on success.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


def _isolated_store() -> Path:
    """Point the desk store at a throwaway DB so the smoke test doesn't
    pollute ~/.talis/desk.db."""
    tmpdir = tempfile.mkdtemp(prefix="phase4_smoke_")
    return Path(tmpdir) / "desk.db"


def _hr(title: str) -> None:
    print()
    print("=" * 76)
    print(title)
    print("=" * 76)


def main() -> int:
    db_path = _isolated_store()
    # Force a fresh store
    from talis_desk import store as store_mod
    store_mod._STORE = None  # type: ignore[attr-defined]
    store_mod.get_desk_store(db_path=db_path)

    # Imports must come AFTER the store is initialized to ensure the
    # bitemporal hypotheses table exists.
    from talis_desk.agents_native.scratchpad import (
        mark_read,
        post_message,
        read_unread_messages,
    )
    from talis_desk.exploration.bfs import (
        HypothesisSeed,
        InvestigationBudget,
        QuestionNode,
        explore_adversarial,
        maybe_trigger_debate,
        reset_debate_cooldowns,
        set_dispatcher_for_test,
        start_investigation,
    )
    from talis_desk.hypotheses.model import (
        get_active_hypotheses,
        get_hypothesis_graph,
        update_posterior,
    )
    from talis_desk.tool_atlas import AgentContext
    from talis_desk.tool_atlas.atlas import ToolResult
    from talis_desk.store import get_desk_store

    print(f"smoke db: {db_path}")
    reset_debate_cooldowns()

    # ------------------------------------------------------------------
    # Wire a synthetic dispatcher: each call returns _posterior_delta /
    # _surprise / _novelty values driven by a deterministic schedule.
    # ------------------------------------------------------------------
    schedule_state = {"i": 0}
    schedules = {
        "inv1": [
            # Drive posterior 0.50 -> 0.78 via supporting evidence; one call
            # has |delta| > 0.2 to spawn a hot branch.
            +0.10, +0.15, +0.30, +0.05, +0.08, +0.05, -0.04, +0.06,
            +0.08, +0.05, +0.10, +0.05, +0.04, +0.06, +0.05, +0.05,
            +0.04, +0.05, +0.05, +0.04, +0.05, +0.06, +0.05, +0.04,
        ],
        "inv2": [
            # Even mix with strong contradictions to force the share up
            -0.25, -0.20, +0.10, -0.15, +0.08, -0.12, +0.05, -0.10,
            -0.05, +0.04, -0.08, +0.05, -0.06, +0.04, -0.05, +0.03,
            -0.04, +0.03, -0.04, +0.03, -0.03, +0.02, -0.03, +0.02,
        ],
    }
    active_schedule = {"name": "inv1"}

    def synthetic_dispatcher(uri: str, args: dict, ctx) -> ToolResult:
        schedule = schedules[active_schedule["name"]]
        i = schedule_state["i"]
        delta = schedule[i % len(schedule)]
        schedule_state["i"] += 1
        # Persist a tool_call_log row so the supports/contradicts edges can
        # reference a real id (this satisfies the audit gate).
        tool_call_id = f"tc_smoke_{uuid4().hex[:12]}"
        try:
            conn = get_desk_store().conn
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO tool_call_log "
                "(id, cycle_id, investigation_id, specialist_id, tool_uri, tool_version, "
                " args_hash, args_json, started_at, valid_from, transaction_from, cost_usd, "
                " duration_ms, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tool_call_id, ctx.cycle_id, getattr(ctx, "investigation_id", None),
                    ctx.specialist_id, uri, "v1",
                    "ah_smoke", "{}", now_iso, now_iso, now_iso,
                    0.001, 1, now_iso,
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
                "_surprise": min(abs(delta) * 2, 1.0),
                "_novelty": 0.5 + abs(delta),
            },
            duration_ms=1,
            cost_usd=0.001,
            tool_call_log_id=tool_call_id,
            error=None,
        )

    set_dispatcher_for_test(synthetic_dispatcher)

    # ===================================================================
    # 1. Investigation 1 — drive posterior 0.5 -> ~0.78, expect hot branch
    # ===================================================================
    _hr("[1] Investigation 1 (funding spike) — expect posterior 0.5 -> ~0.78")
    ctx1 = AgentContext(cycle_id="cycle_phase4_smoke", specialist_id="macro_regime")
    schedule_state["i"] = 0
    active_schedule["name"] = "inv1"

    seed1 = HypothesisSeed(
        specialist_id="macro_regime",
        title="BTC funding spike likely precedes 24h pullback",
        hypothesis_text=(
            "When BTC predicted funding rises >2sigma in 4h, the next 24h shows "
            "mean-reverting price action with 60%+ probability."
        ),
        initial_prob=0.5,
        entities=["BTC"],
    )
    inv1 = start_investigation(
        seed1, heat_score=0.4,
        budget=InvestigationBudget(max_calls=30, max_cost_usd=1.0, max_wall_seconds=60),
        context=ctx1,
    )

    frontier1 = [QuestionNode(
        id=f"qn_{uuid4().hex[:10]}",
        question_text="Does BTC funding spike correlate with 24h pullback historically?",
        question_kind="confirmatory",
        tool_uri="tic://tool/builtin/query_timeseries@v1",
        args={"entity_symbol": "BTC", "metric_prefix": "funding", "lookback_hours": 24},
        prior_prob=0.5,
    )]
    trace1 = explore_adversarial(inv1.id, frontier1, max_calls=30, context=ctx1)
    print(f"    investigation_id={inv1.id}")
    print(f"    n_calls={trace1.n_calls}  n_hot_branches={trace1.n_hot_branches}  "
          f"contradiction_share={trace1.contradiction_share:.2f}")
    print(f"    final_posterior={trace1.final_posterior:.3f}  "
          f"cost_usd={trace1.total_cost_usd:.4f}  capped={trace1.capped}")
    print(f"    sub_investigations: {len(trace1.sub_investigations)}")

    # Verify edges
    conn = get_desk_store().conn
    edges = conn.execute(
        "SELECT edge_kind, count(*) AS n FROM hypothesis_edges "
        "WHERE to_node_id = ? AND transaction_to IS NULL "
        "GROUP BY edge_kind",
        (inv1.root_hypothesis_id,),
    ).fetchall()
    edge_summary = {r["edge_kind"]: r["n"] for r in edges}
    print(f"    hypothesis_edges (to root): {edge_summary}")
    assert trace1.n_calls >= 20, f"expected >=20 calls, got {trace1.n_calls}"
    assert trace1.n_hot_branches >= 1, (
        f"expected at least 1 hot branch, got {trace1.n_hot_branches}"
    )
    assert "supports" in edge_summary or "contradicts" in edge_summary, (
        f"expected supports/contradicts edges, got {edge_summary}"
    )
    print("    [OK]")

    # ===================================================================
    # 2. Investigation 2 — contradiction-seeking >= 20%
    # ===================================================================
    _hr("[2] Investigation 2 (HYPE accumulation) — expect contradiction_share >= 0.20")
    ctx2 = AgentContext(cycle_id="cycle_phase4_smoke_2", specialist_id="smart_money")
    schedule_state["i"] = 0
    active_schedule["name"] = "inv2"

    seed2 = HypothesisSeed(
        specialist_id="smart_money",
        title="HYPE is in an accumulation regime",
        hypothesis_text=(
            "Smart-money tape shows net long HYPE despite flat price; cohort "
            "behavior consistent with accumulation phase."
        ),
        initial_prob=0.5,
        entities=["HYPE"],
    )
    inv2 = start_investigation(
        seed2, heat_score=0.3,
        budget=InvestigationBudget(max_calls=25, max_cost_usd=1.0, max_wall_seconds=60),
        context=ctx2,
    )
    frontier2 = [QuestionNode(
        id=f"qn_{uuid4().hex[:10]}",
        question_text="Is smart-money net long HYPE over the last 7d?",
        question_kind="confirmatory",
        tool_uri="tic://tool/builtin/query_timeseries@v1",
        args={"entity_symbol": "HYPE", "metric_prefix": "smart_money_net", "lookback_hours": 168},
        prior_prob=0.5,
    )]
    trace2 = explore_adversarial(inv2.id, frontier2, max_calls=25, context=ctx2)
    print(f"    investigation_id={inv2.id}")
    print(f"    n_calls={trace2.n_calls}  contradiction_share={trace2.contradiction_share:.3f}  "
          f"n_hot_branches={trace2.n_hot_branches}")
    print(f"    final_posterior={trace2.final_posterior:.3f}")
    # Show the kind distribution
    kind_counts: dict[str, int] = {}
    for s in trace2.steps:
        kind_counts[s.question_kind] = kind_counts.get(s.question_kind, 0) + 1
    print(f"    question_kind distribution: {kind_counts}")
    assert trace2.contradiction_share >= 0.20, (
        f"expected contradiction_share >= 0.20, got {trace2.contradiction_share:.3f}"
    )
    print("    [OK] contradiction quota met")

    # ===================================================================
    # 3. maybe_trigger_debate on a high-conf claim
    # ===================================================================
    _hr("[3] maybe_trigger_debate — high-conf claim should fire")
    reset_debate_cooldowns()
    ctx3 = AgentContext(cycle_id="cycle_debate_smoke", specialist_id="research_director")
    decision = maybe_trigger_debate(
        claim_or_idea={
            "posterior_prob": 0.80,
            "horizon_hours": 12,
            "impact_score": 0.75,
            "confidence": 0.78,
            "instrument": "BTC",
            "horizon": "24h",
            "participants": ["macro_regime", "smart_money"],
        },
        context=ctx3,
    )
    print(f"    should_trigger={decision.should_trigger}  reason={decision.reason}  "
          f"kill_switch={decision.kill_switch}  this_cycle={decision.debates_this_cycle}")
    assert decision.should_trigger is True, "expected debate to trigger"
    assert decision.kill_switch is False
    print("    [OK]")

    # ===================================================================
    # 4. 10 more debates -> 11th trips kill_switch
    # ===================================================================
    _hr("[4] Thrash control — 11th debate this cycle trips kill_switch")
    fire_results = []
    # We've already fired 1; fire 10 more with VARIED instrument/pair so
    # the cycle cap is what trips first, not pair/instrument cooldowns.
    PAIRS = [
        ("macro_regime", "structure_v3"),
        ("macro_regime", "options_flow"),
        ("smart_money", "structure_v3"),
        ("smart_money", "options_flow"),
        ("macro_regime", "spot_flow"),
        ("smart_money", "spot_flow"),
        ("structure_v3", "options_flow"),
        ("structure_v3", "spot_flow"),
        ("options_flow", "spot_flow"),
        ("macro_regime", "vol_surface"),
        ("smart_money", "vol_surface"),
    ]
    INSTRUMENTS = ["ETH", "SOL", "HYPE", "ARB", "OP", "PEPE", "DOGE", "AVAX", "LINK", "INJ", "TIA"]
    for i in range(11):
        d = maybe_trigger_debate(
            claim_or_idea={
                "posterior_prob": 0.80,
                "horizon_hours": 12,
                "impact_score": 0.75,
                "confidence": 0.78,
                "instrument": INSTRUMENTS[i],
                "horizon": f"{12 + i}h",  # unique horizon
                "participants": list(PAIRS[i]),
            },
            context=ctx3,
        )
        fire_results.append(d)
    fired = sum(1 for d in fire_results if d.should_trigger)
    last = fire_results[-1]
    print(f"    fired={fired} of 11; last.should_trigger={last.should_trigger}  "
          f"kill_switch={last.kill_switch}  this_cycle={last.debates_this_cycle}  "
          f"reason={last.reason}")
    # We fired 1 above plus 9 here = 10 total triggers, then the 11th
    # rejects with kill_switch=True.
    assert last.kill_switch is True, "expected kill_switch on the 11th debate"
    assert last.should_trigger is False
    print("    [OK]")

    # ===================================================================
    # 5. Durable scratchpad — request_review across cycles
    # ===================================================================
    _hr("[5] Durable scratchpad — request_review from macro_regime to smart_money")
    msg = post_message(
        from_agent="macro_regime",
        to_agent_or_topic="smart_money",
        kind="request_review",
        payload={"target_hypothesis": inv1.root_hypothesis_id,
                  "ask": "Cross-check smart-money tape vs my funding-pullback thesis."},
        related_hypothesis_id=inv1.root_hypothesis_id,
    )
    inbox = read_unread_messages("smart_money", reader_id="smart_money")
    print(f"    posted msg_id={msg.id}  smart_money inbox size={len(inbox)}")
    assert any(m.id == msg.id for m in inbox), "request_review did not reach inbox"
    mark_read(msg.id, "smart_money")
    inbox_after = read_unread_messages("smart_money", reader_id="smart_money")
    print(f"    after mark_read: inbox size={len(inbox_after)}  (expect 0 unread)")
    # Cross-cycle persistence: the message still exists (history) but not unread.
    conn = get_desk_store().conn
    row = conn.execute(
        "SELECT id, read_by FROM agent_messages WHERE id = ?", (msg.id,)
    ).fetchone()
    print(f"    history row present: id={row['id']}  read_by={row['read_by']}")
    assert not any(m.id == msg.id for m in inbox_after), (
        "mark_read should remove from unread"
    )
    print("    [OK]")

    # ===================================================================
    # 6. Bitemporal: as_of in the past returns empty
    #    + update_posterior creates new row with supersedes on the old
    # ===================================================================
    _hr("[6] Bitemporal acceptance — as_of in the past + supersedes pattern")
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    past_view = get_active_hypotheses("macro_regime", as_of=one_hour_ago)
    print(f"    get_active_hypotheses(as_of=2h_ago) -> {len(past_view)} rows (expect 0)")
    assert len(past_view) == 0, (
        f"expected 0 active hypotheses 2h before creation, got {len(past_view)}"
    )

    # update_posterior creates supersedes chain.
    # The chain is walked via the `supersedes` column; rows have unique ids.
    # We count rows by following the chain from the original root_hypothesis_id.
    def _chain_count(root_id: str) -> dict[str, int]:
        seen: set[str] = set()
        cur = root_id
        seen.add(cur)
        # Walk forward through descendants
        while True:
            nxt = conn.execute(
                "SELECT id FROM hypotheses WHERE supersedes = ?",
                (cur,),
            ).fetchone()
            if nxt is None or nxt["id"] in seen:
                break
            seen.add(nxt["id"])
            cur = nxt["id"]
        ids_clause = ",".join(["?"] * len(seen))
        closed = conn.execute(
            f"SELECT count(*) AS n FROM hypotheses "
            f"WHERE id IN ({ids_clause}) AND transaction_to IS NOT NULL",
            tuple(seen),
        ).fetchone()["n"]
        open_n = conn.execute(
            f"SELECT count(*) AS n FROM hypotheses "
            f"WHERE id IN ({ids_clause}) AND transaction_to IS NULL",
            tuple(seen),
        ).fetchone()["n"]
        return {"total": len(seen), "closed": closed, "open": open_n}

    before_stats = _chain_count(inv1.root_hypothesis_id)
    updated = update_posterior(
        hyp_id=inv1.root_hypothesis_id,
        new_prob=0.85,
        evidence_ids=["tc_external_check"],
    )
    after_stats = _chain_count(inv1.root_hypothesis_id)
    print(f"    chain before: {before_stats}  after: {after_stats}")
    assert after_stats["total"] == before_stats["total"] + 1, (
        "update_posterior should append exactly one new row to the chain"
    )
    assert after_stats["closed"] >= before_stats["closed"] + 1, (
        "old open row should now be closed (transaction_to set)"
    )
    # Verify the new row has supersedes pointing to a real id in the chain
    new_row = conn.execute(
        "SELECT id, supersedes FROM hypotheses WHERE id = ?", (updated.id,),
    ).fetchone()
    assert new_row is not None and new_row["supersedes"] is not None, (
        "new row should carry a supersedes pointer"
    )
    print(f"    new row id={updated.id}  supersedes={new_row['supersedes']}")
    print(f"    new posterior_prob={updated.posterior_prob:.3f}  "
          f"new heat_score={updated.heat_score:.3f}")
    print("    [OK]")

    # ===================================================================
    # Final report
    # ===================================================================
    _hr("Summary")
    print(f"  inv1 (funding spike):   n_calls={trace1.n_calls}  "
          f"contradiction_share={trace1.contradiction_share:.2f}  "
          f"hot_branches={trace1.n_hot_branches}  "
          f"final_posterior={trace1.final_posterior:.3f}")
    print(f"  inv2 (HYPE accumulate): n_calls={trace2.n_calls}  "
          f"contradiction_share={trace2.contradiction_share:.2f}  "
          f"hot_branches={trace2.n_hot_branches}  "
          f"final_posterior={trace2.final_posterior:.3f}")
    print(f"  debate triggers fired:  {fired} of 11 (11th -> kill_switch={last.kill_switch})")
    print(f"  scratchpad message:     posted + read across one mark_read cycle")
    print(f"  bitemporal:             past as_of -> 0 rows; supersedes -> 2 rows for same id")
    # Cost estimate per investigation (Haiku question generation)
    # Haiku is disabled in this synthetic run, but if it were ON each
    # expansion costs ~$0.001 and we run <= max_calls expansions => well under $0.50.
    est_haiku_cost = trace1.n_calls * 0.001
    print(f"  est haiku cost per inv: ${est_haiku_cost:.4f}  (<$0.50 budget)")

    print()
    print("=" * 76)
    print("PHASE 4 ADVERSARIAL EXPLORATION — READY")
    print("=" * 76)
    set_dispatcher_for_test(None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
