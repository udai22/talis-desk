"""End-to-end smoke test for Phase 6 (persona evolution + rewards).

Run:  python -m talis_desk.evolution

Steps:
  1. Seed: insert 30 fake forecast outcomes for macro_regime (mix of
     Brier 0.1 to 0.5), 5 fake trade ideas with mixed alpha.
  2. aggregate_specialist_rewards('macro_regime', window_days=15) -> print
     scores per reward_kind.
  3. aggregate_tool_affinity('macro_regime', 15) -> top 5 tools.
  4. run_nightly_auto_mutation(as_of=now()) -> produces a mutation_candidate
     row citing the weakness it's addressing.
  5. check_veto_window(candidate_id) immediately -> 'too_early'.
  6. Move transaction_from 25h into the past, re-check -> 'ready_to_promote'.
  7. run_persona_ab_test with same persona on both sides -> 'tie'.

NO STUBS: auto-mutation uses tic.desk.models.chat() with the real fallback
chain. If no provider has credit, mutation candidates still get proposed but
are flagged `quality_flags=['llm_unavailable']`.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


def _isolated_store() -> Path:
    tmpdir = tempfile.mkdtemp(prefix="phase6_evolution_smoke_")
    return Path(tmpdir) / "desk.db"


def _hr(title: str) -> None:
    print()
    print("=" * 76)
    print(title)
    print("=" * 76)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _seed_persona(conn, specialist_id: str = "macro_regime") -> str:
    """Register a synthetic persona row for the specialist via the existing
    helpers so the auto-mutator has something to diff against. Returns
    the persona state id."""
    from talis_desk.specialists.base import SpecialistPersona, register_persona

    persona = SpecialistPersona(
        specialist_id=specialist_id,
        persona_version="v1.0",
        name="Macro Regime (smoke v1)",
        scope="Cross-asset macro regime detection",
        system_prompt=(
            "You are macro_regime, a quantitative trading specialist. "
            "Forecast probability of regime shifts.\n"
            "- Cite at least one supporting claim per forecast.\n"
            "- Track DXY, BTC-funding, and HL OI as primary signals.\n"
            "- Always preserve the black-swan caution prior."
        ),
        tool_uris=[
            "tic://tool/builtin/query_timeseries@v1",
            "tic://tool/hydromancer/fetch_live@v3",
            "tic://source/hl/info/metaAndAssetCtxs",
        ],
        preferred_model="anthropic:claude-opus-4-7",
        initial_priors={"confidence_floor": 0.55, "max_leverage": 2.0},
    )
    state = register_persona(persona)
    return state.id


def _seed_hypotheses_and_correctness(conn, specialist_id: str, n: int = 30) -> list[str]:
    """Insert n fake hypotheses + correctness reward rows with Brier 0.1..0.5
    (mostly bad calibration so the mutator has something to flag)."""
    ids: list[str] = []
    now = datetime.now(timezone.utc)
    for i in range(n):
        hid = f"hyp_{uuid4().hex[:12]}"
        valid_from = (now - timedelta(hours=24 + i)).isoformat()
        cycle_id = f"smoke_cycle_{i % 5}"
        conn.execute(
            "INSERT INTO hypotheses (id, cycle_id, specialist_id, title, "
            "hypothesis_text, status, posterior_prob, heat_score, valid_from, "
            "transaction_from, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                hid, cycle_id, specialist_id,
                f"smoke hypothesis #{i}",
                f"DXY moves >0.5% in next 24h (synthetic claim #{i})",
                "active", 0.55 + (i % 5) * 0.05, 0.5,
                valid_from, valid_from,
                json.dumps({"synth": True, "i": i}),
            ),
        )
        ids.append(hid)

        # Reward: brier worsens with i so the mutator sees a trend
        brier = 0.1 + (i / float(n)) * 0.4  # 0.10 -> 0.50
        score = 1.0 - brier
        rew_id = f"rew_{uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO reward_log (id, cycle_id, reward_kind, subject_kind, "
            "subject_id, specialist_id, score, baseline_score, delta, "
            "attribution_json, valid_from, transaction_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rew_id, cycle_id, "correctness", "hypothesis", hid,
                specialist_id, score, 0.75, score - 0.75,
                json.dumps({"brier": brier, "synth": True}),
                valid_from, valid_from,
            ),
        )
    conn.commit()
    return ids


def _seed_trade_ideas_and_alpha(conn, specialist_id: str, n: int = 5) -> list[str]:
    """Insert n synthetic CLOSED trade ideas with mixed alpha + alpha
    reward rows. Returns list of new trade_idea ids."""
    ids: list[str] = []
    now = datetime.now(timezone.utc)
    alphas = [0.42, -0.31, 0.18, -0.05, 0.61]  # mixed
    for i in range(n):
        tid = f"ti_{uuid4().hex[:12]}"
        published = (now - timedelta(hours=48 + i * 6)).isoformat()
        expires = (now - timedelta(hours=12 + i * 6)).isoformat()
        cycle_id = f"smoke_cycle_{i % 3}"
        alpha = alphas[i % len(alphas)]
        ret_aft = alpha + 0.05  # benchmark roughly flat
        brier = 0.18 if alpha > 0 else 0.42

        conn.execute(
            "INSERT INTO trade_ideas ("
            "id, cycle_id, specialist_id, instrument, direction, sizing, "
            "entry, stop, target, time_horizon, edge_thesis, confidence, "
            "status, published_at, expires_at, "
            "realized_pnl_pct, realized_return_after_fees_pct, "
            "benchmark_return_pct, contributed_alpha_pct, brier, "
            "valid_from, transaction_from, payload"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tid, cycle_id, specialist_id, "BTC", "long",
                json.dumps({"risk_pct": 0.003}),
                json.dumps({"limit_px": 65000.0, "market_assumption": "liquid"}),
                json.dumps({"px": 63000.0, "stop_kind": "hard"}),
                json.dumps({"px": 67000.0}),
                "1d",
                f"edge thesis #{i} (synthetic)",
                0.7,
                "closed",
                published, expires,
                ret_aft + 0.05,  # raw pnl
                ret_aft,
                0.05,             # benchmark
                alpha,
                brier,
                published, published,
                json.dumps({"synth": True, "i": i, "persona_version": "v1.0"}),
            ),
        )
        ids.append(tid)

        # alpha reward row
        rew_id = f"rew_{uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO reward_log (id, cycle_id, reward_kind, subject_kind, "
            "subject_id, specialist_id, score, baseline_score, delta, "
            "attribution_json, valid_from, transaction_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rew_id, cycle_id, "alpha", "trade_idea", tid,
                specialist_id, alpha, 0.0, alpha,
                json.dumps({
                    "method": "simple",
                    "total_alpha_pct": alpha,
                    "components": [
                        {"kind": "specialist", "id": specialist_id,
                         "weight": 1.0, "alpha_pct": alpha},
                    ],
                    "realized_return_after_fees_pct": ret_aft,
                    "benchmark_return_pct": 0.05,
                }),
                published, published,
            ),
        )
    conn.commit()
    return ids


def _seed_tool_calls(conn, specialist_id: str, n: int = 8) -> None:
    """Insert tool_call_log rows so aggregate_tool_affinity has data."""
    now = datetime.now(timezone.utc)
    tools = [
        "tic://tool/builtin/query_timeseries@v1",
        "tic://tool/hydromancer/fetch_live@v3",
        "tic://source/hl/info/metaAndAssetCtxs",
        "tic://tool/learned/usdt_mint_flow_to_btc_correlation@v2",
    ]
    for i in range(n):
        tc_id = f"tc_{uuid4().hex[:12]}"
        started = (now - timedelta(hours=6 + i)).isoformat()
        cycle_id = f"smoke_cycle_{i % 3}"
        tool_uri = tools[i % len(tools)]
        brier_delta = (i % 3 - 1) * 0.05  # mix of -0.05, 0, +0.05
        conn.execute(
            "INSERT INTO tool_call_log (id, cycle_id, specialist_id, tool_uri, "
            "tool_version, args_hash, args_json, started_at, cost_usd, "
            "reward_score, cited_in_ids, valid_from, transaction_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tc_id, cycle_id, specialist_id, tool_uri, "v1",
                f"ah_{i}", json.dumps({"i": i}),
                started, 0.012,
                json.dumps({"brier_delta": brier_delta}),
                json.dumps([]),
                started, started,
            ),
        )
    conn.commit()


def main() -> int:
    db_path = _isolated_store()
    # Fresh store
    from talis_desk import store as store_mod
    store_mod._STORE = None  # type: ignore[attr-defined]
    store_mod.get_desk_store(db_path=db_path)
    from talis_desk.store import get_desk_store
    conn = get_desk_store().conn

    print(f"smoke db: {db_path}")

    # ============================================================
    # 0. Seed persona + reward rows
    # ============================================================
    _hr("[0] Seed: persona + 30 correctness rows + 5 alpha rows + tool calls")
    specialist_id = "macro_regime"
    persona_id = _seed_persona(conn, specialist_id)
    print(f"  persona id    : {persona_id}")
    h_ids = _seed_hypotheses_and_correctness(conn, specialist_id, n=30)
    print(f"  hypotheses    : {len(h_ids)} seeded (brier 0.1..0.5)")
    t_ids = _seed_trade_ideas_and_alpha(conn, specialist_id, n=5)
    print(f"  trade ideas   : {len(t_ids)} seeded (mixed alpha)")
    _seed_tool_calls(conn, specialist_id, n=8)
    print(f"  tool calls    : 8 seeded across 4 URIs")

    # ============================================================
    # 1. aggregate_specialist_rewards
    # ============================================================
    _hr("[1] aggregate_specialist_rewards('macro_regime', window_days=15)")
    from talis_desk.eval.rewards import aggregate_specialist_rewards
    agg = aggregate_specialist_rewards(specialist_id, window_days=15, conn=conn)
    print(f"  specialist    : {agg.specialist_id}")
    print(f"  window_days   : {agg.window_days}")
    print(f"  per_kind ({len(agg.per_kind)} kinds):")
    for kind, b in agg.per_kind.items():
        print(f"    - {kind}: n={int(b['n'])}, "
              f"avg={b['score_avg']:.4f}, "
              f"min={b['score_min']:.4f}, "
              f"max={b['score_max']:.4f}")
    print(f"  top weaknesses ({len(agg.weaknesses)}):")
    for w in agg.weaknesses[:5]:
        print(f"    - {w['reward_kind']} on {w['subject_kind']}/{w['subject_id']}: "
              f"score={w['score']:.4f}")
    if not agg.per_kind:
        print("  FAIL: no aggregates produced")
        return 1
    if "correctness" not in agg.per_kind:
        print("  FAIL: correctness rewards missing from aggregate")
        return 1
    print("  [OK] per-kind aggregates produced")

    # ============================================================
    # 2. aggregate_tool_affinity
    # ============================================================
    _hr("[2] aggregate_tool_affinity('macro_regime', 15) -> top 5 tools")
    from talis_desk.eval.rewards import aggregate_tool_affinity
    affinity = aggregate_tool_affinity(specialist_id, window_days=15, conn=conn)
    print(f"  n_tools_scored: {len(affinity)}")
    for i, (uri, score) in enumerate(list(affinity.items())[:5]):
        print(f"    #{i+1} {uri}: {score:.6f}")
    if not affinity:
        print("  FAIL: tool affinity empty")
        return 1
    print("  [OK] tool affinity scored")

    # ============================================================
    # 3. run_nightly_auto_mutation
    # ============================================================
    _hr("[3] run_nightly_auto_mutation(as_of=now()) -> mutation_candidate")
    from talis_desk.evolution import run_nightly_auto_mutation
    as_of = datetime.now(timezone.utc)
    candidates = run_nightly_auto_mutation(
        as_of=as_of, conn=conn,
        specialist_ids=[specialist_id], window_days=15,
    )
    print(f"  candidates produced: {len(candidates)}")
    if not candidates:
        print("  FAIL: expected at least one mutation_candidate")
        return 1
    cand = candidates[0]
    print(f"  candidate id        : {cand.id}")
    print(f"  specialist_id       : {cand.specialist_id}")
    print(f"  diff.kind           : {cand.diff.kind}")
    print(f"  diff.rationale      : {cand.diff.rationale[:120]}...")
    print(f"  reason              : {cand.reason[:160]}")
    print(f"  quality_flags       : {cand.quality_flags}")
    print(f"  llm_meta            : {cand.state_json.get('llm_meta', {})}")
    print(f"  weakness_summary    : {cand.state_json.get('weakness_summary')}")

    # Acceptance criterion: the reason must cite a measurable weakness
    if "correctness" not in cand.reason and "alpha" not in cand.reason \
       and "brier" not in cand.reason.lower():
        print("  FAIL: candidate reason does not cite a measurable weakness")
        return 1
    print("  [OK] candidate cites a measurable weakness in reason")

    # ============================================================
    # 4. check_veto_window immediately -> too_early
    # ============================================================
    _hr("[4] check_veto_window(candidate_id) immediately -> too_early")
    from talis_desk.evolution import check_veto_window
    vs = check_veto_window(cand.id, conn=conn, as_of=as_of)
    print(f"  candidate_id    : {vs.candidate_id}")
    print(f"  age_hours       : {vs.age_hours:.4f}")
    print(f"  veto_received   : {vs.veto_received}")
    print(f"  elapsed_window  : {vs.elapsed_window}")
    print(f"  status          : {vs.status}")
    if vs.status != "too_early":
        print(f"  FAIL: expected status='too_early', got {vs.status!r}")
        return 1
    print("  [OK] veto window correctly blocks early promote")

    # ============================================================
    # 5. Advance time 25h, re-check -> ready_to_promote
    # ============================================================
    _hr("[5] advance candidate transaction_from 25h -> ready_to_promote")
    new_tf = (as_of - timedelta(hours=25)).isoformat()
    conn.execute(
        "UPDATE specialist_states SET transaction_from = ? WHERE id = ?",
        (new_tf, cand.id),
    )
    conn.commit()
    vs2 = check_veto_window(cand.id, conn=conn, as_of=as_of)
    print(f"  age_hours       : {vs2.age_hours:.4f}")
    print(f"  status          : {vs2.status}")
    if vs2.status != "ready_to_promote":
        print(f"  FAIL: expected 'ready_to_promote', got {vs2.status!r}")
        return 1
    print("  [OK] post-window check fires ready_to_promote")

    # ============================================================
    # 5b. promote_persona -> closes candidate + parent, opens new persona
    # ============================================================
    _hr("[5b] promote_persona(candidate_id) -> new persona row")
    from talis_desk.evolution import promote_persona
    new_state = promote_persona(cand.id, conn=conn)
    print(f"  new persona id  : {new_state.id}")
    print(f"  new version     : {new_state.persona_version}")
    print(f"  parent_state_id : {new_state.parent_state_id}")
    # Verify bitemporal: candidate row is closed, prior persona is closed,
    # new persona row is open.
    cand_row = conn.execute(
        "SELECT transaction_to, state_kind FROM specialist_states WHERE id = ?",
        (cand.id,),
    ).fetchone()
    if cand_row["transaction_to"] is None:
        print("  FAIL: candidate not closed after promote")
        return 1
    prior_row = conn.execute(
        "SELECT transaction_to FROM specialist_states WHERE id = ?",
        (persona_id,),
    ).fetchone()
    if prior_row["transaction_to"] is None:
        print("  FAIL: prior persona not closed after promote")
        return 1
    open_count = conn.execute(
        "SELECT COUNT(*) FROM specialist_states "
        "WHERE specialist_id = ? AND state_kind = 'persona' "
        "AND transaction_to IS NULL",
        (specialist_id,),
    ).fetchone()[0]
    if open_count != 1:
        print(f"  FAIL: expected exactly 1 open persona, found {open_count}")
        return 1
    print("  [OK] promote closed parent+candidate, opened new persona (bitemporal)")

    # ============================================================
    # 6. run_persona_ab_test with same persona on both sides -> tie
    # ============================================================
    _hr("[6] run_persona_ab_test (same persona on both sides) -> tie")
    from talis_desk.evolution import run_persona_ab_test
    ab = run_persona_ab_test(
        specialist_id=specialist_id,
        base_version="v1.0",
        candidate_version="v1.0",
        window_days=30,
        n_cycles_per_arm=10,
        conn=conn,
    )
    print(f"  n_ideas base   : {ab.n_ideas_base}")
    print(f"  n_ideas cand   : {ab.n_ideas_candidate}")
    print(f"  base_alpha     : {ab.base_alpha_pct}")
    print(f"  cand_alpha     : {ab.candidate_alpha_pct}")
    print(f"  alpha_delta    : {ab.alpha_delta_pct}")
    print(f"  base_brier     : {ab.base_brier_avg}")
    print(f"  cand_brier     : {ab.candidate_brier_avg}")
    print(f"  significance   : {ab.significance:.4f}")
    print(f"  winner         : {ab.winner}")
    print(f"  recommendation : {ab.recommendation}")
    print(f"  quality_flags  : {ab.quality_flags}")
    if ab.winner != "tie":
        print(f"  FAIL: expected winner='tie', got {ab.winner!r}")
        return 1
    print("  [OK] identical-persona A/B returns tie")

    # ============================================================
    # 7. rollback_persona smoke
    # ============================================================
    _hr("[7] rollback_persona to v1.0 (bitemporal append-only)")
    from talis_desk.evolution import rollback_persona
    rolled = rollback_persona(
        specialist_id=specialist_id,
        target_version="v1.0",
        reason="smoke_rollback_test",
        conn=conn,
    )
    print(f"  rolled id      : {rolled.id}")
    print(f"  version        : {rolled.persona_version}")
    # We expect at least 2 historical rows for v1.0 now (initial seed + rollback re-instate)
    n_v10_rows = conn.execute(
        "SELECT COUNT(*) FROM specialist_states "
        "WHERE specialist_id = ? AND persona_version = 'v1.0' "
        "AND state_kind = 'persona'",
        (specialist_id,),
    ).fetchone()[0]
    print(f"  n historical v1.0 rows: {n_v10_rows}")
    if n_v10_rows < 2:
        print("  FAIL: rollback did not append a new v1.0 persona row")
        return 1
    # We also expect a 'rollback' audit row
    n_rollback = conn.execute(
        "SELECT COUNT(*) FROM specialist_states "
        "WHERE specialist_id = ? AND state_kind = 'rollback'",
        (specialist_id,),
    ).fetchone()[0]
    if n_rollback < 1:
        print("  FAIL: rollback audit row not written")
        return 1
    print("  [OK] rollback appended audit + re-opened persona (append-only)")

    _hr("PHASE 6 EVOLUTION + REWARDS — READY")
    print(f"  smoke db: {db_path}")
    print(f"  candidates produced this run: {len(candidates)}")
    print(f"  ab_test winner (identical arms): {ab.winner}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
