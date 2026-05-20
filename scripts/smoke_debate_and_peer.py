"""Smoke tests for Codex findings #5 (debate opener) and #11 (peer
messages).

Both tests run against a fresh isolated desk.db. They DO call real LLMs
through the existing fallback chain — if no provider keys are set the
tests will report "skipped (no LLM)" rather than fabricating a verdict.

Usage:
    python3 scripts/smoke_debate_and_peer.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

# talis-tic must be importable so chat() walks the provider chain.
TIC_PATH = "/Users/udaikhattar/jarvis-ios/docs/research/brief_experiments"
if TIC_PATH not in sys.path:
    sys.path.insert(0, TIC_PATH)
# Also ensure the talis-desk repo root is on sys.path when run via
# `python3 scripts/...` from the repo root.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _isolate_store() -> Path:
    tmpdir = tempfile.mkdtemp(prefix="smoke_dbg_")
    db_path = Path(tmpdir) / "desk.db"
    from talis_desk.store import DeskStore
    import talis_desk.store as _store_mod
    _store_mod._STORE = DeskStore(db_path=db_path)
    return db_path


def test_debate_opener() -> bool:
    """Smoke 1: register 2 specialists, seed a hypothesis, call
    _open_real_debate, assert a debates row exists."""
    print("\n--- smoke 1: debate opener -------------------------------")
    db_path = _isolate_store()
    print(f"  desk.db = {db_path}")

    # Register 2 specialists so the opponent picker can find a peer.
    from talis_desk.specialists.macro_regime import register_macro_regime_v1
    from talis_desk.specialists.microstructure_v1 import (
        register_microstructure_v1,
    )
    s1 = register_macro_regime_v1()
    s2 = register_microstructure_v1()
    print(f"  registered: {s1.specialist_id} + {s2.specialist_id}")

    # Sanity: _list_registered_specialist_ids must see both.
    from talis_desk.loop.runner import (
        _list_registered_specialist_ids,
        _pick_opponent_specialist,
        _open_real_debate,
    )
    reg = _list_registered_specialist_ids()
    print(f"  registry = {reg}")
    assert "macro_regime" in reg, "macro_regime missing from registry"
    assert "microstructure" in reg, "microstructure missing from registry"

    # Seed a hypothesis owned by macro_regime.
    from talis_desk.hypotheses.model import propose_hypothesis, HypothesisDraft
    from talis_desk.tool_atlas import AgentContext
    cycle_id = "smoke_cycle_001"
    ctx = AgentContext(cycle_id=cycle_id, specialist_id="macro_regime")
    draft = HypothesisDraft(
        cycle_id=cycle_id,
        specialist_id="macro_regime",
        title="ETHBTC ratio breaks down on cycle 4 liquidity rotation",
        hypothesis_text=(
            "Macro thesis: persistent USD strength + restrictive rates lead "
            "BTC dominance to absorb crypto liquidity, pinning ETHBTC below "
            "0.05 over the next 30 days. Falsifiable: ETHBTC > 0.055 daily "
            "close before 2026-06-19 invalidates."
        ),
        posterior_prob=0.78,
        heat_score=0.85,
        entity_ids=["ETH", "BTC"],
    )
    hyp = propose_hypothesis(draft, ctx)
    print(f"  hyp = {hyp.id} posterior={hyp.posterior_prob}")

    # Confirm opponent picker fires even with empty hypothesis_edges.
    opp = _pick_opponent_specialist("macro_regime", hyp)
    print(f"  opponent picked = {opp}")
    assert opp == "microstructure", \
        f"expected microstructure as opponent, got {opp}"

    # Call _open_real_debate.
    deb_id = _open_real_debate(
        triggering_specialist="macro_regime",
        hypothesis=hyp,
        trace=None,
        cycle_id=cycle_id,
        base_context=ctx,
        plan_model="anthropic:claude-sonnet-4-6",
    )
    print(f"  debate_id returned = {deb_id}")

    # Query the debates table to confirm a row landed.
    from talis_desk.store import get_desk_store
    conn = get_desk_store().conn
    rows = conn.execute(
        "SELECT id, status, participants FROM debates "
        "WHERE transaction_to IS NULL"
    ).fetchall()
    print(f"  debates rows = {len(rows)}")
    for r in rows:
        print(f"    {dict(r)}")
    if not rows:
        print("  FAIL: 0 debate rows after _open_real_debate")
        return False
    statuses = {r["status"] for r in rows}
    if not statuses & {"open", "judged", "expired", "applied"}:
        print(f"  FAIL: unexpected debate status set: {statuses}")
        return False
    print(f"  PASS: {len(rows)} debate row(s), statuses={statuses}")
    return True


def test_peer_messages() -> bool:
    """Smoke 2: register 2 specialists, seed a published trade idea + a
    resolved hypothesis, call _solicit_peer_messages, assert agent_messages
    rows landed."""
    print("\n--- smoke 2: peer messages -------------------------------")
    db_path = _isolate_store()
    print(f"  desk.db = {db_path}")

    from talis_desk.specialists.macro_regime import register_macro_regime_v1
    from talis_desk.specialists.microstructure_v1 import (
        register_microstructure_v1,
    )
    register_macro_regime_v1()
    register_microstructure_v1()

    from talis_desk.hypotheses.model import propose_hypothesis, HypothesisDraft
    from talis_desk.tool_atlas import AgentContext
    cycle_id = "smoke_cycle_002"
    ctx = AgentContext(cycle_id=cycle_id, specialist_id="macro_regime")
    draft = HypothesisDraft(
        cycle_id=cycle_id,
        specialist_id="macro_regime",
        title="USD strength tailwind to BTC dominance through Q2",
        hypothesis_text=(
            "Bond market re-pricing of fed cuts to 2H 2026 keeps DXY > 104. "
            "BTC dominance climbs above 60%."
        ),
        posterior_prob=0.72,
        heat_score=0.65,
        entity_ids=["BTC"],
    )
    hyp = propose_hypothesis(draft, ctx)

    # Build a fake CycleHydration with the macro persona prompt.
    from talis_desk.loop.runner import _solicit_peer_messages, CycleHydration
    from talis_desk.agents_native.scratchpad import AgentMessage
    from talis_desk.tool_atlas.atlas import ToolAtlasSnapshot

    # ToolAtlasSnapshot needs SOMETHING — empty is fine for this helper
    # because we never query it.
    atlas = ToolAtlasSnapshot(
        as_of=datetime.now(timezone.utc),
        cycle_id=cycle_id,
        rows=[],
        n_tools=0,
        n_skills=0,
        n_sources=0,
    )
    hydration = CycleHydration(
        specialist_id="macro_regime",
        persona_version="v1.0",
        persona_prompt=(
            "You are macro_regime, a top-down rates / liquidity / USD "
            "specialist. Use FRED + COT + Treasury data."
        ),
        persona_tool_uris=[],
        yesterday_state={},
        unread_messages=[],
        recent_brier_outcomes=[],
        tool_atlas=atlas,
        atlas_pinned=False,
        open_hypotheses=[hyp],
        persona_state_id="spst_smoke",
    )

    posted = _solicit_peer_messages(
        hydration=hydration,
        cycle_id=cycle_id,
        own_hypotheses=[hyp],
        own_ideas=[],
        blocked_idea_ids=[],
        base_context=ctx,
        plan_model="anthropic:claude-sonnet-4-6",
        max_messages=3,
    )
    print(f"  posted = {len(posted)} peer messages")

    from talis_desk.store import get_desk_store
    conn = get_desk_store().conn
    rows = conn.execute(
        "SELECT id, from_agent, to_agent_or_topic, message_kind "
        "FROM agent_messages WHERE from_agent = 'macro_regime' "
        "AND transaction_to IS NULL"
    ).fetchall()
    print(f"  agent_messages rows from macro_regime = {len(rows)}")
    for r in rows:
        print(f"    {dict(r)}")
    if not rows:
        print("  FAIL: 0 peer-message rows (likely LLM unavailable; check keys)")
        return False
    print(f"  PASS: {len(rows)} peer-message row(s)")
    return True


def main() -> int:
    results: list[tuple[str, bool, str]] = []
    for name, fn in [
        ("debate_opener", test_debate_opener),
        ("peer_messages", test_peer_messages),
    ]:
        try:
            ok = fn()
            results.append((name, ok, ""))
        except Exception as e:
            traceback.print_exc()
            results.append((name, False, f"{type(e).__name__}: {e}"))

    print("\n========== SMOKE SUMMARY ==========")
    for name, ok, err in results:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}  {err}")
    return 0 if all(ok for _, ok, _ in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
