"""End-to-end smoke test for Phase 5 Section 6 (Multi-Agent Debate).

Run:  python -m talis_desk.debate

Eight checks:

  1. Construct synthetic high-confidence trade idea (BTC long, conf=0.80).
  2. open_debate w/ two specialists in DIFFERENT model families ->
     judge picked from a third family.
  3. Verify request_devils_advocate messages posted to both participants.
  4. submit_debate_argument validates <=200 words + citations resolve.
  5. >200 words -> ValueError.
  6. After both arguments in, judge_debate auto-fires + returns structured
     verdict (Sonnet if ANTHROPIC_API_KEY set, else stub).
  7. apply_debate_verdict writes specialist_states mutation_candidate row.
  8. follow_up_action with downgrade_claim wires through update_posterior.

A "PHASE 5 DEBATE — READY" stamp prints on success.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _isolated_store() -> Path:
    tmpdir = tempfile.mkdtemp(prefix="phase5_debate_smoke_")
    return Path(tmpdir) / "desk.db"


def _hr(title: str) -> None:
    print()
    print("=" * 76)
    print(title)
    print("=" * 76)


def main() -> int:
    db_path = _isolated_store()
    from talis_desk import store as store_mod
    store_mod._STORE = None  # type: ignore[attr-defined]
    store_mod.get_desk_store(db_path=db_path)

    from talis_desk.debate.judge import _pick_judge_provider, _provider_family
    from talis_desk.debate.runner import (
        apply_debate_verdict,
        judge_debate,
        open_debate,
        submit_debate_argument,
    )
    from talis_desk.hypotheses.model import propose_hypothesis
    from talis_desk.hypotheses import HypothesisDraft
    from talis_desk.store import get_desk_store
    from talis_desk.tool_atlas import AgentContext
    from talis_desk.trade_ideas import (
        ContradictionItem,
        EntryPlan,
        SizingPlan,
        StopPlan,
        TargetPlan,
        TradeIdeaDraft,
        emit_trade_idea,
    )

    conn = get_desk_store().conn

    print(f"smoke db: {db_path}")
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    print(f"ANTHROPIC_API_KEY present: {has_key}")
    print(f"(judge will use real Sonnet)" if has_key else "(stub judge fallback path)")

    # ============================================================
    # 0. Setup synthetic specialists' persona states so the judge picker
    #    sees their model families. macro_regime_v2 -> anthropic;
    #    microstructure_v3 -> openai. So judge should pick a third family.
    # ============================================================
    _hr("[0] Seed specialist personas (different model families)")
    now = datetime.now(timezone.utc)
    for spec_id, model in [
        ("macro_regime_v2", "anthropic:claude-sonnet-4-6"),
        ("microstructure_v3", "openai:gpt-5.5"),
    ]:
        conn.execute(
            "INSERT INTO specialist_states "
            "(id, specialist_id, persona_version, cycle_id, state_kind, "
            " state_json, valid_from, transaction_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"spst_{spec_id}_seed", spec_id, "smoke_v1", "smoke_cycle",
                "persona", json.dumps({"persona_model": model}),
                now.isoformat(), now.isoformat(),
            ),
        )
    conn.commit()
    print("  seeded 2 persona rows: macro_regime_v2 (anthropic), "
          "microstructure_v3 (openai)")

    # ============================================================
    # 1. Construct synthetic BTC long with confidence=0.80
    # ============================================================
    _hr("[1] Synthetic BTC long idea (confidence=0.80)")
    ctx = AgentContext(cycle_id="smoke_cycle", specialist_id="microstructure_v3")
    valid_from = now - timedelta(hours=1)
    expires_at = valid_from + timedelta(hours=24)
    entry_px = 78_000.0

    # Seed two hypotheses to act as cite-able claims
    h1 = propose_hypothesis(
        HypothesisDraft(
            cycle_id="smoke_cycle", specialist_id="microstructure_v3",
            title="BTC funding regime supports long",
            hypothesis_text="Funding z-score normalized + L5 OFI flipped to passive bid refill.",
            posterior_prob=0.75, heat_score=0.6, entity_ids=["BTC"],
            valid_from=valid_from,
            payload={"claim_kind": "regime", "side": "pro_long"},
        ),
        ctx,
    )
    h2 = propose_hypothesis(
        HypothesisDraft(
            cycle_id="smoke_cycle", specialist_id="macro_regime_v2",
            title="DXY breakout is risk-off",
            hypothesis_text="DXY breaking 105 + Treasury auction stress argues against BTC longs.",
            posterior_prob=0.65, heat_score=0.5, entity_ids=["BTC", "DXY"],
            valid_from=valid_from,
            payload={"claim_kind": "regime", "side": "anti_long"},
        ),
        ctx,
    )
    print(f"  seeded hypotheses: {h1.id} (pro-long), {h2.id} (anti-long)")

    draft = TradeIdeaDraft(
        cycle_id="smoke_cycle",
        specialist_id="microstructure_v3",
        persona_version="v3.smoke",
        instrument="BTC",
        direction="long",
        sizing=SizingPlan(risk_pct=0.0040, notional_cap_usd=50_000.0,
                          kelly_fraction=0.25, leverage_cap=2.0),
        entry=EntryPlan(
            trigger="market", limit_px=entry_px,
            market_assumption="liquid",
            invalidation="cancel if BTC closes below 76k before trigger",
        ),
        stop=StopPlan(px=entry_px * 0.99,
                      max_loss_usd=entry_px * 0.01 * 0.5, stop_kind="hard"),
        target=TargetPlan(px=entry_px * 1.012, take_profit_pct=1.2),
        time_horizon="1d",
        edge_thesis=(
            "BTC funding sits at +2.4sigma extreme and L5 OFI just flipped "
            "from toxic sell to passive bid refill. Historical playbook: "
            "24h fwd return median +0.6%."
        ),
        claim_ids=[h1.id],
        hypothesis_ids=[h1.id],
        contradicting_evidence=[
            ContradictionItem(claim_id=h2.id,
                              reason="DXY breakout + auction stress argues risk-off",
                              weight=0.55),
        ],
        confluence_score=0.7,
        confidence=0.80,
        expires_at=expires_at,
        valid_from=valid_from,
    )
    idea = emit_trade_idea(draft, ctx)
    print(f"  idea id : {idea.id}")
    print(f"  status  : {idea.status} (expect 'published')")
    if idea.status != "published":
        print("  FAIL: idea did not validate to published")
        return 1
    print("  [OK] high-conf BTC long emitted")

    # ============================================================
    # 2. open_debate -> judge picked from a different family
    # ============================================================
    _hr("[2] open_debate -> judge picked from a different provider family")
    participants = ["microstructure_v3", "macro_regime_v2"]
    deb = open_debate(
        trigger_kind="high_confidence",
        trigger_id=idea.id,
        participants=participants,
        due_in_minutes=30,
        context=ctx,
    )
    print(f"  debate id     : {deb.id}")
    print(f"  judge_model   : {deb.judge_model}")
    print(f"  judge_provider: {deb.judge_provider}")
    print(f"  participants  : {deb.participants}")
    # Verify judge family is different from both participants
    p_families = {
        _provider_family("anthropic:claude-sonnet-4-6"),
        _provider_family("openai:gpt-5.5"),
    }
    if deb.judge_provider in p_families:
        print(f"  FAIL: judge_provider {deb.judge_provider} matches a participant family")
        return 1
    print(f"  [OK] judge provider {deb.judge_provider} is distinct from {p_families}")

    # ============================================================
    # 3. request_devils_advocate messages
    # ============================================================
    _hr("[3] request_devils_advocate messages posted")
    rows = conn.execute(
        "SELECT to_agent_or_topic, message_kind, payload FROM agent_messages "
        "WHERE message_kind = 'request_devils_advocate' "
        "AND from_agent = 'debate_runner' "
        "AND transaction_to IS NULL"
    ).fetchall()
    targets = sorted(r["to_agent_or_topic"] for r in rows)
    print(f"  recipients: {targets}")
    if set(targets) != set(participants):
        print("  FAIL: not all participants notified")
        return 1
    print("  [OK] both participants notified")

    # ============================================================
    # 4. submit_debate_argument (valid)
    # ============================================================
    _hr("[4] submit_debate_argument (valid)")
    arg_a_md = (
        "L5 OFI just flipped from toxic sell to passive bid refill while "
        "funding z-score normalized from +2.4 to +0.8. Both are independent "
        "microstructure signals. Historical playbook shows +0.6% median 24h "
        "forward return when these align. Risk is asymmetric: 1% stop vs "
        "1.2% target."
    )
    a1 = submit_debate_argument(
        debate_id=deb.id, agent_id="microstructure_v3",
        argument_md=arg_a_md, citation_ids=[h1.id],
        falsifiable_crux=(
            "If BTC closes below 76k in the next 4h before funding "
            "re-tests <0, the regime call is wrong."
        ),
        persona_version="v3.smoke",
    )
    print(f"  arg A submitted by {a1.agent_id} ({len(a1.argument_md.split())} words)")

    arg_b_md = (
        "DXY breaking 105 with conviction + Treasury auction tail stress "
        "is classic risk-off macro. Even with friendly micro, every long "
        "BTC entry into a DXY breakout in 2024-26 has under-performed "
        "median by 40 bps. Wait for DXY rejection or auction relief first."
    )
    a2 = submit_debate_argument(
        debate_id=deb.id, agent_id="macro_regime_v2",
        argument_md=arg_b_md, citation_ids=[h2.id],
        falsifiable_crux=(
            "If DXY closes back below 104 in the next 12h, the macro veto lifts."
        ),
        persona_version="v2.smoke",
    )
    print(f"  arg B submitted by {a2.agent_id} ({len(a2.argument_md.split())} words)")
    # submit_debate_argument auto-fires judge once both in
    print("  [OK] arguments accepted; judge auto-fired")

    # ============================================================
    # 5. Word-count gate: >200 words -> ValueError
    # ============================================================
    _hr("[5] argument >200 words -> ValueError")
    long_str = " ".join(["foo"] * 220)
    # Create a one-off debate to use for the negative test (the current one
    # has already moved past 'awaiting_arguments').
    deb2 = open_debate(
        trigger_kind="high_confidence", trigger_id=idea.id,
        participants=["microstructure_v3", "macro_regime_v2"],
        due_in_minutes=30, context=ctx,
    )
    try:
        submit_debate_argument(
            debate_id=deb2.id, agent_id="microstructure_v3",
            argument_md=long_str, citation_ids=[h1.id],
            falsifiable_crux="if A then B.",
        )
        print("  FAIL: expected ValueError on 220-word argument")
        return 1
    except ValueError as e:
        print(f"  raised ValueError as expected: {str(e)[:90]}")
        print("  [OK] word-count gate fires")

    # ============================================================
    # 6. judge verdict on the original debate
    # ============================================================
    _hr("[6] judge verdict")
    deb_final_row = conn.execute(
        "SELECT * FROM debates WHERE id = ? OR supersedes = ? "
        "ORDER BY transaction_from DESC LIMIT 1",
        (deb.id, deb.id),
    ).fetchone()
    if deb_final_row is None:
        print("  FAIL: could not locate final debate row")
        return 1
    # Walk to final open head via supersedes chain (visit ALL rows along
    # the chain, not just transaction_to IS NULL — intermediate rows are
    # closed by the next supersedes step).
    cur_id = deb.id
    visited: set[str] = {cur_id}
    final_id = cur_id
    while True:
        nxt = conn.execute(
            "SELECT id, transaction_to FROM debates WHERE supersedes = ? "
            "ORDER BY transaction_from DESC LIMIT 1",
            (cur_id,),
        ).fetchone()
        if nxt is None:
            break
        if nxt["id"] in visited:
            break
        visited.add(nxt["id"])
        final_id = nxt["id"]
        if nxt["transaction_to"] is None:
            break
        cur_id = nxt["id"]
    print(f"  final debate row id: {final_id}")
    final = conn.execute("SELECT * FROM debates WHERE id = ?", (final_id,)).fetchone()
    print(f"  status   : {final['status']}")
    print(f"  winner   : {final['winner']}")
    print(f"  judge_conf: {final['judge_confidence']}")
    verdict_dict = json.loads(final["verdict"] or "{}")
    rationale = verdict_dict.get("rationale", "")
    print(f"  rationale: {rationale[:200]}")
    fua = verdict_dict.get("follow_up_action")
    print(f"  follow_up_action: {fua}")
    if final["status"] != "judged":
        print(f"  FAIL: expected status='judged', got {final['status']}")
        return 1
    if final["winner"] is None and not has_key:
        # Stub path: winner should be the first participant
        print("  (stub path; winner=first participant)")
    print("  [OK] verdict written")

    # ============================================================
    # 7. apply_debate_verdict -> specialist_states mutation_candidate row
    # ============================================================
    _hr("[7] apply_debate_verdict")
    patches = apply_debate_verdict(deb.id)
    print(f"  patches: {json.dumps(patches, indent=2)[:600]}")
    spst_rows = conn.execute(
        "SELECT id, specialist_id, state_kind FROM specialist_states "
        "WHERE state_kind = 'mutation_candidate' AND transaction_to IS NULL"
    ).fetchall()
    print(f"  mutation_candidate rows: {len(spst_rows)}")
    for r in spst_rows:
        print(f"    - {r['id']} (specialist={r['specialist_id']})")
    if not spst_rows:
        print("  FAIL: expected at least 1 mutation_candidate row")
        return 1
    # Check debate.status now 'applied' — walk the chain from the original
    # debate id forward to the open head.
    from talis_desk.debate.runner import _get_open_debate
    final_open = _get_open_debate(conn, deb.id)
    if final_open.status != "applied":
        print(f"  FAIL: expected debate status='applied', got {final_open.status}")
        return 1
    print(f"  [OK] mutation_candidate row written; status='applied'")

    # ============================================================
    # 8. follow_up_action with downgrade -> update_posterior
    # ============================================================
    _hr("[8] downgrade_claim follow_up wiring (manual)")
    # Even when the judge didn't produce a downgrade action, manually invoke
    # the apply path with a synthetic verdict containing one to prove the
    # plumbing.
    # Open a fresh debate against h1 directly so the downgrade target is a
    # hypothesis id.
    deb3 = open_debate(
        trigger_kind="high_confidence", trigger_id=h1.id,
        participants=["microstructure_v3", "macro_regime_v2"],
        due_in_minutes=30, context=ctx,
    )
    submit_debate_argument(
        debate_id=deb3.id, agent_id="microstructure_v3",
        argument_md="L5 OFI flipped. Long.", citation_ids=[h1.id],
        falsifiable_crux="If OFI re-flips negative within 4h, I'm wrong.",
        persona_version="v3.smoke",
    )
    submit_debate_argument(
        debate_id=deb3.id, agent_id="macro_regime_v2",
        argument_md="DXY breakout vetos.", citation_ids=[h2.id],
        falsifiable_crux="If DXY breaks back below 104, veto lifts.",
        persona_version="v2.smoke",
    )
    # Inject a synthetic verdict with downgrade_claim follow_up
    from talis_desk.debate.runner import _get_open_debate, _supersede_debate
    deb3_open = _get_open_debate(conn, deb3.id)
    from talis_desk.debate.model import DebateVerdict
    injected = DebateVerdict(
        debate_id=deb3_open.id,
        winner="macro_regime_v2",
        confidence=0.65,
        rationale="Macro veto carries on a breakout day; downgrade pro-long claim.",
        follow_up_action={"type": "downgrade_claim", "target_id": h1.id, "new_prob": 0.55},
        required_new_tool_calls=[],
        judge_model="synthetic_test_judge",
        judge_provider="test",
    )
    _supersede_debate(deb3_open, verdict=injected, status="judged")
    patches3 = apply_debate_verdict(deb3.id)
    print(f"  patches: {json.dumps(patches3, indent=2)[:600]}")
    h1_open = conn.execute(
        "SELECT posterior_prob FROM hypotheses WHERE supersedes = ? "
        "AND transaction_to IS NULL ORDER BY transaction_from DESC LIMIT 1",
        (h1.id,),
    ).fetchone()
    print(f"  h1 successor posterior_prob: {h1_open['posterior_prob'] if h1_open else None}")
    if h1_open is None:
        print("  FAIL: no successor hypothesis row found after downgrade")
        return 1
    if abs(float(h1_open["posterior_prob"]) - 0.55) > 0.001:
        print(f"  FAIL: expected posterior=0.55, got {h1_open['posterior_prob']}")
        return 1
    print("  [OK] follow_up_action downgrade_claim wired through update_posterior")

    _hr("PHASE 5 DEBATE — READY")
    print(f"  smoke db: {db_path}")
    print(f"  judge picked: {deb.judge_model} (provider={deb.judge_provider})")
    print(f"  verdict: winner={final['winner']}, "
          f"conf={final['judge_confidence']}")
    print(f"  rationale verbatim: {rationale}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
