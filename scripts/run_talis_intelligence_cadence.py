#!/usr/bin/env python
"""Build or run the production cadence for Talis intelligence.

Default behavior is safe: write an executable plan and stop. Add ``--execute``
to run the commands. Add ``--allow-live-spend`` only when the plan should make
provider calls under the configured cap.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from talis_desk.cadence import (
    build_intelligence_cadence_plan,
    execute_cadence_plan,
    write_cadence_plan,
)


def main() -> int:
    args = _parse_args()
    repo = Path(__file__).resolve().parents[1]
    artifact_dir = (
        Path(args.artifact_dir).expanduser().resolve()
        if args.artifact_dir
        else Path(tempfile.gettempdir()) / f"talis-intelligence-cadence-{args.mode.replace('_', '-')}"
    )
    plan = build_intelligence_cadence_plan(
        mode=args.mode,
        artifact_dir=artifact_dir,
        allow_live_spend=args.allow_live_spend,
        cycle_id=args.cycle_id,
        scout_count=args.scouts,
        ramp_policy=args.ramp_policy,
        repo_root=repo,
        live_cost_cap_usd=args.live_cost_cap_usd,
        concurrency=args.concurrency,
        max_tool_iterations=args.max_tool_iterations,
        brief_budget_usd=args.brief_budget_usd,
    )
    plan_path = write_cadence_plan(plan)
    print("TALIS_CADENCE_PLAN_JSON=" + str(plan_path))
    print("TALIS_CADENCE_PLAN=" + json.dumps({
        "mode": plan.mode,
        "cycle_id": plan.cycle_id,
        "artifact_dir": plan.artifact_dir,
        "allow_live_spend": plan.allow_live_spend,
        "commands": [cmd.shell for cmd in plan.commands],
        "gates": plan.gates,
    }, sort_keys=True))
    if not args.execute:
        print("TALIS_CADENCE_STATUS=planned_not_executed")
        return 0
    report = execute_cadence_plan(plan, repo_root=repo, stop_on_failure=not args.continue_on_failure)
    print("TALIS_CADENCE_REPORT_JSON=" + str(report.get("report_path") or ""))
    print("TALIS_CADENCE_STATUS=" + str(report.get("status") or "unknown"))
    return 0 if report.get("status") == "pass" else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        default="sentinel_tick",
        choices=["sentinel", "sentinel_tick", "always_on", "full", "full_pipeline", "brief"],
    )
    parser.add_argument("--artifact-dir", default="")
    parser.add_argument("--cycle-id", default="")
    parser.add_argument("--scouts", type=int, default=0, help="Override scout count for this cadence mode.")
    parser.add_argument("--concurrency", type=int, default=0)
    parser.add_argument("--live-cost-cap-usd", type=float, default=None)
    parser.add_argument("--max-tool-iterations", type=int, default=1)
    parser.add_argument("--brief-budget-usd", type=float, default=5.0)
    parser.add_argument("--ramp-policy", default="")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--continue-on-failure", action="store_true")
    parser.add_argument("--allow-live-spend", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
