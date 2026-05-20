"""End-to-end: research cycle → daily brief.

Wires the loop runner and brief composer against a SHARED desk.db so the
brief reflects the cycle that just ran. Outputs markdown to stdout +
writes to /tmp/talis_daily_brief_<timestamp>.md.

Usage:
    python3 run_daily_brief.py [--db PATH] [--scope SCOPE]

Defaults to a fresh tempdir-based desk.db; pass --db ~/.talis/desk.db to
run against the persistent location.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/Users/udaikhattar/jarvis-ios/docs/research/brief_experiments")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=str, default=None,
                   help="desk.db path; default: tempdir/desk.db")
    p.add_argument("--scope", type=str, default="market",
                   help="brief scope label (market | strategy | watchlist)")
    p.add_argument("--cycle-id", type=str, default=None,
                   help="loop cycle_id; default: daily_<YYYYMMDD>")
    p.add_argument("--budget", type=float, default=5.0,
                   help="cycle USD budget; default $5")
    args = p.parse_args()

    if args.db is None:
        tmpdir = tempfile.mkdtemp(prefix="daily_brief_")
        db_path = Path(tmpdir) / "desk.db"
    else:
        db_path = Path(args.db)
        db_path.parent.mkdir(parents=True, exist_ok=True)

    cycle_id = args.cycle_id or f"daily_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}"

    print(f"[run_daily_brief] db_path={db_path}")
    print(f"[run_daily_brief] cycle_id={cycle_id}")
    print(f"[run_daily_brief] scope={args.scope}")
    print()

    # Pin the desk store to db_path so both loop runner and brief composer
    # use the same database.
    from talis_desk.store import DeskStore
    import talis_desk.store as _store_mod
    _store_mod._STORE = DeskStore(db_path=db_path)

    # We need a registered persona for the loop runner to hydrate from.
    from talis_desk.specialists.macro_regime import register_macro_regime_v1
    persona_id = register_macro_regime_v1()
    print(f"[run_daily_brief] registered macro_regime persona: {persona_id}")

    # Run the research cycle.
    from talis_desk.loop import run_research_cycle
    from talis_desk.loop.runner import LoopConfig
    cfg = LoopConfig(max_cost_usd=args.budget, paper_only=True)
    print(f"[run_daily_brief] running cycle (max_cost=${cfg.max_cost_usd}, paper_only={cfg.paper_only})...")
    result = run_research_cycle(
        specialist_id="macro_regime",
        cycle_id=cycle_id,
        loop_config=cfg,
    )
    print(f"[run_daily_brief] cycle complete:")
    print(f"  cost_usd       = ${result.total_cost_usd:.4f}")
    print(f"  tool_calls     = {result.total_tool_calls}")
    print(f"  trade_ideas    = {len(result.synthesis.new_trade_ideas)}")
    print(f"  kill_switch    = {result.kill_switch_triggered}")
    print(f"  elapsed_s      = {result.elapsed_seconds:.1f}")
    print()

    # Compose the brief from this cycle's outputs.
    from talis_desk.brief import compose_brief
    print(f"[run_daily_brief] composing brief...")
    brief = compose_brief(
        scope=args.scope,
        cycle_id=cycle_id,
        output_format="markdown",
    )
    print(f"[run_daily_brief] brief composed:")
    print(f"  brief_id            = {brief.id}")
    print(f"  headline_model_used = {brief.headline_model_used}")
    print(f"  quality_flags       = {brief.quality_flags}")
    print(f"  markdown_len        = {len(brief.markdown)} chars")
    print()

    # Write to /tmp for surfacing.
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = Path(f"/tmp/talis_daily_brief_{ts}.md")
    out_path.write_text(brief.markdown)
    print(f"[run_daily_brief] markdown written: {out_path}")
    print()

    # Echo the brief
    print("=" * 80)
    print(brief.markdown)
    print("=" * 80)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
