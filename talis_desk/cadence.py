"""Operating cadence for the Talis intelligence loop.

The desk has two jobs:

1. Produce brief-grade full runs on a human cadence.
2. Keep cheap Flash scouts awake between those runs so information/price
   divergence is never discovered only after consensus has moved.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def default_intelligence_cadence_policy(*, generated_at: str | None = None) -> dict[str, Any]:
    """Return the production-intent cadence as a serializable policy."""
    return {
        "schema_version": "talis_intelligence_cadence_policy_v1",
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "full_pipeline": {
            "mode": "brief_grade_full_pass",
            "cadence": "twice_daily",
            "windows_local": ["morning_brief", "evening_reconciliation"],
            "purpose": (
                "Run the full scout, synthesis, verifier, specialist, evolution, "
                "and brief composition loop for durable daily-brief output."
            ),
            "default_shape": {
                "scouts": 1000,
                "requires_explicit_spend_gate": True,
                "compose_daily_brief": True,
                "write_manifest": True,
            },
        },
        "always_on_flash": {
            "mode": "continuous_sentinel",
            "cadence": "rolling",
            "interval_minutes": 5,
            "scouts_per_tick": {"min": 8, "target": 24, "max": 64},
            "purpose": (
                "Continuously compare new information strings against price, "
                "attention, node, and source geometry so the map notices early."
            ),
            "default_shape": {
                "max_tool_iterations": 1,
                "prefer_read_only_tools": True,
                "no_verifier_spend_unless_triggered": True,
                "aggregate_into_next_full_brief": True,
            },
        },
        "sentinel_triggers": [
            {
                "id": "price_information_divergence",
                "watch": "information_pressure rises while price has not yet moved",
                "action": "widen scouts, attach market_state, then queue verifier if pressure persists",
            },
            {
                "id": "fresh_social_alpha",
                "watch": "Grok/X finds credible early posts, screenshots, or reply-chain confirmations",
                "action": "cross-check with Parallel/web, Hydromancer, node, and source health",
            },
            {
                "id": "node_or_mempool_edge",
                "watch": "node, Hydromancer, builder, route, or pending-intent edge changes",
                "action": "spawn focused actor-route scouts and preserve graph edge receipts",
            },
            {
                "id": "map_gap_self_heal",
                "watch": "geometry shows sparse source families, stale citations, or missing edges",
                "action": "assign patch scouts or tool-creation proposals before the next full run",
            },
        ],
        "daily_brief_contract": {
            "brief_reads": [
                "latest_full_pipeline_synthesis",
                "always_on_flash_sentinel_deltas",
                "price_vs_information_divergence",
                "verified_trade_candidates",
                "unresolved_gaps_and_watchlist",
            ],
            "operator_promise": (
                "The brief is not a single snapshot. It is the twice-daily full "
                "pass plus the continuously maintained market memory since the last pass."
            ),
        },
    }


@dataclass
class CadenceCommand:
    name: str
    command: list[str]
    purpose: str
    required_for: str = "run"
    spend_risk: str = "none"
    expected_artifacts: dict[str, str] = field(default_factory=dict)

    @property
    def shell(self) -> str:
        return " ".join(shlex.quote(part) for part in self.command)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["shell"] = self.shell
        return out


@dataclass
class CadenceRunPlan:
    plan_id: str
    mode: str
    cycle_id: str
    artifact_dir: str
    generated_at: str
    allow_live_spend: bool
    cadence_policy: dict[str, Any]
    commands: list[CadenceCommand]
    gates: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "talis_intelligence_cadence_run_plan_v1",
            "plan_id": self.plan_id,
            "mode": self.mode,
            "cycle_id": self.cycle_id,
            "artifact_dir": self.artifact_dir,
            "generated_at": self.generated_at,
            "allow_live_spend": self.allow_live_spend,
            "cadence_policy": self.cadence_policy,
            "commands": [cmd.to_dict() for cmd in self.commands],
            "gates": self.gates,
            "notes": self.notes,
        }


def build_intelligence_cadence_plan(
    *,
    mode: str,
    artifact_dir: str | Path,
    allow_live_spend: bool = False,
    cycle_id: str = "",
    scout_count: int | None = None,
    ramp_policy: str = "",
    repo_root: str | Path | None = None,
    live_cost_cap_usd: float | None = None,
    concurrency: int | None = None,
    max_tool_iterations: int = 1,
    brief_budget_usd: float = 5.0,
) -> CadenceRunPlan:
    """Build an executable, audit-friendly cadence plan.

    The plan is intentionally explicit: no command includes live-provider spend
    unless ``allow_live_spend`` is true. The caller may execute the plan or just
    publish it as the next operator checklist.
    """
    policy = default_intelligence_cadence_policy()
    repo = Path(repo_root) if repo_root else Path.cwd()
    root = Path(artifact_dir).expanduser().resolve()
    now = datetime.now(timezone.utc)
    normalized_mode = _normalize_mode(mode)
    cycle = cycle_id or f"cadence_{normalized_mode}_{now.strftime('%Y%m%dT%H%M%SZ')}"
    commands: list[CadenceCommand]
    if normalized_mode == "sentinel_tick":
        commands = _sentinel_commands(
            root=root,
            cycle_id=cycle,
            allow_live_spend=allow_live_spend,
            policy=policy,
            scout_count=scout_count,
            ramp_policy=ramp_policy,
            live_cost_cap_usd=live_cost_cap_usd,
            concurrency=concurrency,
            max_tool_iterations=max_tool_iterations,
        )
    elif normalized_mode == "full_pipeline":
        commands = _full_pipeline_commands(
            root=root,
            cycle_id=cycle,
            allow_live_spend=allow_live_spend,
            scout_count=scout_count,
            ramp_policy=ramp_policy,
            live_cost_cap_usd=live_cost_cap_usd,
            concurrency=concurrency,
            max_tool_iterations=max_tool_iterations,
            brief_budget_usd=brief_budget_usd,
        )
    else:
        raise ValueError(f"unsupported_cadence_mode:{mode}")
    return CadenceRunPlan(
        plan_id=f"icp_{cycle}",
        mode=normalized_mode,
        cycle_id=cycle,
        artifact_dir=str(root),
        generated_at=now.isoformat(),
        allow_live_spend=allow_live_spend,
        cadence_policy=policy,
        commands=commands,
        gates=_cadence_gates(normalized_mode, allow_live_spend=allow_live_spend),
        notes=[
            "Always-on sentinel output feeds the next twice-daily brief; it does not publish trades directly.",
            "Full-pipeline scheduled production remains blocked until repeat 1,000-scout shadow evidence is present.",
            f"repo_root={repo}",
        ],
    )


def write_cadence_plan(plan: CadenceRunPlan, *, output_dir: str | Path | None = None) -> Path:
    out_dir = Path(output_dir or plan.artifact_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "talis_intelligence_cadence_plan.json"
    path.write_text(json.dumps(plan.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return path


def execute_cadence_plan(
    plan: CadenceRunPlan,
    *,
    repo_root: str | Path,
    env: dict[str, str] | None = None,
    stop_on_failure: bool = True,
) -> dict[str, Any]:
    """Execute a cadence plan and persist a report beside the plan artifact."""
    repo = Path(repo_root).resolve()
    Path(plan.artifact_dir).mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    started = time.perf_counter()
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    run_env["PYTHONPATH"] = "." + (os.pathsep + run_env["PYTHONPATH"] if run_env.get("PYTHONPATH") else "")
    for command in plan.commands:
        t0 = time.perf_counter()
        proc = subprocess.run(
            command.command,
            cwd=repo,
            env=run_env,
            text=True,
            capture_output=True,
        )
        result = {
            "name": command.name,
            "purpose": command.purpose,
            "returncode": proc.returncode,
            "ok": proc.returncode == 0,
            "elapsed_s": round(time.perf_counter() - t0, 3),
            "stdout_tail": proc.stdout[-6000:],
            "stderr_tail": proc.stderr[-6000:],
            "expected_artifacts": command.expected_artifacts,
        }
        results.append(result)
        if stop_on_failure and proc.returncode != 0:
            break
    scoreboard = _read_json_file(Path(plan.artifact_dir) / "market-evolve" / "market_evolve_scoreboard.json", {})
    control_decision = build_cadence_control_decision(
        scoreboard=scoreboard,
        mode=plan.mode,
        allow_live_spend=plan.allow_live_spend,
    )
    report = {
        "schema_version": "talis_intelligence_cadence_run_report_v1",
        "plan": plan.to_dict(),
        "status": "pass" if results and all(r["ok"] for r in results) else "failed" if results else "not_run",
        "elapsed_s": round(time.perf_counter() - started, 3),
        "results": results,
        "market_evolve_scoreboard": _scoreboard_report_summary(scoreboard),
        "control_decision": control_decision,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    report_path = Path(plan.artifact_dir) / "talis_intelligence_cadence_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def build_cadence_control_decision(
    *,
    scoreboard: dict[str, Any],
    mode: str,
    allow_live_spend: bool,
) -> dict[str, Any]:
    """Convert the MarketEvolve scoreboard into an executable cadence decision."""
    normalized_mode = _normalize_mode(mode)
    if not isinstance(scoreboard, dict) or not scoreboard:
        return {
            "schema_version": "talis_cadence_control_decision_v1",
            "decision": "scoreboard_missing",
            "allowed_next_step": "rerun_market_evolve_scoreboard_export",
            "recommended_next_run": {"mode": normalized_mode, "scouts": 8},
            "spend_gate": "closed",
            "blocks_wider_spend": True,
            "why": "No MarketEvolve scoreboard artifact was available to read.",
            "source_scoreboard_id": "",
            "quality_flags": ["missing_market_evolve_scoreboard"],
        }

    status = str(scoreboard.get("status") or "unknown")
    counts = scoreboard.get("counts") if isinstance(scoreboard.get("counts"), dict) else {}
    readiness = scoreboard.get("cadence_readiness") if isinstance(scoreboard.get("cadence_readiness"), dict) else {}
    memory = scoreboard.get("evolution_memory") if isinstance(scoreboard.get("evolution_memory"), dict) else {}
    next_actions = scoreboard.get("next_actions") if isinstance(scoreboard.get("next_actions"), list) else []
    primary_action = str((next_actions[0] or {}).get("action") or "") if next_actions else ""
    open_experiments = int(counts.get("open_experiments") or 0)
    candidate_count = int(counts.get("candidate_programs") or 0)
    result_window = int(counts.get("result_window") or 0)
    gate_summary = scoreboard.get("hard_experiment_gate_summary") if isinstance(scoreboard.get("hard_experiment_gate_summary"), dict) else {}
    triggered_gates = int(gate_summary.get("triggered") or 0)
    decision = "continue_sentinel_memory"
    allowed_next_step = "sentinel_tick"
    recommended_mode = "sentinel_tick"
    recommended_scouts = 24
    blocks_wider_spend = False
    why = "Keep the always-on market memory warm until a hard experiment or geometry gate changes state."
    flags: list[str] = []

    if status == "not_started":
        decision = "initialize_market_evolve"
        allowed_next_step = "seed_default_policy_then_sentinel"
        recommended_scouts = 8
        blocks_wider_spend = True
        why = "MarketEvolve has no active policy yet; initialize before scaling calls."
    elif status.startswith("repair_needed") or triggered_gates > 0:
        decision = "repair_before_scale"
        allowed_next_step = "tool_prompt_route_repair"
        recommended_scouts = 8
        blocks_wider_spend = True
        why = "A candidate or hard gate failed; preserve the failure as repair signal before widening spend."
        flags.append("hard_gate_or_candidate_failure_blocks_scale")
    elif status == "experiment_running":
        decision = "collect_experiment_evidence"
        allowed_next_step = "paired_evolution_sentinel"
        recommended_scouts = max(16, min(96, open_experiments * 16 or 24))
        why = (
            "Open control/candidate experiments exist. Run matched scout evidence "
            "before accepting, rejecting, or mutating the policy."
        )
        if result_window <= 0:
            flags.append("open_experiment_without_result_window")
    elif status == "learning_continue_candidate":
        decision = "continue_candidate_experiment"
        allowed_next_step = "repeat_matched_experiment"
        recommended_scouts = max(32, min(128, candidate_count * 32 or 64))
        why = "Candidate beat control once but needs repeated out-of-sample evidence before promotion."
    elif status == "evolving_promoted_policy":
        if bool(readiness.get("eligible_for_shadow_schedule_review")):
            decision = "request_shadow_schedule_review"
            allowed_next_step = "shadow_schedule_review"
            recommended_mode = "full_pipeline"
            recommended_scouts = 1000
            why = "A promoted policy has repeated evidence and is eligible for human shadow-schedule review."
        else:
            decision = "widen_shadow_evaluation"
            allowed_next_step = "live_1000_shadow_candidate"
            recommended_mode = "full_pipeline"
            recommended_scouts = 1000
            why = "A policy was promoted; gather repeat 1,000-scout shadow evidence before scheduling."
    elif status == "baseline_active" and bool(memory.get("evolves")) is False:
        decision = "mutate_active_policy"
        allowed_next_step = "sentinel_mutation_tick"
        recommended_scouts = 24
        why = "Only the baseline is active and no mutation memory exists; create candidate pressure."
    elif status == "candidate_waiting_for_experiment":
        decision = "assign_candidate_experiment"
        allowed_next_step = "paired_evolution_sentinel"
        recommended_scouts = max(16, min(96, candidate_count * 16 or 24))
        why = "Candidate programs exist but need matched control/candidate evidence."

    spend_gate = "open" if allow_live_spend and not blocks_wider_spend else "closed"
    if recommended_mode == "full_pipeline" and not allow_live_spend:
        flags.append("explicit_live_spend_gate_required_for_recommended_run")
    return {
        "schema_version": "talis_cadence_control_decision_v1",
        "decision": decision,
        "allowed_next_step": allowed_next_step,
        "recommended_next_run": {
            "mode": recommended_mode,
            "scouts": recommended_scouts,
            "requires_allow_live_spend": recommended_mode == "full_pipeline" or recommended_scouts > 64,
            "primary_market_evolve_action": primary_action,
        },
        "spend_gate": spend_gate,
        "blocks_wider_spend": blocks_wider_spend,
        "why": why,
        "source_scoreboard_id": str(scoreboard.get("id") or ""),
        "scoreboard_status": status,
        "open_experiment_count": open_experiments,
        "candidate_program_count": candidate_count,
        "result_window_count": result_window,
        "best_score_delta_recent": memory.get("best_score_delta_recent"),
        "quality_flags": sorted(set(flags)),
    }


def _normalize_mode(mode: str) -> str:
    raw = str(mode or "").strip().lower().replace("-", "_")
    aliases = {
        "sentinel": "sentinel_tick",
        "always_on": "sentinel_tick",
        "always_on_flash": "sentinel_tick",
        "tick": "sentinel_tick",
        "full": "full_pipeline",
        "full_pass": "full_pipeline",
        "brief": "full_pipeline",
        "twice_daily": "full_pipeline",
    }
    return aliases.get(raw, raw)


def _read_json_file(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def _scoreboard_report_summary(scoreboard: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(scoreboard, dict) or not scoreboard:
        return {}
    memory = scoreboard.get("evolution_memory") if isinstance(scoreboard.get("evolution_memory"), dict) else {}
    counts = scoreboard.get("counts") if isinstance(scoreboard.get("counts"), dict) else {}
    return {
        "id": scoreboard.get("id"),
        "cycle_id": scoreboard.get("cycle_id"),
        "status": scoreboard.get("status"),
        "summary": scoreboard.get("summary"),
        "active_programs": counts.get("active_programs"),
        "candidate_programs": counts.get("candidate_programs"),
        "open_experiments": counts.get("open_experiments"),
        "result_window": counts.get("result_window"),
        "best_score_delta_recent": memory.get("best_score_delta_recent"),
        "next_actions": (scoreboard.get("next_actions") or [])[:4],
        "quality_flags": scoreboard.get("quality_flags") or [],
    }


def _sentinel_commands(
    *,
    root: Path,
    cycle_id: str,
    allow_live_spend: bool,
    policy: dict[str, Any],
    scout_count: int | None,
    ramp_policy: str,
    live_cost_cap_usd: float | None,
    concurrency: int | None,
    max_tool_iterations: int,
) -> list[CadenceCommand]:
    shape = (policy.get("always_on_flash") or {}).get("scouts_per_tick") or {}
    scouts = int(scout_count or shape.get("target") or 24)
    scouts = max(int(shape.get("min") or 8), min(int(shape.get("max") or 64), scouts))
    cost_cap = live_cost_cap_usd if live_cost_cap_usd is not None else max(0.10, round(scouts * 0.011, 2))
    live_root = root / "live_canary"
    prompt_dir = live_root / "prompt_outputs"
    command = [
        sys.executable,
        "scripts/run_live_scout_canary.py",
        "--n-scouts",
        str(scouts),
        "--cycle-id",
        cycle_id,
        "--artifact-dir",
        str(live_root),
        "--prompt-output-dir",
        str(prompt_dir),
        "--concurrency",
        str(concurrency or min(8, max(2, scouts // 6))),
        "--cost-cap-usd",
        f"{float(cost_cap):.2f}",
        "--max-tool-iterations",
        str(max(1, int(max_tool_iterations))),
        "--repair-tool-proposal-contracts",
        "--tool-proposal-repair-limit",
        "200",
        "--market-evolve-pairs",
        str(max(2, min(12, scouts // 4))),
    ]
    if ramp_policy:
        command.extend(["--ramp-policy", ramp_policy])
    if allow_live_spend:
        command.append("--allow-live-spend")
    scoreboard_command = [
        sys.executable,
        "scripts/export_market_evolve_scoreboard.py",
        "--cycle-id",
        cycle_id,
        "--db",
        str(live_root / "desk-live-canary.db"),
        "--cadence-mode",
        "sentinel_tick",
        "--output-dir",
        str(root / "market-evolve"),
    ]
    if allow_live_spend:
        scoreboard_command.append("--allow-live-spend")
    return [
        CadenceCommand(
            name="sentinel_live_canary",
            command=command,
            purpose="Run one always-on Flash sentinel tick against fresh market slices.",
            spend_risk="live_model_calls" if allow_live_spend else "preflight_no_spend",
            expected_artifacts={
                "canary_report": str(prompt_dir / "live_scout_canary_report.json"),
                "slice_preview": str(prompt_dir / "live_scout_slice_preview.json"),
                "outputs": str(prompt_dir / "live_scout_canary_outputs.json"),
            },
        ),
        CadenceCommand(
            name="sentinel_market_evolve_scoreboard",
            command=scoreboard_command,
            purpose="Persist the evaluator-guided learning scoreboard for this sentinel tick.",
            expected_artifacts={
                "scoreboard": str(root / "market-evolve" / "market_evolve_scoreboard.json"),
            },
        ),
        CadenceCommand(
            name="sentinel_agent_graph_export",
            command=[
                sys.executable,
                "scripts/export_agent_graph_viewer.py",
                "--run-dir",
                str(root),
                "--output-dir",
                str(root / "agent-graph"),
            ],
            purpose="Render the sentinel tick as the same clickable agent graph used by full runs.",
            expected_artifacts={
                "agent_graph": str(root / "agent-graph" / "agent_graph_state.json"),
                "index": str(root / "agent-graph" / "index.html"),
            },
        ),
    ]


def _full_pipeline_commands(
    *,
    root: Path,
    cycle_id: str,
    allow_live_spend: bool,
    scout_count: int | None,
    ramp_policy: str,
    live_cost_cap_usd: float | None,
    concurrency: int | None,
    max_tool_iterations: int,
    brief_budget_usd: float,
) -> list[CadenceCommand]:
    scouts = int(scout_count or 1000)
    cost_cap = live_cost_cap_usd if live_cost_cap_usd is not None else 5.0
    scoreboard_cycle_id = f"{cycle_id}_live_{scouts}" if allow_live_spend else f"{cycle_id}_deterministic_100"
    scoreboard_db = (
        root / "live_canary" / "desk-live-canary.db"
        if allow_live_spend else root / "deterministic_100" / "desk-100-scout.db"
    )
    launch_cmd = [
        sys.executable,
        "scripts/run_scout_system_launch_gate.py",
        "--artifact-dir",
        str(root),
        "--cycle-prefix",
        cycle_id,
        "--deterministic-scouts",
        "100",
        "--live-scouts",
        str(scouts),
        "--next-live-scouts",
        str(scouts),
        "--live-concurrency",
        str(concurrency or 8),
        "--live-cost-cap-usd",
        f"{float(cost_cap):.2f}",
        "--max-tool-iterations",
        str(max(1, int(max_tool_iterations))),
        "--repair-tool-proposal-contracts",
        "--tool-proposal-repair-limit",
        "500",
    ]
    if ramp_policy:
        launch_cmd.extend(["--ramp-policy", ramp_policy])
    if allow_live_spend:
        launch_cmd.append("--allow-live-spend")
    scoreboard_cmd = [
        sys.executable,
        "scripts/export_market_evolve_scoreboard.py",
        "--cycle-id",
        scoreboard_cycle_id,
        "--db",
        str(scoreboard_db),
        "--cadence-mode",
        "full_pipeline",
        "--output-dir",
        str(root / "market-evolve"),
    ]
    if allow_live_spend:
        scoreboard_cmd.append("--allow-live-spend")
    brief_cmd = [
        sys.executable,
        "run_full_desk.py",
        "--cycle-id",
        f"{cycle_id}_brief",
        "--scope",
        "market",
        "--budget",
        f"{float(brief_budget_usd):.2f}",
        "--parallel",
        "--paper-only",
    ]
    return [
        CadenceCommand(
            name="full_launch_gate",
            command=launch_cmd,
            purpose="Run the brief-grade scout/evolution launch gate for the twice-daily pass.",
            spend_risk="live_model_calls" if allow_live_spend else "preflight_no_spend",
            expected_artifacts={
                "launch_report": str(root / "launch-gate" / "launch_gate_report.json"),
                "learning_report": str(root / "live_canary" / "prompt_outputs" / "live_scout_learning_report.json"),
            },
        ),
        CadenceCommand(
            name="full_market_evolve_scoreboard",
            command=scoreboard_cmd,
            purpose="Freeze the evolution scoreboard before composing the daily brief.",
            expected_artifacts={
                "scoreboard": str(root / "market-evolve" / "market_evolve_scoreboard.json"),
            },
        ),
        CadenceCommand(
            name="daily_brief_composition",
            command=brief_cmd,
            purpose="Compose the market brief from the full pass plus persistent desk memory.",
            required_for="brief",
            spend_risk="specialist_model_calls",
            expected_artifacts={"manifest_or_stdout": "run_full_desk.py prints the brief path and manifest"},
        ),
    ]


def _cadence_gates(mode: str, *, allow_live_spend: bool) -> list[dict[str, Any]]:
    gates = [
        {
            "id": "explicit_spend_gate",
            "status": "open" if allow_live_spend else "closed",
            "requirement": "--allow-live-spend must be present before provider calls are made.",
        },
        {
            "id": "artifact_first",
            "status": "required",
            "requirement": "Write talis_intelligence_cadence_plan.json before executing cadence commands.",
        },
        {
            "id": "map_memory",
            "status": "required",
            "requirement": "Persist scout strings, geometry, tool proposals, and learning policy into the desk state.",
        },
    ]
    if mode == "full_pipeline":
        gates.append({
            "id": "repeatability_before_schedule",
            "status": "blocked",
            "requirement": "Scheduled production needs two independent 1,000-scout shadow passes before automatic live scheduling.",
        })
    else:
        gates.append({
            "id": "no_direct_trade_publication",
            "status": "required",
            "requirement": "Sentinel ticks only update the map or queue verification; they do not publish trades directly.",
        })
    return gates


__all__ = [
    "CadenceCommand",
    "CadenceRunPlan",
    "build_cadence_control_decision",
    "build_intelligence_cadence_plan",
    "default_intelligence_cadence_policy",
    "execute_cadence_plan",
    "write_cadence_plan",
]
