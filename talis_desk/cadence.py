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
    information_price_loop = _price_loop_report_summary(Path(plan.artifact_dir))
    information_perfusion = _perfusion_report_summary(Path(plan.artifact_dir))
    control_decision = build_cadence_control_decision(
        scoreboard=scoreboard,
        mode=plan.mode,
        allow_live_spend=plan.allow_live_spend,
        information_price_loop=information_price_loop,
        information_perfusion=information_perfusion,
    )
    report = {
        "schema_version": "talis_intelligence_cadence_run_report_v1",
        "plan": plan.to_dict(),
        "status": "pass" if results and all(r["ok"] for r in results) else "failed" if results else "not_run",
        "elapsed_s": round(time.perf_counter() - started, 3),
        "results": results,
        "market_evolve_scoreboard": _scoreboard_report_summary(scoreboard),
        "information_price_loop": information_price_loop,
        "information_perfusion": information_perfusion,
        "control_decision": control_decision,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    report_path = Path(plan.artifact_dir) / "talis_intelligence_cadence_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def build_followup_plan_from_control_decision(
    *,
    control_decision: dict[str, Any],
    artifact_dir: str | Path,
    allow_live_spend: bool = False,
    cycle_id: str = "",
    repo_root: str | Path | None = None,
    live_cost_cap_usd: float | None = None,
    concurrency: int | None = None,
    max_tool_iterations: int = 1,
    brief_budget_usd: float = 5.0,
) -> CadenceRunPlan:
    """Compile a cadence control decision into the next executable run plan."""
    if not isinstance(control_decision, dict) or not control_decision:
        raise ValueError("control_decision_required")
    recommended = (
        control_decision.get("recommended_next_run")
        if isinstance(control_decision.get("recommended_next_run"), dict)
        else {}
    )
    mode = _normalize_mode(str(recommended.get("mode") or "sentinel_tick"))
    scouts = _positive_int(recommended.get("scouts"), default=24)
    decision = str(control_decision.get("decision") or "unknown")
    blocks_wider_spend = bool(control_decision.get("blocks_wider_spend"))
    requested_spend = bool(allow_live_spend)
    allow_for_plan = requested_spend and not blocks_wider_spend
    safe_decision_slug = _slug(decision or "followup")
    source_scoreboard = str(control_decision.get("source_scoreboard_id") or "")
    now = datetime.now(timezone.utc)
    follow_cycle = cycle_id or f"cadence_followup_{safe_decision_slug}_{now.strftime('%Y%m%dT%H%M%SZ')}"
    plan = build_intelligence_cadence_plan(
        mode=mode,
        artifact_dir=artifact_dir,
        allow_live_spend=allow_for_plan,
        cycle_id=follow_cycle,
        scout_count=scouts,
        repo_root=repo_root,
        live_cost_cap_usd=live_cost_cap_usd,
        concurrency=concurrency,
        max_tool_iterations=max_tool_iterations,
        brief_budget_usd=brief_budget_usd,
    )
    plan.notes.extend([
        "Compiled from MarketEvolve cadence_control; no operator should hand-translate JSON into commands.",
        f"control_decision={decision}",
        f"allowed_next_step={control_decision.get('allowed_next_step') or ''}",
        f"source_scoreboard_id={source_scoreboard}",
        f"requested_live_spend={requested_spend}",
        f"live_spend_allowed_in_plan={allow_for_plan}",
    ])
    if blocks_wider_spend and requested_spend:
        plan.gates.append({
            "id": "control_blocks_wider_spend",
            "status": "closed",
            "requirement": "The MarketEvolve control decision blocked wider spend; repair/evidence gates must clear first.",
        })
    return plan


def build_followup_plan_from_report(
    *,
    report_path: str | Path,
    artifact_dir: str | Path,
    allow_live_spend: bool = False,
    cycle_id: str = "",
    repo_root: str | Path | None = None,
    live_cost_cap_usd: float | None = None,
    concurrency: int | None = None,
    max_tool_iterations: int = 1,
    brief_budget_usd: float = 5.0,
) -> CadenceRunPlan:
    report = _read_json_file(Path(report_path).expanduser().resolve(), {})
    control = _control_decision_from_payload(report)
    if not control:
        raise ValueError("report_missing_control_decision")
    return build_followup_plan_from_control_decision(
        control_decision=control,
        artifact_dir=artifact_dir,
        allow_live_spend=allow_live_spend,
        cycle_id=cycle_id,
        repo_root=repo_root,
        live_cost_cap_usd=live_cost_cap_usd,
        concurrency=concurrency,
        max_tool_iterations=max_tool_iterations,
        brief_budget_usd=brief_budget_usd,
    )


def build_followup_plan_from_scoreboard(
    *,
    scoreboard_path: str | Path,
    artifact_dir: str | Path,
    mode: str = "sentinel_tick",
    allow_live_spend: bool = False,
    cycle_id: str = "",
    repo_root: str | Path | None = None,
    live_cost_cap_usd: float | None = None,
    concurrency: int | None = None,
    max_tool_iterations: int = 1,
    brief_budget_usd: float = 5.0,
) -> CadenceRunPlan:
    scoreboard = _read_json_file(Path(scoreboard_path).expanduser().resolve(), {})
    control = _control_decision_from_payload(scoreboard)
    if not control:
        control = build_cadence_control_decision(
            scoreboard=scoreboard,
            mode=mode,
            allow_live_spend=allow_live_spend,
        )
    return build_followup_plan_from_control_decision(
        control_decision=control,
        artifact_dir=artifact_dir,
        allow_live_spend=allow_live_spend,
        cycle_id=cycle_id,
        repo_root=repo_root,
        live_cost_cap_usd=live_cost_cap_usd,
        concurrency=concurrency,
        max_tool_iterations=max_tool_iterations,
        brief_budget_usd=brief_budget_usd,
    )


def build_cadence_control_decision(
    *,
    scoreboard: dict[str, Any],
    mode: str,
    allow_live_spend: bool,
    information_price_loop: dict[str, Any] | None = None,
    information_perfusion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert the MarketEvolve scoreboard into an executable cadence decision."""
    normalized_mode = _normalize_mode(mode)
    price_loop = information_price_loop if isinstance(information_price_loop, dict) else {}
    perfusion = information_perfusion if isinstance(information_perfusion, dict) else {}
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
            "information_price_loop": _control_price_loop_payload(price_loop),
            "information_perfusion": _control_perfusion_payload(perfusion),
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

    price_eval_count = _floatish(price_loop.get("outcome_eval_count"), default=0.0)
    price_observed_rate = _floatish(price_loop.get("outcome_observed_rate"), default=0.0)
    price_direction_hit_rate = _floatish(price_loop.get("outcome_direction_hit_rate"), default=0.0)
    price_avg_edge = _floatish(price_loop.get("avg_realized_edge_score"), default=0.0)
    price_threshold_hit_rate = _floatish(price_loop.get("outcome_threshold_hit_rate"), default=0.0)
    price_loop_status = str(price_loop.get("status") or "missing")
    if price_loop_status in {"missing", "not_found"}:
        flags.append("information_price_loop_missing")
    elif price_eval_count <= 0:
        flags.append("information_price_loop_no_outcomes_yet")

    can_price_loop_steer = decision in {
        "continue_sentinel_memory",
        "mutate_active_policy",
        "assign_candidate_experiment",
    }
    if can_price_loop_steer and price_eval_count >= 1 and price_observed_rate > 0:
        if price_direction_hit_rate >= 0.60 and price_avg_edge >= 0.65:
            decision = "price_loop_confirms_signal"
            allowed_next_step = "paired_evolution_sentinel"
            recommended_mode = "sentinel_tick"
            recommended_scouts = max(recommended_scouts, 32)
            blocks_wider_spend = False
            why = (
                "Information strings are scoring against realized price movement. "
                "Run matched scouts so MarketEvolve can separate repeatable policy skill from luck."
            )
            flags.append("information_price_loop_positive_edge")
        elif price_eval_count >= 5 and price_direction_hit_rate <= 0.40 and price_avg_edge < 0.50:
            decision = "price_loop_repair_before_scale"
            allowed_next_step = "prompt_route_price_feedback_repair"
            recommended_mode = "sentinel_tick"
            recommended_scouts = 8
            blocks_wider_spend = True
            why = (
                "Information strings are maturing against price without directional skill. "
                "Repair prompts, routing, and source selection before widening spend."
            )
            flags.append("information_price_loop_negative_edge_blocks_scale")
    if price_threshold_hit_rate >= 0.50 and price_eval_count >= 1:
        flags.append("information_price_loop_threshold_hits_present")

    perfusion_status = str(perfusion.get("status") or "missing")
    perfusion_cell_count = _floatish(perfusion.get("cell_count"), default=0.0)
    perfusion_routed_cell_count = _floatish(perfusion.get("routed_cell_count"), default=0.0)
    perfusion_avg_pressure_gradient = _floatish(perfusion.get("avg_pressure_gradient"), default=0.0)
    perfusion_max_dilation = _floatish(perfusion.get("max_dilation_score"), default=0.0)
    perfusion_high_unabsorbed = _floatish(perfusion.get("high_pressure_unabsorbed_rate"), default=0.0)
    perfusion_oxygenation_gap = _floatish(perfusion.get("oxygenation_gap_rate"), default=0.0)
    perfusion_avg_source_oxygenation = _floatish(perfusion.get("avg_source_oxygenation"), default=0.0)
    perfusion_avg_latch_risk = _floatish(perfusion.get("avg_latch_risk"), default=0.0)
    perfusion_high_latch_risk = _floatish(perfusion.get("high_latch_risk_rate"), default=0.0)
    if perfusion_status in {"missing", "not_found"}:
        flags.append("information_perfusion_missing")
    elif perfusion_cell_count <= 0:
        flags.append("information_perfusion_no_cells")
    elif perfusion_routed_cell_count <= 0:
        flags.append("information_perfusion_no_routed_cells")

    can_perfusion_steer = decision in {
        "continue_sentinel_memory",
        "mutate_active_policy",
        "assign_candidate_experiment",
    }
    if can_perfusion_steer and perfusion_cell_count >= 1:
        if (
            perfusion_high_latch_risk >= 0.25
            and perfusion_avg_latch_risk >= 0.45
            and perfusion_avg_pressure_gradient >= 0.35
        ):
            decision = "perfusion_latch_repair_sentinel"
            allowed_next_step = "perfusion_latch_repair"
            recommended_mode = "sentinel_tick"
            recommended_scouts = max(recommended_scouts, 16)
            blocks_wider_spend = False
            why = (
                "The perfusion matrix sees information pressure trapped behind resistance. "
                "Run a small repair sentinel to unlatch stale, congested, or over-constrained evidence before adding broad scout flow."
            )
            flags.append("information_perfusion_latch_risk")
        elif (
            perfusion_high_unabsorbed >= 0.25
            and perfusion_avg_pressure_gradient >= 0.45
            and perfusion_max_dilation >= 0.55
        ):
            decision = "perfusion_pressure_requests_sentinel"
            allowed_next_step = "perfusion_pressure_sentinel"
            recommended_mode = "sentinel_tick"
            recommended_scouts = max(recommended_scouts, 32)
            blocks_wider_spend = False
            why = (
                "The perfusion matrix sees oxygenated information pressure that price has not absorbed. "
                "Run a matched sentinel slice before the next full brief so MarketEvolve can test whether the pressure repeats."
            )
            flags.append("information_perfusion_positive_pressure")
        elif perfusion_oxygenation_gap >= 0.25 and perfusion_avg_pressure_gradient >= 0.35:
            decision = "perfusion_source_oxygenation_repair"
            allowed_next_step = "source_oxygenation_sentinel"
            recommended_mode = "sentinel_tick"
            recommended_scouts = max(recommended_scouts, 16)
            blocks_wider_spend = False
            why = (
                "The perfusion matrix sees pressure, but source oxygenation is weak. "
                "Run a small source-repair sentinel before widening verification spend."
            )
            flags.append("information_perfusion_oxygenation_gap")
    if perfusion_avg_source_oxygenation >= 0.70 and perfusion_cell_count >= 1:
        flags.append("information_perfusion_well_oxygenated")

    requires_live_spend = _control_decision_requires_live_spend(
        decision=decision,
        recommended_mode=recommended_mode,
        recommended_scouts=recommended_scouts,
    )
    spend_gate = "open" if allow_live_spend and not blocks_wider_spend else "closed"
    if requires_live_spend and not allow_live_spend:
        flags.append("explicit_live_spend_gate_required_for_recommended_run")
    if recommended_mode == "full_pipeline" and not allow_live_spend:
        flags.append("explicit_live_spend_gate_required_for_recommended_run")
    return {
        "schema_version": "talis_cadence_control_decision_v1",
        "decision": decision,
        "allowed_next_step": allowed_next_step,
        "recommended_next_run": {
            "mode": recommended_mode,
            "scouts": recommended_scouts,
            "requires_allow_live_spend": requires_live_spend,
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
        "information_price_loop": _control_price_loop_payload(price_loop),
        "information_perfusion": _control_perfusion_payload(perfusion),
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


def _control_decision_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    direct = payload.get("control_decision")
    if isinstance(direct, dict) and direct.get("schema_version") == "talis_cadence_control_decision_v1":
        return direct
    nested = payload.get("cadence_control")
    if isinstance(nested, dict) and nested.get("schema_version") == "talis_cadence_control_decision_v1":
        return nested
    return {}


def _control_decision_requires_live_spend(
    *,
    decision: str,
    recommended_mode: str,
    recommended_scouts: int,
) -> bool:
    if recommended_mode == "full_pipeline":
        return True
    if recommended_scouts > 64:
        return True
    return decision in {
        "collect_experiment_evidence",
        "continue_candidate_experiment",
        "perfusion_latch_repair_sentinel",
        "perfusion_pressure_requests_sentinel",
        "perfusion_source_oxygenation_repair",
        "price_loop_confirms_signal",
        "widen_shadow_evaluation",
        "request_shadow_schedule_review",
    }


def _positive_int(raw: Any, *, default: int) -> int:
    try:
        value = int(raw)
    except Exception:
        value = int(default)
    return max(1, value)


def _slug(raw: str) -> str:
    out = []
    for ch in str(raw).lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in {"_", "-", " "}:
            out.append("_")
    return "".join(out).strip("_")[:80] or "control"


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


def _price_loop_report_summary(root: Path) -> dict[str, Any]:
    outcome_payload = _read_first_json(root, "information_price_outcomes.json")
    start_payload = _read_first_json(root, "live_price_observations_start.json")
    final_payload = _read_first_json(root, "live_price_observations_final.json")
    if not outcome_payload and not start_payload and not final_payload:
        return {
            "schema_version": "talis_information_price_loop_summary_v1",
            "status": "missing",
            "outcome_eval_count": 0.0,
            "quality_flags": ["information_price_loop_artifacts_missing"],
        }
    summary = outcome_payload.get("summary") if isinstance(outcome_payload.get("summary"), dict) else {}
    outcomes = outcome_payload.get("outcomes") if isinstance(outcome_payload.get("outcomes"), list) else []
    quality_flags = []
    for payload in (outcome_payload, start_payload, final_payload):
        if isinstance(payload, dict):
            quality_flags.extend(_string_list(payload.get("quality_flags")))
    return {
        "schema_version": "talis_information_price_loop_summary_v1",
        "status": (
            "scored"
            if _floatish(summary.get("outcome_eval_count") or outcome_payload.get("evaluated_count"), default=0.0) > 0
            else "observed"
            if start_payload or final_payload
            else "missing"
        ),
        "cycle_id": outcome_payload.get("cycle_id") or "",
        "start_observations": _price_observation_stage_summary(start_payload),
        "final_observations": _price_observation_stage_summary(final_payload),
        "outcome_eval_count": _floatish(summary.get("outcome_eval_count") or outcome_payload.get("evaluated_count"), default=0.0),
        "outcome_observed_count": _floatish(summary.get("outcome_observed_count"), default=0.0),
        "outcome_observed_rate": _floatish(summary.get("outcome_observed_rate"), default=0.0),
        "outcome_direction_hit_rate": _floatish(summary.get("outcome_direction_hit_rate"), default=0.0),
        "outcome_threshold_hit_rate": _floatish(summary.get("outcome_threshold_hit_rate"), default=0.0),
        "avg_realized_edge_score": _floatish(summary.get("avg_realized_edge_score"), default=0.0),
        "avg_abs_price_return_pct": _floatish(summary.get("avg_abs_price_return_pct"), default=0.0),
        "avg_signed_return_pct": _floatish(summary.get("avg_signed_return_pct"), default=0.0),
        "early_repricing_hit_rate": _floatish(summary.get("early_repricing_hit_rate"), default=0.0),
        "top_outcomes": _top_information_price_outcomes(outcomes),
        "quality_flags": sorted(set(quality_flags)),
    }


def _perfusion_report_summary(root: Path) -> dict[str, Any]:
    payload = _read_first_json(root, "information_perfusion.json")
    if not payload:
        return {
            "schema_version": "talis_information_perfusion_summary_v1",
            "status": "missing",
            "cell_count": 0.0,
            "routed_cell_count": 0.0,
            "quality_flags": ["information_perfusion_artifact_missing"],
        }
    global_metrics = payload.get("global_metrics") if isinstance(payload.get("global_metrics"), dict) else {}
    cells = payload.get("cells") if isinstance(payload.get("cells"), list) else []
    cell_rows = [cell for cell in cells if isinstance(cell, dict)]
    routed = [
        cell
        for cell in cell_rows
        if str(cell.get("route_directive") or "maintain") != "maintain"
    ]
    quality_flags = _string_list(payload.get("quality_flags"))
    high_pressure_unabsorbed = 0
    oxygenation_gaps = 0
    high_latch_risk = 0
    recommended_scouts = 0.0
    for cell in cell_rows:
        cell_flags = _string_list(cell.get("quality_flags"))
        quality_flags.extend(cell_flags)
        metrics = cell.get("metrics") if isinstance(cell.get("metrics"), dict) else {}
        pressure_gradient = _floatish(metrics.get("pressure_gradient"), default=0.0)
        price_absorption = _floatish(metrics.get("price_absorption"), default=1.0)
        latch_risk = _floatish(metrics.get("latch_risk"), default=0.0)
        if "information_not_absorbed_by_price" in cell_flags or (
            pressure_gradient >= 0.50 and price_absorption <= 0.35
        ):
            high_pressure_unabsorbed += 1
        if "information_latch_risk" in cell_flags or latch_risk >= 0.45:
            high_latch_risk += 1
        if str(cell.get("route_directive") or "") == "oxygenate_sources":
            oxygenation_gaps += 1
        recommended_scouts += _floatish(cell.get("recommended_scouts"), default=0.0)
    return {
        "schema_version": "talis_information_perfusion_summary_v1",
        "status": "ready" if cell_rows else "empty",
        "cycle_id": payload.get("cycle_id") or "",
        "cell_count": _floatish(global_metrics.get("cell_count"), default=float(len(cell_rows))),
        "routed_cell_count": _floatish(global_metrics.get("routed_cell_count"), default=float(len(routed))),
        "avg_information_pressure": _floatish(
            global_metrics.get("avg_information_pressure"),
            default=_avgish([_cell_metric(cell, "information_pressure") for cell in cell_rows]),
        ),
        "avg_pressure_gradient": _floatish(
            global_metrics.get("avg_pressure_gradient"),
            default=_avgish([_cell_metric(cell, "pressure_gradient") for cell in cell_rows]),
        ),
        "avg_source_oxygenation": _floatish(
            global_metrics.get("avg_source_oxygenation"),
            default=_avgish([_cell_metric(cell, "source_oxygenation") for cell in cell_rows]),
        ),
        "avg_resistance": _floatish(
            global_metrics.get("avg_resistance"),
            default=_avgish([_cell_metric(cell, "resistance") for cell in cell_rows]),
        ),
        "avg_latch_risk": _floatish(
            global_metrics.get("avg_latch_risk"),
            default=_avgish([_cell_metric(cell, "latch_risk") for cell in cell_rows]),
        ),
        "avg_flow_shear": _floatish(
            global_metrics.get("avg_flow_shear"),
            default=_avgish([_cell_metric(cell, "flow_shear") for cell in cell_rows]),
        ),
        "avg_transport_cost": _floatish(
            global_metrics.get("avg_transport_cost"),
            default=_avgish([_cell_metric(cell, "transport_cost") for cell in cell_rows]),
        ),
        "avg_perfusion_efficiency": _floatish(
            global_metrics.get("avg_perfusion_efficiency"),
            default=_avgish([_cell_metric(cell, "perfusion_efficiency") for cell in cell_rows]),
        ),
        "max_dilation_score": _floatish(
            global_metrics.get("max_dilation_score"),
            default=max([_cell_metric(cell, "dilation_score") for cell in cell_rows] or [0.0]),
        ),
        "recommended_scouts": _floatish(
            global_metrics.get("recommended_scouts"),
            default=recommended_scouts,
        ),
        "high_pressure_unabsorbed_rate": round(high_pressure_unabsorbed / max(1, len(cell_rows)), 4),
        "oxygenation_gap_rate": round(oxygenation_gaps / max(1, len(cell_rows)), 4),
        "high_latch_risk_rate": round(high_latch_risk / max(1, len(cell_rows)), 4),
        "top_cells": _top_information_perfusion_cells(cell_rows),
        "quality_flags": sorted(set(quality_flags)),
    }


def _read_first_json(root: Path, filename: str) -> dict[str, Any]:
    for path in _artifact_candidates(root, filename):
        payload = _read_json_file(path, {})
        if isinstance(payload, dict) and payload:
            return payload
    return {}


def _artifact_candidates(root: Path, filename: str) -> list[Path]:
    return [
        root / "live_canary" / "prompt_outputs" / filename,
        root / "launch-gate" / "raw" / filename,
        root / "deterministic_100" / "prompt_outputs" / filename,
        root / "agent-graph" / "raw" / filename,
        root / filename,
    ]


def _price_observation_stage_summary(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        return {"status": "missing", "observed_count": 0, "persisted_count": 0}
    observations = payload.get("observations") if isinstance(payload.get("observations"), list) else []
    return {
        "status": payload.get("status") or ("collected" if observations else "empty"),
        "source": payload.get("source") or "",
        "observed_count": int(payload.get("observed_count") or len(observations)),
        "persisted_count": int(payload.get("persisted_count") or 0),
        "missing_entities": _string_list(payload.get("missing_entities")),
        "quality_flags": _string_list(payload.get("quality_flags")),
    }


def _top_information_price_outcomes(outcomes: list[Any], *, limit: int = 5) -> list[dict[str, Any]]:
    rows = [row for row in outcomes if isinstance(row, dict)]
    rows.sort(key=lambda row: _floatish(row.get("realized_edge_score"), default=0.0), reverse=True)
    return [
        {
            "id": row.get("id"),
            "string_id": row.get("string_id"),
            "entity": row.get("entity"),
            "expected_direction": row.get("expected_direction"),
            "direction_hit": bool(row.get("direction_hit")),
            "threshold_hit": bool(row.get("threshold_hit")),
            "realized_edge_score": _floatish(row.get("realized_edge_score"), default=0.0),
            "signed_return_pct": _floatish(row.get("signed_return_pct"), default=0.0),
            "quality_flags": _string_list(row.get("quality_flags")),
        }
        for row in rows[:limit]
    ]


def _control_price_loop_payload(price_loop: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(price_loop, dict) or not price_loop:
        return {"status": "missing", "outcome_eval_count": 0.0}
    return {
        "status": price_loop.get("status") or "missing",
        "outcome_eval_count": _floatish(price_loop.get("outcome_eval_count"), default=0.0),
        "outcome_observed_rate": _floatish(price_loop.get("outcome_observed_rate"), default=0.0),
        "outcome_direction_hit_rate": _floatish(price_loop.get("outcome_direction_hit_rate"), default=0.0),
        "outcome_threshold_hit_rate": _floatish(price_loop.get("outcome_threshold_hit_rate"), default=0.0),
        "avg_realized_edge_score": _floatish(price_loop.get("avg_realized_edge_score"), default=0.0),
        "early_repricing_hit_rate": _floatish(price_loop.get("early_repricing_hit_rate"), default=0.0),
    }


def _top_information_perfusion_cells(cells: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    rows = list(cells)
    rows.sort(
        key=lambda cell: (
            _cell_metric(cell, "dilation_score"),
            _cell_metric(cell, "pressure_gradient"),
        ),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    for cell in rows[:limit]:
        out.append({
            "cell_key": cell.get("cell_key"),
            "entity": cell.get("entity"),
            "horizon": cell.get("horizon"),
            "lens": cell.get("lens"),
            "theme": cell.get("theme"),
            "route_directive": cell.get("route_directive") or "maintain",
            "recommended_scouts": int(_floatish(cell.get("recommended_scouts"), default=0.0)),
            "information_pressure": _cell_metric(cell, "information_pressure"),
            "pressure_gradient": _cell_metric(cell, "pressure_gradient"),
            "price_absorption": _cell_metric(cell, "price_absorption"),
            "source_oxygenation": _cell_metric(cell, "source_oxygenation"),
            "resistance": _cell_metric(cell, "resistance"),
            "dilation_score": _cell_metric(cell, "dilation_score"),
            "latch_risk": _cell_metric(cell, "latch_risk"),
            "flow_shear": _cell_metric(cell, "flow_shear"),
            "transport_cost": _cell_metric(cell, "transport_cost"),
            "perfusion_efficiency": _cell_metric(cell, "perfusion_efficiency"),
            "quality_flags": _string_list(cell.get("quality_flags")),
        })
    return out


def _control_perfusion_payload(perfusion: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(perfusion, dict) or not perfusion:
        return {"status": "missing", "cell_count": 0.0, "routed_cell_count": 0.0}
    return {
        "status": perfusion.get("status") or "missing",
        "cell_count": _floatish(perfusion.get("cell_count"), default=0.0),
        "routed_cell_count": _floatish(perfusion.get("routed_cell_count"), default=0.0),
        "avg_information_pressure": _floatish(perfusion.get("avg_information_pressure"), default=0.0),
        "avg_pressure_gradient": _floatish(perfusion.get("avg_pressure_gradient"), default=0.0),
        "avg_source_oxygenation": _floatish(perfusion.get("avg_source_oxygenation"), default=0.0),
        "avg_resistance": _floatish(perfusion.get("avg_resistance"), default=0.0),
        "avg_latch_risk": _floatish(perfusion.get("avg_latch_risk"), default=0.0),
        "avg_flow_shear": _floatish(perfusion.get("avg_flow_shear"), default=0.0),
        "avg_transport_cost": _floatish(perfusion.get("avg_transport_cost"), default=0.0),
        "avg_perfusion_efficiency": _floatish(perfusion.get("avg_perfusion_efficiency"), default=0.0),
        "max_dilation_score": _floatish(perfusion.get("max_dilation_score"), default=0.0),
        "high_pressure_unabsorbed_rate": _floatish(perfusion.get("high_pressure_unabsorbed_rate"), default=0.0),
        "oxygenation_gap_rate": _floatish(perfusion.get("oxygenation_gap_rate"), default=0.0),
        "high_latch_risk_rate": _floatish(perfusion.get("high_latch_risk_rate"), default=0.0),
        "top_cells": (perfusion.get("top_cells") if isinstance(perfusion.get("top_cells"), list) else [])[:5],
    }


def _cell_metric(cell: dict[str, Any], key: str) -> float:
    metrics = cell.get("metrics") if isinstance(cell.get("metrics"), dict) else {}
    return _floatish(metrics.get(key), default=0.0)


def _avgish(values: list[float]) -> float:
    nums = [float(value) for value in values]
    if not nums:
        return 0.0
    return round(sum(nums) / len(nums), 4)


def _floatish(raw: Any, *, default: float) -> float:
    try:
        return float(raw)
    except Exception:
        return default


def _string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if item not in (None, "")]


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
        "--preserve-db",
        "--collect-price-observations",
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
        "--collect-price-observations",
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
        {
            "id": "information_price_loop",
            "status": "required",
            "requirement": "Capture price observations and evaluate information strings against realized movement before the next routing decision.",
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
    "build_followup_plan_from_control_decision",
    "build_followup_plan_from_report",
    "build_followup_plan_from_scoreboard",
    "build_intelligence_cadence_plan",
    "default_intelligence_cadence_policy",
    "execute_cadence_plan",
    "write_cadence_plan",
]
