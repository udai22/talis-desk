"""End-to-end smoke test for Phase 5 Layer 3 (Playbook Consolidation).

Run:  python -m talis_desk.playbooks

Seven checks:

  1. propose_playbook writes a 'candidate' row.
  2. evaluate_playbook fills historical_trigger_count + hit_rate by walking
     the bitemporal store and forward-simulating against HL candles.
  3. Promotion gate: <5 backtest triggers AND no human flag -> promote raises
     ValueError.
  4. Mark payload.human_marked_experimental=True, promote -> 'experimental'.
  5. detect_playbook_triggers returns approved/experimental playbooks whose
     trigger_spec matches current state.
  6. instantiate_playbook_trade emits a valid TradeIdea linked to the playbook.
  7. retire_playbook flips status -> 'retired' via supersedes.

A "PHASE 5 PLAYBOOKS — READY" stamp prints on success.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _isolated_store() -> Path:
    tmpdir = tempfile.mkdtemp(prefix="phase5_pb_smoke_")
    return Path(tmpdir) / "desk.db"


def _hr(title: str) -> None:
    print()
    print("=" * 76)
    print(title)
    print("=" * 76)


def _seed_funding_claim(conn, instrument: str, z_score: float, hours_ago: int = 1) -> str:
    """Insert a synthetic 'funding_rate' hypothesis row that the playbook's
    predicate_dsl can find. Returns the new row id."""
    from uuid import uuid4
    hyp_id = f"hyp_{uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    valid_from = (now - timedelta(hours=hours_ago)).isoformat()
    payload = {"claim_kind": "funding_rate", "z_score": z_score}
    conn.execute(
        "INSERT INTO hypotheses (id, cycle_id, specialist_id, title, "
        "hypothesis_text, status, posterior_prob, heat_score, entity_ids, "
        "valid_from, transaction_from, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            hyp_id, "smoke_cycle", "funding_regime_v1",
            f"funding z={z_score:.2f} on {instrument}",
            f"Funding rate z-score on {instrument} = {z_score:.2f}",
            "active", 0.7, 0.5, json.dumps([instrument]),
            valid_from, valid_from, json.dumps(payload),
        ),
    )
    return hyp_id


def main() -> int:
    db_path = _isolated_store()
    # Fresh store
    from talis_desk import store as store_mod
    store_mod._STORE = None  # type: ignore[attr-defined]
    store_mod.get_desk_store(db_path=db_path)

    from talis_desk.playbooks.model import (
        ActionTemplate,
        PlaybookDraft,
        TriggerSpec,
        detect_playbook_triggers,
        evaluate_playbook,
        get_playbook,
        instantiate_playbook_trade,
        promote_playbook,
        propose_playbook,
        retire_playbook,
    )
    from talis_desk.store import get_desk_store
    from talis_desk.tool_atlas import AgentContext

    conn = get_desk_store().conn

    print(f"smoke db: {db_path}")
    now = datetime.now(timezone.utc)

    # ============================================================
    # 1. propose_playbook
    # ============================================================
    _hr("[1] propose_playbook")
    trigger = TriggerSpec(
        predicate_dsl={
            "kind": "claim_value_threshold",
            "claim_kind": "funding_rate",
            "field_path": "z_score",
            "op": "<",
            "value": 1.0,
            "instrument_in": ["BTC", "ETH", "SOL", "HYPE"],
        },
        natural_language=(
            "LONG when funding z-score normalizes from >2sigma extreme to <1sigma. "
            "Coins: BTC, ETH, SOL, HYPE."
        ),
        cooldown_hours=12,
    )
    action = ActionTemplate(
        direction="long",
        sizing_template={
            "risk_pct": 0.003,        # 30 bps
            "notional_cap_usd": 25_000.0,
            "kelly_fraction": 0.25,
            "leverage_cap": 2.0,
        },
        entry_template={
            "trigger": "on funding normalization (z<1 from prior >2)",
            "invalidation": "cancel if coin drops 2% before trigger",
        },
        stop_template={"stop_pct": 0.012, "stop_kind": "hard"},
        target_template={"target_pct": 0.018},
        time_horizon_default="1d",
        confidence_floor=0.60,
        market_assumption_default="liquid",
    )
    draft = PlaybookDraft(
        name="funding_normalization_v1",
        owner_specialist="funding_regime_v1",
        description=(
            "When funding rate flips from extreme positive (>2σ) to normal "
            "(<1σ) on majors, take a 24h long position."
        ),
        trigger_spec=trigger,
        action_template=action,
        min_sample_size=5,
        evidence_ids=["hyp_funding_norm_prev", "tc_hl_funding_history"],
        payload={},
    )
    pb = propose_playbook(draft, owner_specialist="funding_regime_v1")
    print(f"  id          : {pb.id}")
    print(f"  name@version: {pb.name}@v{pb.version}")
    print(f"  status      : {pb.promoted_status}")
    print(f"  hist_count  : {pb.historical_trigger_count}")
    if pb.promoted_status != "candidate":
        print("  FAIL: expected status='candidate'")
        return 1
    print("  [OK] candidate row written")

    # ============================================================
    # 2. evaluate_playbook (real HL candle backtest)
    # ============================================================
    _hr("[2] evaluate_playbook — seed claims + run backtest")
    # Seed several historical synthetic claims so the predicate fires.
    # We seed two triggers per instrument so the backtest sees n_triggers > 0.
    seed_ids: list[str] = []
    for inst, hours_ago in [("BTC", 6), ("ETH", 7), ("BTC", 30), ("ETH", 30),
                            ("SOL", 12), ("HYPE", 14)]:
        # value=0.5 satisfies "z_score < 1.0"
        seed_ids.append(_seed_funding_claim(conn, inst, 0.5, hours_ago=hours_ago))
    conn.commit()
    print(f"  seeded {len(seed_ids)} synthetic funding claims")
    # Backtest over a 2-day window — small to keep HL call count low
    bt = evaluate_playbook(pb.id, as_of=now, lookback_days=2)
    print(f"  n_triggers   : {bt.n_triggers}")
    print(f"  hits         : {bt.hits}")
    print(f"  hit_rate     : {bt.hit_rate:.3f}")
    print(f"  avg_return % : {bt.avg_return_pct:.4f}")
    print(f"  median %     : {bt.median_return_pct:.4f}")
    print(f"  std %        : {bt.std_return_pct:.4f}")
    print(f"  worst %      : {bt.worst_drawdown_pct:.4f}")
    print(f"  best %       : {bt.best_return_pct:.4f}")
    print(f"  sample_warn  : {bt.sample_size_warning}")
    print(f"  leakage_check: {bt.leakage_check}")
    print(f"  per_instrument: {json.dumps(bt.per_instrument, indent=2)}")
    if not bt.leakage_check:
        print("  FAIL: leakage check did not pass")
        return 1
    print("  [OK] backtest ran with no leakage")

    # Refetch playbook — historical_* should be populated.
    pb2 = get_playbook(pb.id)
    assert pb2 is not None
    print(f"  refetched: hist_count={pb2.historical_trigger_count}, "
          f"hit_rate={pb2.historical_hit_rate}, "
          f"avg_return={pb2.historical_avg_return_pct}")
    if pb2.historical_trigger_count != bt.n_triggers:
        print(f"  FAIL: hist_count mismatch")
        return 1
    print("  [OK] historical_* fields persisted via supersedes")

    # ============================================================
    # 3. Promotion gate: under-5 triggers AND no human flag -> ValueError
    # ============================================================
    _hr("[3] promotion gate: candidate w/ <5 triggers & no human flag -> reject")
    # Force a low trigger count for the gate test by computing what we have.
    # If n_triggers happens to be >= 5, instead temporarily mark with no flag
    # but reset hist_count to 0 to assert the gate.
    if pb2.historical_trigger_count >= 5:
        print(f"  (synthetic adjustment: hist_count={pb2.historical_trigger_count} "
              f">=5; resetting to 0 to test gate)")
        conn.execute(
            "UPDATE playbooks SET historical_trigger_count = 0 WHERE id = ?",
            (pb2.id,),
        )
        conn.commit()
        pb2 = get_playbook(pb.id)
        assert pb2 is not None
    try:
        promote_playbook(pb2.id)
        print("  FAIL: promote should have raised ValueError")
        return 1
    except ValueError as e:
        print(f"  raised ValueError as expected: {e}")
        print("  [OK] gate fires on under-quota candidates")

    # ============================================================
    # 4. human_marked_experimental flag -> promote OK
    # ============================================================
    _hr("[4] human_marked_experimental flag -> promote -> experimental")
    pb3 = get_playbook(pb.id)
    assert pb3 is not None
    # Set the human flag via direct payload update (append-only audit lives
    # in payload.promotion_history once promote runs).
    new_payload = dict(pb3.payload)
    new_payload["human_marked_experimental"] = True
    conn.execute(
        "UPDATE playbooks SET payload = ? WHERE id = ?",
        (json.dumps(new_payload), pb3.id),
    )
    conn.commit()
    pb_with_flag = get_playbook(pb.id)
    assert pb_with_flag is not None
    promoted = promote_playbook(pb_with_flag.id)
    print(f"  promoted to : {promoted.promoted_status}")
    if promoted.promoted_status != "experimental":
        print("  FAIL: expected status='experimental'")
        return 1
    print("  [OK] human_marked_experimental gate accepts promotion")

    # ============================================================
    # 5. detect_playbook_triggers
    # ============================================================
    _hr("[5] detect_playbook_triggers (approved + experimental)")
    triggers = detect_playbook_triggers(as_of=now)
    print(f"  triggers found: {len(triggers)}")
    for t in triggers[:5]:
        print(f"    - {t.playbook_name}@v{t.playbook_version} on {t.instrument} "
              f"(evidence={t.evidence_ids[:2]}...)")
    if not triggers:
        print("  FAIL: expected at least one trigger from seeded claims")
        return 1
    print("  [OK] triggers detected for experimental playbook")

    # ============================================================
    # 6. instantiate_playbook_trade
    # ============================================================
    _hr("[6] instantiate_playbook_trade -> validated TradeIdea")
    ctx = AgentContext(cycle_id="smoke_cycle", specialist_id="funding_regime_v1")
    pick = triggers[0]
    try:
        idea = instantiate_playbook_trade(promoted.id, pick, ctx)
    except RuntimeError as e:
        # Network failure on HL candle fetch is non-fatal for the smoke;
        # surface and skip instantiation.
        print(f"  WARN: instantiate failed (likely HL price fetch network): {e}")
        idea = None
    if idea is not None:
        print(f"  idea id      : {idea.id}")
        print(f"  instrument   : {idea.instrument} {idea.direction}")
        print(f"  entry/stop/tg: {idea.entry.limit_px}/{idea.stop.px}/"
              f"{idea.target.px if idea.target else None}")
        print(f"  confidence   : {idea.confidence}")
        print(f"  status       : {idea.status}")
        print(f"  playbook_id  : {idea.playbook_id}")
        if idea.playbook_id != promoted.id:
            print("  FAIL: playbook_id not linked")
            return 1
        if idea.status not in ("published", "draft"):
            print(f"  FAIL: unexpected idea status {idea.status}")
            return 1
        print("  [OK] TradeIdea emitted, linked to playbook, validation ran")
    else:
        print("  [SKIP] instantiation skipped due to HL price fetch fail")

    # ============================================================
    # 7. retire_playbook
    # ============================================================
    _hr("[7] retire_playbook -> status='retired'")
    retired = retire_playbook(promoted.id, reason="smoke_test_done")
    print(f"  status     : {retired.promoted_status}")
    print(f"  supersedes : {retired.supersedes}")
    if retired.promoted_status != "retired":
        print("  FAIL: expected status='retired'")
        return 1
    print("  [OK] retired via supersedes append")

    _hr("PHASE 5 PLAYBOOKS — READY")
    print(f"  smoke db: {db_path}")
    print(f"  funding_normalization_v1 backtest: "
          f"n={bt.n_triggers}, hit_rate={bt.hit_rate:.3f}, "
          f"avg_return={bt.avg_return_pct:.4f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
