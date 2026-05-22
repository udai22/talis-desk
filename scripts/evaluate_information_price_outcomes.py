#!/usr/bin/env python
"""Evaluate information strings against later price observations."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from talis_desk.information_map import evaluate_information_price_outcomes
from talis_desk.store import DeskStore


def main() -> int:
    args = _parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    observations = _read_observations(args.price_observations_json)
    store = DeskStore(db_path=Path(args.db).expanduser().resolve()) if args.db else None
    try:
        report = evaluate_information_price_outcomes(
            cycle_id=args.cycle_id,
            price_observations=observations,
            min_move_threshold_pct=args.min_move_threshold_pct,
            limit=args.limit,
            conn=store.conn if store else None,
        )
    finally:
        if store:
            store.close()
    path = output_dir / "information_price_outcomes.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print("INFORMATION_PRICE_OUTCOMES_JSON=" + str(path))
    print("INFORMATION_PRICE_OUTCOMES_EVALUATED=" + str(report.get("evaluated_count") or 0))
    return 0


def _read_observations(path: str) -> list[dict[str, object]]:
    if not path:
        return []
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("observations") or payload.get("prices") or []
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--db", default="")
    parser.add_argument("--price-observations-json", default="")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--min-move-threshold-pct", type=float, default=0.02)
    parser.add_argument("--limit", type=int, default=500)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
