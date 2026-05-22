#!/usr/bin/env python
"""Export the MarketEvolve learning scoreboard.

This is the inspectable read model for the evaluator-guided loop: current
programs, hard experiments, promotion/rejection decisions, and the next action
the cadence should take before spending wider.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from talis_desk.cadence import build_cadence_control_decision
from talis_desk.information_map import build_market_evolve_scoreboard
from talis_desk.information_map import persist_market_evolve_scoreboard
from talis_desk.store import DeskStore


def main() -> int:
    args = _parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    store = DeskStore(db_path=Path(args.db).expanduser().resolve()) if args.db else None
    try:
        scoreboard = build_market_evolve_scoreboard(
            cycle_id=args.cycle_id,
            limit=args.limit,
            persist=not args.no_persist,
            conn=store.conn if store else None,
        )
        scoreboard["cadence_control"] = build_cadence_control_decision(
            scoreboard=scoreboard,
            mode=args.cadence_mode,
            allow_live_spend=args.allow_live_spend,
        )
        if not args.no_persist:
            persist_market_evolve_scoreboard(scoreboard, conn=store.conn if store else None)
    finally:
        if store:
            store.close()
    path = output_dir / "market_evolve_scoreboard.json"
    path.write_text(json.dumps(scoreboard, indent=2, sort_keys=True), encoding="utf-8")
    print("MARKET_EVOLVE_SCOREBOARD_JSON=" + str(path))
    print("MARKET_EVOLVE_SCOREBOARD_STATUS=" + str(scoreboard.get("status") or "unknown"))
    print("MARKET_EVOLVE_CADENCE_DECISION=" + str((scoreboard.get("cadence_control") or {}).get("decision") or "unknown"))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle-id", default="")
    parser.add_argument("--db", default="", help="Desk DB to read and persist into without resetting it.")
    parser.add_argument("--cadence-mode", default="sentinel_tick")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--limit", type=int, default=64)
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--allow-live-spend", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
