"""Phase 3 smoke test — Trade Idea Pipeline + Resolver.

Seeds 3 fake ideas at historical timestamps, runs the full lifecycle:
draft -> validate -> emit -> resolve -> trade_book_metrics.

Run with:

    python -m talis_desk.trade_ideas

Exits 0 on success. Prints a full report. Uses a scratch desk.db so the
smoke run doesn't pollute the dev database.

Honest gaps (also see model.py + resolver.py docstrings):
  - Benchmark fetches live HL candles (one network call per top-10 coin per
    idea). On a clean run that's ~30 HTTP calls; cached so reruns are cheap.
  - Brier on 3 ideas is not a meaningful statistic — this is a wiring test,
    not a calibration test. 30d ideas needed before Brier means anything.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from talis_desk import (
    apply_sota_schema,
    build_replay_context,
)
from talis_desk.store import DeskStore
from talis_desk.trade_ideas import (
    TradeIdea,
    TradeIdeaDraft,
    SizingPlan,
    EntryPlan,
    StopPlan,
    TargetPlan,
    ContradictionItem,
    validate_trade_idea,
    emit_trade_idea,
)
from talis_desk.eval import (
    resolve_trade_idea,
    resolve_all_due,
    compute_trade_book_metrics,
    attribute_alpha,
)
from talis_desk.tool_atlas import AgentContext


# ============================================================================
# Helpers
# ============================================================================

def _print_div(s: str) -> None:
    print()
    print("=" * 78)
    print(s)
    print("=" * 78)


def _section(s: str) -> None:
    print()
    print(f"-- {s} " + "-" * (74 - len(s)))


def _fmt_pct(v):
    if v is None:
        return "  None  "
    return f"{v:+8.4f}%"


# ============================================================================
# Seeds
# ============================================================================

def _seed_btc_long(now: datetime, cycle_id: str) -> TradeIdeaDraft:
    """Idea 1: BTC long, 24h horizon, entry on funding normalization.

    Window is historical (we can resolve against actual HL candles). We seed
    the published_at by setting valid_from to (now - 36h); expires_at = +24h
    from valid_from so it should be due for resolution by `now`.
    """
    valid_from = now - timedelta(hours=36)
    expires_at = valid_from + timedelta(hours=24)
    # Entry ~ historical BTC price around that time; resolver will use HL
    # candleSnapshot's first close as the actual entry. For the model we
    # just need internally-consistent stop/target relative to a placeholder
    # entry price.
    entry_px = 78_000.0
    return TradeIdeaDraft(
        cycle_id=cycle_id,
        specialist_id="microstructure_v3",
        persona_version="v3.2026-05-19",
        instrument="BTC",
        direction="long",
        sizing=SizingPlan(
            risk_pct=0.0035,     # 35 bps
            notional_cap_usd=50_000.0,
            kelly_fraction=0.25,
            leverage_cap=2.0,
        ),
        entry=EntryPlan(
            trigger="on funding normalization (z<+1.5 from current >+2.4σ)",
            limit_px=entry_px,
            market_assumption="liquid",
            invalidation="cancel if BTC closes below 76k before trigger",
        ),
        stop=StopPlan(
            px=entry_px * 0.99,   # -1%
            max_loss_usd=entry_px * 0.01 * 0.5,  # placeholder
            stop_kind="hard",
        ),
        target=TargetPlan(
            px=entry_px * 1.012,
            take_profit_pct=1.2,
        ),
        time_horizon="1d",
        edge_thesis=(
            "BTC funding sits at +2.4σ extreme while L5 OFI just flipped from "
            "toxic sell to passive bid refill. Historical playbook: 24h fwd "
            "return median +0.6% when these align."
        ),
        claim_ids=["cl_btc_funding_z24", "cl_btc_ofi_flip"],
        hypothesis_ids=["hyp_funding_norm_24h"],
        tool_call_ids=["tc_hl_funding_btc", "tc_hl_l5_ofi_btc"],
        contradicting_evidence=[
            ContradictionItem(
                claim_id="cl_macro_dxy_risk_off",
                reason="DXY breaking above 105 + Treasury auction stress is risk-off",
                weight=0.6,
            ),
        ],
        confluence_score=0.71,
        confidence=0.72,
        expires_at=expires_at,
        valid_from=valid_from,
        payload={"playbook_ref": "funding_squeeze_v1"},
    )


def _seed_hype_short(now: datetime, cycle_id: str) -> TradeIdeaDraft:
    """Idea 2: HYPE short, 12h horizon, fade noob cluster.

    The noob-cluster citation is synthetic — Phase 2's reject_corpus would
    have real wallet ids. Validation only requires a non-empty claim_id, so
    this is fine.
    """
    valid_from = now - timedelta(hours=18)
    expires_at = valid_from + timedelta(hours=12)
    entry_px = 40.0
    return TradeIdeaDraft(
        cycle_id=cycle_id,
        specialist_id="noob_fade_v2",
        persona_version="v2.2026-05-12",
        instrument="HYPE",
        direction="short",
        sizing=SizingPlan(
            risk_pct=0.0025,    # 25 bps
            notional_cap_usd=20_000.0,
            kelly_fraction=0.20,
            leverage_cap=1.5,
        ),
        entry=EntryPlan(
            trigger="market (immediate after noob-cluster spike)",
            limit_px=entry_px,
            market_assumption="thin_book",
            invalidation="cancel if HYPE breaks above 42 with vol > 2x 24h avg",
        ),
        stop=StopPlan(
            px=entry_px * 1.015,
            max_loss_usd=entry_px * 0.015 * 0.5,
            stop_kind="hard",
        ),
        target=TargetPlan(
            px=entry_px * 0.985,
            take_profit_pct=1.5,
        ),
        time_horizon="12h",
        edge_thesis=(
            "Noob-cluster wallets (reject_corpus tier-A, n=14 in last 6h) just "
            "loaded longs at $40 after FOMO bar. Historical: 75% revert within "
            "12h, median -1.2%."
        ),
        claim_ids=["cl_hype_noob_cluster_load"],
        hypothesis_ids=["hyp_noob_fade_hype_12h"],
        tool_call_ids=["tc_wallet_pnl_noob", "tc_hl_l2_hype"],
        contradicting_evidence=[
            ContradictionItem(
                claim_id="cl_hype_treasury_buyback_rumor",
                reason="Unconfirmed treasury buyback rumor circulating on X",
                weight=0.4,
            ),
        ],
        confluence_score=0.68,
        confidence=0.71,
        expires_at=expires_at,
        valid_from=valid_from,
        payload={},
    )


def _seed_eth_spread(now: datetime, cycle_id: str) -> TradeIdeaDraft:
    """Idea 3: ETH neutral spread, 3d horizon — tests 'spread' direction.

    Confidence kept BELOW 0.7 so no contradiction is required — this also
    exercises gate 3 (the contradiction-required gate). We do include one
    contradiction anyway for realism.
    """
    valid_from = now - timedelta(hours=96)
    expires_at = valid_from + timedelta(hours=72)  # 3d
    entry_px = 3_100.0
    return TradeIdeaDraft(
        cycle_id=cycle_id,
        specialist_id="vol_regime_v1",
        persona_version="v1.2026-05-01",
        instrument="ETH",
        direction="spread",
        sizing=SizingPlan(
            risk_pct=0.0020,
            notional_cap_usd=30_000.0,
            kelly_fraction=0.15,
            leverage_cap=1.0,
        ),
        entry=EntryPlan(
            trigger="market spread entry (long IV / short RV proxy)",
            limit_px=entry_px,
            market_assumption="liquid",
            invalidation="cancel if 7d RV breaks above IV",
        ),
        stop=StopPlan(
            px=entry_px * 0.93,   # absolute deviation > 7% kills the spread
            max_loss_usd=entry_px * 0.07 * 0.3,
            stop_kind="time",
        ),
        target=TargetPlan(
            px=entry_px,           # spread target = mean reversion to entry
            take_profit_pct=0.0,
        ),
        time_horizon="3d",
        edge_thesis=(
            "ETH 7d IV at 86 vs 30d RV at 58 — 28-vol-point gap, top quartile "
            "of 90d distribution. Funding flat; basis stable. Spread should "
            "compress over a 3d window absent a macro tape bomb."
        ),
        claim_ids=["cl_eth_iv_rv_spread_top_quartile"],
        hypothesis_ids=["hyp_eth_vol_compression_3d"],
        tool_call_ids=["tc_deribit_iv_eth", "tc_hl_candles_eth"],
        contradicting_evidence=[
            ContradictionItem(
                claim_id="cl_eth_eip_pending_catalyst",
                reason="EIP merge window in 4d could spike RV",
                weight=0.5,
            ),
        ],
        confluence_score=0.55,
        confidence=0.55,
        expires_at=expires_at,
        valid_from=valid_from,
        payload={},
    )


# ============================================================================
# Smoke runner
# ============================================================================

def main() -> int:
    _print_div("PHASE 3 — TRADE IDEA PIPELINE + RESOLVER SMOKE TEST")

    tmp = tempfile.mkdtemp(prefix="phase3_")
    db_path = Path(tmp) / "desk_smoke.db"
    print(f"  tmpdir: {tmp}")
    print(f"  db    : {db_path}")

    store = DeskStore(db_path=db_path)
    # Point the singleton at our scratch DB so emit/resolve write here.
    import talis_desk.store as _ds
    _ds._STORE = store  # type: ignore[attr-defined]

    now = datetime.now(timezone.utc)
    cycle_id = "smoke_phase3"
    ctx = AgentContext(cycle_id=cycle_id, specialist_id="smoke_runner")

    _section("(1) Build 3 drafts")
    drafts = [
        ("BTC long 24h",  _seed_btc_long(now, cycle_id)),
        ("HYPE short 12h", _seed_hype_short(now, cycle_id)),
        ("ETH spread 3d",  _seed_eth_spread(now, cycle_id)),
    ]
    for label, d in drafts:
        print(f"    - {label:<18}  conf={d.confidence:.2f} "
              f"horizon={d.time_horizon} expires_at={d.expires_at.isoformat()}")

    _section("(2) Validate (expect ok=True for all 3)")
    validation_results = []
    all_ok = True
    for label, d in drafts:
        idea_obj = d.to_trade_idea()
        rep = validate_trade_idea(idea_obj)
        validation_results.append((label, rep))
        print(f"    - {label:<18}  ok={rep.ok}  "
              f"errors={len(rep.errors)} warnings={len(rep.warnings)}")
        if rep.errors:
            for e in rep.errors:
                print(f"        ! err: {e}")
            all_ok = False
        if rep.warnings:
            for w in rep.warnings:
                print(f"        ~ warn: {w}")
    if not all_ok:
        print("FAIL: validation errors above")
        return 1

    _section("(3) Emit all 3 (write to desk.db + post to trade_book)")
    emitted: list[TradeIdea] = []
    for label, d in drafts:
        idea = emit_trade_idea(d, ctx)
        emitted.append(idea)
        print(f"    - {label:<18}  id={idea.id} status={idea.status} "
              f"published_at={idea.published_at}")

    _section("(4) Inspect mv_trade_book_open (expect 3 rows)")
    rows_open = store.conn.execute(
        "SELECT id, instrument, direction, confidence, expires_at "
        "FROM mv_trade_book_open ORDER BY published_at"
    ).fetchall()
    print(f"    rows in mv_trade_book_open: {len(rows_open)}")
    for r in rows_open:
        print(f"      - {r['id']} {r['instrument']:<5} {r['direction']:<6} "
              f"conf={r['confidence']:.2f} expires={r['expires_at']}")
    if len(rows_open) != 3:
        print(f"FAIL: expected 3 in mv_trade_book_open, got {len(rows_open)}")
        return 1

    _section("(5) Inspect agent_messages topic=trade_book (expect 3 rows)")
    msg_rows = store.conn.execute(
        "SELECT id, from_agent, message_kind, related_trade_idea_id "
        "FROM agent_messages WHERE to_agent_or_topic = 'trade_book' "
        "ORDER BY posted_at"
    ).fetchall()
    for m in msg_rows:
        print(f"      - msg {m['id']} from={m['from_agent']} "
              f"kind={m['message_kind']} -> idea={m['related_trade_idea_id']}")
    if len(msg_rows) != 3:
        print(f"FAIL: expected 3 trade_book messages, got {len(msg_rows)}")
        return 1

    _section("(6) Resolve each via resolve_trade_idea (fills realized fields)")
    outcomes = []
    for idea in emitted:
        outcome = resolve_trade_idea(idea.id, as_of=now, conn=store.conn)
        outcomes.append((idea, outcome))
        print(
            f"    - {idea.instrument:<5} {idea.direction:<6}  "
            f"pnl_pct={_fmt_pct(outcome.realized_pnl_pct)}  "
            f"after_fees={_fmt_pct(outcome.realized_return_after_fees_pct)}  "
            f"bench={_fmt_pct(outcome.benchmark_return_pct)}  "
            f"alpha={_fmt_pct(outcome.contributed_alpha_pct)}  "
            f"brier={outcome.brier if outcome.brier is not None else 'None'}"
        )
        print(f"      status={outcome.status} new_id={outcome.new_idea_id} "
              f"exit_reason={outcome.realized_outcome.get('exit_reason')}")

    _section("(7) mv_trade_book_open after resolution (expect 0)")
    open_after = store.conn.execute(
        "SELECT id FROM mv_trade_book_open"
    ).fetchall()
    print(f"    rows in mv_trade_book_open: {len(open_after)}")
    if len(open_after) != 0:
        print("FAIL: expected 0 open ideas post-resolution")
        return 1

    _section("(8) Idempotency — re-resolve all 3, expect no new rows")
    pre_count = store.conn.execute(
        "SELECT count(*) AS n FROM trade_ideas"
    ).fetchone()["n"]
    for idea, _ in outcomes:
        again = resolve_trade_idea(idea.id, as_of=now, conn=store.conn)
        assert again.notes[0].startswith("already_terminal_status="), \
            f"expected idempotent no-op, got notes={again.notes}"
    post_count = store.conn.execute(
        "SELECT count(*) AS n FROM trade_ideas"
    ).fetchone()["n"]
    print(f"    pre_count={pre_count}  post_count={post_count}  "
          f"delta={post_count - pre_count} (expect 0)")
    if post_count != pre_count:
        print("FAIL: idempotency violated — extra rows inserted")
        return 1

    _section("(9) Bitemporal replay — original row visible at published_at")
    # Replay at the FIRST idea's published_at; we should see the ORIGINAL
    # 'published' row, not the 'closed' supersedes row.
    first_idea = emitted[0]
    pub_at = first_idea.published_at
    assert pub_at is not None
    ctx_replay = build_replay_context(
        as_of_valid=pub_at + timedelta(seconds=1),
        as_of_transaction=pub_at + timedelta(seconds=1),
    )
    where, params = ctx_replay.where_clause("trade_ideas")
    sql = (f"SELECT id, status FROM trade_ideas WHERE id = ? AND {where}")
    rows = store.conn.execute(sql, [first_idea.id, *params]).fetchall()
    print(f"    replay at published_at+1s -> rows={len(rows)}")
    for r in rows:
        print(f"      - {r['id']} status={r['status']}")
    if not rows or rows[0]["status"] != "published":
        print(f"FAIL: expected 'published' at replay, got {[dict(r) for r in rows]}")
        return 1

    _section("(10) Bitemporal replay — closed row visible at as_of=now")
    # Use a current-time anchor: resolution writes valid_from = wall-clock at
    # write time, which may be many seconds past the captured `now`.
    now_after = datetime.now(timezone.utc) + timedelta(seconds=1)
    ctx_now = build_replay_context(
        as_of_valid=now_after,
        as_of_transaction=now_after,
    )
    where2, params2 = ctx_now.where_clause("trade_ideas")
    sql2 = (f"SELECT id, supersedes, status FROM trade_ideas "
             f"WHERE supersedes = ? AND {where2}")
    closed_rows = store.conn.execute(sql2, [first_idea.id, *params2]).fetchall()
    print(f"    replay at now_after -> supersedes rows={len(closed_rows)}")
    for r in closed_rows:
        print(f"      - {r['id']} supersedes={r['supersedes']} status={r['status']}")
    if not closed_rows or closed_rows[0]["status"] not in ("closed", "expired", "invalidated"):
        print("FAIL: expected closed/expired/invalidated supersedes row")
        return 1

    _section("(11) Alpha attribution (simple) for idea 1")
    attr = attribute_alpha(first_idea.id, method="simple", conn=store.conn)
    print(f"    total_alpha_pct={_fmt_pct(attr.total_alpha_pct)}  "
          f"components={len(attr.components)}")
    for c in attr.components:
        print(f"      - {c['kind']:<12} {c['id']:<32}  "
              f"weight={c['weight']:.3f}  alpha={c['alpha_pct']:+.4f}%")

    _section("(12) Trade book metrics (window=30d)")
    metrics = compute_trade_book_metrics(window_days=30, conn=store.conn)
    print(f"    n_closed                  = {metrics.n_closed}")
    print(f"    hit_rate                  = "
          f"{metrics.hit_rate if metrics.hit_rate is None else f'{metrics.hit_rate*100:.1f}%'}")
    print(f"    avg_return_after_fees_pct = {_fmt_pct(metrics.avg_return_after_fees_pct)}")
    print(f"    avg_alpha_pct             = {_fmt_pct(metrics.avg_alpha_pct)}")
    print(f"    sharpe                    = "
          f"{metrics.sharpe if metrics.sharpe is not None else 'None'}")
    print(f"    brier_avg                 = "
          f"{metrics.brier_avg if metrics.brier_avg is not None else 'None'}")
    print(f"    biggest_loss              = "
          f"{_fmt_pct(metrics.biggest_loss_pct)} ({metrics.biggest_loss_idea_id})")
    print(f"    best_alpha                = "
          f"{_fmt_pct(metrics.best_alpha_pct)} ({metrics.best_alpha_idea_id})")
    print(f"    worst_alpha               = "
          f"{_fmt_pct(metrics.worst_alpha_pct)} ({metrics.worst_alpha_idea_id})")

    _section("(13) resolve_all_due dry-run (expect 0 due)")
    rep = resolve_all_due(as_of=now, conn=store.conn)
    print(f"    run_id={rep.run_id}  n_due={rep.n_due}  n_resolved={rep.n_resolved}  "
          f"n_errors={rep.n_errors}")
    if rep.n_due != 0:
        print(f"FAIL: expected 0 due after all-resolved, got {rep.n_due}")
        return 1

    _print_div("PHASE 3 — READY")
    print(f"  db: {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
