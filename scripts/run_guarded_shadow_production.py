#!/usr/bin/env python
"""Run the promoted scout policy as a guarded shadow-production job.

This is the production posture after the live scout tournament promotes a
policy to ``promote_to_scheduled_production_candidate``. It is intentionally a
wrapper, not a second implementation of the scout run:

1. require a tournament report that proves scheduled-shadow readiness;
2. enforce a shadow-only safety policy with no trade execution;
3. run the existing live canary under hard caps when live spend is explicit;
4. re-score the new artifact with the same tournament evaluator; and
5. write a small job report suitable for a scheduler, monitor, and phone page.

The script can also run in dry-run mode. Dry runs validate the production
envelope and command without calling a model provider.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.evaluate_live_scout_tournament import evaluate_live_scout_tournament


SAFETY_POLICY = {
    "execution_mode": "shadow_only",
    "trade_execution_enabled": False,
    "order_submission_enabled": False,
    "allowed_side_effects": [
        "read_only_tool_calls",
        "model_calls_under_cost_cap",
        "local_artifact_writes",
        "local_shadow_database_writes",
    ],
    "forbidden_side_effects": [
        "exchange_order_submission",
        "wallet_or_key_mutation",
        "position_sizing_commit",
        "portfolio_rebalance_commit",
    ],
}


DEFAULT_MAX_COST_CAP_USD = 5.00
DEFAULT_MAX_SCOUTS = 1000


def main() -> int:
    args = _parse_args()
    started = time.perf_counter()
    artifact_dir = (
        Path(args.artifact_dir).expanduser().resolve()
        if args.artifact_dir
        else _artifact_dir()
    )
    prompt_output_dir = (
        Path(args.prompt_output_dir).expanduser().resolve()
        if args.prompt_output_dir
        else artifact_dir / "prompt_outputs"
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    prompt_output_dir.mkdir(parents=True, exist_ok=True)

    tournament = _load_or_evaluate_tournament(args, output_dir=prompt_output_dir)
    plan = build_guarded_shadow_plan(
        tournament=tournament,
        n_scouts=args.n_scouts,
        concurrency=args.concurrency,
        cost_cap_usd=args.cost_cap_usd,
        provider_timeout_s=args.provider_timeout_s,
        prompt_variant=args.prompt_variant,
        max_tool_iterations=args.max_tool_iterations,
        seed_rng=args.seed_rng,
        model=args.model,
        fallback=args.fallback,
        artifact_dir=artifact_dir,
        prompt_output_dir=prompt_output_dir,
        allow_live_spend=args.allow_live_spend,
        dry_run=args.dry_run,
        max_cost_cap_usd=args.max_cost_cap_usd,
        max_scouts=args.max_scouts,
        cycle_id=args.cycle_id,
    )
    report: dict[str, Any] = {
        "schema_version": "talis_guarded_shadow_production_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifact_dir": str(artifact_dir),
        "prompt_output_dir": str(prompt_output_dir),
        "safety_policy": SAFETY_POLICY,
        "input_tournament_report": str(Path(args.tournament_report).expanduser().resolve()) if args.tournament_report else "",
        "input_canary_reports": [str(Path(p).expanduser().resolve()) for p in args.canary_report],
        "plan": plan,
        "preflight": preflight_from_plan(plan),
    }

    if not plan["allowed_to_start"]:
        report["status"] = "blocked"
        report["elapsed_s"] = round(time.perf_counter() - started, 3)
        _write_shadow_report(prompt_output_dir, report)
        _print_report_paths(prompt_output_dir, report)
        return 1

    if args.dry_run or not args.allow_live_spend:
        report["status"] = "dry_run" if args.dry_run else "blocked_no_live_spend"
        report["elapsed_s"] = round(time.perf_counter() - started, 3)
        report["interpretation"] = (
            "No provider calls were made. The scheduled-shadow envelope is valid, "
            "but the live job still requires --allow-live-spend."
        )
        _write_shadow_report(prompt_output_dir, report)
        _print_report_paths(prompt_output_dir, report)
        return 0

    run_result = _run_canary_command(plan["command"], cwd=Path(args.repo_root).expanduser().resolve())
    report["canary_subprocess"] = run_result
    new_canary_report = prompt_output_dir / "live_scout_canary_report.json"
    if not new_canary_report.exists():
        report["status"] = "failed"
        report["failed_reason"] = "live_scout_canary_report_missing"
        report["elapsed_s"] = round(time.perf_counter() - started, 3)
        _write_shadow_report(prompt_output_dir, report)
        _print_report_paths(prompt_output_dir, report)
        return 1

    source_reports = _source_reports_from_tournament(tournament)
    source_reports.append(new_canary_report)
    refreshed = evaluate_live_scout_tournament(source_reports)
    report["refreshed_tournament"] = refreshed
    report["post_run_gates"] = post_run_gates(
        canary_report=_read_json(new_canary_report),
        refreshed_tournament=refreshed,
        scheduled_n_scouts=args.n_scouts,
    )
    failed = [name for name, ok in report["post_run_gates"].items() if not ok]
    report["status"] = "pass" if not failed else "warn"
    report["failed_gates"] = failed
    report["elapsed_s"] = round(time.perf_counter() - started, 3)
    _write_shadow_report(prompt_output_dir, report)
    _print_report_paths(prompt_output_dir, report)
    return 0 if report["status"] == "pass" else 1


def build_guarded_shadow_plan(
    *,
    tournament: dict[str, Any],
    n_scouts: int,
    concurrency: int,
    cost_cap_usd: float,
    provider_timeout_s: float,
    prompt_variant: str,
    max_tool_iterations: int,
    seed_rng: int,
    model: str,
    fallback: str,
    artifact_dir: Path,
    prompt_output_dir: Path,
    allow_live_spend: bool,
    dry_run: bool,
    max_cost_cap_usd: float,
    max_scouts: int,
    cycle_id: str = "",
) -> dict[str, Any]:
    decision = tournament.get("promotion_decision") if isinstance(tournament.get("promotion_decision"), dict) else {}
    winner = tournament.get("winner") if isinstance(tournament.get("winner"), dict) else {}
    winner_config = winner.get("configuration") if isinstance(winner.get("configuration"), dict) else {}
    winner_variants = winner_config.get("prompt_variants") if isinstance(winner_config.get("prompt_variants"), list) else []
    chosen_variant = prompt_variant or (str(winner_variants[0]) if winner_variants else "flash_temporal_v4")
    chosen_iterations = int(max_tool_iterations if max_tool_iterations >= 0 else winner_config.get("max_tool_iterations") or 1)
    gates = {
        "prior_tournament_ready_for_scheduled_production": bool(decision.get("ready_for_scheduled_production")),
        "decision_is_scheduled_candidate": decision.get("decision") == "promote_to_scheduled_production_candidate",
        "trade_execution_disabled": SAFETY_POLICY["trade_execution_enabled"] is False and SAFETY_POLICY["order_submission_enabled"] is False,
        "scout_count_within_policy": 1 <= int(n_scouts) <= int(max_scouts),
        "cost_cap_within_policy": 0 < float(cost_cap_usd) <= float(max_cost_cap_usd),
        "concurrency_positive": int(concurrency) >= 1,
        "provider_timeout_positive": float(provider_timeout_s) > 0,
        "prompt_variant_selected": bool(chosen_variant.strip()),
    }
    command_parts = [
        sys.executable,
        "scripts/run_live_scout_canary.py",
        "--n-scouts",
        str(int(n_scouts)),
        "--concurrency",
        str(int(concurrency)),
        "--cost-cap-usd",
        f"{float(cost_cap_usd):.4f}",
        "--provider-timeout-s",
        f"{float(provider_timeout_s):.1f}",
        "--prompt-variant",
        chosen_variant,
        "--max-tool-iterations",
        str(chosen_iterations),
        "--seed-rng",
        str(int(seed_rng)),
        "--model",
        model,
        "--fallback",
        fallback,
        "--artifact-dir",
        str(artifact_dir),
        "--prompt-output-dir",
        str(prompt_output_dir),
    ]
    if cycle_id:
        command_parts.extend(["--cycle-id", cycle_id])
    if allow_live_spend:
        command_parts.append("--allow-live-spend")
    command = " ".join(shlex.quote(part) for part in command_parts)
    return {
        "allowed_to_start": all(gates.values()),
        "dry_run": bool(dry_run),
        "allow_live_spend": bool(allow_live_spend),
        "command": command,
        "gates": gates,
        "failed_gates": [name for name, ok in gates.items() if not ok],
        "shadow_scope": "scheduled_probe" if int(n_scouts) < DEFAULT_MAX_SCOUTS else "scheduled_1000_scout_shadow_job",
        "n_scouts": int(n_scouts),
        "concurrency": int(concurrency),
        "cost_cap_usd": float(cost_cap_usd),
        "max_cost_cap_usd": float(max_cost_cap_usd),
        "max_scouts": int(max_scouts),
        "provider_timeout_s": float(provider_timeout_s),
        "prompt_variant": chosen_variant,
        "max_tool_iterations": chosen_iterations,
        "seed_rng": int(seed_rng),
        "model": model,
        "fallback": fallback,
        "decision": decision,
        "repeatability": tournament.get("shadow_repeatability") if isinstance(tournament.get("shadow_repeatability"), dict) else {},
    }


def preflight_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "allowed_to_start": bool(plan.get("allowed_to_start")),
        "failed_gates": plan.get("failed_gates") or [],
        "shadow_only": SAFETY_POLICY["execution_mode"] == "shadow_only",
        "trade_execution_enabled": False,
        "dry_run": bool(plan.get("dry_run")),
        "allow_live_spend": bool(plan.get("allow_live_spend")),
    }


def post_run_gates(
    *,
    canary_report: dict[str, Any],
    refreshed_tournament: dict[str, Any],
    scheduled_n_scouts: int,
) -> dict[str, bool]:
    verdict = canary_report.get("verdict") if isinstance(canary_report.get("verdict"), dict) else {}
    metrics = canary_report.get("metrics") if isinstance(canary_report.get("metrics"), dict) else {}
    info = metrics.get("information_map") if isinstance(metrics.get("information_map"), dict) else {}
    geometry = metrics.get("geometry") if isinstance(metrics.get("geometry"), dict) else {}
    tournament_decision = (
        refreshed_tournament.get("promotion_decision")
        if isinstance(refreshed_tournament.get("promotion_decision"), dict)
        else {}
    )
    gates = {
        "canary_status_pass": verdict.get("status") == "pass",
        "information_strings_created": int(info.get("string_count") or 0) > 0,
        "geometry_cells_created": int(geometry.get("cell_count") or 0) > 0,
        "trade_execution_still_disabled": SAFETY_POLICY["trade_execution_enabled"] is False,
    }
    if int(scheduled_n_scouts) >= DEFAULT_MAX_SCOUTS:
        gates["refreshed_tournament_still_scheduled_ready"] = bool(
            tournament_decision.get("ready_for_scheduled_production")
        )
    else:
        gates["probe_completed_without_opening_full_spend"] = True
    return gates


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tournament-report", default="", help="Existing live_scout_tournament_report.json proving scheduled readiness.")
    parser.add_argument("--canary-report", action="append", default=[], help="Canary reports to evaluate when --tournament-report is not provided.")
    parser.add_argument("--repo-root", default="/Users/udaikhattar/talis-desk")
    parser.add_argument("--artifact-dir", default="")
    parser.add_argument("--prompt-output-dir", default="")
    parser.add_argument("--cycle-id", default="")
    parser.add_argument("--n-scouts", type=int, default=DEFAULT_MAX_SCOUTS)
    parser.add_argument("--max-scouts", type=int, default=DEFAULT_MAX_SCOUTS)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--cost-cap-usd", type=float, default=DEFAULT_MAX_COST_CAP_USD)
    parser.add_argument("--max-cost-cap-usd", type=float, default=DEFAULT_MAX_COST_CAP_USD)
    parser.add_argument("--provider-timeout-s", type=float, default=45.0)
    parser.add_argument("--prompt-variant", default="")
    parser.add_argument("--max-tool-iterations", type=int, default=-1)
    parser.add_argument("--seed-rng", type=int, default=int(datetime.now(timezone.utc).strftime("%Y%m%d")))
    parser.add_argument("--model", default="deepseek:v4-flash")
    parser.add_argument("--fallback", default="anthropic:claude-haiku-4-5")
    parser.add_argument("--dry-run", action="store_true", help="Validate the scheduled envelope without model calls.")
    parser.add_argument("--allow-live-spend", action="store_true", help="Actually run the live canary under this wrapper.")
    return parser.parse_args()


def _load_or_evaluate_tournament(args: argparse.Namespace, *, output_dir: Path) -> dict[str, Any]:
    if args.tournament_report:
        return _read_json(Path(args.tournament_report).expanduser().resolve())
    if not args.canary_report:
        raise SystemExit("--tournament-report or at least one --canary-report is required")
    paths = [Path(p).expanduser().resolve() for p in args.canary_report]
    tournament = evaluate_live_scout_tournament(paths)
    _write_json(output_dir / "live_scout_tournament_report.json", tournament)
    return tournament


def _source_reports_from_tournament(tournament: dict[str, Any]) -> list[Path]:
    raw = tournament.get("input_reports") if isinstance(tournament.get("input_reports"), list) else []
    return [Path(str(p)).expanduser().resolve() for p in raw if str(p).strip()]


def _run_canary_command(command: str, *, cwd: Path) -> dict[str, Any]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    py_path = ".:talis_tic" + (f":{existing}" if existing else "")
    env["PYTHONPATH"] = py_path
    started = datetime.now(timezone.utc).isoformat()
    proc = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        shell=True,
        text=True,
        capture_output=True,
        timeout=None,
    )
    return {
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "returncode": proc.returncode,
        "stdout": proc.stdout[-8000:],
        "stderr": proc.stderr[-8000:],
    }


def _artifact_dir() -> Path:
    return Path(tempfile_dir()) / f"talis-guarded-shadow-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def tempfile_dir() -> str:
    return os.environ.get("TMPDIR") or "/tmp"


def _write_shadow_report(prompt_output_dir: Path, report: dict[str, Any]) -> None:
    _write_json(prompt_output_dir / "guarded_shadow_production_report.json", report)
    _write_text(prompt_output_dir / "guarded_shadow_production_report.md", render_shadow_markdown(report))


def render_shadow_markdown(report: dict[str, Any]) -> str:
    plan = report.get("plan") if isinstance(report.get("plan"), dict) else {}
    preflight = report.get("preflight") if isinstance(report.get("preflight"), dict) else {}
    post = report.get("post_run_gates") if isinstance(report.get("post_run_gates"), dict) else {}
    lines = [
        "# Guarded Shadow Production",
        "",
        f"- status: `{report.get('status')}`",
        f"- scope: `{plan.get('shadow_scope')}`",
        f"- scouts: `{plan.get('n_scouts')}`",
        f"- cost_cap_usd: `{plan.get('cost_cap_usd')}`",
        f"- trade_execution_enabled: `{SAFETY_POLICY['trade_execution_enabled']}`",
        f"- allow_live_spend: `{preflight.get('allow_live_spend')}`",
        f"- dry_run: `{preflight.get('dry_run')}`",
        "",
        "## Preflight Gates",
        "",
    ]
    for name, ok in (plan.get("gates") or {}).items():
        lines.append(f"- {'PASS' if ok else 'FAIL'} `{name}`")
    if post:
        lines.extend(["", "## Post-Run Gates", ""])
        for name, ok in post.items():
            lines.append(f"- {'PASS' if ok else 'FAIL'} `{name}`")
    lines.extend([
        "",
        "## Command",
        "",
        "```bash",
        str(plan.get("command") or ""),
        "```",
        "",
    ])
    if report.get("failed_reason"):
        lines.extend(["## Failed Reason", "", str(report.get("failed_reason")), ""])
    return "\n".join(lines)


def _print_report_paths(prompt_output_dir: Path, report: dict[str, Any]) -> None:
    print(f"GUARDED_SHADOW_STATUS={report.get('status')}")
    print(f"GUARDED_SHADOW_REPORT_JSON={prompt_output_dir / 'guarded_shadow_production_report.json'}")
    print(f"GUARDED_SHADOW_REPORT_MD={prompt_output_dir / 'guarded_shadow_production_report.md'}")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
