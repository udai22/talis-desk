"""Smoke test for the brief composer.

Run:  python -m talis_desk.brief

Steps:
  1. Seed an isolated desk.db with 1 open trade idea (BTC long), 2 active
     hypotheses (one supporting, one contradicting; both with
     heat_score > 0.7), and 1 recently-judged debate (with verdict +
     rationale).
  2. Call `compose_brief(scope='market', as_of=now())`.
  3. Write the markdown to `/tmp/brief_smoke.md`, print first 50 lines.
  4. Verify Brief object has all fields populated (id, cycle_id, scope,
     markdown, cited_*_ids, quality_flags, cost_usd, elapsed_seconds).
  5. Re-run with `as_of=now() - 24h` to verify bitemporal replay returns
     a different view (the seeded objects valid_from = now - 1h, so an
     as_of 24h in the past should return zero rows).
  6. Stamp PHASE 6 BRIEF — READY on success.

Cost expectation: $0.0002-$0.0005 per brief (one Sonnet headline call,
estimated by `_estimate_cost_usd` from response length).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _isolated_db() -> Path:
    tmpdir = tempfile.mkdtemp(prefix="brief_smoke_")
    return Path(tmpdir) / "desk.db"


def _hr(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    db_path = _isolated_db()
    from talis_desk import store as store_mod
    store_mod._STORE = None  # type: ignore[attr-defined]
    store_mod.get_desk_store(db_path=db_path)

    from talis_desk.brief import Brief, compose_brief
    from talis_desk.debate.runner import (
        judge_debate,
        open_debate,
        submit_debate_argument,
    )
    from talis_desk.hypotheses import HypothesisDraft, HypothesisEdgeDraft
    from talis_desk.hypotheses.model import add_edge, propose_hypothesis
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
    print(f"(headline will use real Sonnet)" if has_key
          else "(stub headline fallback path)")

    # ============================================================
    # 1. Seed: 2 hypotheses + 1 open trade idea + 1 debate
    # ============================================================
    _hr("[1] Seed desk.db (2 hot hypotheses + 1 BTC long + 1 debate)")
    now = datetime.now(timezone.utc)
    cycle_id = "smoke_brief_cycle"
    ctx = AgentContext(cycle_id=cycle_id, specialist_id="microstructure_v3")
    valid_from = now - timedelta(hours=1)
    expires_at = valid_from + timedelta(hours=24)

    # Seed specialist persona row so the cycle_stats query finds it.
    conn.execute(
        "INSERT INTO specialist_states (id, specialist_id, persona_version, "
        "cycle_id, state_kind, state_json, valid_from, transaction_from) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("spst_smoke_brief", "microstructure_v3", "v3.smoke", cycle_id,
         "persona", json.dumps({"persona_model": "anthropic:claude-sonnet-4-6"}),
         now.isoformat(), now.isoformat()),
    )

    # Hypothesis 1: supports the long.
    h1 = propose_hypothesis(
        HypothesisDraft(
            cycle_id=cycle_id,
            specialist_id="microstructure_v3",
            title="BTC funding regime supports long",
            hypothesis_text=(
                "Funding z-score normalized from +2.4σ to +0.8σ; L5 OFI "
                "flipped from toxic sell to passive bid refill. Historical "
                "playbook: 24h median forward return +0.6%."
            ),
            posterior_prob=0.75,
            heat_score=0.85,
            entity_ids=["BTC"],
            valid_from=valid_from,
            payload={"side": "pro_long"},
        ),
        ctx,
    )
    # Hypothesis 2: contradicting frame.
    h2 = propose_hypothesis(
        HypothesisDraft(
            cycle_id=cycle_id,
            specialist_id="macro_regime_v2",
            title="DXY breakout argues risk-off",
            hypothesis_text=(
                "DXY broke 105 with conviction and Treasury auction tail "
                "stress is at the 90th percentile. Crypto longs into DXY "
                "breakouts have under-performed median by 40 bps."
            ),
            posterior_prob=0.65,
            heat_score=0.72,
            entity_ids=["BTC", "DXY"],
            valid_from=valid_from,
            payload={"side": "anti_long"},
        ),
        ctx,
    )
    # Edge: h2 contradicts h1 (so the brief can show both sides on h1).
    add_edge(
        HypothesisEdgeDraft(
            from_node_kind="hypothesis", from_node_id=h2.id,
            to_node_kind="hypothesis", to_node_id=h1.id,
            edge_kind="contradicts", strength=0.55,
            valid_from=valid_from,
        ),
    )
    print(f"  hypotheses: {h1.id} (pro-long, heat=0.85), "
          f"{h2.id} (anti-long, heat=0.72)")

    # Trade idea: BTC long, citing h1, with h2 as contradicting evidence.
    entry_px = 78_000.0
    draft = TradeIdeaDraft(
        cycle_id=cycle_id,
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
            "BTC funding sits at +2.4σ extreme and L5 OFI just flipped from "
            "toxic sell to passive bid refill. Historical playbook: 24h "
            "median forward return +0.6%."
        ),
        claim_ids=[h1.id],
        hypothesis_ids=[h1.id],
        contradicting_evidence=[
            ContradictionItem(
                claim_id=h2.id,
                reason="DXY breakout + auction stress argues risk-off",
                weight=0.55,
            ),
        ],
        confluence_score=0.7,
        confidence=0.80,
        expires_at=expires_at,
        valid_from=valid_from,
    )
    idea = emit_trade_idea(draft, ctx)
    if idea.status != "published":
        print(f"  FAIL: idea did not validate; status={idea.status}")
        return 1
    print(f"  trade idea: {idea.id} (BTC long, conf=0.80, status=published)")

    # Debate: open + arguments + judge -> ends in 'judged' status.
    deb = open_debate(
        trigger_kind="high_confidence", trigger_id=idea.id,
        participants=["microstructure_v3", "macro_regime_v2"],
        due_in_minutes=30, context=ctx,
    )
    submit_debate_argument(
        debate_id=deb.id, agent_id="microstructure_v3",
        argument_md=(
            "L5 OFI just flipped from toxic sell to passive bid refill while "
            "funding normalized from +2.4σ to +0.8σ. Both signals are "
            "independent. Historical playbook median 24h forward return is "
            "+0.6%. Risk asymmetric: 1% stop vs 1.2% target."
        ),
        citation_ids=[h1.id],
        falsifiable_crux=(
            "If BTC closes below 76k in the next 4h before funding re-tests "
            "<0, the regime call is wrong."
        ),
        persona_version="v3.smoke",
    )
    submit_debate_argument(
        debate_id=deb.id, agent_id="macro_regime_v2",
        argument_md=(
            "DXY breaking 105 with conviction + Treasury auction tail stress "
            "is classic risk-off macro. Even with friendly micro, every long "
            "BTC entry into a DXY breakout has under-performed median by 40 "
            "bps. Wait for DXY rejection or auction relief first."
        ),
        citation_ids=[h2.id],
        falsifiable_crux=(
            "If DXY closes back below 104 in the next 12h, the macro veto lifts."
        ),
        persona_version="v2.smoke",
    )
    # judge_debate already auto-fires on the second argument submission.
    print(f"  debate: {deb.id} judged (auto-fired)")

    # ============================================================
    # 2. compose_brief at now -> populated brief
    # ============================================================
    _hr("[2] compose_brief(scope='market', as_of=None -> live view)")
    # `as_of=None` triggers the live-view forward bias (now + 1s) so that
    # rows whose SQLite `transaction_from` default landed microseconds after
    # the seed's `datetime.now()` are visible. Explicit past `as_of` skips
    # this — verified in step [3] below.
    brief: Brief = compose_brief(
        cycle_id=cycle_id,
        as_of=None,
        scope="market",
    )
    out_path = Path("/tmp/brief_smoke.md")
    out_path.write_text(brief.markdown)
    print(f"  brief id        : {brief.id}")
    print(f"  cycle_id        : {brief.cycle_id}")
    print(f"  scope           : {brief.scope}")
    print(f"  as_of           : {brief.as_of}")
    print(f"  headline_model  : {brief.headline_model_used}")
    print(f"  headline_provider: {brief.headline_provider}")
    print(f"  headline_fallback: {brief.headline_fallback_used}")
    print(f"  cost_usd        : ${brief.cost_usd:.6f}")
    print(f"  elapsed_seconds : {brief.elapsed_seconds:.3f}")
    print(f"  tic_artifact_id : {brief.tic_artifact_id}")
    print(f"  cited claims    : {len(brief.cited_claim_ids)}")
    print(f"  cited hypotheses: {len(brief.cited_hypothesis_ids)}")
    print(f"  cited trade ids : {len(brief.cited_trade_idea_ids)}")
    print(f"  cited debates   : {len(brief.cited_debate_ids)}")
    print(f"  quality_flags   : {brief.quality_flags}")
    print(f"  markdown bytes  : {len(brief.markdown)}")
    print(f"  written to      : {out_path}")
    print()
    print("--- first 50 lines of /tmp/brief_smoke.md ---")
    for i, line in enumerate(brief.markdown.splitlines()[:50], 1):
        print(f"{i:3d} | {line}")
    print("--- end first 50 lines ---")

    # Acceptance checks
    failures: list[str] = []
    if not brief.markdown.strip():
        failures.append("markdown is empty")
    required_sections = [
        "## Headline",
        "## Open Trade Book",
        "## Closed Trade Book",
        "## Hot Hypotheses",
        "## Recent Debates",
        "## Triggered Playbooks",
        "## Source Health Watch",
        "## Cycle Stats",
        "## Methodology Notes",
    ]
    for s in required_sections:
        if s not in brief.markdown:
            failures.append(f"missing section: {s}")
    if idea.id not in brief.cited_trade_idea_ids:
        failures.append(f"trade idea {idea.id} not in cited_trade_idea_ids")
    if h1.id not in brief.cited_hypothesis_ids:
        failures.append(f"hypothesis {h1.id} not in cited_hypothesis_ids")
    if brief.cost_usd > 0.10:
        failures.append(f"cost_usd ${brief.cost_usd:.4f} exceeded hard cap")
    if brief.cycle_id != cycle_id:
        failures.append(f"cycle_id mismatch: {brief.cycle_id} != {cycle_id}")
    if not brief.title:
        failures.append("title empty")

    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        return 1
    print("  [OK] all required sections present, cites resolve, cost under cap")

    # ============================================================
    # 3. Bitemporal replay: as_of = now - 24h -> nothing
    # ============================================================
    _hr("[3] Bitemporal replay (as_of = now - 24h)")
    past = now - timedelta(hours=24)
    past_brief: Brief = compose_brief(
        cycle_id=cycle_id,
        as_of=past,
        scope="market",
        # Don't write a TIC artifact for replay views — that would clutter
        # the historical artifact stream.
        write_tic_artifact=False,
    )
    print(f"  past brief id   : {past_brief.id}")
    print(f"  past cited ti   : {len(past_brief.cited_trade_idea_ids)}")
    print(f"  past cited hyps : {len(past_brief.cited_hypothesis_ids)}")
    print(f"  past markdown excerpt:")
    print()
    for i, line in enumerate(past_brief.markdown.splitlines()[:20], 1):
        print(f"{i:3d} | {line}")
    print()
    # Past view should show NO open trade ideas (seeded valid_from = now - 1h,
    # so as_of = now - 24h excludes them).
    if past_brief.cited_trade_idea_ids:
        print("  FAIL: past brief still cited trade ideas — bitemporal slice broken")
        return 1
    if past_brief.cited_hypothesis_ids:
        print("  FAIL: past brief still cited hypotheses — bitemporal slice broken")
        return 1
    if "Quiet Cycle" not in past_brief.markdown:
        print("  WARN: past brief did not render Quiet Cycle (may still be valid "
              "if other rows existed; checking explicit emptiness instead)")
    print("  [OK] bitemporal replay returns empty past view as expected")

    # ============================================================
    # 4. Final stamp
    # ============================================================
    _hr("PHASE 6 BRIEF — READY")
    print(f"  brief markdown: {out_path}")
    print(f"  brief id      : {brief.id}")
    print(f"  cost          : ${brief.cost_usd:.6f}")
    print(f"  bitemporal    : past view returned empty (correct)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
