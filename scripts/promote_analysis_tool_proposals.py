#!/usr/bin/env python
"""Promote pending analysis tool proposals into learned tools."""
from __future__ import annotations

import argparse
import json
import sqlite3

from talis_desk.tool_atlas.learned_lifecycle import (
    create_runtime_adapter_work_orders,
    mark_runtime_adapter_ready,
    promote_pending_analysis_tool_proposals,
    repair_low_quality_analysis_tool_proposals,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Promote Talis analysis tool proposals.")
    parser.add_argument("--cycle-id", default="", help="Optional proposal cycle filter.")
    parser.add_argument("--db-path", default="", help="Optional desk SQLite DB path.")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--runtime-adapter-work-orders",
        action="store_true",
        help="Also materialize work orders for needs_runtime_adapter proposals.",
    )
    parser.add_argument(
        "--repair-contracts",
        action="store_true",
        help="Iterate low-quality proposal contracts before promotion.",
    )
    parser.add_argument(
        "--skip-promotion",
        action="store_true",
        help="Do not promote proposals after optional repair/work-order steps.",
    )
    parser.add_argument(
        "--mark-adapter-ready",
        default="",
        help="Proposal id or work-order JSON path whose runtime adapter has been implemented.",
    )
    args = parser.parse_args()
    conn = None
    if args.db_path:
        conn = sqlite3.connect(args.db_path)
        conn.row_factory = sqlite3.Row
    ready = None
    if args.mark_adapter_ready:
        token = args.mark_adapter_ready
        if token.endswith(".json") or "/" in token:
            ready = mark_runtime_adapter_ready(work_order_path=token, conn=conn)
        else:
            ready = mark_runtime_adapter_ready(proposal_id=token, conn=conn)
    repairs = (
        repair_low_quality_analysis_tool_proposals(
            cycle_id=args.cycle_id,
            limit=max(1, args.limit),
            conn=conn,
        )
        if args.repair_contracts else []
    )
    results = (
        []
        if args.skip_promotion else
        promote_pending_analysis_tool_proposals(
            cycle_id=args.cycle_id,
            limit=max(1, args.limit),
            conn=conn,
        )
    )
    work_orders = (
        create_runtime_adapter_work_orders(cycle_id=args.cycle_id, limit=max(1, args.limit), conn=conn)
        if args.runtime_adapter_work_orders else []
    )
    print(json.dumps({
        "promotions": [
            {
                "proposal_id": r.proposal_id,
                "tool_name": r.tool_name,
                "tool_uri": r.tool_uri,
                "status": r.status,
                "passed": r.passed,
                "tool_dir": r.tool_dir,
                "eval_report": r.eval_report,
                "iteration_proposal_id": r.iteration_proposal_id,
            }
            for r in results
        ],
        "runtime_adapter_work_orders": [
            {
                "proposal_id": r.proposal_id,
                "tool_name": r.tool_name,
                "runtime": r.runtime,
                "status": r.status,
                "work_order_path": r.work_order_path,
                "quality_flags": r.quality_flags,
            }
            for r in work_orders
        ],
        "contract_repairs": [
            {
                "parent_proposal_id": r.parent_proposal_id,
                "repaired_proposal_id": r.repaired_proposal_id,
                "tool_name": r.tool_name,
                "previous_score": r.previous_score,
                "repaired_score": r.repaired_score,
                "status": r.status,
                "quality_flags": r.quality_flags,
            }
            for r in repairs
        ],
        "runtime_adapter_readiness": (
            {
                "proposal_id": ready.proposal_id,
                "runtime": ready.runtime,
                "ready": ready.ready,
                "status": ready.status,
                "quality_flags": ready.quality_flags,
                "work_order_path": ready.work_order_path,
            }
            if ready else None
        ),
    }, indent=2, sort_keys=True))
    if conn is not None:
        conn.close()
    return 0 if (not results or all(r.passed for r in results)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
