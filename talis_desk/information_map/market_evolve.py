"""Evaluator-guided evolution for Talis research policy.

AlphaEvolve evolves programs against automated evaluators. MarketEvolve is the
market-intelligence analogue: it evolves the research policy that decides how
scouts are prompted, how slices are routed, which tools deserve budget, and
which alpha-geometry cells graduate to verification.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from ..store import get_desk_store
from .alpha_geometry import (
    compute_alpha_geometry,
    load_alpha_geometry,
    normalize_geometry_weights,
    normalize_routing_thresholds,
    score_alpha_geometry_components,
)
from .objective import ACCEPTANCE_THRESHOLD, evaluate_prompt_scale_gate
from .perfusion import compute_information_perfusion, load_information_perfusion


EVALUATOR_VERSION = "market_evolve_v1"
PROGRAM_KIND = "research_policy"


DEFAULT_MARKET_EVOLVE_GENOME: dict[str, Any] = {
    "prompt_policy": {
        "default_variant": "mycelial_network_v1",
        "variant_map": {
            "default": "depth_ladder_v1",
            "seam_lenses": "mycelial_network_v1",
            "fast_alpha": "adversarial_alpha_v1",
            "temporal_context": "temporal_pyramid_v1",
            "primary_source": "source_first_v1",
        },
        "fallback_variants": [
            "skeptical_operator_v1",
            "depth_ladder_v1",
            "early_alpha_v1",
        ],
        "min_information_strings": 1,
        "require_mechanism": True,
        "require_kill_signal": True,
        "require_evidence_refs": True,
        "prior_context_topk": 6,
    },
    "routing_thresholds": {
        "min_valid_string_rate": 0.70,
        "min_source_independence": 0.45,
        "min_verifier_readiness": 0.55,
        "max_fragility": 0.45,
        "max_low_ev_tool_rate": 0.25,
        "coverage_gap_budget_share": 0.10,
        "frontier_exploitation_budget_share": 0.04,
        "coverage_exploration_budget_share": 0.04,
        "price_feedback_exploitation_budget_share": 0.02,
        "perfusion_followup_budget_share": 0.02,
        "use_frontier_llm_governor": True,
        "frontier_llm_governor_model": "anthropic:claude-opus-4-7",
        "frontier_llm_governor_seed_share": 0.25,
    },
    "tool_request_policy": {
        "max_tool_candidates_per_seed": 10,
        "max_new_tool_proposals_per_cycle": 12,
        "max_tool_promotions_per_cycle": 3,
        "require_expected_edge": True,
        "require_eval_plan": True,
        "auto_promote_high_priority_tools": False,
        "prefer_learned_tools": False,
        "learned_tool_priority_boost": 0.0,
        "prefer_price_anchor_tools": False,
        "require_disconfirming_price_check": False,
        "prefer_information_perfusion_tools": False,
        "source_family_targets": [
            "hydromancer",
            "our_hl_node",
            "market_microstructure",
            "web_attention",
            "fundamentals_filings",
            "macro_official",
        ],
    },
    "cortex_policy": {
        "min_task_completion_rate": 0.80,
        "min_shape_observation_rate": 0.80,
        "max_task_failure_rate": 0.20,
        "require_shape_tool_before_followups": True,
        "defer_external_followups_until_shape_read": True,
        "execute_bounded_followup_tools": True,
        "max_tools_per_task": 4,
    },
    "evolution_policy": {
        "max_open_experiments_per_parent": 4,
        "max_open_same_signature_per_parent": 1,
        "max_mutations_per_evaluation": 2,
        "diversity_axis": "mutation_family",
        "population_mode": "bounded_parallel",
    },
    "geometry_weights": {
        "frontier_pressure": 0.28,
        "source_independence": 0.22,
        "verifier_readiness": 0.22,
        "tension": 0.14,
        "support_mass": 0.14,
        "fragility_penalty": 0.24,
    },
    "objective_weights": {
        "string_yield_per_scout": 0.12,
        "valid_string_rate": 0.18,
        "prompt_quality": 0.08,
        "prompt_pass_rate": 0.06,
        "source_independence": 0.14,
        "verifier_readiness": 0.14,
        "frontier_candidate_rate": 0.12,
        "verifier_pass_rate": 0.10,
        "report_yield": 0.08,
        "tool_activation_rate": 0.06,
        "learned_tool_usage_per_scout": 0.06,
        "learned_tool_success_rate": 0.04,
        "governor_string_yield_per_seed": 0.08,
        "governor_valid_string_rate": 0.06,
        "governor_gap_repair_rate": 0.06,
        "governor_source_independence": 0.05,
        "route_contract_success_rate": 0.07,
        "geometry_route_action_rate": 0.04,
        "cortex_task_completion_rate": 0.05,
        "cortex_shape_observation_rate": 0.05,
        "cortex_followup_execution_rate": 0.03,
        "outcome_observed_rate": 0.04,
        "outcome_direction_hit_rate": 0.10,
        "outcome_threshold_hit_rate": 0.08,
        "avg_realized_edge_score": 0.08,
        "perfusion_routed_cell_rate": 0.04,
        "perfusion_avg_pressure_gradient": 0.05,
        "perfusion_avg_source_oxygenation": 0.04,
        "perfusion_max_dilation_score": 0.04,
        "fragility_penalty": 0.12,
        "fragile_verify_penalty": 0.09,
        "high_signal_observe_penalty": 0.06,
        "prompt_contract_failure_penalty": 0.08,
        "route_contract_failure_penalty": 0.07,
        "low_ev_tool_penalty": 0.06,
        "tool_eval_failed_penalty": 0.05,
        "runtime_adapter_backlog_penalty": 0.04,
        "verifier_abstain_penalty": 0.04,
        "governor_waste_penalty": 0.05,
        "cortex_task_failure_penalty": 0.06,
        "cortex_task_pending_penalty": 0.03,
        "cortex_shape_blocked_followup_penalty": 0.03,
        "outcome_unobserved_penalty": 0.05,
        "perfusion_high_resistance_penalty": 0.04,
        "perfusion_price_sensor_gap_penalty": 0.03,
    },
}


DEFAULT_OBJECTIVE: dict[str, Any] = {
    "name": "accepted_unique_high_quality_coverage_per_dollar",
    "hard_gates": {
        "min_valid_string_rate": 0.60,
        "max_avg_fragility": 0.65,
        "max_fragile_verify_rate": 0.20,
        "max_low_ev_tool_rate": 0.40,
        "max_tool_eval_failed_rate": 0.60,
        "min_cortex_task_completion_rate": 0.50,
        "max_cortex_task_failure_rate": 0.50,
    },
    "promotion_rule": (
        "Candidate programs must beat the active baseline on evaluator score "
        "and pass hard gates before any future activation."
    ),
}


@dataclass
class MarketEvolveProgram:
    program_id: str
    program_kind: str
    name: str
    generation: int = 0
    parent_program_ids: list[str] = field(default_factory=list)
    genome: dict[str, Any] = field(default_factory=dict)
    objective: dict[str, Any] = field(default_factory=dict)
    status: str = "candidate"
    created_from_cycle_id: str = ""
    score: float = 0.0
    metrics: dict[str, float] = field(default_factory=dict)
    quality_flags: list[str] = field(default_factory=list)


@dataclass
class MarketEvolveEvaluation:
    evaluation_id: str
    program_id: str
    cycle_id: str
    evaluator_version: str
    score: float
    metrics: dict[str, float] = field(default_factory=dict)
    baseline_metrics: dict[str, float] = field(default_factory=dict)
    passed: bool = False
    rationale: str = ""
    quality_flags: list[str] = field(default_factory=list)


@dataclass
class MarketEvolveMutation:
    mutation_id: str
    parent_program_id: str
    child_program_id: str
    cycle_id: str
    mutation_kind: str
    mutation: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    status: str = "proposed"


@dataclass
class MarketEvolveStep:
    cycle_id: str
    programs: list[MarketEvolveProgram] = field(default_factory=list)
    evaluations: list[MarketEvolveEvaluation] = field(default_factory=list)
    mutations: list[MarketEvolveMutation] = field(default_factory=list)
    child_programs: list[MarketEvolveProgram] = field(default_factory=list)
    experiment_plans: list[dict[str, Any]] = field(default_factory=list)
    experiment_results: list[dict[str, Any]] = field(default_factory=list)
    quality_flags: list[str] = field(default_factory=list)

    @property
    def best_evaluation(self) -> Optional[MarketEvolveEvaluation]:
        if not self.evaluations:
            return None
        return max(self.evaluations, key=lambda e: e.score)


@dataclass
class MarketEvolveMutationCandidate:
    mutation_kind: str
    rationale: str
    mutation_patch: dict[str, Any]
    mutation_source: dict[str, Any] = field(default_factory=dict)


def run_market_evolve_step(
    *,
    cycle_id: str,
    conn: Optional[sqlite3.Connection] = None,
) -> MarketEvolveStep:
    """Evaluate active research-policy programs and persist candidate mutations."""
    db = conn or get_desk_store().conn
    _ensure_market_evolve_tables(db)
    experiment_results = evaluate_market_evolve_experiments(cycle_id=cycle_id, conn=db)
    programs = load_market_evolve_programs(status="active", conn=db)
    if not programs:
        programs = [seed_default_market_evolve_program(cycle_id=cycle_id, conn=db)]

    evaluations: list[MarketEvolveEvaluation] = []
    mutations: list[MarketEvolveMutation] = []
    child_programs: list[MarketEvolveProgram] = []
    for program in programs:
        evaluation = evaluate_market_evolve_program(
            program=program,
            cycle_id=cycle_id,
            conn=db,
        )
        evaluations.append(evaluation)
        cortex_review: dict[str, Any] = {}
        try:
            from .geometry_cortex import build_alpha_geometry_cortex_review

            cortex_review = build_alpha_geometry_cortex_review(
                cycle_id=cycle_id,
                metrics=evaluation.metrics,
                use_llm=False,
                conn=db,
            )
        except Exception as exc:
            cortex_review = {
                "schema_version": "alpha_geometry_cortex_review_v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        children = propose_market_evolve_mutations(
            program=program,
            evaluation=evaluation,
            cycle_id=cycle_id,
            cortex_review=cortex_review,
            conn=db,
        )
        for child in children:
            child_programs.append(child)
            mutation = _last_mutation_for_child(
                child_program_id=child.program_id,
                cycle_id=cycle_id,
                conn=db,
            )
            if mutation is not None:
                mutations.append(mutation)

    flags = ["market_evolve_v1"]
    if not any(e.passed for e in evaluations):
        flags.append("no_program_passed_hard_gates")
    if child_programs:
        flags.append("candidate_mutations_proposed")
    if any(
        isinstance((m.mutation or {}).get("_geometry_cortex_review"), dict)
        for m in mutations
    ):
        flags.append("geometry_cortex_review_driven_mutation")
    if experiment_results:
        flags.append("experiment_results_evaluated")
    experiment_plans = load_market_evolve_experiments(cycle_id=cycle_id, conn=db)
    if experiment_plans:
        flags.append("hard_experiment_plans_available")
    return MarketEvolveStep(
        cycle_id=cycle_id,
        programs=programs,
        evaluations=evaluations,
        mutations=mutations,
        child_programs=child_programs,
        experiment_plans=experiment_plans,
        experiment_results=experiment_results,
        quality_flags=flags,
    )


def seed_default_market_evolve_program(
    *,
    cycle_id: str = "",
    conn: Optional[sqlite3.Connection] = None,
) -> MarketEvolveProgram:
    db = conn or get_desk_store().conn
    _ensure_market_evolve_tables(db)
    program = MarketEvolveProgram(
        program_id=_program_id(
            PROGRAM_KIND,
            "rosewood_research_policy_v1",
            0,
            DEFAULT_MARKET_EVOLVE_GENOME,
            [],
        ),
        program_kind=PROGRAM_KIND,
        name="rosewood_research_policy_v1",
        generation=0,
        parent_program_ids=[],
        genome=copy.deepcopy(DEFAULT_MARKET_EVOLVE_GENOME),
        objective=copy.deepcopy(DEFAULT_OBJECTIVE),
        status="active",
        created_from_cycle_id=cycle_id,
        quality_flags=["seed_program", "requires_cycle_evidence"],
    )
    persist_market_evolve_program(program, conn=db)
    return program


def load_market_evolve_programs(
    *,
    status: str = "",
    program_kind: str = PROGRAM_KIND,
    limit: int = 16,
    conn: Optional[sqlite3.Connection] = None,
) -> list[MarketEvolveProgram]:
    db = conn or get_desk_store().conn
    _ensure_market_evolve_tables(db)
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if program_kind:
        clauses.append("program_kind = ?")
        params.append(program_kind)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(int(limit))
    rows = db.execute(
        f"""
        SELECT * FROM market_evolve_programs
        {where}
        ORDER BY status = 'active' DESC, score DESC, generation DESC, created_at DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [_program_from_row(row) for row in rows]


def load_active_market_evolve_program(
    *,
    cycle_id: str = "",
    conn: Optional[sqlite3.Connection] = None,
) -> MarketEvolveProgram:
    db = conn or get_desk_store().conn
    programs = load_market_evolve_programs(status="active", limit=1, conn=db)
    if programs:
        return programs[0]
    return seed_default_market_evolve_program(cycle_id=cycle_id, conn=db)


def apply_market_evolve_policy_to_seeds(
    seeds: list[Any],
    *,
    cycle_id: str,
    conn: Optional[sqlite3.Connection] = None,
    persist: bool = True,
) -> MarketEvolveProgram:
    """Stamp active research-policy decisions onto scout seeds.

    The seed payload becomes the auditable contract consumed by Tier 1:
    prompt variant, tool budget, source-family targets, and active program
    lineage. This is the point where evolution starts changing behavior.
    """
    db = conn or get_desk_store().conn
    active_program = load_active_market_evolve_program(cycle_id=cycle_id, conn=db)
    experiments = _load_assignable_experiments(conn=db)
    programs_by_id = {active_program.program_id: active_program}
    for exp in experiments:
        for pid in (exp.get("parent_program_id"), exp.get("candidate_program_id")):
            if pid and pid not in programs_by_id:
                program = _load_program_by_id(str(pid), conn=db)
                if program is not None:
                    programs_by_id[program.program_id] = program
    balanced_assignments = _balanced_experiment_assignment_map(seeds, experiments)
    for seed in seeds:
        assigned_program = active_program
        experiment_id = ""
        experiment_arm = ""
        payload = _seed_payload(seed)
        forced_experiment_id = str(payload.get("market_evolve_forced_experiment_id") or "").strip()
        forced_arm = str(payload.get("market_evolve_forced_experiment_arm") or "").strip()
        balanced_assignment = balanced_assignments.get(_seed_attr(seed, "seed_id"))
        if forced_experiment_id:
            experiment = _experiment_by_id(forced_experiment_id, experiments)
        elif balanced_assignment is not None:
            experiment = balanced_assignment[0]
            forced_arm = balanced_assignment[1]
        else:
            experiment = _experiment_for_seed(seed, experiments)
        if experiment:
            experiment_id = str(experiment.get("id") or "")
            arm = forced_arm if forced_arm in {"control", "candidate"} else _experiment_arm_for_seed(seed, experiment_id)
            parent = programs_by_id.get(str(experiment.get("parent_program_id") or ""))
            candidate = programs_by_id.get(str(experiment.get("candidate_program_id") or ""))
            if arm == "candidate" and candidate is not None:
                assigned_program = candidate
                experiment_arm = "candidate"
            elif parent is not None:
                assigned_program = parent
                experiment_arm = "control"
            else:
                experiment_id = ""
                experiment_arm = ""
        tool_policy = dict((assigned_program.genome or {}).get("tool_request_policy") or {})
        prompt_policy = dict((assigned_program.genome or {}).get("prompt_policy") or {})
        routing_policy = dict((assigned_program.genome or {}).get("routing_thresholds") or {})
        tool_limit = _int_between(
            tool_policy.get("max_tool_candidates_per_seed"),
            default=10,
            lo=4,
            hi=24,
        )
        evidence_limit = _int_between(
            tool_policy.get("max_evidence_tools_per_seed"),
            default=2,
            lo=1,
            hi=8,
        )
        source_targets = [
            str(x)
            for x in (tool_policy.get("source_family_targets") or [])
            if str(x).strip()
        ]
        seed_lineage = _market_map_seed_lineage(payload)
        prompt_variant = choose_market_evolve_prompt_variant(seed, program=assigned_program)
        existing_tools = [
            str(x).strip()
            for x in (payload.get("tool_candidates") or [])
            if str(x).strip()
        ]
        payload.update({
            "market_evolve_program_id": assigned_program.program_id,
            "market_evolve_program_name": assigned_program.name,
            "market_evolve_generation": assigned_program.generation,
            "market_evolve_parent_program_ids": assigned_program.parent_program_ids,
            "market_evolve_program_status": assigned_program.status,
            "market_evolve_experiment_id": experiment_id,
            "market_evolve_experiment_arm": experiment_arm,
            "prompt_variant": prompt_variant,
            "tool_candidate_limit": tool_limit,
            "max_evidence_tools": evidence_limit,
            "source_family_targets": source_targets,
            "prefer_learned_tools": bool(tool_policy.get("prefer_learned_tools")),
            "learned_tool_priority_boost": _float(tool_policy.get("learned_tool_priority_boost"), 0.0),
            "max_tool_promotions_per_cycle": _int_between(
                tool_policy.get("max_tool_promotions_per_cycle"),
                default=3,
                lo=0,
                hi=12,
            ),
            "auto_promote_high_priority_tools": bool(tool_policy.get("auto_promote_high_priority_tools")),
            "prefer_price_anchor_tools": bool(tool_policy.get("prefer_price_anchor_tools")),
            "require_disconfirming_price_check": bool(tool_policy.get("require_disconfirming_price_check")),
            "prefer_information_perfusion_tools": bool(tool_policy.get("prefer_information_perfusion_tools")),
            "price_feedback_exploitation_budget_share": _float(
                routing_policy.get("price_feedback_exploitation_budget_share"),
                0.0,
            ),
            "perfusion_followup_budget_share": _float(
                routing_policy.get("perfusion_followup_budget_share"),
                0.0,
            ),
            "prompt_emphasize_price_feedback_refs": bool(prompt_policy.get("emphasize_price_feedback_refs")),
            "prompt_emphasize_perfusion_state": bool(prompt_policy.get("emphasize_perfusion_state")),
            "prompt_contract_pressure": prompt_policy.get("contract_pressure", "normal"),
            "prompt_min_information_strings": _int_between(
                prompt_policy.get("min_information_strings"),
                default=1,
                lo=0,
                hi=3,
            ),
            "prompt_require_mechanism": bool(prompt_policy.get("require_mechanism", True)),
            "prompt_require_kill_signal": bool(prompt_policy.get("require_kill_signal", True)),
            "prompt_require_evidence_refs": bool(prompt_policy.get("require_evidence_refs", True)),
            "market_evolve_applied": True,
        })
        if existing_tools:
            payload["tool_candidates"] = existing_tools[:tool_limit]
        if persist:
            _persist_policy_application(
                program=assigned_program,
                seed=seed,
                cycle_id=cycle_id,
                prompt_variant=prompt_variant,
                tool_candidate_limit=tool_limit,
                evidence_tool_limit=evidence_limit,
                experiment_id=experiment_id,
                experiment_arm=experiment_arm,
                applied={
                    "source_family_targets": source_targets,
                    "prompt_policy": prompt_policy,
                    "tool_request_policy": tool_policy,
                    "active_program_id": active_program.program_id,
                    **seed_lineage,
                },
                conn=db,
            )
    return active_program


def prepare_market_evolve_experiment_seed_pairs(
    seeds: list[Any],
    *,
    cycle_id: str,
    conn: Optional[sqlite3.Connection] = None,
    max_pairs: Optional[int] = None,
) -> int:
    """Replace selected seed cells with control/candidate pairs.

    Hash-splitting is useful for broad online learning, but hard policy
    experiments need some identical market slices where both arms see the
    same entity/horizon/lens/bias/theme. This mutates `seeds` in place and
    returns the number of paired slices created.
    """
    if not seeds:
        return 0
    db = conn or get_desk_store().conn
    experiments = _load_assignable_experiments(conn=db)
    if not experiments:
        return 0
    pair_budget = _experiment_pair_budget(len(seeds), experiments, max_pairs=max_pairs)
    if pair_budget <= 0:
        return 0
    selected_experiments = _selected_experiments_for_pair_budget(
        experiments,
        pair_budget=pair_budget,
    )
    if not selected_experiments:
        return 0

    paired = 0
    seen_units: set[tuple[str, str]] = set()
    new_seeds: list[Any] = []
    for seed in list(seeds):
        experiment = selected_experiments[paired % len(selected_experiments)]
        experiment_id = str((experiment or {}).get("id") or "")
        unit_key = _experiment_slice_key(seed)
        if experiment and paired < pair_budget and (experiment_id, unit_key) not in seen_units:
            seen_units.add((experiment_id, unit_key))
            pair_id = f"mexp_pair_{_hash_int(experiment_id + '|' + unit_key):012x}"
            new_seeds.append(_clone_seed_for_experiment_arm(seed, experiment_id, "control", pair_id))
            new_seeds.append(_clone_seed_for_experiment_arm(seed, experiment_id, "candidate", pair_id))
            paired += 1
            continue
        new_seeds.append(seed)
    seeds[:] = new_seeds
    return paired


def choose_market_evolve_prompt_variant(
    seed: Any,
    *,
    program: Optional[MarketEvolveProgram] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    if program is None:
        program = load_active_market_evolve_program(conn=conn)
    prompt_policy = dict((program.genome or {}).get("prompt_policy") or {})
    variant_map = dict(
        DEFAULT_MARKET_EVOLVE_GENOME["prompt_policy"]["variant_map"]
    )
    variant_map.update({
        str(k): str(v)
        for k, v in (prompt_policy.get("variant_map") or {}).items()
        if str(v).strip()
    })
    default_variant = str(
        prompt_policy.get("default_variant")
        or variant_map.get("default")
        or "depth_ladder_v1"
    )
    lens = _seed_attr(seed, "lens").lower()
    theme = _seed_attr(seed, "theme").lower()
    horizon = _seed_attr(seed, "horizon").lower()
    bias = _seed_attr(seed, "bias_mode").lower()
    if horizon in {"tick", "second", "minute", "hour", "intraday"}:
        return variant_map.get("temporal_context") or default_variant
    if lens in {"filing", "headline", "headlines", "catalyst", "material_info"}:
        return variant_map.get("primary_source") or default_variant
    if lens in {"rotation", "on_chain", "money_velocity", "smart_money", "macro", "factor"}:
        return variant_map.get("seam_lenses") or default_variant
    if any(tok in theme for tok in ("flow", "rotation", "liquidity", "velocity", "vehicle")):
        return variant_map.get("seam_lenses") or default_variant
    if bias in {"frontier", "contrarian"}:
        return variant_map.get("fast_alpha") or default_variant
    return variant_map.get("default") or default_variant


def load_market_evolve_policy_applications(
    *,
    cycle_id: str = "",
    program_id: str = "",
    limit: int = 100,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    db = conn or get_desk_store().conn
    _ensure_market_evolve_tables(db)
    clauses: list[str] = []
    params: list[Any] = []
    if cycle_id:
        clauses.append("cycle_id = ?")
        params.append(cycle_id)
    if program_id:
        clauses.append("program_id = ?")
        params.append(program_id)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(int(limit))
    rows = db.execute(
        f"""
        SELECT *
        FROM market_evolve_policy_applications
        {where}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["applied"] = _json_load(d.get("applied_json"), {})
        d.pop("applied_json", None)
        out.append(d)
    return out


def load_market_evolve_experiments(
    *,
    cycle_id: str = "",
    status: str = "",
    limit: int = 100,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    db = conn or get_desk_store().conn
    _ensure_market_evolve_tables(db)
    clauses: list[str] = []
    params: list[Any] = []
    if cycle_id:
        clauses.append("cycle_id = ?")
        params.append(cycle_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(int(limit))
    rows = db.execute(
        f"""
        SELECT *
        FROM market_evolve_experiments
        {where}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        for src, dst, default in (
            ("matched_slice_json", "matched_slice", {}),
            ("arms_json", "arms", []),
            ("success_criteria_json", "success_criteria", {}),
            ("quality_flags", "quality_flags", []),
        ):
            d[dst] = _json_load(d.get(src), default)
            if src != dst:
                d.pop(src, None)
        out.append(d)
    return out


def load_market_evolve_experiment_results(
    *,
    cycle_id: str = "",
    experiment_id: str = "",
    limit: int = 100,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    db = conn or get_desk_store().conn
    _ensure_market_evolve_tables(db)
    clauses: list[str] = []
    params: list[Any] = []
    if cycle_id:
        clauses.append("cycle_id = ?")
        params.append(cycle_id)
    if experiment_id:
        clauses.append("experiment_id = ?")
        params.append(experiment_id)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(int(limit))
    rows = db.execute(
        f"""
        SELECT *
        FROM market_evolve_experiment_results
        {where}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        for src, dst, default in (
            ("control_metrics_json", "control_metrics", {}),
            ("candidate_metrics_json", "candidate_metrics", {}),
            ("falsification_gate_results_json", "falsification_gate_results", []),
            ("quality_flags", "quality_flags", []),
        ):
            d[dst] = _json_load(d.get(src), default)
            if src != dst:
                d.pop(src, None)
        out.append(d)
    return out


def build_market_evolve_lineage(
    *,
    limit: int = 64,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    """Return an inspectable evolution graph plus the current program frontier."""
    db = conn or get_desk_store().conn
    _ensure_market_evolve_tables(db)
    programs = load_market_evolve_programs(status="", limit=limit, conn=db)
    mutations = _load_market_evolve_mutations(limit=limit * 2, conn=db)
    experiments = load_market_evolve_experiments(status="", limit=limit * 2, conn=db)
    results = load_market_evolve_experiment_results(limit=limit * 4, conn=db)
    mutation_by_child = {
        str(m.get("child_program_id") or ""): m
        for m in mutations
        if str(m.get("child_program_id") or "")
    }
    experiments_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for exp in experiments:
        experiments_by_candidate.setdefault(str(exp.get("candidate_program_id") or ""), []).append(exp)
    results_by_experiment: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        results_by_experiment.setdefault(str(result.get("experiment_id") or ""), []).append(result)

    nodes: list[dict[str, Any]] = []
    for program in programs:
        mutation = mutation_by_child.get(program.program_id, {})
        proof = _mutation_proof_from_record(mutation)
        node_experiments = experiments_by_candidate.get(program.program_id, [])
        latest_result = _latest_result_for_experiments(node_experiments, results_by_experiment)
        changed_paths = _changed_path_names(proof)
        proof_summary = _lineage_proof_summary(proof)
        nodes.append({
            "program_id": program.program_id,
            "name": program.name,
            "status": program.status,
            "generation": program.generation,
            "parent_program_ids": program.parent_program_ids,
            "score": round(float(program.score or 0.0), 4),
            "created_from_cycle_id": program.created_from_cycle_id,
            "mutation_kind": mutation.get("mutation_kind"),
            "mutation_status": mutation.get("status"),
            "diversity_signature": _diversity_signature(
                mutation_kind=str(mutation.get("mutation_kind") or "seed"),
                changed_paths=changed_paths,
            ),
            "changed_paths": changed_paths[:12],
            "proof_schema": proof.get("schema_version"),
            "mutation_source": proof_summary.get("mutation_source"),
            "source_candidate_rank": proof_summary.get("source_candidate_rank"),
            "source_diagnostic_codes": proof_summary.get("source_diagnostic_codes"),
            "mutation_hypothesis": proof_summary.get("hypothesis"),
            "intended_effect": proof_summary.get("intended_effect"),
            "kill_signal": proof_summary.get("kill_signal"),
            "promotion_evidence_required": proof_summary.get("promotion_evidence_required"),
            "falsification_gate_count": proof_summary.get("falsification_gate_count"),
            "population_gate": proof_summary.get("population_gate"),
            "target_metrics": proof.get("target_metrics") or {},
            "latest_experiment_id": (node_experiments[0].get("id") if node_experiments else ""),
            "latest_experiment_status": (node_experiments[0].get("status") if node_experiments else ""),
            "latest_decision": latest_result.get("decision") if latest_result else "",
            "latest_score_delta": latest_result.get("score_delta") if latest_result else None,
            "proof_gate_summary": _proof_gate_summary(latest_result.get("falsification_gate_results") if latest_result else []),
            "quality_flags": program.quality_flags,
        })

    node_by_id = {str(node["program_id"]): node for node in nodes}
    edges: list[dict[str, Any]] = []
    for mutation in mutations:
        child_id = str(mutation.get("child_program_id") or "")
        parent_id = str(mutation.get("parent_program_id") or "")
        if child_id not in node_by_id and parent_id not in node_by_id:
            continue
        child_experiments = experiments_by_candidate.get(child_id, [])
        latest_result = _latest_result_for_experiments(child_experiments, results_by_experiment)
        proof = _mutation_proof_from_record(mutation)
        proof_summary = _lineage_proof_summary(proof)
        edges.append({
            "from_program_id": parent_id,
            "to_program_id": child_id,
            "mutation_id": mutation.get("id"),
            "mutation_kind": mutation.get("mutation_kind"),
            "mutation_status": mutation.get("status"),
            "mutation_source": proof_summary.get("mutation_source"),
            "source_candidate_rank": proof_summary.get("source_candidate_rank"),
            "kill_signal": proof_summary.get("kill_signal"),
            "promotion_evidence_required": proof_summary.get("promotion_evidence_required"),
            "falsification_gate_count": proof_summary.get("falsification_gate_count"),
            "population_gate": proof_summary.get("population_gate"),
            "experiment_id": child_experiments[0].get("id") if child_experiments else "",
            "experiment_status": child_experiments[0].get("status") if child_experiments else "",
            "decision": latest_result.get("decision") if latest_result else "",
            "score_delta": latest_result.get("score_delta") if latest_result else None,
            "proof_gate_summary": _proof_gate_summary(latest_result.get("falsification_gate_results") if latest_result else []),
        })

    frontier = _market_evolve_frontier(nodes)
    return {
        "schema_version": "market_evolve_lineage_v1",
        "generated_at": _now(),
        "program_count": len(nodes),
        "mutation_count": len(mutations),
        "experiment_count": len(experiments),
        "nodes": nodes,
        "edges": edges,
        "frontier": frontier,
        "active_program_ids": [
            str(node["program_id"]) for node in nodes
            if node.get("status") == "active"
        ],
        "candidate_program_ids": [
            str(node["program_id"]) for node in nodes
            if node.get("status") == "candidate"
        ],
        "quality_flags": ["market_evolve_lineage_graph"],
    }


def build_market_evolve_scoreboard(
    *,
    cycle_id: str = "",
    limit: int = 64,
    persist: bool = True,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    """Build and optionally persist the operator/cortex read model.

    Lineage is the graph. The scoreboard is the decision surface: what is
    learning, what is blocked, what can be promoted, and what the next cadence
    run should do before any wider spend.
    """
    db = conn or get_desk_store().conn
    _ensure_market_evolve_tables(db)
    generated_at = _now()
    lineage = build_market_evolve_lineage(limit=limit, conn=db)
    nodes = list(lineage.get("nodes") or [])
    edges = list(lineage.get("edges") or [])
    active = [n for n in nodes if n.get("status") == "active"]
    candidates = [n for n in nodes if n.get("status") == "candidate"]
    rejected = [n for n in nodes if n.get("status") == "rejected"]
    experiments = load_market_evolve_experiments(status="", limit=limit * 2, conn=db)
    recent_results = load_market_evolve_experiment_results(limit=limit * 4, conn=db)
    cycle_results = (
        load_market_evolve_experiment_results(cycle_id=cycle_id, limit=limit * 4, conn=db)
        if cycle_id else []
    )
    result_window = cycle_results or recent_results
    open_experiments = [
        e for e in experiments
        if str(e.get("status") or "") in {"planned", "running", "insufficient_sample"}
    ]
    applications = (
        load_market_evolve_policy_applications(cycle_id=cycle_id, limit=limit * 4, conn=db)
        if cycle_id else []
    )
    hard_gate_summary = _scoreboard_hard_gate_summary(result_window)
    decision_counts = _count_values(str(r.get("decision") or "unknown") for r in result_window)
    experiment_status_counts = _count_values(str(e.get("status") or "unknown") for e in experiments)
    program_status_counts = _count_values(str(n.get("status") or "unknown") for n in nodes)
    promotion_candidates = _scoreboard_rows([
        n for n in nodes
        if str(n.get("latest_decision") or "") == "promote_candidate"
        or "promoted_by_market_evolve_experiment" in _string_list(n.get("quality_flags"))
    ])
    continuation_candidates = _scoreboard_rows([
        n for n in nodes
        if str(n.get("latest_decision") or "") == "continue_candidate"
    ])
    blocked_candidates = _scoreboard_rows([
        n for n in [*candidates, *rejected]
        if str(n.get("latest_decision") or "") == "reject_candidate"
        or int((n.get("proof_gate_summary") or {}).get("triggered") or 0) > 0
        or str(n.get("mutation_status") or "") == "rejected"
        or n.get("status") == "rejected"
    ])
    frontier = _scoreboard_rows(list(lineage.get("frontier") or []), include_frontier=True)
    status = _scoreboard_status(
        active=active,
        candidates=candidates,
        open_experiments=open_experiments,
        promotion_candidates=promotion_candidates,
        continuation_candidates=continuation_candidates,
        blocked_candidates=blocked_candidates,
        result_window=result_window,
    )
    summary = _scoreboard_summary(
        status=status,
        active_count=len(active),
        candidate_count=len(candidates),
        open_experiment_count=len(open_experiments),
        promotion_count=len(promotion_candidates),
        blocked_count=len(blocked_candidates),
    )
    scoreboard = {
        "schema_version": "market_evolve_scoreboard_v1",
        "id": _market_evolve_scoreboard_id(cycle_id=cycle_id, generated_at=generated_at),
        "cycle_id": cycle_id,
        "generated_at": generated_at,
        "status": status,
        "summary": summary,
        "counts": {
            "programs": len(nodes),
            "active_programs": len(active),
            "candidate_programs": len(candidates),
            "rejected_programs": len(rejected),
            "mutations": int(lineage.get("mutation_count") or len(edges)),
            "experiments": len(experiments),
            "open_experiments": len(open_experiments),
            "cycle_policy_applications": len(applications),
            "result_window": len(result_window),
        },
        "program_status_counts": program_status_counts,
        "experiment_status_counts": experiment_status_counts,
        "decision_counts": decision_counts,
        "active_programs": _scoreboard_rows(active),
        "candidate_programs": _scoreboard_rows(candidates),
        "promotion_candidates": promotion_candidates,
        "continuation_candidates": continuation_candidates,
        "blocked_candidates": blocked_candidates,
        "hard_experiment_gate_summary": hard_gate_summary,
        "evolution_memory": _scoreboard_evolution_memory(
            result_window=result_window,
            recent_results=recent_results,
            open_experiment_count=len(open_experiments),
            mutation_count=int(lineage.get("mutation_count") or len(edges)),
        ),
        "frontier": frontier,
        "next_actions": _scoreboard_next_actions(
            status=status,
            frontier=frontier,
            open_experiments=open_experiments,
            applications=applications,
            cycle_id=cycle_id,
            result_window=result_window,
        ),
        "cadence_readiness": _scoreboard_cadence_readiness(
            status=status,
            hard_gate_summary=hard_gate_summary,
            recent_results=recent_results,
        ),
        "lineage_ref": {
            "schema_version": lineage.get("schema_version"),
            "program_count": lineage.get("program_count"),
            "mutation_count": lineage.get("mutation_count"),
            "experiment_count": lineage.get("experiment_count"),
            "active_program_ids": lineage.get("active_program_ids") or [],
            "candidate_program_ids": lineage.get("candidate_program_ids") or [],
        },
        "quality_flags": sorted(set([
            "market_evolve_scoreboard",
            "durable_evolution_memory",
            *([] if not cycle_id or cycle_results else ["cycle_window_empty"]),
        ])),
    }
    if persist:
        persist_market_evolve_scoreboard(scoreboard, conn=db)
    return scoreboard


def persist_market_evolve_scoreboard(
    scoreboard: dict[str, Any],
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    db = conn or get_desk_store().conn
    _ensure_market_evolve_tables(db)
    now = _now()
    row_id = str(scoreboard.get("id") or _market_evolve_scoreboard_id(
        cycle_id=str(scoreboard.get("cycle_id") or ""),
        generated_at=now,
    ))
    db.execute(
        """
        INSERT OR REPLACE INTO market_evolve_scoreboards (
            id, cycle_id, status, summary, scoreboard_json,
            created_at, valid_from, transaction_from, transaction_to
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            row_id,
            str(scoreboard.get("cycle_id") or ""),
            str(scoreboard.get("status") or "unknown"),
            str(scoreboard.get("summary") or "")[:3000],
            json.dumps(scoreboard, sort_keys=True),
            now,
            now,
            now,
        ),
    )
    db.commit()
    return row_id


def load_market_evolve_scoreboards(
    *,
    cycle_id: str = "",
    limit: int = 20,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    db = conn or get_desk_store().conn
    _ensure_market_evolve_tables(db)
    clauses: list[str] = ["transaction_to IS NULL"]
    params: list[Any] = []
    if cycle_id:
        clauses.append("cycle_id = ?")
        params.append(cycle_id)
    params.append(int(limit))
    rows = db.execute(
        f"""
        SELECT *
        FROM market_evolve_scoreboards
        WHERE {' AND '.join(clauses)}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = _json_load(row["scoreboard_json"], {})
        if isinstance(payload, dict):
            out.append(payload)
    return out


def evaluate_market_evolve_program(
    *,
    program: MarketEvolveProgram,
    cycle_id: str,
    conn: Optional[sqlite3.Connection] = None,
) -> MarketEvolveEvaluation:
    db = conn or get_desk_store().conn
    _ensure_market_evolve_tables(db)
    metrics = collect_market_evolve_metrics(
        cycle_id=cycle_id,
        geometry_weights=(program.genome or {}).get("geometry_weights"),
        routing_thresholds=(program.genome or {}).get("routing_thresholds"),
        conn=db,
    )
    score = score_market_evolve_metrics(metrics, program=program)
    passed, flags = _hard_gate_result(metrics, program)
    rationale = _evaluation_rationale(score, metrics, flags)
    evaluation = MarketEvolveEvaluation(
        evaluation_id=f"meval_{uuid4().hex[:16]}",
        program_id=program.program_id,
        cycle_id=cycle_id,
        evaluator_version=EVALUATOR_VERSION,
        score=score,
        metrics=metrics,
        baseline_metrics=_baseline_metrics_for_program(program, conn=db),
        passed=passed,
        rationale=rationale,
        quality_flags=flags,
    )
    persist_market_evolve_evaluation(evaluation, conn=db)
    program.score = score
    program.metrics = metrics
    structural_flags = [
        flag for flag in program.quality_flags
        if not str(flag).startswith("last_eval:")
    ]
    program.quality_flags = sorted(set([
        *structural_flags,
        *[f"last_eval:{flag}" for flag in flags],
    ]))
    persist_market_evolve_program(program, conn=db)
    return evaluation


def collect_market_evolve_metrics(
    *,
    cycle_id: str,
    seed_ids: Optional[list[str]] = None,
    program_id: str = "",
    experiment_id: str = "",
    experiment_arm: str = "",
    geometry_weights: Optional[dict[str, Any]] = None,
    routing_thresholds: Optional[dict[str, Any]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, float]:
    db = conn or get_desk_store().conn
    _ensure_market_evolve_tables(db)
    scoped_seed_ids = list(seed_ids or [])
    if not scoped_seed_ids and (program_id or experiment_id or experiment_arm):
        scoped_seed_ids = _policy_application_seed_ids(
            cycle_id=cycle_id,
            program_id=program_id,
            experiment_id=experiment_id,
            experiment_arm=experiment_arm,
            conn=db,
        )
    scout_count = (
        len(scoped_seed_ids)
        if scoped_seed_ids
        else _count_table(db, "hypotheses", "cycle_id = ?", (cycle_id,))
    )
    string_rows = _information_string_rows(db, cycle_id, seed_ids=scoped_seed_ids or None)
    string_count = len(string_rows)
    valid_strings = _valid_string_rows(string_rows)
    geometry_rows = [] if scoped_seed_ids else load_alpha_geometry(cycle_id=cycle_id, conn=db, limit=512)
    if not geometry_rows and string_rows:
        if scoped_seed_ids:
            geometry_metrics = _scoped_geometry_metrics_from_strings(
                string_rows,
                geometry_weights=geometry_weights,
                routing_thresholds=routing_thresholds,
            )
            geometry_rows = []
        else:
            try:
                compute_alpha_geometry(
                    cycle_id=cycle_id,
                    geometry_weights=geometry_weights,
                    routing_thresholds=routing_thresholds,
                    conn=db,
                    persist=True,
                )
                geometry_rows = load_alpha_geometry(cycle_id=cycle_id, conn=db, limit=512)
            except Exception:
                geometry_rows = []
            geometry_metrics = [dict(row.get("metrics") or {}) for row in geometry_rows]
    else:
        geometry_metrics = [dict(row.get("metrics") or {}) for row in geometry_rows]

    source_independence_values = [
        _float(m.get("source_independence"), 0.0) for m in geometry_metrics
    ]
    verifier_readiness_values = [
        _float(m.get("verifier_readiness"), 0.0) for m in geometry_metrics
    ]
    fragility_values = [_float(m.get("fragility"), 0.0) for m in geometry_metrics]
    trade_scream_values = (
        [
            _float(row.get("trade_scream_score"), _float((row.get("metrics") or {}).get("trade_scream_score"), 0.0))
            for row in geometry_rows
        ]
        if geometry_rows
        else [_float(m.get("trade_scream_score"), 0.0) for m in geometry_metrics]
    )
    frontier_cells = [
        row for row in geometry_rows
        if "frontier_trade_candidate" in _string_list(row.get("quality_flags"))
        or str(row.get("route_directive") or "") == "verify_now"
    ]
    route_directives = [
        str(row.get("route_directive") or "observe")
        for row in geometry_rows
    ]
    routed_cells = [d for d in route_directives if d != "observe"]
    route_denominator = max(1.0, float(len(route_directives)))
    raw_thresholds = dict(
        routing_thresholds
        or DEFAULT_MARKET_EVOLVE_GENOME.get("routing_thresholds")
        or {}
    )
    normalized_thresholds = normalize_routing_thresholds(raw_thresholds)
    max_verify_fragility = _clamp01(
        _float(
            raw_thresholds.get("verify_allow_fragility_max", raw_thresholds.get("max_fragility")),
            1.0,
        )
    )
    min_verify_source_independence = _clamp01(
        _float(
            raw_thresholds.get("verify_source_independence_min", raw_thresholds.get("min_source_independence")),
            0.0,
        )
    )
    fragile_verify_cells = [
        row for row in geometry_rows
        if str(row.get("route_directive") or "") == "verify_now"
        and (
            _float((row.get("metrics") or {}).get("fragility"), 0.0) > max_verify_fragility
            or _float((row.get("metrics") or {}).get("source_independence"), 0.0) < min_verify_source_independence
        )
    ]
    high_signal_observe_cells = [
        row for row in geometry_rows
        if str(row.get("route_directive") or "observe") == "observe"
        and _float((row.get("metrics") or {}).get("fragility"), 0.0) <= max_verify_fragility
        and _float((row.get("metrics") or {}).get("verifier_readiness"), 0.0) >= normalized_thresholds["verify_readiness_min"]
        and (
            _float(row.get("trade_scream_score"), _float((row.get("metrics") or {}).get("trade_scream_score"), 0.0))
            >= normalized_thresholds["verify_trade_scream_min"] * 0.85
            or _float((row.get("metrics") or {}).get("frontier_pressure"), 0.0)
            >= normalized_thresholds["widen_sources_frontier_min"]
        )
    ]
    if scoped_seed_ids and geometry_metrics:
        frontier_candidate_rate = _clamp01(
            _float(geometry_metrics[0].get("frontier_candidate_rate"), 0.0)
        )
        geometry_cell_count = float(max(1, int(geometry_metrics[0].get("geometry_cell_count") or 1)))
    else:
        frontier_candidate_rate = float(len(frontier_cells)) / max(1.0, float(len(geometry_rows)))
        geometry_cell_count = float(len(geometry_rows))

    votes = _vote_counts(db, cycle_id)
    total_votes = sum(votes.values())
    tool_status_counts = _tool_proposal_status_counts(db, cycle_id)
    proposed_tools = sum(tool_status_counts.values())
    active_tools = tool_status_counts.get("active", 0)
    eval_failed_tools = tool_status_counts.get("eval_failed", 0)
    runtime_adapter_backlog = tool_status_counts.get("needs_runtime_adapter", 0)
    learned_tool_stats = _learned_tool_call_stats(db, cycle_id)
    low_ev_tools = _low_ev_tool_count(db, cycle_id)
    report_count = _count_table(db, "research_reports", "cycle_id = ?", (cycle_id,))
    promoted_candidates = _count_table(db, "promoted_candidates", "cycle_id = ?", (cycle_id,))
    queued_candidates = _count_table(
        db,
        "promoted_candidates",
        "cycle_id = ? AND status = ?",
        (cycle_id, "queued_verifier"),
    )
    prompt_stats = _scout_prompt_stats(db, cycle_id, seed_ids=scoped_seed_ids or None)
    governor_stats = _governor_routing_stats(
        db,
        cycle_id=cycle_id,
        seed_ids=scoped_seed_ids or None,
        geometry_weights=geometry_weights,
        routing_thresholds=routing_thresholds,
    )
    cortex_task_stats = _cortex_task_execution_stats(
        db,
        cycle_id,
        program_id=program_id,
        experiment_id=experiment_id,
        experiment_arm=experiment_arm,
        seed_scoped=bool(scoped_seed_ids),
    )
    outcome_stats = _information_price_outcome_stats(
        db,
        cycle_id=cycle_id,
        seed_ids=scoped_seed_ids or None,
    )
    perfusion_stats = _information_perfusion_stats(
        db,
        cycle_id=cycle_id,
        seed_ids=scoped_seed_ids or None,
        string_rows=string_rows,
    )

    denominator_scouts = max(1.0, float(scout_count or string_count or 1))
    metrics = {
        "scout_count": float(scout_count),
        "string_count": float(string_count),
        "string_yield_per_scout": _clamp01(float(string_count) / denominator_scouts),
        "valid_string_rate": float(len(valid_strings)) / max(1.0, float(string_count)),
        "prompt_eval_count": float(prompt_stats["n"]),
        "avg_prompt_quality": prompt_stats["avg_score"],
        "prompt_pass_rate": prompt_stats["pass_rate"],
        "prompt_contract_failure_rate": prompt_stats["contract_failure_rate"],
        "prompt_invented_tool_rate": prompt_stats["invented_tool_rate"],
        "prompt_missing_information_strings_rate": prompt_stats["missing_information_strings_rate"],
        "prompt_scale_gate_passed": 1.0 if prompt_stats["scale_gate_passed"] else 0.0,
        "prompt_scale_gate_median_score": prompt_stats["scale_gate_median_score"],
        "route_contract_eval_count": float(prompt_stats["route_contract_eval_count"]),
        "route_contract_success_rate": prompt_stats["route_contract_success_rate"],
        "route_contract_failure_rate": prompt_stats["route_contract_failure_rate"],
        "geometry_cell_count": geometry_cell_count,
        "avg_source_independence": _avg(source_independence_values),
        "avg_verifier_readiness": _avg(verifier_readiness_values),
        "avg_fragility": _avg(fragility_values),
        "avg_trade_scream_score": _avg(trade_scream_values),
        "frontier_candidate_rate": frontier_candidate_rate,
        "geometry_route_action_rate": float(len(routed_cells)) / route_denominator,
        "geometry_observe_rate": route_directives.count("observe") / route_denominator,
        "geometry_verify_now_rate": route_directives.count("verify_now") / route_denominator,
        "geometry_route_entropy": _entropy_from_values(route_directives),
        "fragile_verify_rate": float(len(fragile_verify_cells)) / route_denominator,
        "high_signal_observe_rate": float(len(high_signal_observe_cells)) / route_denominator,
        "promoted_candidate_count": float(promoted_candidates),
        "queued_verifier_count": float(queued_candidates),
        "tool_proposal_count": float(proposed_tools),
        "active_tool_proposal_count": float(active_tools),
        "eval_failed_tool_proposal_count": float(eval_failed_tools),
        "runtime_adapter_backlog_count": float(runtime_adapter_backlog),
        "tool_activation_rate": float(active_tools) / max(1.0, float(proposed_tools)),
        "tool_eval_failed_rate": float(eval_failed_tools) / max(1.0, float(active_tools + eval_failed_tools)),
        "runtime_adapter_backlog_rate": float(runtime_adapter_backlog) / max(1.0, float(proposed_tools)),
        "low_ev_tool_rate": float(low_ev_tools) / max(1.0, float(proposed_tools)),
        "learned_tool_call_count": float(learned_tool_stats["calls"]),
        "learned_tool_success_count": float(learned_tool_stats["successes"]),
        "learned_tool_success_rate": float(learned_tool_stats["successes"]) / max(1.0, float(learned_tool_stats["calls"])),
        "learned_tool_usage_per_scout": _clamp01(float(learned_tool_stats["successes"]) / denominator_scouts),
        **governor_stats,
        **cortex_task_stats,
        **outcome_stats,
        **perfusion_stats,
        "verifier_pass_rate": float(votes.get("pass", 0)) / max(1.0, float(total_votes)),
        "verifier_fail_rate": float(votes.get("fail", 0)) / max(1.0, float(total_votes)),
        "verifier_abstain_rate": float(votes.get("abstain", 0) + votes.get("needs_review", 0)) / max(1.0, float(total_votes)),
        "report_count": float(report_count),
        "report_yield": _clamp01(float(report_count) / max(1.0, float(queued_candidates or promoted_candidates or 1))),
        "policy_cost_usd": round(sum(_float(row.get("cost_usd"), 0.0) for row in string_rows), 6),
    }
    if routing_thresholds is not None:
        metrics.update({
            f"routing_threshold_{key}": round(value, 4)
            for key, value in normalize_routing_thresholds(routing_thresholds).items()
        })
    base_score = score_market_evolve_metrics(metrics, program=MarketEvolveProgram(
        program_id="metrics_probe",
        program_kind=PROGRAM_KIND,
        name="metrics_probe",
        genome=DEFAULT_MARKET_EVOLVE_GENOME,
        objective=DEFAULT_OBJECTIVE,
    ))
    metrics["accepted_unique_high_quality_coverage_per_dollar"] = _quality_per_dollar(
        base_score,
        metrics.get("policy_cost_usd", 0.0),
        string_count,
    )
    return {k: round(float(v), 4) for k, v in metrics.items()}


def score_market_evolve_metrics(
    metrics: dict[str, float],
    *,
    program: MarketEvolveProgram,
) -> float:
    weights = dict(
        ((program.genome or {}).get("objective_weights") or DEFAULT_MARKET_EVOLVE_GENOME["objective_weights"])
    )
    positive = (
        _w(weights, "string_yield_per_scout") * _clamp01(metrics.get("string_yield_per_scout", 0.0))
        + _w(weights, "valid_string_rate") * _clamp01(metrics.get("valid_string_rate", 0.0))
        + _w(weights, "prompt_quality") * _clamp01(metrics.get("avg_prompt_quality", 0.0))
        + _w(weights, "prompt_pass_rate") * _clamp01(metrics.get("prompt_pass_rate", 0.0))
        + _w(weights, "source_independence") * _clamp01(metrics.get("avg_source_independence", 0.0))
        + _w(weights, "verifier_readiness") * _clamp01(metrics.get("avg_verifier_readiness", 0.0))
        + _w(weights, "frontier_candidate_rate") * _clamp01(metrics.get("frontier_candidate_rate", 0.0))
        + _w(weights, "verifier_pass_rate") * _clamp01(metrics.get("verifier_pass_rate", 0.0))
        + _w(weights, "report_yield") * _clamp01(metrics.get("report_yield", 0.0))
        + _w(weights, "tool_activation_rate") * _clamp01(metrics.get("tool_activation_rate", 0.0))
        + _w(weights, "learned_tool_usage_per_scout") * _clamp01(metrics.get("learned_tool_usage_per_scout", 0.0))
        + _w(weights, "learned_tool_success_rate") * _clamp01(metrics.get("learned_tool_success_rate", 0.0))
        + _w(weights, "governor_string_yield_per_seed") * _clamp01(metrics.get("governor_string_yield_per_seed", 0.0))
        + _w(weights, "governor_valid_string_rate") * _clamp01(metrics.get("governor_valid_string_rate", 0.0))
        + _w(weights, "governor_gap_repair_rate") * _clamp01(metrics.get("governor_gap_repair_rate", 0.0))
        + _w(weights, "governor_source_independence") * _clamp01(metrics.get("governor_avg_source_independence", 0.0))
        + _w(weights, "route_contract_success_rate") * _clamp01(metrics.get("route_contract_success_rate", 0.0))
        + _w(weights, "geometry_route_action_rate") * _clamp01(metrics.get("geometry_route_action_rate", 0.0))
        + _w(weights, "cortex_task_completion_rate") * _clamp01(metrics.get("cortex_task_completion_rate", 0.0))
        + _w(weights, "cortex_shape_observation_rate") * _clamp01(metrics.get("cortex_shape_observation_rate", 0.0))
        + _w(weights, "cortex_followup_execution_rate") * _clamp01(metrics.get("cortex_followup_execution_rate", 0.0))
        + _w(weights, "outcome_observed_rate") * _clamp01(metrics.get("outcome_observed_rate", 0.0))
        + _w(weights, "outcome_direction_hit_rate") * _clamp01(metrics.get("outcome_direction_hit_rate", 0.0))
        + _w(weights, "outcome_threshold_hit_rate") * _clamp01(metrics.get("outcome_threshold_hit_rate", 0.0))
        + _w(weights, "avg_realized_edge_score") * _clamp01(metrics.get("avg_realized_edge_score", 0.0))
        + _w(weights, "perfusion_routed_cell_rate") * _clamp01(metrics.get("perfusion_routed_cell_rate", 0.0))
        + _w(weights, "perfusion_avg_pressure_gradient") * _clamp01(metrics.get("perfusion_avg_pressure_gradient", 0.0))
        + _w(weights, "perfusion_avg_source_oxygenation") * _clamp01(metrics.get("perfusion_avg_source_oxygenation", 0.0))
        + _w(weights, "perfusion_max_dilation_score") * _clamp01(metrics.get("perfusion_max_dilation_score", 0.0))
    )
    penalty = (
        _w(weights, "fragility_penalty") * _clamp01(metrics.get("avg_fragility", 0.0))
        + _w(weights, "fragile_verify_penalty") * _clamp01(metrics.get("fragile_verify_rate", 0.0))
        + _w(weights, "high_signal_observe_penalty") * _clamp01(metrics.get("high_signal_observe_rate", 0.0))
        + _w(weights, "prompt_contract_failure_penalty") * _clamp01(metrics.get("prompt_contract_failure_rate", 0.0))
        + _w(weights, "route_contract_failure_penalty") * _clamp01(metrics.get("route_contract_failure_rate", 0.0))
        + _w(weights, "low_ev_tool_penalty") * _clamp01(metrics.get("low_ev_tool_rate", 0.0))
        + _w(weights, "tool_eval_failed_penalty") * _clamp01(metrics.get("tool_eval_failed_rate", 0.0))
        + _w(weights, "runtime_adapter_backlog_penalty") * _clamp01(metrics.get("runtime_adapter_backlog_rate", 0.0))
        + _w(weights, "verifier_abstain_penalty") * _clamp01(metrics.get("verifier_abstain_rate", 0.0))
        + _w(weights, "governor_waste_penalty") * _clamp01(metrics.get("governor_waste_rate", 0.0))
        + _w(weights, "cortex_task_failure_penalty") * _clamp01(metrics.get("cortex_task_failure_rate", 0.0))
        + _w(weights, "cortex_task_pending_penalty") * _clamp01(metrics.get("cortex_task_pending_rate", 0.0))
        + _w(weights, "cortex_shape_blocked_followup_penalty") * _clamp01(metrics.get("cortex_shape_blocked_followup_rate", 0.0))
        + _w(weights, "outcome_unobserved_penalty") * (1.0 - _clamp01(metrics.get("outcome_observed_rate", 0.0)))
        + _w(weights, "perfusion_high_resistance_penalty") * _clamp01(metrics.get("perfusion_high_resistance_rate", 0.0))
        + _w(weights, "perfusion_price_sensor_gap_penalty") * _clamp01(metrics.get("perfusion_price_sensor_gap_rate", 0.0))
    )
    return round(_clamp01(positive - penalty), 4)


def evaluate_market_evolve_experiments(
    *,
    cycle_id: str,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    """Evaluate online candidate-vs-control policy experiments for a cycle."""
    db = conn or get_desk_store().conn
    _ensure_market_evolve_tables(db)
    experiment_ids = [
        str(row["experiment_id"])
        for row in db.execute(
            """
            SELECT DISTINCT experiment_id
            FROM market_evolve_policy_applications
            WHERE cycle_id = ?
              AND experiment_id IS NOT NULL
              AND experiment_id != ''
              AND transaction_to IS NULL
            """,
            (cycle_id,),
        ).fetchall()
        if str(row["experiment_id"] or "").strip()
    ]
    results: list[dict[str, Any]] = []
    for experiment_id in experiment_ids:
        experiment = _load_experiment_by_id(experiment_id, conn=db)
        if not experiment:
            continue
        existing = _load_existing_experiment_result(
            cycle_id=cycle_id,
            experiment_id=experiment_id,
            conn=db,
        )
        if existing is not None:
            existing["quality_flags"] = sorted(set([
                *_string_list(existing.get("quality_flags")),
                "existing_experiment_result_reused",
            ]))
            results.append(existing)
            continue
        if str(experiment.get("status") or "") not in {"planned", "running", "insufficient_sample"}:
            continue
        parent = _load_program_by_id(str(experiment.get("parent_program_id") or ""), conn=db)
        candidate = _load_program_by_id(str(experiment.get("candidate_program_id") or ""), conn=db)
        if parent is None or candidate is None:
            continue
        control_seeds = _policy_application_seed_ids(
            cycle_id=cycle_id,
            experiment_id=experiment_id,
            experiment_arm="control",
            conn=db,
        )
        candidate_seeds = _policy_application_seed_ids(
            cycle_id=cycle_id,
            experiment_id=experiment_id,
            experiment_arm="candidate",
            conn=db,
        )
        control_metrics = collect_market_evolve_metrics(
            cycle_id=cycle_id,
            seed_ids=control_seeds,
            program_id=parent.program_id,
            experiment_id=experiment_id,
            experiment_arm="control",
            geometry_weights=(parent.genome or {}).get("geometry_weights"),
            routing_thresholds=(parent.genome or {}).get("routing_thresholds"),
            conn=db,
        )
        candidate_metrics = collect_market_evolve_metrics(
            cycle_id=cycle_id,
            seed_ids=candidate_seeds,
            program_id=candidate.program_id,
            experiment_id=experiment_id,
            experiment_arm="candidate",
            geometry_weights=(candidate.genome or {}).get("geometry_weights"),
            routing_thresholds=(candidate.genome or {}).get("routing_thresholds"),
            conn=db,
        )
        control_score = score_market_evolve_metrics(control_metrics, program=parent)
        candidate_score = score_market_evolve_metrics(candidate_metrics, program=candidate)
        prior_results = _load_experiment_result_history(
            experiment_id=experiment_id,
            conn=db,
        )
        result = _experiment_decision(
            experiment=experiment,
            control_metrics=control_metrics,
            candidate_metrics=candidate_metrics,
            control_score=control_score,
            candidate_score=candidate_score,
            prior_results=prior_results,
        )
        result.update({
            "control_score": control_score,
            "candidate_score": candidate_score,
            "score_delta": round(candidate_score - control_score, 4),
        })
        result_id = _persist_experiment_result(
            experiment=experiment,
            cycle_id=cycle_id,
            control_metrics=control_metrics,
            candidate_metrics=candidate_metrics,
            control_score=control_score,
            candidate_score=candidate_score,
            decision=result["decision"],
            rationale=result["rationale"],
            quality_flags=result["quality_flags"],
            falsification_gate_results=result.get("falsification_gate_results") or [],
            conn=db,
        )
        result["id"] = result_id
        result["experiment_id"] = experiment_id
        results.append(result)
        if result["decision"] == "promote_candidate":
            _promote_candidate_program(parent=parent, candidate=candidate, result=result, conn=db)
            _set_mutation_status_for_child(candidate.program_id, "promoted", conn=db)
            _set_experiment_status(experiment_id, "promoted", conn=db)
        elif result["decision"] == "reject_candidate":
            _reject_candidate_program(candidate=candidate, result=result, conn=db)
            _set_mutation_status_for_child(candidate.program_id, "rejected", conn=db)
            _set_experiment_status(experiment_id, "rejected", conn=db)
        elif result["decision"] in {"insufficient_sample", "insufficient_proof"}:
            _set_mutation_status_for_child(candidate.program_id, "needs_more_data", conn=db)
            _set_experiment_status(experiment_id, "running", conn=db)
        elif result["decision"] == "continue_candidate":
            _set_mutation_status_for_child(candidate.program_id, "needs_more_data", conn=db)
            _set_experiment_status(experiment_id, "running", conn=db)
        else:
            _set_experiment_status(experiment_id, "running", conn=db)
    return results


def propose_market_evolve_mutation(
    *,
    program: MarketEvolveProgram,
    evaluation: MarketEvolveEvaluation,
    cycle_id: str,
    cortex_review: Optional[dict[str, Any]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[MarketEvolveProgram]:
    children = propose_market_evolve_mutations(
        program=program,
        evaluation=evaluation,
        cycle_id=cycle_id,
        cortex_review=cortex_review,
        max_children=1,
        conn=conn,
    )
    return children[0] if children else None


def propose_market_evolve_mutations(
    *,
    program: MarketEvolveProgram,
    evaluation: MarketEvolveEvaluation,
    cycle_id: str,
    cortex_review: Optional[dict[str, Any]] = None,
    max_children: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[MarketEvolveProgram]:
    db = conn or get_desk_store().conn
    _ensure_market_evolve_tables(db)
    if max_children is None:
        evolution_policy = dict(
            ((program.genome or {}).get("evolution_policy") or DEFAULT_MARKET_EVOLVE_GENOME["evolution_policy"])
        )
        max_children = int(evolution_policy.get("max_mutations_per_evaluation") or 2)
    max_children = max(1, min(8, int(max_children)))
    children: list[MarketEvolveProgram] = []
    for candidate in _mutation_candidates_with_cortex_review(
        metrics=evaluation.metrics,
        program=program,
        cortex_review=cortex_review,
    ):
        child = _persist_market_evolve_mutation_candidate(
            program=program,
            evaluation=evaluation,
            cycle_id=cycle_id,
            candidate=candidate,
            conn=db,
        )
        if child is None:
            continue
        children.append(child)
        if len(children) >= max_children:
            break
    return children


def _persist_market_evolve_mutation_candidate(
    *,
    program: MarketEvolveProgram,
    evaluation: MarketEvolveEvaluation,
    cycle_id: str,
    candidate: MarketEvolveMutationCandidate,
    conn: sqlite3.Connection,
) -> Optional[MarketEvolveProgram]:
    mutation_kind = candidate.mutation_kind
    rationale = candidate.rationale
    mutation_patch = candidate.mutation_patch
    mutation_source = candidate.mutation_source
    if not mutation_kind or not mutation_patch:
        return None
    child_genome = _deep_merge(copy.deepcopy(program.genome), mutation_patch)
    changed_path_rows = _mutation_changed_paths(
        parent_genome=program.genome,
        child_genome=child_genome,
        mutation_patch=mutation_patch,
    )
    changed_path_names = [
        str(row.get("path"))
        for row in changed_path_rows
        if isinstance(row, dict) and row.get("path")
    ]
    diversity_signature = _diversity_signature(
        mutation_kind=mutation_kind,
        changed_paths=changed_path_names,
    )
    population_gate = _open_experiment_population_gate(
        parent=program,
        mutation_kind=mutation_kind,
        diversity_signature=diversity_signature,
        conn=conn,
    )
    if not population_gate["allowed"]:
        return None
    child_program_id = _program_id(
        program.program_kind,
        f"{program.name}__{mutation_kind}",
        program.generation + 1,
        child_genome,
        [program.program_id],
    )
    existing_child = _load_program_by_id(child_program_id, conn=conn)
    if existing_child is not None and existing_child.status in {"rejected", "superseded"}:
        return None
    evolution_proof = _build_mutation_evolution_proof(
        parent=program,
        child_genome=child_genome,
        mutation_kind=mutation_kind,
        mutation_patch=mutation_patch,
        metrics=evaluation.metrics,
        rationale=rationale,
        mutation_source=mutation_source,
    )
    evolution_proof["population_gate"] = population_gate
    mutation_record = copy.deepcopy(mutation_patch)
    mutation_record["_evolution_proof"] = evolution_proof
    if mutation_source.get("source") == "alpha_geometry_cortex_review":
        mutation_record["_geometry_cortex_review"] = mutation_source
    child = MarketEvolveProgram(
        program_id=child_program_id,
        program_kind=program.program_kind,
        name=f"{program.name}__{mutation_kind}",
        generation=program.generation + 1,
        parent_program_ids=[program.program_id],
        genome=child_genome,
        objective=copy.deepcopy(program.objective),
        status="candidate",
        created_from_cycle_id=cycle_id,
        score=0.0,
        metrics={},
        quality_flags=["candidate_program", f"mutation:{mutation_kind}"],
    )
    persist_market_evolve_program(child, conn=conn)
    mutation = MarketEvolveMutation(
        mutation_id=f"mmut_{uuid4().hex[:16]}",
        parent_program_id=program.program_id,
        child_program_id=child.program_id,
        cycle_id=cycle_id,
        mutation_kind=mutation_kind,
        mutation=mutation_record,
        rationale=rationale,
        status="proposed",
    )
    persist_market_evolve_mutation(mutation, conn=conn)
    _persist_experiment_plan(
        parent=program,
        child=child,
        mutation=mutation,
        metrics=evaluation.metrics,
        conn=conn,
    )
    return child


def _has_open_experiment_for_parent(parent_program_id: str, *, conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM market_evolve_experiments
        WHERE parent_program_id = ?
          AND status IN ('planned', 'running', 'insufficient_sample')
          AND transaction_to IS NULL
        LIMIT 1
        """,
        (parent_program_id,),
    ).fetchone()
    return row is not None


def _open_experiment_population_gate(
    *,
    parent: MarketEvolveProgram,
    mutation_kind: str,
    diversity_signature: str,
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    policy = dict(
        ((parent.genome or {}).get("evolution_policy") or DEFAULT_MARKET_EVOLVE_GENOME["evolution_policy"])
    )
    max_open = max(1, min(16, int(policy.get("max_open_experiments_per_parent") or 1)))
    max_same = max(1, min(max_open, int(policy.get("max_open_same_signature_per_parent") or 1)))
    rows = _open_experiment_population(parent.program_id, conn=conn)
    same_signature = [
        row for row in rows
        if str(row.get("diversity_signature") or "") == diversity_signature
    ]
    same_kind = [
        row for row in rows
        if str(row.get("mutation_kind") or "") == mutation_kind
    ]
    allowed = len(rows) < max_open and len(same_signature) < max_same
    reason = "allowed"
    if len(rows) >= max_open:
        reason = "max_open_experiments_per_parent"
    elif len(same_signature) >= max_same:
        reason = "max_open_same_signature_per_parent"
    return {
        "schema_version": "market_evolve_population_gate_v1",
        "allowed": allowed,
        "reason": reason,
        "parent_program_id": parent.program_id,
        "candidate_mutation_kind": mutation_kind,
        "candidate_diversity_signature": diversity_signature,
        "open_experiment_count": len(rows),
        "open_same_signature_count": len(same_signature),
        "open_same_kind_count": len(same_kind),
        "max_open_experiments_per_parent": max_open,
        "max_open_same_signature_per_parent": max_same,
        "population_mode": str(policy.get("population_mode") or "bounded_parallel"),
    }


def _open_experiment_population(
    parent_program_id: str,
    *,
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            e.id AS experiment_id,
            e.candidate_program_id AS candidate_program_id,
            e.status AS experiment_status,
            m.mutation_kind AS mutation_kind,
            m.mutation_json AS mutation_json
        FROM market_evolve_experiments e
        LEFT JOIN market_evolve_mutations m
          ON m.parent_program_id = e.parent_program_id
         AND m.child_program_id = e.candidate_program_id
         AND m.transaction_to IS NULL
        WHERE e.parent_program_id = ?
          AND e.status IN ('planned', 'running', 'insufficient_sample')
          AND e.transaction_to IS NULL
        ORDER BY e.created_at ASC
        """,
        (parent_program_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        mutation = _json_load(row["mutation_json"], {}) if row["mutation_json"] else {}
        proof = mutation.get("_evolution_proof") if isinstance(mutation, dict) else {}
        proof = proof if isinstance(proof, dict) else {}
        changed_paths = _changed_path_names(proof)
        mutation_kind = str(row["mutation_kind"] or proof.get("mutation_kind") or "")
        signature = _diversity_signature(
            mutation_kind=mutation_kind,
            changed_paths=changed_paths,
        ) if mutation_kind else ""
        out.append({
            "experiment_id": str(row["experiment_id"] or ""),
            "candidate_program_id": str(row["candidate_program_id"] or ""),
            "status": str(row["experiment_status"] or ""),
            "mutation_kind": mutation_kind,
            "diversity_signature": signature,
            "changed_paths": changed_paths[:12],
        })
    return out


def persist_market_evolve_program(
    program: MarketEvolveProgram,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    db = conn or get_desk_store().conn
    _ensure_market_evolve_tables(db)
    now = _now()
    db.execute(
        """
        INSERT OR REPLACE INTO market_evolve_programs (
            id, program_kind, name, generation, parent_program_ids_json,
            genome_json, objective_json, status, created_from_cycle_id,
            score, metrics_json, quality_flags, created_at, valid_from,
            transaction_from, transaction_to
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            program.program_id,
            program.program_kind,
            program.name,
            int(program.generation),
            json.dumps(program.parent_program_ids, sort_keys=True),
            json.dumps(program.genome, sort_keys=True),
            json.dumps(program.objective, sort_keys=True),
            program.status,
            program.created_from_cycle_id,
            float(program.score or 0.0),
            json.dumps(program.metrics, sort_keys=True),
            json.dumps(program.quality_flags, sort_keys=True),
            now,
            now,
            now,
        ),
    )
    db.commit()
    return program.program_id


def persist_market_evolve_evaluation(
    evaluation: MarketEvolveEvaluation,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    db = conn or get_desk_store().conn
    _ensure_market_evolve_tables(db)
    now = _now()
    db.execute(
        """
        INSERT OR REPLACE INTO market_evolve_evaluations (
            id, program_id, cycle_id, evaluator_version, score, metrics_json,
            baseline_metrics_json, passed, rationale, quality_flags,
            evaluated_at, valid_from, transaction_from, transaction_to
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            evaluation.evaluation_id,
            evaluation.program_id,
            evaluation.cycle_id,
            evaluation.evaluator_version,
            float(evaluation.score or 0.0),
            json.dumps(evaluation.metrics, sort_keys=True),
            json.dumps(evaluation.baseline_metrics, sort_keys=True),
            1 if evaluation.passed else 0,
            evaluation.rationale,
            json.dumps(evaluation.quality_flags, sort_keys=True),
            now,
            now,
            now,
        ),
    )
    db.commit()
    return evaluation.evaluation_id


def persist_market_evolve_mutation(
    mutation: MarketEvolveMutation,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    db = conn or get_desk_store().conn
    _ensure_market_evolve_tables(db)
    now = _now()
    db.execute(
        """
        INSERT OR REPLACE INTO market_evolve_mutations (
            id, parent_program_id, child_program_id, cycle_id, mutation_kind,
            mutation_json, rationale, status, created_at, valid_from,
            transaction_from, transaction_to
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            mutation.mutation_id,
            mutation.parent_program_id,
            mutation.child_program_id,
            mutation.cycle_id,
            mutation.mutation_kind,
            json.dumps(mutation.mutation, sort_keys=True),
            mutation.rationale,
            mutation.status,
            now,
            now,
            now,
        ),
    )
    db.commit()
    return mutation.mutation_id


def _mutation_candidates_with_cortex_review(
    *,
    metrics: dict[str, float],
    program: MarketEvolveProgram,
    cortex_review: Optional[dict[str, Any]] = None,
) -> list[MarketEvolveMutationCandidate]:
    candidates: list[MarketEvolveMutationCandidate] = []
    seen: set[str] = set()

    def add(
        mutation_kind: str,
        rationale: str,
        mutation_patch: dict[str, Any],
        mutation_source: dict[str, Any],
    ) -> None:
        mutation_kind_s = str(mutation_kind or "").strip()
        patch = _sanitize_mutation_patch(mutation_patch)
        if not mutation_kind_s or not patch or mutation_kind_s in seen:
            return
        seen.add(mutation_kind_s)
        candidates.append(MarketEvolveMutationCandidate(
            mutation_kind=mutation_kind_s,
            rationale=rationale,
            mutation_patch=patch,
            mutation_source=mutation_source,
        ))

    review_kind, review_rationale, review_patch, review_source = _mutation_from_cortex_review(cortex_review)
    add(review_kind, review_rationale, review_patch, review_source)

    primary_kind, primary_rationale, primary_patch = _choose_mutation(metrics, program)
    primary_source = {
        "source": "market_evolve_metric_heuristic",
        "schema_version": "",
        "diagnostic_codes": [],
        "work_order_ids": [],
        "candidate_rank": 0,
    }
    if primary_kind != "widen_scout_coverage":
        add(primary_kind, primary_rationale, primary_patch, primary_source)

    for rank, candidate in enumerate(_secondary_metric_mutation_candidates(metrics, program), start=1):
        source = dict(candidate.mutation_source or {})
        source.setdefault("source", "market_evolve_metric_heuristic")
        source.setdefault("candidate_rank", rank)
        add(candidate.mutation_kind, candidate.rationale, candidate.mutation_patch, source)

    if not candidates:
        add(primary_kind, primary_rationale, primary_patch, primary_source)
    return candidates


def _secondary_metric_mutation_candidates(
    metrics: dict[str, float],
    program: MarketEvolveProgram,
) -> list[MarketEvolveMutationCandidate]:
    thresholds = dict(
        ((program.genome or {}).get("routing_thresholds") or DEFAULT_MARKET_EVOLVE_GENOME["routing_thresholds"])
    )
    prompt_policy = dict((program.genome or {}).get("prompt_policy") or {})
    tool_policy = dict((program.genome or {}).get("tool_request_policy") or {})
    out: list[MarketEvolveMutationCandidate] = []

    def source(*codes: str) -> dict[str, Any]:
        return {
            "source": "market_evolve_metric_heuristic",
            "schema_version": "",
            "diagnostic_codes": [code for code in codes if code],
            "work_order_ids": [],
        }

    if (
        metrics.get("cortex_task_count", 0.0) >= 2
        and (
            metrics.get("cortex_task_completion_rate", 1.0) < 0.80
            or metrics.get("cortex_shape_observation_rate", 1.0) < 0.80
            or metrics.get("cortex_task_failure_rate", 0.0) > 0.20
        )
    ):
        out.append(MarketEvolveMutationCandidate(
            mutation_kind="tighten_cortex_task_harness",
            rationale="Cortex work orders are being posted, but the worker loop is not reliably completing shape-observed tasks.",
            mutation_patch={
                "cortex_policy": {
                    "min_task_completion_rate": 0.90,
                    "min_shape_observation_rate": 0.90,
                    "max_task_failure_rate": 0.10,
                    "require_shape_tool_before_followups": True,
                    "defer_external_followups_until_shape_read": True,
                },
                "routing_thresholds": {
                    "cortex_task_min_completion_rate": 0.90,
                    "cortex_task_max_failure_rate": 0.10,
                },
                "tool_request_policy": {
                    "prefer_native_shape_tools": True,
                    "require_expected_edge": True,
                    "require_eval_plan": True,
                },
            },
            mutation_source=source("cortex_task_harness_pressure"),
        ))

    if (
        metrics.get("prompt_eval_count", 0.0) >= 3
        and (
            metrics.get("avg_prompt_quality", 1.0) < ACCEPTANCE_THRESHOLD
            or metrics.get("prompt_pass_rate", 1.0) < 0.70
            or metrics.get("prompt_contract_failure_rate", 0.0) > 0.30
        )
    ):
        out.append(MarketEvolveMutationCandidate(
            mutation_kind="tighten_prompt_contract",
            rationale="Scout prompt outputs failed the deterministic quality gate; raise contract pressure before scaling Flash.",
            mutation_patch={
                "prompt_policy": {
                    "require_mechanism": True,
                    "require_kill_signal": True,
                    "require_evidence_refs": True,
                    "min_information_strings": min(
                        3,
                        max(2, int(prompt_policy.get("min_information_strings") or 1) + 1),
                    ),
                    "contract_pressure": "raise",
                    "fallback_variants": [
                        "skeptical_operator_v1",
                        "depth_ladder_v1",
                        "source_first_v1",
                    ],
                }
            },
            mutation_source=source("prompt_contract_pressure"),
        ))

    if (
        metrics.get("outcome_eval_count", 0.0) >= 1
        and metrics.get("outcome_observed_rate", 0.0) >= 0.75
        and metrics.get("outcome_direction_hit_rate", 0.0) >= 0.60
        and metrics.get("avg_realized_edge_score", 0.0) >= 0.65
    ):
        source_targets = _merged_source_family_targets(
            tool_policy.get("source_family_targets"),
            ["our_hl_node", "market_microstructure", "web_attention"],
        )
        out.append(MarketEvolveMutationCandidate(
            mutation_kind="exploit_price_feedback_surface",
            rationale=(
                "Information strings are maturing into realized directional price movement; "
                "bias the next scout wave toward price-anchored tools, prior outcome refs, and matched repetition."
            ),
            mutation_patch={
                "prompt_policy": {
                    "emphasize_price_feedback_refs": True,
                    "prior_context_topk": min(
                        10,
                        int(prompt_policy.get("prior_context_topk") or 6) + 1,
                    ),
                },
                "routing_thresholds": {
                    "price_feedback_exploitation_budget_share": min(
                        0.20,
                        float(thresholds.get("price_feedback_exploitation_budget_share") or 0.02) + 0.04,
                    ),
                    "frontier_exploitation_budget_share": min(
                        0.24,
                        float(thresholds.get("frontier_exploitation_budget_share") or 0.04) + 0.03,
                    ),
                },
                "tool_request_policy": {
                    "prefer_price_anchor_tools": True,
                    "require_expected_edge": True,
                    "require_eval_plan": True,
                    "source_family_targets": source_targets,
                    "max_tool_candidates_per_seed": min(
                        16,
                        int(tool_policy.get("max_tool_candidates_per_seed", 10)) + 1,
                    ),
                },
            },
            mutation_source=source("information_price_loop_positive_edge"),
        ))

    if (
        metrics.get("outcome_eval_count", 0.0) >= 5
        and metrics.get("outcome_observed_rate", 0.0) >= 0.75
        and metrics.get("outcome_direction_hit_rate", 1.0) <= 0.40
        and metrics.get("avg_realized_edge_score", 1.0) < 0.50
    ):
        out.append(MarketEvolveMutationCandidate(
            mutation_kind="tighten_price_feedback_contract",
            rationale=(
                "Information strings are reaching price evaluation but failing directional skill; "
                "force scouts to include price anchors, disconfirming checks, and stricter kill signals."
            ),
            mutation_patch={
                "prompt_policy": {
                    "contract_pressure": "raise",
                    "emphasize_price_feedback_refs": True,
                    "require_mechanism": True,
                    "require_kill_signal": True,
                    "require_evidence_refs": True,
                    "fallback_variants": [
                        "skeptical_operator_v1",
                        "source_first_v1",
                        "temporal_pyramid_v1",
                    ],
                },
                "routing_thresholds": {
                    "price_feedback_min_direction_hit_rate": 0.55,
                    "price_feedback_min_observed_rate": 0.75,
                    "frontier_exploitation_budget_share": max(
                        0.02,
                        float(thresholds.get("frontier_exploitation_budget_share") or 0.04) - 0.02,
                    ),
                },
                "tool_request_policy": {
                    "prefer_price_anchor_tools": True,
                    "require_disconfirming_price_check": True,
                    "require_expected_edge": True,
                    "require_eval_plan": True,
                    "max_new_tool_proposals_per_cycle": max(
                        4,
                        int(tool_policy.get("max_new_tool_proposals_per_cycle", 12)) - 2,
                    ),
                },
            },
            mutation_source=source("information_price_loop_negative_edge"),
        ))

    if (
        metrics.get("perfusion_cell_count", 0.0) >= 1
        and metrics.get("perfusion_high_pressure_unabsorbed_rate", 0.0) >= 0.25
        and metrics.get("perfusion_avg_source_oxygenation", 0.0) >= 0.52
        and metrics.get("perfusion_max_dilation_score", 0.0) >= 0.55
    ):
        out.append(MarketEvolveMutationCandidate(
            mutation_kind="exploit_information_perfusion_pressure",
            rationale=(
                "The perfusion matrix shows oxygenated information pressure that price has not absorbed; "
                "give the next scout wave a larger pressure-followup budget and explicit perfusion context."
            ),
            mutation_patch={
                "prompt_policy": {
                    "emphasize_perfusion_state": True,
                    "emphasize_price_feedback_refs": True,
                    "prior_context_topk": min(
                        10,
                        int(prompt_policy.get("prior_context_topk") or 6) + 1,
                    ),
                },
                "routing_thresholds": {
                    "perfusion_followup_budget_share": min(
                        0.24,
                        float(thresholds.get("perfusion_followup_budget_share") or 0.02) + 0.05,
                    ),
                    "price_feedback_exploitation_budget_share": min(
                        0.24,
                        float(thresholds.get("price_feedback_exploitation_budget_share") or 0.02) + 0.03,
                    ),
                },
                "tool_request_policy": {
                    "prefer_information_perfusion_tools": True,
                    "prefer_price_anchor_tools": True,
                    "require_expected_edge": True,
                    "require_eval_plan": True,
                    "max_tool_candidates_per_seed": min(
                        16,
                        int(tool_policy.get("max_tool_candidates_per_seed", 10)) + 1,
                    ),
                },
            },
            mutation_source=source("information_perfusion_positive_pressure"),
        ))

    if (
        metrics.get("perfusion_cell_count", 0.0) >= 1
        and metrics.get("perfusion_avg_pressure_gradient", 0.0) >= 0.35
        and (
            metrics.get("perfusion_oxygenation_gap_rate", 0.0) >= 0.25
            or metrics.get("perfusion_avg_source_oxygenation", 1.0) < 0.52
            or metrics.get("perfusion_high_resistance_rate", 0.0) >= 0.25
        )
    ):
        source_targets = _merged_source_family_targets(
            tool_policy.get("source_family_targets"),
            ["our_hl_node", "hydromancer", "market_microstructure", "grok_x_alpha", "web_attention"],
        )
        out.append(MarketEvolveMutationCandidate(
            mutation_kind="oxygenate_information_perfusion_sources",
            rationale=(
                "The perfusion matrix sees pressure, but source oxygenation/resistance is not good enough; "
                "route the next wave toward missing source families before broad verifier spend."
            ),
            mutation_patch={
                "routing_thresholds": {
                    "perfusion_followup_budget_share": min(
                        0.20,
                        float(thresholds.get("perfusion_followup_budget_share") or 0.02) + 0.04,
                    ),
                    "coverage_gap_budget_share": min(
                        0.22,
                        float(thresholds.get("coverage_gap_budget_share") or 0.10) + 0.03,
                    ),
                },
                "tool_request_policy": {
                    "prefer_information_perfusion_tools": True,
                    "prefer_missing_source_family": True,
                    "source_repair_budget_share": min(
                        0.14,
                        float(tool_policy.get("source_repair_budget_share") or 0.04) + 0.04,
                    ),
                    "source_family_targets": source_targets,
                    "require_expected_edge": True,
                    "require_eval_plan": True,
                    "max_tool_candidates_per_seed": min(
                        16,
                        int(tool_policy.get("max_tool_candidates_per_seed", 10)) + 2,
                    ),
                },
            },
            mutation_source=source("information_perfusion_oxygenation_gap"),
        ))

    if (
        metrics.get("avg_source_independence", 0.0) < float(thresholds.get("min_source_independence", 0.45))
        and not (
            metrics.get("governor_seed_count", 0.0) >= 3
            and metrics.get("governor_string_yield_per_seed", 0.0) >= 0.65
            and metrics.get("governor_valid_string_rate", 0.0) >= 0.70
            and metrics.get("governor_gap_repair_rate", 0.0) >= 0.50
        )
    ):
        out.append(MarketEvolveMutationCandidate(
            mutation_kind="widen_source_families",
            rationale="The map is too dependent on a narrow source family; widen scout tool surfaces before scaling spend.",
            mutation_patch={
                "tool_request_policy": {
                    "min_source_families_per_trade_cell": 3,
                    "prefer_missing_source_family": True,
                    "max_tool_candidates_per_seed": min(
                        14,
                        int(tool_policy.get("max_tool_candidates_per_seed", 10)) + 2,
                    ),
                }
            },
            mutation_source=source("source_independence_pressure"),
        ))

    if metrics.get("low_ev_tool_rate", 0.0) > float(thresholds.get("max_low_ev_tool_rate", 0.25)):
        out.append(MarketEvolveMutationCandidate(
            mutation_kind="tighten_tool_ev",
            rationale="Too many proposed tools lack expected edge or robust evaluation plans.",
            mutation_patch={
                "tool_request_policy": {
                    "require_expected_edge": True,
                    "require_eval_plan": True,
                    "max_new_tool_proposals_per_cycle": max(
                        4,
                        int(tool_policy.get("max_new_tool_proposals_per_cycle", 12)) - 2,
                    ),
                }
            },
            mutation_source=source("tool_ev_pressure"),
        ))

    if metrics.get("runtime_adapter_backlog_count", 0.0) >= 1:
        out.append(MarketEvolveMutationCandidate(
            mutation_kind="build_runtime_adapter_surface",
            rationale="Tool proposals are conceptually useful but blocked on learned-runtime adapters; route engineering budget to adapter fixtures and promotion gates.",
            mutation_patch={
                "tool_request_policy": {
                    "runtime_adapter_backlog_priority": "high",
                    "max_runtime_adapter_builds_per_cycle": min(
                        3,
                        int(tool_policy.get("max_runtime_adapter_builds_per_cycle") or 0) + 1,
                    ),
                    "require_runtime_adapter_eval_fixtures": True,
                    "auto_promote_high_priority_tools": True,
                    "require_eval_plan": True,
                    "require_expected_edge": True,
                }
            },
            mutation_source=source("runtime_adapter_backlog_pressure"),
        ))

    if (
        metrics.get("learned_tool_success_rate", 0.0) >= 0.80
        and metrics.get("learned_tool_usage_per_scout", 0.0) >= 0.05
    ):
        out.append(MarketEvolveMutationCandidate(
            mutation_kind="exploit_learned_tool_surface",
            rationale="Learned tools are producing successful evidence calls; bias future scout tool surfaces toward them.",
            mutation_patch={
                "tool_request_policy": {
                    "prefer_learned_tools": True,
                    "learned_tool_priority_boost": min(
                        3.0,
                        float(tool_policy.get("learned_tool_priority_boost", 0.0)) + 1.0,
                    ),
                    "max_tool_candidates_per_seed": min(
                        16,
                        int(tool_policy.get("max_tool_candidates_per_seed", 10)) + 1,
                    ),
                }
            },
            mutation_source=source("learned_tool_success_pressure"),
        ))
    return out


def _choose_mutation_with_cortex_review(
    metrics: dict[str, float],
    program: MarketEvolveProgram,
    *,
    cortex_review: Optional[dict[str, Any]] = None,
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    review_choice = _mutation_from_cortex_review(cortex_review)
    if review_choice[0]:
        return review_choice
    mutation_kind, rationale, mutation_patch = _choose_mutation(metrics, program)
    return mutation_kind, rationale, mutation_patch, {
        "source": "market_evolve_metric_heuristic",
        "schema_version": "",
        "diagnostic_codes": [],
        "work_order_ids": [],
    }


def _mutation_from_cortex_review(
    cortex_review: Optional[dict[str, Any]],
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    if not isinstance(cortex_review, dict):
        return "", "", {}, {}
    proposed = (
        cortex_review.get("proposed_geometry_policy")
        if isinstance(cortex_review.get("proposed_geometry_policy"), dict)
        else {}
    )
    mutation_kind = str(proposed.get("mutation_kind_hint") or "").strip()
    if not mutation_kind or mutation_kind == "continue_shape_guided_routing":
        return "", "", {}, {}
    patch = _sanitize_mutation_patch(proposed.get("policy_patch"))
    if not patch:
        return "", "", {}, {}
    diagnostics = [
        d for d in (cortex_review.get("diagnostics") or [])
        if isinstance(d, dict)
    ]
    actionable_diagnostics = [
        d for d in diagnostics
        if _float(d.get("severity_score"), 0.0) >= 0.70
        or str(d.get("severity") or "") in {"critical", "high"}
    ]
    if not actionable_diagnostics:
        return "", "", {}, {}
    work_orders = [
        w for w in (cortex_review.get("cortex_work_orders") or [])
        if isinstance(w, dict)
    ]
    rationale = str(proposed.get("why_this_matters") or "").strip()
    if not rationale:
        rationale = "; ".join(
            str(d.get("diagnosis") or "")
            for d in actionable_diagnostics[:3]
            if str(d.get("diagnosis") or "").strip()
        )
    if not rationale:
        rationale = "The geometry cortex found a shape-policy pressure point that should be tested against control."
    mutation_source = {
        "source": "alpha_geometry_cortex_review",
        "schema_version": cortex_review.get("schema_version"),
        "status": cortex_review.get("status"),
        "shape_can_direct_next": cortex_review.get("shape_can_direct_next"),
        "diagnostic_codes": [
            str(d.get("code"))
            for d in actionable_diagnostics
            if d.get("code")
        ],
        "diagnostics": [
            {
                "code": d.get("code"),
                "severity": d.get("severity"),
                "diagnosis": d.get("diagnosis"),
                "target_metrics": d.get("target_metrics") or [],
            }
            for d in actionable_diagnostics[:6]
        ],
        "shape_health": cortex_review.get("shape_health") if isinstance(cortex_review.get("shape_health"), dict) else {},
        "work_order_ids": [
            str(w.get("order_id"))
            for w in work_orders
            if w.get("order_id")
        ],
        "target_metrics": proposed.get("target_metrics") if isinstance(proposed.get("target_metrics"), dict) else {},
        "falsification_gates": proposed.get("falsification_gates") if isinstance(proposed.get("falsification_gates"), list) else [],
    }
    return mutation_kind, rationale, patch, mutation_source


def _sanitize_mutation_patch(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    allowed_sections = {
        "prompt_policy",
        "routing_thresholds",
        "tool_request_policy",
        "cortex_policy",
        "evolution_policy",
        "geometry_weights",
        "objective_weights",
    }
    out: dict[str, Any] = {}
    for section, value in raw.items():
        if section not in allowed_sections or not isinstance(value, dict):
            continue
        cleaned = {
            str(k): v
            for k, v in value.items()
            if isinstance(k, str) and isinstance(v, (int, float, str, bool, list))
        }
        if cleaned:
            out[str(section)] = cleaned
    return out


def _choose_mutation(
    metrics: dict[str, float],
    program: MarketEvolveProgram,
) -> tuple[str, str, dict[str, Any]]:
    thresholds = dict(
        ((program.genome or {}).get("routing_thresholds") or DEFAULT_MARKET_EVOLVE_GENOME["routing_thresholds"])
    )
    if (
        metrics.get("route_contract_eval_count", 0.0) >= 3
        and metrics.get("route_contract_success_rate", 1.0) < 0.60
    ):
        prompt_policy = dict((program.genome or {}).get("prompt_policy") or {})
        return (
            "tighten_shape_route_contract",
            "Geometry-routed scouts saw the route contract but failed to move the missing edge often enough.",
            {
                "prompt_policy": {
                    "require_mechanism": True,
                    "require_kill_signal": True,
                    "require_evidence_refs": True,
                    "min_information_strings": min(
                        3,
                        max(2, int(prompt_policy.get("min_information_strings") or 1) + 1),
                    ),
                    "contract_pressure": "raise",
                    "route_contract_pressure": "strict",
                },
                "routing_thresholds": {
                    "route_contract_min_success_rate": 0.70,
                },
                "tool_request_policy": {
                    "require_expected_edge": True,
                    "require_eval_plan": True,
                    "prefer_missing_source_family": True,
                },
            },
        )
    if (
        metrics.get("cortex_task_count", 0.0) >= 2
        and (
            metrics.get("cortex_task_completion_rate", 1.0) < 0.80
            or metrics.get("cortex_shape_observation_rate", 1.0) < 0.80
            or metrics.get("cortex_task_failure_rate", 0.0) > 0.20
        )
    ):
        return (
            "tighten_cortex_task_harness",
            "Cortex work orders are being posted, but the worker loop is not reliably completing shape-observed tasks.",
            {
                "cortex_policy": {
                    "min_task_completion_rate": 0.90,
                    "min_shape_observation_rate": 0.90,
                    "max_task_failure_rate": 0.10,
                    "require_shape_tool_before_followups": True,
                    "defer_external_followups_until_shape_read": True,
                },
                "routing_thresholds": {
                    "cortex_task_min_completion_rate": 0.90,
                    "cortex_task_max_failure_rate": 0.10,
                },
                "tool_request_policy": {
                    "prefer_native_shape_tools": True,
                    "require_expected_edge": True,
                    "require_eval_plan": True,
                },
            },
        )
    if (
        metrics.get("geometry_cell_count", 0.0) >= 3
        and metrics.get("fragile_verify_rate", 0.0) > 0.20
    ):
        weights = normalize_geometry_weights((program.genome or {}).get("geometry_weights"))
        return (
            "retune_geometry_repair_before_verify",
            "The current geometry is sending brittle or under-sourced high-scream cells to verifier spend; make the cortex repair shape quality before promotion.",
            {
                "geometry_weights": {
                    "source_independence": min(0.40, weights.get("source_independence", 0.0) + 0.10),
                    "fragility_penalty": min(0.45, weights.get("fragility_penalty", 0.18) + 0.08),
                    "verifier_readiness": max(0.16, weights.get("verifier_readiness", 0.24) - 0.02),
                },
                "routing_thresholds": {
                    "verify_allow_fragility_max": max(
                        0.25,
                        min(
                            0.55,
                            float(thresholds.get("max_fragility", 0.45)),
                        ),
                    ),
                    "verify_source_independence_min": min(
                        0.75,
                        max(0.45, float(thresholds.get("min_source_independence", 0.45))),
                    ),
                    "repair_sources_before_verify": True,
                    "repair_fragility_min": max(
                        0.25,
                        min(0.55, float(thresholds.get("max_fragility", 0.45))),
                    ),
                },
                "tool_request_policy": {
                    "prefer_missing_source_family": True,
                    "require_expected_edge": True,
                    "require_eval_plan": True,
                },
            },
        )
    if (
        metrics.get("geometry_cell_count", 0.0) >= 3
        and metrics.get("high_signal_observe_rate", 0.0) > 0.35
        and metrics.get("avg_fragility", 1.0) <= float(thresholds.get("max_fragility", 0.45))
    ):
        weights = normalize_geometry_weights((program.genome or {}).get("geometry_weights"))
        return (
            "retune_geometry_surface_hidden_edges",
            "Too many clean high-signal cells remain in observe; make the map shape more expressive so the cortex routes hidden edges instead of staying mute.",
            {
                "geometry_weights": {
                    "frontier_pressure": min(0.50, weights.get("frontier_pressure", 0.38) + 0.08),
                    "support_mass": min(0.24, weights.get("support_mass", 0.14) + 0.04),
                    "verifier_readiness": min(0.34, weights.get("verifier_readiness", 0.24) + 0.04),
                },
                "routing_thresholds": {
                    "verify_trade_scream_min": max(
                        0.50,
                        float(thresholds.get("verify_trade_scream_min", 0.62)) - 0.04,
                    ),
                    "widen_sources_frontier_min": max(
                        0.42,
                        float(thresholds.get("widen_sources_frontier_min", 0.50)) - 0.04,
                    ),
                    "frontier_exploitation_budget_share": min(
                        0.18,
                        float(thresholds.get("frontier_exploitation_budget_share", 0.04)) + 0.04,
                    ),
                },
            },
        )
    if (
        metrics.get("prompt_eval_count", 0.0) >= 3
        and (
            metrics.get("avg_prompt_quality", 1.0) < ACCEPTANCE_THRESHOLD
            or metrics.get("prompt_pass_rate", 1.0) < 0.70
            or metrics.get("prompt_contract_failure_rate", 0.0) > 0.30
        )
    ):
        prompt_policy = dict((program.genome or {}).get("prompt_policy") or {})
        return (
            "tighten_prompt_contract",
            "Scout prompt outputs failed the deterministic quality gate; raise contract pressure before scaling Flash.",
            {
                "prompt_policy": {
                    "require_mechanism": True,
                    "require_kill_signal": True,
                    "require_evidence_refs": True,
                    "min_information_strings": min(
                        3,
                        max(2, int(prompt_policy.get("min_information_strings") or 1) + 1),
                    ),
                    "contract_pressure": "raise",
                    "fallback_variants": [
                        "skeptical_operator_v1",
                        "depth_ladder_v1",
                        "source_first_v1",
                    ],
                }
            },
        )
    if metrics.get("valid_string_rate", 0.0) < float(thresholds.get("min_valid_string_rate", 0.70)):
        return (
            "tighten_prompt_contract",
            "Too few scout strings passed structural and evidence checks.",
            {
                "prompt_policy": {
                    "require_mechanism": True,
                    "require_kill_signal": True,
                    "require_evidence_refs": True,
                    "min_information_strings": 2,
                    "contract_pressure": "raise",
                }
            },
        )
    governor_is_working = (
        metrics.get("governor_seed_count", 0.0) >= 3
        and metrics.get("governor_string_yield_per_seed", 0.0) >= 0.65
        and metrics.get("governor_valid_string_rate", 0.0) >= 0.70
        and metrics.get("governor_gap_repair_rate", 0.0) >= 0.50
    )
    if (
        metrics.get("avg_source_independence", 0.0) < float(thresholds.get("min_source_independence", 0.45))
        and not governor_is_working
    ):
        return (
            "widen_source_families",
            "The map is too dependent on a narrow source family; widen scout tool surfaces before scaling spend.",
            {
                "tool_request_policy": {
                    "min_source_families_per_trade_cell": 3,
                    "prefer_missing_source_family": True,
                    "max_tool_candidates_per_seed": min(
                        14,
                        int(((program.genome or {}).get("tool_request_policy") or {}).get("max_tool_candidates_per_seed", 10)) + 2,
                    ),
                }
            },
        )
    if metrics.get("avg_fragility", 0.0) > float(thresholds.get("max_fragility", 0.45)):
        return (
            "raise_geometry_repair_priority",
            "The geometry is high-fragility; route more cells to source repair before verifier spend.",
            {
                "routing_thresholds": {
                    "max_fragility": max(0.25, float(thresholds.get("max_fragility", 0.45)) - 0.05),
                    "repair_sources_before_verify": True,
                }
            },
        )
    if metrics.get("low_ev_tool_rate", 0.0) > float(thresholds.get("max_low_ev_tool_rate", 0.25)):
        return (
            "tighten_tool_ev",
            "Too many proposed tools lack expected edge or robust evaluation plans.",
            {
                "tool_request_policy": {
                    "require_expected_edge": True,
                    "require_eval_plan": True,
                    "max_new_tool_proposals_per_cycle": max(
                        4,
                        int(((program.genome or {}).get("tool_request_policy") or {}).get("max_new_tool_proposals_per_cycle", 12)) - 2,
                    ),
                }
            },
        )
    if metrics.get("runtime_adapter_backlog_count", 0.0) >= 1:
        tool_policy = dict((program.genome or {}).get("tool_request_policy") or {})
        return (
            "build_runtime_adapter_surface",
            "Tool proposals are conceptually useful but blocked on learned-runtime adapters; route engineering budget to adapter fixtures and promotion gates.",
            {
                "tool_request_policy": {
                    "runtime_adapter_backlog_priority": "high",
                    "max_runtime_adapter_builds_per_cycle": min(
                        3,
                        int(tool_policy.get("max_runtime_adapter_builds_per_cycle") or 0) + 1,
                    ),
                    "require_runtime_adapter_eval_fixtures": True,
                    "auto_promote_high_priority_tools": True,
                    "require_eval_plan": True,
                    "require_expected_edge": True,
                }
            },
        )
    if governor_is_working:
        routing = dict((program.genome or {}).get("routing_thresholds") or {})
        tool_policy = dict((program.genome or {}).get("tool_request_policy") or {})
        return (
            "exploit_market_map_governor",
            "Governor-routed frontier/gap seeds are producing valid strings; allocate more next-cycle scout budget to coverage repair with source-family discipline.",
            {
                "routing_thresholds": {
                    "coverage_gap_budget_share": min(
                        0.24,
                        float(routing.get("coverage_gap_budget_share") or 0.10) + 0.04,
                    ),
                    "coverage_exploration_budget_share": max(
                        0.04,
                        float(routing.get("coverage_exploration_budget_share") or 0.04),
                    ),
                    "governor_gap_min_valid_string_rate": max(
                        0.70,
                        metrics.get("governor_valid_string_rate", 0.70),
                    ),
                },
                "tool_request_policy": {
                    "prefer_missing_source_family": True,
                    "source_repair_budget_share": min(
                        0.12,
                        float(tool_policy.get("source_repair_budget_share") or 0.04) + 0.02,
                    ),
                    "require_expected_edge": True,
                    "require_eval_plan": True,
                },
            },
        )
    if (
        metrics.get("governor_seed_count", 0.0) >= 3
        and metrics.get("governor_waste_rate", 0.0) > 0.55
    ):
        routing = dict((program.genome or {}).get("routing_thresholds") or {})
        return (
            "tighten_market_map_governor",
            "Governor-routed gap seeds are consuming scout slots without enough valid strings; tighten frontier ranking and source requirements before increasing allocation.",
            {
                "routing_thresholds": {
                    "coverage_gap_budget_share": max(
                        0.04,
                        float(routing.get("coverage_gap_budget_share") or 0.10) - 0.03,
                    ),
                    "governor_min_priority_score": 0.78,
                    "governor_require_source_surface_priorities": True,
                },
                "tool_request_policy": {
                    "prefer_missing_source_family": True,
                    "require_expected_edge": True,
                    "require_eval_plan": True,
                },
            },
        )
    if (
        metrics.get("tool_proposal_count", 0.0) >= 3
        and metrics.get("tool_activation_rate", 0.0) < 0.20
        and metrics.get("low_ev_tool_rate", 0.0) <= float(thresholds.get("max_low_ev_tool_rate", 0.25))
    ):
        return (
            "raise_tool_promotion_discipline",
            "Agents are proposing plausible tools, but too few are reaching evaluated active-tool status.",
            {
                "tool_request_policy": {
                    "auto_promote_high_priority_tools": True,
                    "max_tool_promotions_per_cycle": min(
                        6,
                        int(((program.genome or {}).get("tool_request_policy") or {}).get("max_tool_promotions_per_cycle", 3)) + 1,
                    ),
                    "require_eval_plan": True,
                    "require_expected_edge": True,
                }
            },
        )
    if (
        metrics.get("learned_tool_success_rate", 0.0) >= 0.80
        and metrics.get("learned_tool_usage_per_scout", 0.0) >= 0.05
    ):
        return (
            "exploit_learned_tool_surface",
            "Learned tools are producing successful evidence calls; bias future scout tool surfaces toward them.",
            {
                "tool_request_policy": {
                    "prefer_learned_tools": True,
                    "learned_tool_priority_boost": min(
                        3.0,
                        float(((program.genome or {}).get("tool_request_policy") or {}).get("learned_tool_priority_boost", 0.0)) + 1.0,
                    ),
                    "max_tool_candidates_per_seed": min(
                        16,
                        int(((program.genome or {}).get("tool_request_policy") or {}).get("max_tool_candidates_per_seed", 10)) + 1,
                    ),
                }
            },
        )
    if (
        metrics.get("avg_verifier_readiness", 0.0) >= 0.60
        and metrics.get("frontier_candidate_rate", 0.0) >= 0.15
        and metrics.get("verifier_pass_rate", 0.0) < 0.34
    ):
        return (
            "raise_verifier_gate",
            "Geometry looks ready but verifier pass-through is weak; demand stronger cross-source support before graduation.",
            {
                "routing_thresholds": {
                    "min_verifier_readiness": min(0.75, float(thresholds.get("min_verifier_readiness", 0.55)) + 0.05),
                    "min_source_independence": min(0.75, float(thresholds.get("min_source_independence", 0.45)) + 0.05),
                }
            },
        )
    if (
        metrics.get("avg_trade_scream_score", 0.0) >= 0.55
        and metrics.get("verifier_pass_rate", 0.0) >= 0.50
    ):
        return (
            "exploit_geometry_thresholds",
            "The current map produced high-scream, verifier-supported cells; spend more next cycle around similar geometry.",
            {
                "routing_thresholds": {
                    "exploit_trade_scream_min": max(0.50, metrics.get("avg_trade_scream_score", 0.55)),
                    "frontier_exploitation_budget_share": 0.20,
                }
            },
        )
    return (
        "widen_scout_coverage",
        "No dominant failure surfaced; modestly widen market coverage while retaining gates.",
        {
            "prompt_policy": {
                "prior_context_topk": min(
                    10,
                    int(((program.genome or {}).get("prompt_policy") or {}).get("prior_context_topk", 6)) + 1,
                )
            },
            "routing_thresholds": {
                "coverage_exploration_budget_share": 0.12,
            },
        },
    )


def _hard_gate_result(metrics: dict[str, float], program: MarketEvolveProgram) -> tuple[bool, list[str]]:
    objective = program.objective or DEFAULT_OBJECTIVE
    hard = dict(objective.get("hard_gates") or {})
    flags: list[str] = []
    if metrics.get("string_count", 0.0) <= 0:
        flags.append("no_strings_to_evaluate")
    if metrics.get("valid_string_rate", 0.0) < float(hard.get("min_valid_string_rate", 0.60)):
        flags.append("low_valid_string_rate")
    if metrics.get("avg_fragility", 0.0) > float(hard.get("max_avg_fragility", 0.65)):
        flags.append("high_avg_fragility")
    if metrics.get("fragile_verify_rate", 0.0) > float(hard.get("max_fragile_verify_rate", 0.20)):
        flags.append("fragile_cells_sent_to_verifier")
    if metrics.get("low_ev_tool_rate", 0.0) > float(hard.get("max_low_ev_tool_rate", 0.40)):
        flags.append("high_low_ev_tool_rate")
    if metrics.get("tool_eval_failed_rate", 0.0) > float(hard.get("max_tool_eval_failed_rate", 0.60)):
        flags.append("high_tool_eval_failed_rate")
    if metrics.get("geometry_cell_count", 0.0) <= 0 and metrics.get("string_count", 0.0) > 0:
        flags.append("missing_geometry_cells")
    if metrics.get("cortex_task_count", 0.0) > 0:
        if metrics.get("cortex_task_completion_rate", 0.0) < float(hard.get("min_cortex_task_completion_rate", 0.50)):
            flags.append("low_cortex_task_completion_rate")
        if metrics.get("cortex_task_failure_rate", 0.0) > float(hard.get("max_cortex_task_failure_rate", 0.50)):
            flags.append("high_cortex_task_failure_rate")
    if not flags:
        flags.append("hard_gates_passed")
    return flags == ["hard_gates_passed"], flags


def _evaluation_rationale(score: float, metrics: dict[str, float], flags: list[str]) -> str:
    return (
        f"score={score:.2f}; strings={metrics.get('string_count', 0):.0f}; "
        f"valid={metrics.get('valid_string_rate', 0):.2f}; "
        f"src_ind={metrics.get('avg_source_independence', 0):.2f}; "
        f"ready={metrics.get('avg_verifier_readiness', 0):.2f}; "
        f"fragility={metrics.get('avg_fragility', 0):.2f}; "
        f"governor_seeds={metrics.get('governor_seed_count', 0):.0f}; "
        f"governor_yield={metrics.get('governor_string_yield_per_seed', 0):.2f}; "
        f"perfusion_gradient={metrics.get('perfusion_avg_pressure_gradient', 0):.2f}; "
        f"perfusion_dilation={metrics.get('perfusion_max_dilation_score', 0):.2f}; "
        f"cortex_tasks={metrics.get('cortex_task_count', 0):.0f}; "
        f"cortex_done={metrics.get('cortex_task_completion_rate', 0):.2f}; "
        f"flags={','.join(flags)}"
    )


def _baseline_metrics_for_program(
    program: MarketEvolveProgram,
    *,
    conn: sqlite3.Connection,
) -> dict[str, float]:
    row = conn.execute(
        """
        SELECT metrics_json
        FROM market_evolve_evaluations
        WHERE program_id = ?
        ORDER BY evaluated_at DESC
        LIMIT 1
        """,
        (program.program_id,),
    ).fetchone()
    if not row:
        return {}
    return {
        str(k): float(v)
        for k, v in (_json_load(row["metrics_json"], {}) or {}).items()
        if isinstance(v, (int, float))
    }


def _last_mutation_for_child(
    *,
    child_program_id: str,
    cycle_id: str,
    conn: sqlite3.Connection,
) -> Optional[MarketEvolveMutation]:
    row = conn.execute(
        """
        SELECT *
        FROM market_evolve_mutations
        WHERE child_program_id = ? AND cycle_id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (child_program_id, cycle_id),
    ).fetchone()
    if not row:
        return None
    return MarketEvolveMutation(
        mutation_id=str(row["id"]),
        parent_program_id=str(row["parent_program_id"]),
        child_program_id=str(row["child_program_id"]),
        cycle_id=str(row["cycle_id"]),
        mutation_kind=str(row["mutation_kind"]),
        mutation=_json_load(row["mutation_json"], {}),
        rationale=str(row["rationale"] or ""),
        status=str(row["status"] or "proposed"),
    )


def _load_market_evolve_mutations(
    *,
    limit: int = 128,
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM market_evolve_mutations
        WHERE transaction_to IS NULL
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["mutation"] = _json_load(d.get("mutation_json"), {})
        d.pop("mutation_json", None)
        out.append(d)
    return out


def _persist_policy_application(
    *,
    program: MarketEvolveProgram,
    seed: Any,
    cycle_id: str,
    prompt_variant: str,
    tool_candidate_limit: int,
    evidence_tool_limit: int,
    experiment_id: str = "",
    experiment_arm: str = "",
    applied: dict[str, Any],
    conn: sqlite3.Connection,
) -> str:
    _ensure_market_evolve_tables(conn)
    now = _now()
    seed_id = _seed_attr(seed, "seed_id")
    row_id = _policy_application_id(cycle_id, program.program_id, seed_id)
    conn.execute(
        """
        INSERT OR REPLACE INTO market_evolve_policy_applications (
            id, cycle_id, program_id, program_name, generation, seed_id,
            entity, horizon, lens, bias_mode, theme, prompt_variant,
            tool_candidate_limit, evidence_tool_limit, experiment_id,
            experiment_arm, applied_json,
            created_at, valid_from, transaction_from, transaction_to
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            row_id,
            cycle_id,
            program.program_id,
            program.name,
            int(program.generation),
            seed_id,
            _seed_attr(seed, "entity"),
            _seed_attr(seed, "horizon"),
            _seed_attr(seed, "lens"),
            _seed_attr(seed, "bias_mode"),
            _seed_attr(seed, "theme"),
            prompt_variant,
            int(tool_candidate_limit),
            int(evidence_tool_limit),
            experiment_id,
            experiment_arm,
            json.dumps(applied, sort_keys=True),
            now,
            now,
            now,
        ),
    )
    conn.commit()
    return row_id


def _market_map_seed_lineage(payload: dict[str, Any]) -> dict[str, Any]:
    seed_source = str(
        payload.get("market_evolve_original_seed_source")
        or payload.get("source")
        or ""
    )
    lineage = {
        "seed_source": seed_source,
        "market_evolve_original_seed_source": payload.get("market_evolve_original_seed_source"),
        "gap_id": payload.get("gap_id"),
        "missing_surfaces": payload.get("missing_surfaces") or [],
        "expected_edges": payload.get("expected_edges") or [],
        "frontier_llm_reason": payload.get("frontier_llm_reason"),
        "frontier_llm_stop_condition": payload.get("frontier_llm_stop_condition"),
        "frontier_llm_requested_scout_count": payload.get("frontier_llm_requested_scout_count"),
        "market_map_completion_pressure": payload.get("market_map_completion_pressure"),
        "market_map_valid_cell_count": payload.get("market_map_valid_cell_count"),
        "market_map_known_entity_count": payload.get("market_map_known_entity_count"),
        "market_map_governor_schema": payload.get("market_map_governor_schema"),
    }
    geometry_context = payload.get("market_map_alpha_geometry_context")
    if isinstance(geometry_context, dict):
        metrics = geometry_context.get("global_metrics") if isinstance(geometry_context.get("global_metrics"), dict) else {}
        lineage.update({
            "alpha_geometry_status": geometry_context.get("status"),
            "alpha_geometry_source": geometry_context.get("source"),
            "alpha_geometry_cell_count": metrics.get("cell_count"),
            "alpha_geometry_avg_trade_scream_score": metrics.get("avg_trade_scream_score"),
            "alpha_geometry_avg_verifier_readiness": metrics.get("avg_verifier_readiness"),
        })
    return {
        key: value
        for key, value in lineage.items()
        if value not in ("", None, [], {})
    }


def _persist_experiment_plan(
    *,
    parent: MarketEvolveProgram,
    child: MarketEvolveProgram,
    mutation: MarketEvolveMutation,
    metrics: dict[str, float],
    conn: sqlite3.Connection,
) -> str:
    _ensure_market_evolve_tables(conn)
    now = _now()
    experiment_id = _experiment_id(mutation.cycle_id, parent.program_id, child.program_id)
    matched_slice = {
        "assignment": "paired_seed_clone_then_deterministic_hash",
        "unit": "seed_cell",
        "axes": ["entity", "horizon", "lens", "bias_mode", "theme"],
        "min_seeds_per_arm": 20,
        "pairing": {
            "enabled": True,
            "unit_key": "entity|horizon|lens|bias_mode|theme",
            "max_seed_budget_share": 0.12,
        },
        "hold_constant": [
            "entity_universe",
            "calendar_gate",
            "budget_per_seed",
            "model_family",
        ],
    }
    arms = [
        {
            "arm": "control",
            "program_id": parent.program_id,
            "status": parent.status,
            "generation": parent.generation,
        },
        {
            "arm": "candidate",
            "program_id": child.program_id,
            "status": child.status,
            "generation": child.generation,
            "mutation_kind": mutation.mutation_kind,
        },
    ]
    evolution_proof = (
        mutation.mutation.get("_evolution_proof")
        if isinstance(mutation.mutation, dict) and isinstance(mutation.mutation.get("_evolution_proof"), dict)
        else {}
    )
    success_criteria = {
        "primary_metric": "accepted_unique_high_quality_coverage_per_dollar",
        "min_score_delta": 0.05,
        "min_completed_cycles": 2,
        "min_candidate_wins": 2,
        "min_consecutive_candidate_wins": 2,
        "min_valid_string_rate": max(0.60, metrics.get("valid_string_rate", 0.0)),
        "min_source_independence": max(0.45, metrics.get("avg_source_independence", 0.0)),
        "max_avg_fragility": min(0.65, max(0.25, metrics.get("avg_fragility", 0.45))),
        "max_low_ev_tool_rate": 0.25,
        "max_tool_eval_failed_rate": 0.50,
        "mutation_intent": evolution_proof,
        "mutation_target_metrics": evolution_proof.get("target_metrics") or {},
        "falsification_gates": evolution_proof.get("falsification_gates") or [],
        "kill_signal": (
            evolution_proof.get("kill_signal")
            or "Reject the candidate if it loses the matched A/B gate, fails hard gates, or creates cost/quality regressions."
        ),
        "promotion_requires": [
            "passes_hard_gates",
            "beats_control_out_of_sample",
            "multi_cycle_out_of_sample_evidence",
            "no_cost_explosion",
            "human_readable_lineage",
            "evolution_proof_required",
        ],
    }
    quality_flags = ["hard_experiment_required", f"mutation:{mutation.mutation_kind}"]
    quality_flags.append("mutation_intent_packet" if evolution_proof else "mutation_intent_missing")
    conn.execute(
        """
        INSERT OR REPLACE INTO market_evolve_experiments (
            id, cycle_id, parent_program_id, candidate_program_id,
            experiment_kind, matched_slice_json, arms_json,
            success_criteria_json, status, rationale, quality_flags,
            created_at, valid_from, transaction_from, transaction_to
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            experiment_id,
            mutation.cycle_id,
            parent.program_id,
            child.program_id,
            "matched_policy_ab",
            json.dumps(matched_slice, sort_keys=True),
            json.dumps(arms, sort_keys=True),
            json.dumps(success_criteria, sort_keys=True),
            "planned",
            mutation.rationale,
            json.dumps(quality_flags),
            now,
            now,
            now,
        ),
    )
    conn.commit()
    return experiment_id


def _load_assignable_experiments(*, conn: sqlite3.Connection) -> list[dict[str, Any]]:
    experiments = load_market_evolve_experiments(status="", limit=32, conn=conn)
    return [
        exp for exp in experiments
        if exp.get("parent_program_id")
        and exp.get("candidate_program_id")
        and str(exp.get("status") or "") in {"planned", "running", "insufficient_sample"}
    ]


def _experiment_for_seed(seed: Any, experiments: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not experiments:
        return None
    idx = _hash_int(_experiment_slice_key(seed)) % len(experiments)
    return experiments[idx]


def _experiment_arm_for_seed(seed: Any, experiment_id: str) -> str:
    raw = f"{experiment_id}|{_seed_attr(seed, 'seed_id')}|arm"
    return "candidate" if _hash_int(raw) % 100 < 50 else "control"


def _experiment_by_id(experiment_id: str, experiments: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not experiment_id:
        return None
    for experiment in experiments:
        if str(experiment.get("id") or "") == experiment_id:
            return experiment
    return None


def _balanced_experiment_assignment_map(
    seeds: list[Any],
    experiments: list[dict[str, Any]],
) -> dict[str, tuple[dict[str, Any], str]]:
    if not seeds or not experiments:
        return {}
    assignable = [
        seed for seed in seeds
        if not str(_seed_payload(seed).get("market_evolve_forced_experiment_id") or "").strip()
    ]
    if not assignable:
        return {}
    ordered_experiments = _ordered_experiments_for_assignment(experiments)
    min_per_arm_values: list[int] = []
    for experiment in ordered_experiments:
        matched = dict(experiment.get("matched_slice") or {})
        try:
            min_per_arm = int(matched.get("min_seeds_per_arm") or 20)
        except Exception:
            min_per_arm = 20
        min_per_arm_values.append(max(4, min(32, min_per_arm)))
    required_per_experiment = max(8, 2 * max(min_per_arm_values or [20]))
    max_experiments = max(1, len(assignable) // required_per_experiment)
    selected = ordered_experiments[:max(1, min(len(ordered_experiments), max_experiments))]
    if not selected:
        return {}
    ordered_seeds = sorted(
        assignable,
        key=lambda seed: (
            _hash_int(_seed_attr(seed, "seed_id")),
            _seed_attr(seed, "seed_id"),
        ),
    )
    out: dict[str, tuple[dict[str, Any], str]] = {}
    n_slots = len(selected) * 2
    for i, seed in enumerate(ordered_seeds):
        slot = i % n_slots
        experiment = selected[slot // 2]
        arm = "control" if slot % 2 == 0 else "candidate"
        seed_id = _seed_attr(seed, "seed_id")
        if seed_id:
            out[seed_id] = (experiment, arm)
    return out


def _experiment_slice_key(seed: Any) -> str:
    return "|".join([
        _seed_attr(seed, "entity"),
        _seed_attr(seed, "horizon"),
        _seed_attr(seed, "lens"),
        _seed_attr(seed, "bias_mode"),
        _seed_attr(seed, "theme"),
    ])


def _experiment_pair_budget(
    n_seeds: int,
    experiments: list[dict[str, Any]],
    *,
    max_pairs: Optional[int] = None,
) -> int:
    if max_pairs is not None:
        return max(0, min(int(max_pairs), max(0, int(n_seeds))))
    if n_seeds <= 0 or not experiments:
        return 0
    per_experiment_caps = []
    for experiment in experiments:
        matched = dict(experiment.get("matched_slice") or {})
        min_per_arm = int(matched.get("min_seeds_per_arm") or 20)
        per_experiment_caps.append(max(4, min(32, min_per_arm)))
    target = sum(per_experiment_caps)
    global_cap = max(1, min(96, int(round(n_seeds * 0.12))))
    return max(0, min(target, global_cap, n_seeds))


def _selected_experiments_for_pair_budget(
    experiments: list[dict[str, Any]],
    *,
    pair_budget: int,
) -> list[dict[str, Any]]:
    if not experiments or pair_budget <= 0:
        return []
    ordered = _ordered_experiments_for_assignment(experiments)
    min_pair_values: list[int] = []
    for experiment in ordered:
        matched = dict(experiment.get("matched_slice") or {})
        try:
            min_pairs = int(matched.get("min_seeds_per_arm") or 20)
        except Exception:
            min_pairs = 20
        min_pair_values.append(max(4, min(32, min_pairs)))
    required_pairs_per_experiment = max(4, max(min_pair_values or [20]))
    max_experiments = max(1, pair_budget // required_pairs_per_experiment)
    return ordered[:max(1, min(len(ordered), max_experiments))]


def _ordered_experiments_for_assignment(
    experiments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    status_rank = {
        "running": 0,
        "insufficient_sample": 0,
        "planned": 1,
    }
    return sorted(
        experiments,
        key=lambda exp: (
            status_rank.get(str(exp.get("status") or ""), 2),
            _experiment_created_desc_rank(exp),
            str(exp.get("id") or ""),
        ),
    )


def _experiment_created_desc_rank(experiment: dict[str, Any]) -> float:
    raw = str(experiment.get("created_at") or "")
    if not raw:
        return 0.0
    try:
        return -datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _clone_seed_for_experiment_arm(seed: Any, experiment_id: str, arm: str, pair_id: str) -> Any:
    try:
        from ..swarm.seed_generator import SeedCell

        if isinstance(seed, SeedCell):
            payload = copy.deepcopy(seed.payload)
            payload.update(_experiment_pair_payload(seed, experiment_id, arm, pair_id))
            return SeedCell(
                seed_id=f"{seed.seed_id}__{pair_id}_{arm}",
                entity=seed.entity,
                horizon=seed.horizon,
                lens=seed.lens,
                bias_mode=seed.bias_mode,
                theme=seed.theme,
                weight=seed.weight,
                frontier_boost=seed.frontier_boost,
                coverage_penalty=seed.coverage_penalty,
                payload=payload,
            )
    except Exception:
        pass

    clone = copy.deepcopy(seed)
    try:
        original_seed_id = _seed_attr(seed, "seed_id")
        setattr(clone, "seed_id", f"{original_seed_id}__{pair_id}_{arm}")
    except Exception:
        pass
    payload = _seed_payload(clone)
    payload.update(_experiment_pair_payload(seed, experiment_id, arm, pair_id))
    return clone


def _experiment_pair_payload(seed: Any, experiment_id: str, arm: str, pair_id: str) -> dict[str, Any]:
    return {
        "market_evolve_pair_id": pair_id,
        "market_evolve_pair_unit_key": _experiment_slice_key(seed),
        "market_evolve_original_seed_id": _seed_attr(seed, "seed_id"),
        "market_evolve_original_seed_source": _seed_payload(seed).get("source"),
        "market_evolve_forced_experiment_id": experiment_id,
        "market_evolve_forced_experiment_arm": arm,
        "source": "market_evolve_paired_experiment",
    }


def _load_experiment_by_id(experiment_id: str, *, conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM market_evolve_experiments
        WHERE id = ?
        LIMIT 1
        """,
        (experiment_id,),
    ).fetchall()
    if not rows:
        return None
    row = dict(rows[0])
    for src, dst, default in (
        ("matched_slice_json", "matched_slice", {}),
        ("arms_json", "arms", []),
        ("success_criteria_json", "success_criteria", {}),
        ("quality_flags", "quality_flags", []),
    ):
        row[dst] = _json_load(row.get(src), default)
        if src != dst:
            row.pop(src, None)
    return row


def _load_program_by_id(program_id: str, *, conn: sqlite3.Connection) -> Optional[MarketEvolveProgram]:
    if not program_id:
        return None
    _ensure_market_evolve_tables(conn)
    row = conn.execute(
        """
        SELECT *
        FROM market_evolve_programs
        WHERE id = ?
        LIMIT 1
        """,
        (program_id,),
    ).fetchone()
    return _program_from_row(row) if row else None


def _load_existing_experiment_result(
    *,
    cycle_id: str,
    experiment_id: str,
    conn: sqlite3.Connection,
) -> Optional[dict[str, Any]]:
    if not _table_exists(conn, "market_evolve_experiment_results"):
        return None
    row = conn.execute(
        """
        SELECT *
        FROM market_evolve_experiment_results
        WHERE cycle_id = ? AND experiment_id = ? AND transaction_to IS NULL
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (cycle_id, experiment_id),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    for src, dst, default in (
        ("control_metrics_json", "control_metrics", {}),
        ("candidate_metrics_json", "candidate_metrics", {}),
        ("quality_flags", "quality_flags", []),
    ):
        result[dst] = _json_load(result.get(src), default)
        if src != dst:
            result.pop(src, None)
    return result


def _load_experiment_result_history(
    *,
    experiment_id: str,
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "market_evolve_experiment_results"):
        return []
    rows = conn.execute(
        """
        SELECT *
        FROM market_evolve_experiment_results
        WHERE experiment_id = ? AND transaction_to IS NULL
        ORDER BY created_at ASC
        """,
        (experiment_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        for src, dst, default in (
            ("control_metrics_json", "control_metrics", {}),
            ("candidate_metrics_json", "candidate_metrics", {}),
            ("quality_flags", "quality_flags", []),
        ):
            d[dst] = _json_load(d.get(src), default)
            if src != dst:
                d.pop(src, None)
        out.append(d)
    return out


def _policy_application_seed_ids(
    *,
    cycle_id: str,
    program_id: str = "",
    experiment_id: str = "",
    experiment_arm: str = "",
    conn: sqlite3.Connection,
) -> list[str]:
    if not _table_exists(conn, "market_evolve_policy_applications"):
        return []
    clauses = ["cycle_id = ?", "transaction_to IS NULL"]
    params: list[Any] = [cycle_id]
    if program_id:
        clauses.append("program_id = ?")
        params.append(program_id)
    if experiment_id:
        clauses.append("experiment_id = ?")
        params.append(experiment_id)
    if experiment_arm:
        clauses.append("experiment_arm = ?")
        params.append(experiment_arm)
    rows = conn.execute(
        f"""
        SELECT seed_id
        FROM market_evolve_policy_applications
        WHERE {" AND ".join(clauses)}
        """,
        tuple(params),
    ).fetchall()
    return sorted({str(row["seed_id"]) for row in rows if row["seed_id"]})


def _governor_routing_stats(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    seed_ids: Optional[list[str]] = None,
    geometry_weights: Optional[dict[str, Any]] = None,
    routing_thresholds: Optional[dict[str, Any]] = None,
) -> dict[str, float]:
    if not _table_exists(conn, "market_evolve_policy_applications"):
        return _empty_governor_stats()
    allowed = set(seed_ids or [])
    applications = load_market_evolve_policy_applications(cycle_id=cycle_id, limit=10000, conn=conn)
    if allowed:
        applications = [row for row in applications if str(row.get("seed_id") or "") in allowed]
    governor_apps = [
        row for row in applications
        if _application_seed_source(row) == "market_map_governor"
    ]
    governor_seed_ids = sorted({
        str(row.get("seed_id") or "")
        for row in governor_apps
        if row.get("seed_id")
    })
    if not governor_seed_ids:
        return _empty_governor_stats(policy_application_count=len(applications))

    rows = _information_string_rows(conn, cycle_id, seed_ids=governor_seed_ids)
    valid_rows = _valid_string_rows(rows)
    geom = _scoped_geometry_metrics_from_strings(
        rows,
        geometry_weights=geometry_weights,
        routing_thresholds=routing_thresholds,
    )
    geom0 = geom[0] if geom else {}
    seed_to_gap: dict[str, str] = {}
    missing_surfaces: set[str] = set()
    expected_edges: set[str] = set()
    for app in governor_apps:
        applied = app.get("applied") if isinstance(app.get("applied"), dict) else {}
        seed_id = str(app.get("seed_id") or "")
        gap_id = str(applied.get("gap_id") or "")
        if seed_id and gap_id:
            seed_to_gap[seed_id] = gap_id
        missing_surfaces.update(str(x) for x in (applied.get("missing_surfaces") or []) if str(x).strip())
        expected_edges.update(str(x) for x in (applied.get("expected_edges") or []) if str(x).strip())
    string_seed_ids = {str(row.get("seed_id") or "") for row in rows if row.get("seed_id")}
    gap_ids = {gap for gap in seed_to_gap.values() if gap}
    gaps_with_strings = {
        seed_to_gap[seed_id]
        for seed_id in string_seed_ids
        if seed_id in seed_to_gap
    }
    n_seeds = float(len(governor_seed_ids))
    n_strings = float(len(rows))
    return {
        "policy_application_count": float(len(applications)),
        "governor_seed_count": n_seeds,
        "governor_seed_share": _clamp01(n_seeds / max(1.0, float(len(applications)))),
        "governor_string_count": n_strings,
        "governor_string_yield_per_seed": _clamp01(n_strings / max(1.0, n_seeds)),
        "governor_valid_string_rate": float(len(valid_rows)) / max(1.0, n_strings),
        "governor_waste_rate": _clamp01(1.0 - (n_strings / max(1.0, n_seeds))),
        "governor_gap_count": float(len(gap_ids)),
        "governor_gap_repair_rate": float(len(gaps_with_strings)) / max(1.0, float(len(gap_ids))),
        "governor_missing_surface_count": float(len(missing_surfaces)),
        "governor_expected_edge_count": float(len(expected_edges)),
        "governor_avg_source_independence": _float(geom0.get("source_independence"), 0.0),
        "governor_avg_verifier_readiness": _float(geom0.get("verifier_readiness"), 0.0),
        "governor_avg_fragility": _float(geom0.get("fragility"), 0.0),
    }


def _empty_governor_stats(*, policy_application_count: int = 0) -> dict[str, float]:
    return {
        "policy_application_count": float(policy_application_count),
        "governor_seed_count": 0.0,
        "governor_seed_share": 0.0,
        "governor_string_count": 0.0,
        "governor_string_yield_per_seed": 0.0,
        "governor_valid_string_rate": 0.0,
        "governor_waste_rate": 0.0,
        "governor_gap_count": 0.0,
        "governor_gap_repair_rate": 0.0,
        "governor_missing_surface_count": 0.0,
        "governor_expected_edge_count": 0.0,
        "governor_avg_source_independence": 0.0,
        "governor_avg_verifier_readiness": 0.0,
        "governor_avg_fragility": 0.0,
    }


def _application_seed_source(row: dict[str, Any]) -> str:
    applied = row.get("applied") if isinstance(row.get("applied"), dict) else {}
    return str(
        applied.get("seed_source")
        or applied.get("market_evolve_original_seed_source")
        or ""
    )


def _valid_string_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if not _has_any_flag(
            row,
            (
                "adversarial_decision:quarantine",
                "failed_call_as_evidence",
                "missing_mechanism",
                "missing_depth_layers",
                "no_supported_tool_refs",
            ),
        )
    ]


def _experiment_decision(
    *,
    experiment: dict[str, Any],
    control_metrics: dict[str, float],
    candidate_metrics: dict[str, float],
    control_score: float,
    candidate_score: float,
    prior_results: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    matched = dict(experiment.get("matched_slice") or {})
    criteria = dict(experiment.get("success_criteria") or {})
    min_per_arm = int(matched.get("min_seeds_per_arm") or 20)
    score_delta = round(candidate_score - control_score, 4)
    flags: list[str] = ["market_evolve_online_experiment_v1"]
    if (
        candidate_metrics.get("scout_count", 0.0) < min_per_arm
        or control_metrics.get("scout_count", 0.0) < min_per_arm
    ):
        flags.append("insufficient_sample")
        return {
            "decision": "insufficient_sample",
            "score_delta": score_delta,
            "rationale": (
                f"insufficient sample: control={control_metrics.get('scout_count', 0):.0f}, "
                f"candidate={candidate_metrics.get('scout_count', 0):.0f}, min={min_per_arm}"
            ),
            "quality_flags": flags,
        }
    gate_results = _evaluate_falsification_gates(
        criteria.get("falsification_gates") or [],
        control_metrics=control_metrics,
        candidate_metrics=candidate_metrics,
        control_score=control_score,
        candidate_score=candidate_score,
        score_delta=score_delta,
    )
    if gate_results:
        flags.append(f"falsification_gates_evaluated:{len(gate_results)}")
        for gate in gate_results:
            metric = _metric_flag_slug(str(gate.get("metric") or "unknown"))
            status = str(gate.get("status") or "")
            if status == "not_observed":
                flags.append(f"proof_gate_not_observed:{metric}")
            else:
                flags.append(
                    f"proof_gate_{'triggered' if gate.get('triggered') else 'passed'}:{metric}"
                )
    blockers: list[str] = []
    if score_delta < float(criteria.get("min_score_delta", 0.05)):
        blockers.append("score_delta_below_gate")
    if candidate_metrics.get("valid_string_rate", 0.0) < float(criteria.get("min_valid_string_rate", 0.60)):
        blockers.append("candidate_low_valid_string_rate")
    if candidate_metrics.get("avg_source_independence", 0.0) < float(criteria.get("min_source_independence", 0.45)):
        blockers.append("candidate_low_source_independence")
    if candidate_metrics.get("avg_fragility", 0.0) > float(criteria.get("max_avg_fragility", 0.65)):
        blockers.append("candidate_high_fragility")
    if candidate_metrics.get("low_ev_tool_rate", 0.0) > float(criteria.get("max_low_ev_tool_rate", 0.25)):
        blockers.append("candidate_high_low_ev_tool_rate")
    if candidate_metrics.get("tool_eval_failed_rate", 0.0) > float(criteria.get("max_tool_eval_failed_rate", 0.60)):
        blockers.append("candidate_high_tool_eval_failed_rate")
    if _cost_exploded(control_metrics, candidate_metrics):
        blockers.append("candidate_cost_explosion")
    proof_blockers = [
        gate for gate in gate_results
        if bool(gate.get("triggered"))
        and str(gate.get("decision") or "") in {
            "reject_candidate",
            "reject_or_continue_candidate",
            "needs_more_source_evidence",
        }
    ]
    for gate in proof_blockers:
        blockers.append(f"proof_gate_failed:{_metric_flag_slug(str(gate.get('metric') or 'unknown'))}")
    if blockers:
        flags.extend(blockers)
        return {
            "decision": "reject_candidate",
            "score_delta": score_delta,
            "rationale": f"candidate failed gates: {', '.join(blockers)}; delta={score_delta:.3f}",
            "quality_flags": sorted(set(flags)),
            "falsification_gate_results": gate_results,
        }
    unobserved_proof_gates = [
        gate for gate in gate_results
        if str(gate.get("status") or "") == "not_observed"
        and str(gate.get("decision") or "") in {
            "reject_candidate",
            "reject_or_continue_candidate",
            "needs_more_source_evidence",
            "inspect",
        }
    ]
    if unobserved_proof_gates:
        flags.extend([
            "hard_experiment_proof_incomplete",
            "proof_gate_observation_pending",
            *[
                f"proof_gate_not_observed_blocker:{_metric_flag_slug(str(gate.get('metric') or 'unknown'))}"
                for gate in unobserved_proof_gates
            ],
        ])
        pending_metrics = ", ".join(
            str(gate.get("metric") or "unknown")
            for gate in unobserved_proof_gates[:6]
        )
        return {
            "decision": "insufficient_proof",
            "score_delta": score_delta,
            "rationale": (
                "candidate passed measured score gates, but hard-experiment "
                f"proof metrics were not observed yet: {pending_metrics}; "
                "keep the matched experiment running instead of counting this as a win"
            ),
            "quality_flags": sorted(set(flags)),
            "falsification_gate_results": gate_results,
        }
    prior_results = list(prior_results or [])
    prior_completed = [
        r for r in prior_results
        if str(r.get("decision") or "") in {"continue_candidate", "promote_candidate", "reject_candidate"}
    ]
    prior_wins = [
        r for r in prior_completed
        if str(r.get("decision") or "") in {"continue_candidate", "promote_candidate"}
        and float(r.get("score_delta") or 0.0) >= float(criteria.get("min_score_delta", 0.05))
    ]
    min_completed = int(criteria.get("min_completed_cycles") or 1)
    min_wins = int(criteria.get("min_candidate_wins") or min_completed)
    min_consecutive = int(criteria.get("min_consecutive_candidate_wins") or min_wins)
    completed_cycles = len(prior_completed) + 1
    win_count = len(prior_wins) + 1
    consecutive_wins = _candidate_consecutive_wins(prior_completed) + 1
    if (
        completed_cycles < min_completed
        or win_count < min_wins
        or consecutive_wins < min_consecutive
    ):
        flags.extend([
            "candidate_passed_cycle_gate",
            "multi_cycle_promotion_pending",
            f"completed_cycles:{completed_cycles}",
            f"candidate_wins:{win_count}",
            f"consecutive_candidate_wins:{consecutive_wins}",
        ])
        return {
            "decision": "continue_candidate",
            "score_delta": score_delta,
            "rationale": (
                f"candidate won this cycle by {score_delta:.3f} but needs "
                f"{min_completed} completed cycles, {min_wins} wins, and "
                f"{min_consecutive} consecutive wins before promotion "
                f"(now cycles={completed_cycles}, wins={win_count}, consecutive={consecutive_wins})"
            ),
            "quality_flags": sorted(set(flags)),
            "falsification_gate_results": gate_results,
        }
    flags.append("candidate_passed_promotion_gates")
    flags.extend([
        "multi_cycle_promotion_gate_passed",
        f"completed_cycles:{completed_cycles}",
        f"candidate_wins:{win_count}",
        f"consecutive_candidate_wins:{consecutive_wins}",
    ])
    return {
        "decision": "promote_candidate",
        "score_delta": score_delta,
        "rationale": (
            f"candidate beat control by {score_delta:.3f}, passed hard gates, "
            f"and satisfied multi-cycle evidence (cycles={completed_cycles}, "
            f"wins={win_count}, consecutive={consecutive_wins})"
        ),
        "quality_flags": sorted(set(flags)),
        "falsification_gate_results": gate_results,
    }


def _evaluate_falsification_gates(
    gates: Any,
    *,
    control_metrics: dict[str, float],
    candidate_metrics: dict[str, float],
    control_score: float,
    candidate_score: float,
    score_delta: float,
) -> list[dict[str, Any]]:
    if not isinstance(gates, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in gates[:24]:
        if not isinstance(raw, dict):
            continue
        metric = str(raw.get("metric") or "").strip()
        op = str(raw.get("operator") or "").strip()
        threshold = _optional_float(raw.get("threshold"))
        if not metric or op not in {"<", "<=", ">", ">=", "==", "!="} or threshold is None:
            out.append({
                "metric": metric or "unknown",
                "operator": op or "?",
                "threshold": raw.get("threshold"),
                "observed": None,
                "triggered": False,
                "decision": raw.get("decision") or "inspect",
                "status": "unsupported_gate",
            })
            continue
        observed = _experiment_gate_metric_value(
            metric,
            control_metrics=control_metrics,
            candidate_metrics=candidate_metrics,
            control_score=control_score,
            candidate_score=candidate_score,
            score_delta=score_delta,
        )
        if not _experiment_gate_metric_observed(
            metric,
            control_metrics=control_metrics,
            candidate_metrics=candidate_metrics,
        ):
            out.append({
                "metric": metric,
                "operator": op,
                "threshold": round(threshold, 4),
                "observed": round(observed, 4),
                "triggered": False,
                "decision": raw.get("decision") or "inspect",
                "status": "not_observed",
            })
            continue
        triggered = _gate_operator_triggered(observed, op, threshold)
        out.append({
            "metric": metric,
            "operator": op,
            "threshold": round(threshold, 4),
            "observed": round(observed, 4),
            "triggered": triggered,
            "decision": raw.get("decision") or "inspect",
            "status": "triggered" if triggered else "passed",
        })
    return out


def _experiment_gate_metric_value(
    metric: str,
    *,
    control_metrics: dict[str, float],
    candidate_metrics: dict[str, float],
    control_score: float,
    candidate_score: float,
    score_delta: float,
) -> float:
    if metric == "score_delta":
        return float(score_delta)
    if metric == "candidate_score":
        return float(candidate_score)
    if metric == "control_score":
        return float(control_score)
    if metric.startswith("candidate_"):
        key = metric.removeprefix("candidate_")
        return float(candidate_metrics.get(key, 0.0))
    if metric.startswith("control_"):
        key = metric.removeprefix("control_")
        return float(control_metrics.get(key, 0.0))
    return float(candidate_metrics.get(metric, control_metrics.get(metric, 0.0)))


def _experiment_gate_metric_observed(
    metric: str,
    *,
    control_metrics: dict[str, float],
    candidate_metrics: dict[str, float],
) -> bool:
    if metric in {"score_delta", "candidate_score", "control_score"}:
        return True
    metrics = candidate_metrics if metric.startswith("candidate_") else control_metrics if metric.startswith("control_") else candidate_metrics
    key = metric.removeprefix("candidate_").removeprefix("control_")
    if "prompt" in key:
        return float(metrics.get("prompt_eval_count", 0.0)) > 0.0
    if "route_contract" in key:
        return float(metrics.get("route_contract_eval_count", 0.0)) > 0.0
    if key in {
        "fragile_verify_rate",
        "high_signal_observe_rate",
        "geometry_route_action_rate",
        "geometry_observe_rate",
        "geometry_verify_now_rate",
        "geometry_route_entropy",
        "avg_trade_scream_score",
        "frontier_candidate_rate",
    }:
        return float(metrics.get("geometry_cell_count", 0.0)) > 0.0
    if key in {"avg_source_independence", "avg_fragility", "avg_verifier_readiness"}:
        return (
            float(metrics.get("string_count", 0.0)) > 0.0
            or float(metrics.get("geometry_cell_count", 0.0)) > 0.0
        )
    return True


def _gate_operator_triggered(observed: float, operator: str, threshold: float) -> bool:
    if operator == "<":
        return observed < threshold
    if operator == "<=":
        return observed <= threshold
    if operator == ">":
        return observed > threshold
    if operator == ">=":
        return observed >= threshold
    if operator == "==":
        return observed == threshold
    if operator == "!=":
        return observed != threshold
    return False


def _optional_float(raw: Any) -> Optional[float]:
    try:
        return float(raw)
    except Exception:
        return None


def _metric_flag_slug(metric: str) -> str:
    out = []
    for ch in metric.lower():
        if ch.isalnum():
            out.append(ch)
        else:
            out.append("_")
    return "".join(out).strip("_")[:80] or "unknown"


def _candidate_consecutive_wins(results: list[dict[str, Any]]) -> int:
    count = 0
    for result in reversed(results):
        decision = str(result.get("decision") or "")
        if decision not in {"continue_candidate", "promote_candidate"}:
            break
        count += 1
    return count


def _persist_experiment_result(
    *,
    experiment: dict[str, Any],
    cycle_id: str,
    control_metrics: dict[str, float],
    candidate_metrics: dict[str, float],
    control_score: float,
    candidate_score: float,
    decision: str,
    rationale: str,
    quality_flags: list[str],
    falsification_gate_results: list[dict[str, Any]],
    conn: sqlite3.Connection,
) -> str:
    _ensure_market_evolve_tables(conn)
    now = _now()
    experiment_id = str(experiment["id"])
    result_id = "mres_" + hashlib.sha256(f"{experiment_id}|{cycle_id}".encode()).hexdigest()[:16]
    score_delta = round(candidate_score - control_score, 4)
    conn.execute(
        """
        INSERT OR REPLACE INTO market_evolve_experiment_results (
            id, experiment_id, cycle_id, parent_program_id, candidate_program_id,
            control_metrics_json, candidate_metrics_json, control_score,
            candidate_score, score_delta, decision, rationale, quality_flags,
            falsification_gate_results_json,
            created_at, valid_from, transaction_from, transaction_to
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            result_id,
            experiment_id,
            cycle_id,
            str(experiment.get("parent_program_id") or ""),
            str(experiment.get("candidate_program_id") or ""),
            json.dumps(control_metrics, sort_keys=True),
            json.dumps(candidate_metrics, sort_keys=True),
            float(control_score),
            float(candidate_score),
            score_delta,
            decision,
            rationale,
            json.dumps(quality_flags, sort_keys=True),
            json.dumps(falsification_gate_results, sort_keys=True),
            now,
            now,
            now,
        ),
    )
    conn.commit()
    return result_id


def _set_experiment_status(experiment_id: str, status: str, *, conn: sqlite3.Connection) -> None:
    now = _now()
    conn.execute(
        """
        UPDATE market_evolve_experiments
        SET status = ?, transaction_from = ?
        WHERE id = ?
        """,
        (status, now, experiment_id),
    )
    conn.commit()


def _set_mutation_status_for_child(child_program_id: str, status: str, *, conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE market_evolve_mutations
        SET status = ?
        WHERE child_program_id = ?
        """,
        (status, child_program_id),
    )
    conn.commit()


def _promote_candidate_program(
    *,
    parent: MarketEvolveProgram,
    candidate: MarketEvolveProgram,
    result: dict[str, Any],
    conn: sqlite3.Connection,
) -> None:
    parent.status = "superseded"
    parent.quality_flags = sorted(set([*parent.quality_flags, "superseded_by_market_evolve_experiment"]))
    candidate.status = "active"
    candidate.score = float(result.get("candidate_score") or candidate.score or 0.0)
    candidate.quality_flags = sorted(set([
        *candidate.quality_flags,
        "promoted_by_market_evolve_experiment",
        f"experiment_result:{result.get('id', '')}",
    ]))
    persist_market_evolve_program(parent, conn=conn)
    persist_market_evolve_program(candidate, conn=conn)


def _reject_candidate_program(
    *,
    candidate: MarketEvolveProgram,
    result: dict[str, Any],
    conn: sqlite3.Connection,
) -> None:
    candidate.status = "rejected"
    candidate.score = float(result.get("candidate_score") or candidate.score or 0.0)
    candidate.quality_flags = sorted(set([
        *candidate.quality_flags,
        "rejected_by_market_evolve_experiment",
        f"experiment_result:{result.get('id', '')}",
    ]))
    persist_market_evolve_program(candidate, conn=conn)


def _information_string_rows(
    conn: sqlite3.Connection,
    cycle_id: str,
    *,
    seed_ids: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "information_strings"):
        return []
    params: list[Any] = [cycle_id]
    seed_clause = ""
    if seed_ids:
        placeholders = ", ".join("?" for _ in seed_ids)
        seed_clause = f"AND seed_id IN ({placeholders})"
        params.extend(seed_ids)
    rows = conn.execute(
        f"""
        SELECT id, seed_id, quality_flags, evidence_refs, source_tool_call_ids,
               conviction, novelty_score, crowdedness, attention_score, cost_usd
        FROM information_strings
        WHERE cycle_id = ? AND transaction_to IS NULL
        {seed_clause}
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def _scout_prompt_stats(
    conn: sqlite3.Connection,
    cycle_id: str,
    *,
    seed_ids: Optional[list[str]] = None,
) -> dict[str, Any]:
    if not _table_exists(conn, "hypotheses"):
        return _empty_prompt_stats()
    try:
        rows = conn.execute(
            """
            SELECT payload
            FROM hypotheses
            WHERE cycle_id = ?
              AND transaction_to IS NULL
            """,
            (cycle_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return _empty_prompt_stats()
    allowed_seed_ids = set(seed_ids or [])
    evals: list[dict[str, Any]] = []
    for row in rows:
        payload = _json_load(row["payload"] if "payload" in row.keys() else None, {})
        if not isinstance(payload, dict):
            continue
        if str(payload.get("tier") or "") not in {"", "scout"}:
            continue
        seed_id = str(payload.get("seed_id") or "")
        if allowed_seed_ids and seed_id not in allowed_seed_ids:
            continue
        flags = _string_list(payload.get("quality_flags"))
        score = _prompt_quality_from_flags(flags)
        if score is None:
            continue
        prompt_flags = [
            flag.removeprefix("prompt_")
            for flag in flags
            if flag.startswith("prompt_")
        ]
        route_flags = [
            flag
            for flag in flags
            if flag.startswith("route_contract")
        ]
        evals.append({
            "score": score,
            "passed": (
                score >= ACCEPTANCE_THRESHOLD
                and "missing_information_strings" not in prompt_flags
                and "route_contract_failed" not in route_flags
            ),
            "flags": [*prompt_flags, *route_flags],
        })
    if not evals:
        return _empty_prompt_stats()
    gate = evaluate_prompt_scale_gate(evals)
    scores = [float(e["score"]) for e in evals]
    flags_flat = [
        flag
        for e in evals
        for flag in (e.get("flags") or [])
    ]
    contract_failures = sum(
        1 for e in evals
        if (
            not bool(e.get("passed"))
            or any(
                flag in {
                    "missing_information_strings",
                    "string_missing_thesis_mechanism",
                    "string_missing_expected_outcome_kill_signal",
                    "string_missing_mechanism_expected_outcome",
                    "string_missing_kill_signal_entities_chain",
                    "too_few_valid_tools",
                    "invented_tool",
                    "route_contract_failed",
                    "route_contract_no_edge_moved",
                    "route_contract_success_gate_missing",
                }
                for flag in (e.get("flags") or [])
            )
        )
    )
    route_evals = [
        e for e in evals
        if any(str(flag).startswith("route_contract_alignment:") for flag in (e.get("flags") or []))
    ]
    return {
        "n": len(evals),
        "avg_score": _avg(scores),
        "pass_rate": sum(1 for e in evals if e.get("passed")) / max(1, len(evals)),
        "contract_failure_rate": contract_failures / max(1, len(evals)),
        "invented_tool_rate": sum(1 for e in evals if "invented_tool" in (e.get("flags") or [])) / max(1, len(evals)),
        "missing_information_strings_rate": sum(1 for e in evals if "missing_information_strings" in (e.get("flags") or [])) / max(1, len(evals)),
        "scale_gate_passed": bool(gate.passed),
        "scale_gate_median_score": gate.median_score,
        "scale_gate_flags": gate.flags,
        "route_contract_eval_count": len(route_evals),
        "route_contract_success_rate": (
            sum(1 for e in route_evals if "route_contract_satisfied" in (e.get("flags") or []))
            / max(1, len(route_evals))
        ),
        "route_contract_failure_rate": (
            sum(1 for e in route_evals if "route_contract_failed" in (e.get("flags") or []))
            / max(1, len(route_evals))
        ),
        "flags": sorted(set(flags_flat)),
    }


def _empty_prompt_stats() -> dict[str, Any]:
    return {
        "n": 0,
        "avg_score": 0.0,
        "pass_rate": 0.0,
        "contract_failure_rate": 0.0,
        "invented_tool_rate": 0.0,
        "missing_information_strings_rate": 0.0,
        "scale_gate_passed": False,
        "scale_gate_median_score": 0.0,
        "scale_gate_flags": ["no_evaluations"],
        "route_contract_eval_count": 0,
        "route_contract_success_rate": 0.0,
        "route_contract_failure_rate": 0.0,
        "flags": [],
    }


def _prompt_quality_from_flags(flags: list[str]) -> Optional[float]:
    for flag in flags:
        if not flag.startswith("prompt_quality:"):
            continue
        try:
            return _clamp01(float(flag.split(":", 1)[1]))
        except Exception:
            return None
    return None


def _vote_counts(conn: sqlite3.Connection, cycle_id: str) -> dict[str, int]:
    if not _table_exists(conn, "claim_votes"):
        return {}
    rows = conn.execute(
        """
        SELECT vote, COUNT(*) AS n
        FROM claim_votes
        WHERE cycle_id = ? AND transaction_to IS NULL
        GROUP BY vote
        """,
        (cycle_id,),
    ).fetchall()
    return {str(row["vote"]): int(row["n"]) for row in rows}


def _low_ev_tool_count(conn: sqlite3.Connection, cycle_id: str) -> int:
    if not _table_exists(conn, "analysis_tool_proposals"):
        return 0
    rows = conn.execute(
        """
        SELECT *
        FROM analysis_tool_proposals
        WHERE cycle_id = ? AND transaction_to IS NULL
        """,
        (cycle_id,),
    ).fetchall()
    n = 0
    try:
        from ..tool_atlas.discovery import evaluate_analysis_tool_proposal
    except Exception:
        evaluate_analysis_tool_proposal = None  # type: ignore[assignment]
    for row in rows:
        d = dict(row)
        flags = _string_list(d.get("quality_flags"))
        purpose = str(row["purpose"] or "").lower()
        quality = evaluate_analysis_tool_proposal(d) if evaluate_analysis_tool_proposal else None
        if (
            str(row["priority"] or "").lower() == "low"
            or (quality is not None and not quality.passed)
            or any("low_ev" in f or "missing_eval" in f for f in flags)
            or "no eval" in purpose
        ):
            n += 1
    return n


def _tool_proposal_status_counts(conn: sqlite3.Connection, cycle_id: str) -> dict[str, int]:
    if not _table_exists(conn, "analysis_tool_proposals"):
        return {}
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS n
        FROM analysis_tool_proposals
        WHERE cycle_id = ? AND transaction_to IS NULL
        GROUP BY status
        """,
        (cycle_id,),
    ).fetchall()
    return {str(row["status"] or "unknown"): int(row["n"] or 0) for row in rows}


def _learned_tool_call_stats(conn: sqlite3.Connection, cycle_id: str) -> dict[str, int]:
    if not _table_exists(conn, "tool_call_log"):
        return {"calls": 0, "successes": 0}
    rows = conn.execute(
        """
        SELECT error, quality_flags
        FROM tool_call_log
        WHERE cycle_id = ?
          AND tool_uri LIKE 'tic://tool/learned/%'
          AND transaction_to IS NULL
        """,
        (cycle_id,),
    ).fetchall()
    calls = len(rows)
    successes = 0
    for row in rows:
        flags = _string_list(row["quality_flags"]) if "quality_flags" in row.keys() else []
        if not row["error"] and not any("failed" in f or "error" in f for f in flags):
            successes += 1
    return {"calls": calls, "successes": successes}


def _cortex_task_execution_stats(
    conn: sqlite3.Connection,
    cycle_id: str,
    *,
    program_id: str = "",
    experiment_id: str = "",
    experiment_arm: str = "",
    seed_scoped: bool = False,
) -> dict[str, float]:
    zero = {
        "cortex_task_count": 0.0,
        "cortex_task_completed_count": 0.0,
        "cortex_task_failed_count": 0.0,
        "cortex_task_pending_count": 0.0,
        "cortex_task_completion_rate": 0.0,
        "cortex_task_failure_rate": 0.0,
        "cortex_task_pending_rate": 0.0,
        "cortex_shape_observation_rate": 0.0,
        "cortex_observations_per_task": 0.0,
        "cortex_deferred_followup_rate": 0.0,
        "cortex_followup_execution_rate": 0.0,
        "cortex_followup_observations_per_task": 0.0,
        "cortex_shape_blocked_followup_rate": 0.0,
    }
    if not _table_exists(conn, "task_contracts"):
        return zero
    if seed_scoped and not (program_id or experiment_id or experiment_arm):
        return zero
    topics = (
        "alpha_geometry.route",
        "alpha_geometry.verify",
        "alpha_geometry.cortex",
        "market_evolve.frontier",
    )
    placeholders = ",".join("?" for _ in topics)
    rows = conn.execute(
        f"""
        SELECT id, topic, status, payload
        FROM task_contracts
        WHERE cycle_id = ?
          AND topic IN ({placeholders})
          AND transaction_to IS NULL
        """,
        (cycle_id, *topics),
    ).fetchall()
    rows = [
        row for row in rows
        if _cortex_task_matches_market_scope(
            _json_load(row["payload"], {}),
            program_id=program_id,
            experiment_id=experiment_id,
            experiment_arm=experiment_arm,
        )
    ]
    task_count = len(rows)
    if task_count <= 0:
        return zero
    status_counts: dict[str, int] = {}
    for row in rows:
        status = str(row["status"] or "")
        status_counts[status] = status_counts.get(status, 0) + 1
    completed_count = status_counts.get("completed", 0)
    failed_count = status_counts.get("failed", 0)
    pending_count = sum(status_counts.get(status, 0) for status in ("posted", "claimed", "running"))

    shape_observed = 0
    observation_count = 0
    deferred_followup_tasks = 0
    followup_observation_count = 0
    followup_executed_tasks = 0
    shape_blocked_followup_tasks = 0
    if _table_exists(conn, "blackboard_events"):
        task_ids = [str(row["id"]) for row in rows if row["id"]]
        task_placeholders = ",".join("?" for _ in task_ids)
        event_rows = conn.execute(
            f"""
            SELECT event_type, payload
            FROM blackboard_events
            WHERE cycle_id = ?
              AND topic IN ({placeholders})
              AND task_id IN ({task_placeholders})
              AND event_type IN ('task.completed', 'task.failed')
            ORDER BY occurred_at ASC
            """,
            (cycle_id, *topics, *task_ids),
        ).fetchall()
        completion_payloads = [
            _json_load(row["payload"], {})
            for row in event_rows
            if str(row["event_type"] or "") in {"task.completed", "task.failed"}
        ]
        for payload in completion_payloads:
            proof = payload.get("proof") if isinstance(payload.get("proof"), dict) else {}
            observations = payload.get("observations") if isinstance(payload.get("observations"), list) else []
            deferred = payload.get("deferred_tool_sequence") if isinstance(payload.get("deferred_tool_sequence"), list) else []
            if bool(proof.get("shape_tool_observed")):
                shape_observed += 1
            observation_count += int(proof.get("observations_logged") or len(observations) or 0)
            followup_n = int(proof.get("followup_observations_logged") or 0)
            if followup_n <= 0:
                followup_n = sum(
                    1 for obs in observations
                    if str(obs.get("phase") or "") == "cortex_followup"
                )
            followup_observation_count += followup_n
            if followup_n > 0:
                followup_executed_tasks += 1
            if bool(proof.get("followup_tools_blocked_by_shape")):
                shape_blocked_followup_tasks += 1
            if deferred:
                deferred_followup_tasks += 1

    denominator = max(1.0, float(task_count))
    return {
        "cortex_task_count": float(task_count),
        "cortex_task_completed_count": float(completed_count),
        "cortex_task_failed_count": float(failed_count),
        "cortex_task_pending_count": float(pending_count),
        "cortex_task_completion_rate": float(completed_count) / denominator,
        "cortex_task_failure_rate": float(failed_count) / denominator,
        "cortex_task_pending_rate": float(pending_count) / denominator,
        "cortex_shape_observation_rate": float(shape_observed) / denominator,
        "cortex_observations_per_task": _clamp01(float(observation_count) / denominator),
        "cortex_deferred_followup_rate": float(deferred_followup_tasks) / denominator,
        "cortex_followup_execution_rate": float(followup_executed_tasks) / denominator,
        "cortex_followup_observations_per_task": _clamp01(float(followup_observation_count) / denominator),
        "cortex_shape_blocked_followup_rate": float(shape_blocked_followup_tasks) / denominator,
    }


def _cortex_task_matches_market_scope(
    payload: dict[str, Any],
    *,
    program_id: str = "",
    experiment_id: str = "",
    experiment_arm: str = "",
) -> bool:
    if program_id and str(payload.get("market_evolve_program_id") or "") != str(program_id):
        return False
    if experiment_id and str(payload.get("market_evolve_experiment_id") or "") != str(experiment_id):
        return False
    if experiment_arm and str(payload.get("market_evolve_experiment_arm") or "") != str(experiment_arm):
        return False
    return True


def _information_price_outcome_stats(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    seed_ids: Optional[list[str]] = None,
) -> dict[str, float]:
    zero = {
        "outcome_eval_count": 0.0,
        "outcome_observed_count": 0.0,
        "outcome_observed_rate": 0.0,
        "outcome_direction_hit_rate": 0.0,
        "outcome_threshold_hit_rate": 0.0,
        "avg_realized_edge_score": 0.0,
        "avg_abs_price_return_pct": 0.0,
        "avg_signed_return_pct": 0.0,
        "early_repricing_hit_rate": 0.0,
    }
    if not _table_exists(conn, "information_string_outcomes"):
        return zero
    try:
        from .outcomes import summarize_information_price_outcomes

        summary = summarize_information_price_outcomes(
            cycle_id=cycle_id,
            seed_ids=seed_ids,
            conn=conn,
        )
    except Exception:
        return zero
    out = dict(zero)
    out.update({
        key: float(value)
        for key, value in summary.items()
        if isinstance(value, (int, float))
    })
    return out


def _information_perfusion_stats(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    seed_ids: Optional[list[str]] = None,
    string_rows: Optional[list[dict[str, Any]]] = None,
) -> dict[str, float]:
    zero = {
        "perfusion_cell_count": 0.0,
        "perfusion_routed_cell_count": 0.0,
        "perfusion_routed_cell_rate": 0.0,
        "perfusion_dilate_route_rate": 0.0,
        "perfusion_price_sensor_gap_rate": 0.0,
        "perfusion_oxygenation_gap_rate": 0.0,
        "perfusion_high_pressure_unabsorbed_rate": 0.0,
        "perfusion_high_resistance_rate": 0.0,
        "perfusion_avg_information_pressure": 0.0,
        "perfusion_avg_price_absorption": 0.0,
        "perfusion_avg_pressure_gradient": 0.0,
        "perfusion_avg_source_oxygenation": 0.0,
        "perfusion_avg_resistance": 0.0,
        "perfusion_max_dilation_score": 0.0,
        "perfusion_recommended_scouts_per_cell": 0.0,
    }
    rows_for_scope = list(string_rows or [])
    if not rows_for_scope:
        rows_for_scope = _information_string_rows(conn, cycle_id, seed_ids=seed_ids)
    if not rows_for_scope:
        return zero
    try:
        rows = load_information_perfusion(cycle_id=cycle_id, limit=512, conn=conn)
        if not rows:
            compute_information_perfusion(cycle_id=cycle_id, scout_budget=24, persist=True, conn=conn)
            rows = load_information_perfusion(cycle_id=cycle_id, limit=512, conn=conn)
    except Exception:
        return zero
    if seed_ids:
        scoped_cell_keys = {_information_cell_key(row) for row in rows_for_scope}
        rows = [row for row in rows if str(row.get("cell_key") or "") in scoped_cell_keys]
    if not rows:
        return zero
    metrics = [dict(row.get("metrics") or {}) for row in rows]
    route_directives = [str(row.get("route_directive") or "maintain") for row in rows]
    flags = [
        flag
        for row in rows
        for flag in _string_list(row.get("quality_flags"))
    ]
    n = max(1.0, float(len(rows)))
    routed_count = sum(1 for directive in route_directives if directive != "maintain")
    high_pressure_unabsorbed = sum(
        1
        for row, metric in zip(rows, metrics)
        if "information_not_absorbed_by_price" in _string_list(row.get("quality_flags"))
        or (
            _float(metric.get("pressure_gradient"), 0.0) >= 0.50
            and _float(metric.get("price_absorption"), 0.0) <= 0.35
        )
    )
    return {
        "perfusion_cell_count": float(len(rows)),
        "perfusion_routed_cell_count": float(routed_count),
        "perfusion_routed_cell_rate": float(routed_count) / n,
        "perfusion_dilate_route_rate": route_directives.count("dilate_scouts") / n,
        "perfusion_price_sensor_gap_rate": route_directives.count("attach_price_sensors") / n,
        "perfusion_oxygenation_gap_rate": route_directives.count("oxygenate_sources") / n,
        "perfusion_high_pressure_unabsorbed_rate": float(high_pressure_unabsorbed) / n,
        "perfusion_high_resistance_rate": sum(1 for flag in flags if flag == "high_resistance") / n,
        "perfusion_avg_information_pressure": _avg([
            _float(metric.get("information_pressure"), 0.0) for metric in metrics
        ]),
        "perfusion_avg_price_absorption": _avg([
            _float(metric.get("price_absorption"), 0.0) for metric in metrics
        ]),
        "perfusion_avg_pressure_gradient": _avg([
            _float(metric.get("pressure_gradient"), 0.0) for metric in metrics
        ]),
        "perfusion_avg_source_oxygenation": _avg([
            _float(metric.get("source_oxygenation"), 0.0) for metric in metrics
        ]),
        "perfusion_avg_resistance": _avg([
            _float(metric.get("resistance"), 0.0) for metric in metrics
        ]),
        "perfusion_max_dilation_score": max(
            [_float(metric.get("dilation_score"), 0.0) for metric in metrics] or [0.0]
        ),
        "perfusion_recommended_scouts_per_cell": _clamp01(
            sum(int(row.get("recommended_scouts") or 0) for row in rows) / n / 24.0
        ),
    }


def _information_cell_key(row: dict[str, Any]) -> str:
    explicit = str(row.get("coverage_cell_key") or "").strip()
    if explicit:
        return explicit
    return "|".join([
        str(row.get("entity") or "UNKNOWN"),
        str(row.get("horizon") or row.get("time_horizon") or "intraday"),
        str(row.get("lens") or "anomaly"),
        str(row.get("theme") or ""),
    ])


def _count_table(
    conn: sqlite3.Connection,
    table: str,
    where: str = "",
    params: tuple[Any, ...] = (),
) -> int:
    if not _table_exists(conn, table):
        return 0
    sql = f"SELECT COUNT(*) AS n FROM {table}"
    if where:
        sql += f" WHERE {where}"
    row = conn.execute(sql, params).fetchone()
    return int(row["n"] if row is not None else 0)


def _program_from_row(row: sqlite3.Row) -> MarketEvolveProgram:
    return MarketEvolveProgram(
        program_id=str(row["id"]),
        program_kind=str(row["program_kind"]),
        name=str(row["name"]),
        generation=int(row["generation"] or 0),
        parent_program_ids=_string_list(row["parent_program_ids_json"]),
        genome=_json_load(row["genome_json"], {}),
        objective=_json_load(row["objective_json"], {}),
        status=str(row["status"] or "candidate"),
        created_from_cycle_id=str(row["created_from_cycle_id"] or ""),
        score=float(row["score"] or 0.0),
        metrics={
            str(k): float(v)
            for k, v in (_json_load(row["metrics_json"], {}) or {}).items()
            if isinstance(v, (int, float))
        },
        quality_flags=_string_list(row["quality_flags"]),
    )


def _program_id(
    kind: str,
    name: str,
    generation: int,
    genome: dict[str, Any],
    parent_ids: list[str],
) -> str:
    raw = json.dumps(
        {
            "kind": kind,
            "name": name,
            "generation": generation,
            "genome": genome,
            "parents": sorted(parent_ids),
        },
        sort_keys=True,
    )
    return "mprog_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _policy_application_id(cycle_id: str, program_id: str, seed_id: str) -> str:
    raw = f"{cycle_id}|{program_id}|{seed_id}"
    return "mapp_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _experiment_id(cycle_id: str, parent_id: str, child_id: str) -> str:
    raw = f"{cycle_id}|{parent_id}|{child_id}"
    return "mexp_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _ensure_market_evolve_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_evolve_programs (
            id TEXT PRIMARY KEY,
            program_kind TEXT NOT NULL,
            name TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0,
            parent_program_ids_json TEXT NOT NULL DEFAULT '[]',
            genome_json TEXT NOT NULL DEFAULT '{}',
            objective_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'candidate',
            created_from_cycle_id TEXT,
            score REAL NOT NULL DEFAULT 0.0,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            quality_flags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_programs_status "
        "ON market_evolve_programs(status, program_kind, score DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_programs_generation "
        "ON market_evolve_programs(program_kind, generation DESC, score DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_evolve_evaluations (
            id TEXT PRIMARY KEY,
            program_id TEXT NOT NULL,
            cycle_id TEXT NOT NULL,
            evaluator_version TEXT NOT NULL,
            score REAL NOT NULL DEFAULT 0.0,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            baseline_metrics_json TEXT NOT NULL DEFAULT '{}',
            passed INTEGER NOT NULL DEFAULT 0,
            rationale TEXT,
            quality_flags TEXT NOT NULL DEFAULT '[]',
            evaluated_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_eval_cycle "
        "ON market_evolve_evaluations(cycle_id, score DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_eval_program "
        "ON market_evolve_evaluations(program_id, evaluated_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_evolve_mutations (
            id TEXT PRIMARY KEY,
            parent_program_id TEXT NOT NULL,
            child_program_id TEXT NOT NULL,
            cycle_id TEXT NOT NULL,
            mutation_kind TEXT NOT NULL,
            mutation_json TEXT NOT NULL DEFAULT '{}',
            rationale TEXT,
            status TEXT NOT NULL DEFAULT 'proposed',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_mut_parent "
        "ON market_evolve_mutations(parent_program_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_mut_cycle "
        "ON market_evolve_mutations(cycle_id, status)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_evolve_policy_applications (
            id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            program_id TEXT NOT NULL,
            program_name TEXT,
            generation INTEGER NOT NULL DEFAULT 0,
            seed_id TEXT NOT NULL,
            entity TEXT,
            horizon TEXT,
            lens TEXT,
            bias_mode TEXT,
            theme TEXT,
            prompt_variant TEXT,
            tool_candidate_limit INTEGER NOT NULL DEFAULT 0,
            evidence_tool_limit INTEGER NOT NULL DEFAULT 0,
            experiment_id TEXT,
            experiment_arm TEXT,
            applied_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_policy_app_cycle "
        "ON market_evolve_policy_applications(cycle_id, program_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_policy_app_seed "
        "ON market_evolve_policy_applications(seed_id, cycle_id)"
    )
    _ensure_column(conn, "market_evolve_policy_applications", "experiment_id", "TEXT")
    _ensure_column(conn, "market_evolve_policy_applications", "experiment_arm", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_policy_app_experiment "
        "ON market_evolve_policy_applications(experiment_id, experiment_arm, cycle_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_evolve_experiments (
            id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            parent_program_id TEXT NOT NULL,
            candidate_program_id TEXT NOT NULL,
            experiment_kind TEXT NOT NULL,
            matched_slice_json TEXT NOT NULL DEFAULT '{}',
            arms_json TEXT NOT NULL DEFAULT '[]',
            success_criteria_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'planned',
            rationale TEXT,
            quality_flags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_exp_cycle "
        "ON market_evolve_experiments(cycle_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_exp_candidate "
        "ON market_evolve_experiments(candidate_program_id, status)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_evolve_experiment_results (
            id TEXT PRIMARY KEY,
            experiment_id TEXT NOT NULL,
            cycle_id TEXT NOT NULL,
            parent_program_id TEXT NOT NULL,
            candidate_program_id TEXT NOT NULL,
            control_metrics_json TEXT NOT NULL DEFAULT '{}',
            candidate_metrics_json TEXT NOT NULL DEFAULT '{}',
            control_score REAL NOT NULL DEFAULT 0.0,
            candidate_score REAL NOT NULL DEFAULT 0.0,
            score_delta REAL NOT NULL DEFAULT 0.0,
            decision TEXT NOT NULL,
            rationale TEXT,
            quality_flags TEXT NOT NULL DEFAULT '[]',
            falsification_gate_results_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    _ensure_column(
        conn,
        "market_evolve_experiment_results",
        "falsification_gate_results_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_exp_result_cycle "
        "ON market_evolve_experiment_results(cycle_id, decision, score_delta)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_exp_result_experiment "
        "ON market_evolve_experiment_results(experiment_id, created_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_evolve_scoreboards (
            id TEXT PRIMARY KEY,
            cycle_id TEXT,
            status TEXT NOT NULL,
            summary TEXT,
            scoreboard_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_scoreboards_cycle "
        "ON market_evolve_scoreboards(cycle_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_scoreboards_status "
        "ON market_evolve_scoreboards(status, created_at DESC)"
    )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _has_any_flag(row: dict[str, Any], needles: tuple[str, ...]) -> bool:
    flags = _string_list(row.get("quality_flags"))
    return any(any(needle in flag for needle in needles) for flag in flags)


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _deep_merge(dict(base[key]), value)
        else:
            base[key] = value
    return base


def _mutation_proof_from_record(record: dict[str, Any]) -> dict[str, Any]:
    mutation = record.get("mutation") if isinstance(record.get("mutation"), dict) else {}
    proof = mutation.get("_evolution_proof") if isinstance(mutation.get("_evolution_proof"), dict) else {}
    return proof


def _lineage_proof_summary(proof: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(proof, dict):
        return {}
    gates = proof.get("falsification_gates") if isinstance(proof.get("falsification_gates"), list) else []
    population_gate = (
        proof.get("population_gate")
        if isinstance(proof.get("population_gate"), dict)
        else {}
    )
    return {
        "mutation_source": proof.get("mutation_source"),
        "source_candidate_rank": proof.get("source_candidate_rank"),
        "source_diagnostic_codes": proof.get("source_diagnostic_codes") or [],
        "hypothesis": proof.get("hypothesis"),
        "intended_effect": proof.get("intended_effect"),
        "kill_signal": proof.get("kill_signal"),
        "promotion_evidence_required": proof.get("promotion_evidence_required") or [],
        "falsification_gate_count": len(gates),
        "population_gate": {
            "schema_version": population_gate.get("schema_version"),
            "allowed": population_gate.get("allowed"),
            "reason": population_gate.get("reason"),
            "open_experiment_count": population_gate.get("open_experiment_count"),
            "open_same_signature_count": population_gate.get("open_same_signature_count"),
            "max_open_experiments_per_parent": population_gate.get("max_open_experiments_per_parent"),
            "max_open_same_signature_per_parent": population_gate.get("max_open_same_signature_per_parent"),
            "candidate_diversity_signature": population_gate.get("candidate_diversity_signature"),
        } if population_gate else {},
    }


def _changed_path_names(proof: dict[str, Any]) -> list[str]:
    changed = proof.get("changed_paths") if isinstance(proof.get("changed_paths"), list) else []
    out = []
    for row in changed:
        if isinstance(row, dict) and str(row.get("path") or "").strip():
            out.append(str(row["path"]))
    return out


def _latest_result_for_experiments(
    experiments: list[dict[str, Any]],
    results_by_experiment: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    for experiment in experiments:
        rows = results_by_experiment.get(str(experiment.get("id") or ""), [])
        if rows:
            return rows[0]
    return {}


def _proof_gate_summary(raw: Any) -> dict[str, Any]:
    rows = raw if isinstance(raw, list) else []
    return {
        "evaluated": len(rows),
        "passed": sum(1 for row in rows if isinstance(row, dict) and row.get("status") == "passed"),
        "triggered": sum(1 for row in rows if isinstance(row, dict) and row.get("triggered")),
        "not_observed": sum(1 for row in rows if isinstance(row, dict) and row.get("status") == "not_observed"),
        "triggered_metrics": [
            str(row.get("metric"))
            for row in rows
            if isinstance(row, dict) and row.get("triggered") and row.get("metric")
        ],
    }


def _diversity_signature(*, mutation_kind: str, changed_paths: list[str]) -> str:
    families = sorted({
        path.split(".", 1)[0]
        for path in changed_paths
        if path.strip()
    })
    raw = "|".join([mutation_kind, *families])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _market_evolve_scoreboard_id(*, cycle_id: str, generated_at: str) -> str:
    raw = f"{cycle_id or 'latest'}|{generated_at}"
    return "mscore_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _count_values(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _scoreboard_rows(rows: list[dict[str, Any]], *, include_frontier: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows[:16]:
        gate_summary = row.get("proof_gate_summary") if isinstance(row.get("proof_gate_summary"), dict) else {}
        compact = {
            "program_id": row.get("program_id"),
            "name": row.get("name"),
            "status": row.get("status"),
            "generation": row.get("generation"),
            "score": row.get("score"),
            "mutation_kind": row.get("mutation_kind"),
            "mutation_status": row.get("mutation_status"),
            "latest_experiment_id": row.get("latest_experiment_id"),
            "latest_experiment_status": row.get("latest_experiment_status"),
            "latest_decision": row.get("latest_decision"),
            "latest_score_delta": row.get("latest_score_delta"),
            "hard_gate_triggered_count": int(gate_summary.get("triggered") or 0),
            "hard_gate_not_observed_count": int(gate_summary.get("not_observed") or 0),
            "changed_paths": list(row.get("changed_paths") or [])[:8],
            "kill_signal": row.get("kill_signal"),
            "next_action": row.get("next_action"),
        }
        if include_frontier:
            compact.update({
                "frontier_priority_score": row.get("frontier_priority_score"),
                "diversity_signature": row.get("diversity_signature"),
            })
        out.append(compact)
    return out


def _scoreboard_hard_gate_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    gate_rows = [
        gate
        for result in results
        for gate in (result.get("falsification_gate_results") or [])
        if isinstance(gate, dict)
    ]
    triggered = [g for g in gate_rows if g.get("triggered")]
    not_observed = [g for g in gate_rows if g.get("status") == "not_observed"]
    passed = [g for g in gate_rows if g.get("status") == "passed"]
    return {
        "evaluated": len(gate_rows),
        "passed": len(passed),
        "triggered": len(triggered),
        "not_observed": len(not_observed),
        "triggered_metrics": sorted({
            str(g.get("metric"))
            for g in triggered
            if str(g.get("metric") or "").strip()
        }),
        "not_observed_metrics": sorted({
            str(g.get("metric"))
            for g in not_observed
            if str(g.get("metric") or "").strip()
        }),
    }


def _scoreboard_status(
    *,
    active: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    open_experiments: list[dict[str, Any]],
    promotion_candidates: list[dict[str, Any]],
    continuation_candidates: list[dict[str, Any]],
    blocked_candidates: list[dict[str, Any]],
    result_window: list[dict[str, Any]],
) -> str:
    if not active and not candidates:
        return "not_started"
    if promotion_candidates:
        return "evolving_promoted_policy"
    if continuation_candidates:
        return "learning_continue_candidate"
    if open_experiments:
        return "experiment_running"
    if blocked_candidates and not result_window:
        return "repair_needed_no_recent_evidence"
    if blocked_candidates:
        return "repair_needed"
    if candidates:
        return "candidate_waiting_for_experiment"
    if active:
        return "baseline_active"
    return "unknown"


def _scoreboard_summary(
    *,
    status: str,
    active_count: int,
    candidate_count: int,
    open_experiment_count: int,
    promotion_count: int,
    blocked_count: int,
) -> str:
    return (
        f"MarketEvolve status={status}: {active_count} active program(s), "
        f"{candidate_count} candidate(s), {open_experiment_count} open hard "
        f"experiment(s), {promotion_count} promotion candidate(s), "
        f"{blocked_count} blocked/rejected candidate(s)."
    )


def _scoreboard_evolution_memory(
    *,
    result_window: list[dict[str, Any]],
    recent_results: list[dict[str, Any]],
    open_experiment_count: int,
    mutation_count: int,
) -> dict[str, Any]:
    deltas = [float(r.get("score_delta") or 0.0) for r in result_window]
    recent_deltas = [float(r.get("score_delta") or 0.0) for r in recent_results]
    promoted = [r for r in recent_results if str(r.get("decision") or "") == "promote_candidate"]
    rejected = [r for r in recent_results if str(r.get("decision") or "") == "reject_candidate"]
    continued = [r for r in recent_results if str(r.get("decision") or "") == "continue_candidate"]
    return {
        "evolves": bool(mutation_count or recent_results or open_experiment_count),
        "mutation_count": mutation_count,
        "open_experiment_count": open_experiment_count,
        "accepted_policy_delta_count": len(promoted),
        "continued_policy_delta_count": len(continued),
        "rejected_policy_delta_count": len(rejected),
        "best_score_delta_window": round(max(deltas) if deltas else 0.0, 4),
        "best_score_delta_recent": round(max(recent_deltas) if recent_deltas else 0.0, 4),
        "latest_result_ids": [
            str(r.get("id"))
            for r in recent_results[:8]
            if str(r.get("id") or "").strip()
        ],
    }


def _scoreboard_next_actions(
    *,
    status: str,
    frontier: list[dict[str, Any]],
    open_experiments: list[dict[str, Any]],
    applications: list[dict[str, Any]],
    cycle_id: str,
    result_window: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if status == "not_started":
        actions.append({
            "action": "seed_default_market_evolve_program",
            "why": "No active research policy exists yet.",
        })
    if open_experiments and cycle_id and not applications:
        actions.append({
            "action": "apply_policy_to_paired_scout_slices",
            "why": "Open hard experiments need matched control/candidate scout evidence.",
        })
    if open_experiments and not result_window:
        actions.append({
            "action": "run_scouts_before_deciding_experiment",
            "why": "The experiment exists, but this window has no measured result yet.",
        })
    pending_proof = [
        row for row in result_window
        if str(row.get("decision") or "") == "insufficient_proof"
    ]
    if pending_proof:
        pending_metrics = sorted({
            metric
            for row in pending_proof
            for metric in _proof_gate_summary(
                row.get("falsification_gate_results") or {}
            ).get("not_observed_metrics", [])
            if str(metric).strip()
        })
        actions.append({
            "action": "collect_missing_falsification_gate_metrics",
            "why": "A candidate beat measured score gates, but promotion proof gates were not observed.",
            "metrics": pending_metrics[:8],
        })
    for row in frontier[:4]:
        action = str(row.get("next_action") or "")
        if action:
            actions.append({
                "action": action,
                "program_id": row.get("program_id"),
                "priority": row.get("frontier_priority_score"),
                "why": "Highest-priority point on the MarketEvolve frontier.",
            })
    if status.startswith("repair_needed"):
        actions.append({
            "action": "mine_failed_candidate_for_counterfactual_mutation",
            "why": "A candidate failed a hard gate or was rejected; preserve the learning signal.",
        })
    if not actions:
        actions.append({
            "action": "run_next_sentinel_tick",
            "why": "Maintain cheap information-vs-price memory until the next full pass.",
        })
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for action in actions:
        key = (str(action.get("action") or ""), str(action.get("program_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(action)
    return deduped[:8]


def _scoreboard_cadence_readiness(
    *,
    status: str,
    hard_gate_summary: dict[str, Any],
    recent_results: list[dict[str, Any]],
) -> dict[str, Any]:
    promoted_cycles = sorted({
        str(r.get("cycle_id"))
        for r in recent_results
        if str(r.get("decision") or "") == "promote_candidate"
        and str(r.get("cycle_id") or "").strip()
    })
    triggered = int(hard_gate_summary.get("triggered") or 0)
    eligible_for_shadow_review = len(promoted_cycles) >= 2 and triggered == 0
    return {
        "always_on_sentinel_allowed": status not in {"not_started"},
        "full_pipeline_plan_allowed": True,
        "scheduled_production_allowed": False,
        "eligible_for_shadow_schedule_review": eligible_for_shadow_review,
        "blocking_reason": (
            "repeat_1000_scout_shadow_pass_required"
            if not eligible_for_shadow_review
            else "human_schedule_review_required"
        ),
        "promoted_result_cycles": promoted_cycles[:8],
    }


def _market_evolve_frontier(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    signature_counts: dict[str, int] = {}
    for node in nodes:
        signature = str(node.get("diversity_signature") or "")
        if signature:
            signature_counts[signature] = signature_counts.get(signature, 0) + 1
    frontier = []
    for node in nodes:
        status = str(node.get("status") or "")
        if status not in {"active", "candidate", "rejected"}:
            continue
        gate_summary = node.get("proof_gate_summary") if isinstance(node.get("proof_gate_summary"), dict) else {}
        novelty_bonus = 1.0 / max(1.0, float(signature_counts.get(str(node.get("diversity_signature") or ""), 1)))
        decision = str(node.get("latest_decision") or "")
        score_delta = _float(node.get("latest_score_delta"), 0.0)
        priority = (
            _clamp01(float(node.get("score") or 0.0)) * 0.42
            + min(1.0, max(0.0, float(node.get("generation") or 0) / 5.0)) * 0.12
            + novelty_bonus * 0.18
            + (0.14 if status == "active" else 0.10 if status == "candidate" else 0.0)
            + (0.10 if decision == "continue_candidate" else 0.16 if decision == "promote_candidate" else 0.0)
            + _clamp01(score_delta) * 0.08
            - min(0.24, float(gate_summary.get("triggered") or 0) * 0.12)
        )
        frontier.append({
            "program_id": node.get("program_id"),
            "name": node.get("name"),
            "status": status,
            "generation": node.get("generation"),
            "score": node.get("score"),
            "mutation_kind": node.get("mutation_kind"),
            "diversity_signature": node.get("diversity_signature"),
            "mutation_source": node.get("mutation_source"),
            "source_candidate_rank": node.get("source_candidate_rank"),
            "kill_signal": node.get("kill_signal"),
            "falsification_gate_count": node.get("falsification_gate_count"),
            "population_gate": node.get("population_gate") or {},
            "frontier_priority_score": round(_clamp01(priority), 4),
            "latest_decision": decision,
            "latest_score_delta": node.get("latest_score_delta"),
            "proof_gate_summary": gate_summary,
            "next_action": _frontier_next_action(status=status, latest_decision=decision, gate_summary=gate_summary),
        })
    frontier.sort(
        key=lambda row: (
            float(row.get("frontier_priority_score") or 0.0),
            int(row.get("generation") or 0),
            str(row.get("program_id") or ""),
        ),
        reverse=True,
    )
    return frontier[:16]


def _frontier_next_action(
    *,
    status: str,
    latest_decision: str,
    gate_summary: dict[str, Any],
) -> str:
    if status == "active":
        return "mutate_active_policy"
    if status == "candidate" and latest_decision == "continue_candidate":
        return "continue_matched_experiment"
    if status == "candidate":
        return "collect_candidate_experiment_evidence"
    if int(gate_summary.get("triggered") or 0) > 0:
        return "mine_failed_candidate_for_counterfactual_mutation"
    return "archive_or_revisit_if_market_regime_changes"


def _build_mutation_evolution_proof(
    *,
    parent: MarketEvolveProgram,
    child_genome: dict[str, Any],
    mutation_kind: str,
    mutation_patch: dict[str, Any],
    metrics: dict[str, float],
    rationale: str,
    mutation_source: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    target_metrics = _mutation_target_metrics(mutation_kind, metrics)
    source = mutation_source if isinstance(mutation_source, dict) else {}
    if source.get("target_metrics"):
        target_metrics = {
            **target_metrics,
            **{
                str(k): float(metrics.get(str(k), 0.0))
                for k in (source.get("target_metrics") or {})
                if isinstance(k, str)
            },
        }
    falsification_gates = _mutation_falsification_gates(
        mutation_kind,
        metrics,
        target_metrics,
    )
    if source.get("falsification_gates"):
        falsification_gates = [
            *falsification_gates,
            *[
                gate for gate in (source.get("falsification_gates") or [])
                if isinstance(gate, dict)
            ],
        ][:12]
    return {
        "schema_version": "market_evolve_mutation_proof_v1",
        "mutation_kind": mutation_kind,
        "parent_program_id": parent.program_id,
        "parent_generation": parent.generation,
        "mutation_source": source.get("source") or "market_evolve_metric_heuristic",
        "source_schema_version": source.get("schema_version"),
        "source_candidate_rank": source.get("candidate_rank"),
        "source_diagnostic_codes": source.get("diagnostic_codes") or [],
        "source_work_order_ids": source.get("work_order_ids") or [],
        "source_shape_health": source.get("shape_health") or {},
        "hypothesis": _mutation_hypothesis(mutation_kind, rationale),
        "intended_effect": rationale,
        "target_metrics": target_metrics,
        "changed_paths": _mutation_changed_paths(
            parent_genome=parent.genome or {},
            child_genome=child_genome,
            mutation_patch=mutation_patch,
        ),
        "falsification_gates": falsification_gates,
        "kill_signal": _mutation_kill_signal(mutation_kind),
        "promotion_evidence_required": [
            "matched control/candidate seed slices",
            "multi-cycle out-of-sample candidate win",
            "hard gates passed",
            "no cost explosion",
            "lineage remains human-readable",
        ],
    }


def _mutation_hypothesis(mutation_kind: str, rationale: str) -> str:
    if mutation_kind == "retune_geometry_repair_before_verify":
        return "If fragile high-scream cells are routed to source repair before verification, verifier spend becomes more selective without losing real frontier cells."
    if mutation_kind == "retune_geometry_surface_hidden_edges":
        return "If clean high-signal observe cells receive more geometry pressure, the cortex will route hidden edges that the current map keeps quiet."
    if mutation_kind == "tighten_shape_route_contract":
        return "If routed scouts are forced to move the missing edge and success gate, geometry recurrence wastes fewer follow-up calls."
    if mutation_kind == "tighten_cortex_task_harness":
        return "If cortex tasks must claim, observe the shape, and complete before external follow-ups, the map-to-worker loop becomes reliable enough to train routing policy."
    if mutation_kind == "tighten_prompt_contract":
        return "If the scout contract is stricter, cheap model calls emit more valid decision-changing strings per dollar."
    if mutation_kind == "exploit_learned_tool_surface":
        return "If proven learned tools are exposed earlier, scouts gather more useful evidence without widening raw call count."
    if mutation_kind == "exploit_price_feedback_surface":
        return "If strings that beat price are routed back into prompt context and price-anchor tools, scouts should repeat early repricing discovery rather than merely explain moves after the fact."
    if mutation_kind == "tighten_price_feedback_contract":
        return "If failed price-matured strings force disconfirming price checks and stricter causal contracts, the next scout wave should reduce attractive but non-predictive narratives."
    if mutation_kind == "exploit_information_perfusion_pressure":
        return "If oxygenated information pressure is not yet absorbed by price, routing more scouts through perfusion context should discover repeatable early repricing setups."
    if mutation_kind == "oxygenate_information_perfusion_sources":
        return "If pressure exists but the cell is source-thin or resistant, routing scouts toward missing source families should convert fragile pressure into verifiable map edges."
    if mutation_kind == "widen_source_families":
        return "If the source surface broadens, map fragility falls and cross-source confirmation improves."
    if mutation_kind == "exploit_market_map_governor":
        return "If governor-routed gap/frontier seeds get more budget, coverage repair becomes more efficient."
    if mutation_kind == "tighten_market_map_governor":
        return "If governor routing is tightened, gap repair spends fewer scouts on low-yield cells."
    if mutation_kind == "raise_tool_promotion_discipline":
        return "If tool promotion is more disciplined, useful proposed tools reach active status faster."
    if mutation_kind == "build_runtime_adapter_surface":
        return "If runtime adapter backlog is treated as first-class work, proposed tools become executable senses instead of paperwork."
    if mutation_kind == "tighten_tool_ev":
        return "If tool requests must name expected edges and eval plans, tool creation produces less low-EV clutter."
    if mutation_kind == "raise_verifier_gate":
        return "If verifier graduation requires stronger support, high-readiness geometry converts to cleaner downstream decisions."
    if mutation_kind == "exploit_geometry_thresholds":
        return "If high-scream verified geometry gets more follow-up budget, the desk captures repeatable market-map structure."
    return rationale or f"{mutation_kind} should improve accepted unique high-quality coverage per dollar."


def _mutation_target_metrics(mutation_kind: str, metrics: dict[str, float]) -> dict[str, float]:
    target_names = {
        "retune_geometry_repair_before_verify": [
            "fragile_verify_rate",
            "avg_fragility",
            "avg_source_independence",
            "geometry_verify_now_rate",
        ],
        "retune_geometry_surface_hidden_edges": [
            "high_signal_observe_rate",
            "geometry_route_action_rate",
            "geometry_route_entropy",
            "avg_trade_scream_score",
        ],
        "tighten_shape_route_contract": [
            "route_contract_success_rate",
            "route_contract_failure_rate",
            "route_contract_eval_count",
        ],
        "tighten_cortex_task_harness": [
            "cortex_task_completion_rate",
            "cortex_shape_observation_rate",
            "cortex_task_failure_rate",
            "cortex_task_count",
        ],
        "tighten_prompt_contract": [
            "avg_prompt_quality",
            "prompt_pass_rate",
            "prompt_contract_failure_rate",
        ],
        "widen_source_families": [
            "avg_source_independence",
            "avg_fragility",
            "valid_string_rate",
        ],
        "exploit_market_map_governor": [
            "governor_string_yield_per_seed",
            "governor_valid_string_rate",
            "governor_gap_repair_rate",
        ],
        "tighten_market_map_governor": [
            "governor_waste_rate",
            "governor_valid_string_rate",
            "governor_gap_repair_rate",
        ],
        "exploit_learned_tool_surface": [
            "learned_tool_success_rate",
            "learned_tool_usage_per_scout",
            "tool_activation_rate",
        ],
        "exploit_price_feedback_surface": [
            "outcome_observed_rate",
            "outcome_direction_hit_rate",
            "outcome_threshold_hit_rate",
            "avg_realized_edge_score",
        ],
        "tighten_price_feedback_contract": [
            "outcome_observed_rate",
            "outcome_direction_hit_rate",
            "avg_realized_edge_score",
            "prompt_contract_failure_rate",
        ],
        "exploit_information_perfusion_pressure": [
            "perfusion_high_pressure_unabsorbed_rate",
            "perfusion_avg_pressure_gradient",
            "perfusion_max_dilation_score",
            "outcome_threshold_hit_rate",
        ],
        "oxygenate_information_perfusion_sources": [
            "perfusion_avg_source_oxygenation",
            "perfusion_oxygenation_gap_rate",
            "perfusion_high_resistance_rate",
            "avg_source_independence",
        ],
        "raise_tool_promotion_discipline": [
            "tool_activation_rate",
            "tool_proposal_count",
            "active_tool_proposal_count",
        ],
        "build_runtime_adapter_surface": [
            "runtime_adapter_backlog_count",
            "tool_activation_rate",
            "learned_tool_success_rate",
        ],
        "tighten_tool_ev": [
            "low_ev_tool_rate",
            "tool_eval_failed_rate",
            "tool_activation_rate",
        ],
        "raise_verifier_gate": [
            "verifier_pass_rate",
            "verifier_abstain_rate",
            "frontier_candidate_rate",
        ],
        "exploit_geometry_thresholds": [
            "avg_trade_scream_score",
            "verifier_pass_rate",
            "frontier_candidate_rate",
        ],
    }.get(mutation_kind, [
        "accepted_unique_high_quality_coverage_per_dollar",
        "valid_string_rate",
        "avg_fragility",
    ])
    return {
        name: round(float(metrics.get(name, 0.0)), 4)
        for name in target_names
    }


def _mutation_changed_paths(
    *,
    parent_genome: dict[str, Any],
    child_genome: dict[str, Any],
    mutation_patch: dict[str, Any],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def walk(prefix: list[str], patch_value: Any) -> None:
        if isinstance(patch_value, dict):
            for key, child in patch_value.items():
                walk([*prefix, str(key)], child)
            return
        before = _nested_get(parent_genome, prefix)
        after = _nested_get(child_genome, prefix)
        out.append({
            "path": ".".join(prefix),
            "before": before,
            "after": after,
        })

    walk([], mutation_patch)
    return out[:48]


def _nested_get(raw: dict[str, Any], path: list[str]) -> Any:
    value: Any = raw
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _mutation_falsification_gates(
    mutation_kind: str,
    metrics: dict[str, float],
    target_metrics: dict[str, float],
) -> list[dict[str, Any]]:
    gates: list[dict[str, Any]] = [
        {
            "metric": "score_delta",
            "operator": "<",
            "threshold": 0.05,
            "decision": "reject_or_continue_candidate",
        },
        {
            "metric": "candidate_valid_string_rate",
            "operator": "<",
            "threshold": max(0.60, float(metrics.get("valid_string_rate", 0.0))),
            "decision": "reject_candidate",
        },
    ]
    if mutation_kind == "retune_geometry_repair_before_verify":
        gates.extend([
            {
                "metric": "candidate_fragile_verify_rate",
                "operator": ">=",
                "threshold": max(0.01, float(target_metrics.get("fragile_verify_rate", 0.0))),
                "decision": "reject_candidate",
            },
            {
                "metric": "candidate_avg_source_independence",
                "operator": "<",
                "threshold": max(0.45, float(metrics.get("avg_source_independence", 0.0))),
                "decision": "needs_more_source_evidence",
            },
        ])
    elif mutation_kind == "retune_geometry_surface_hidden_edges":
        gates.extend([
            {
                "metric": "candidate_high_signal_observe_rate",
                "operator": ">=",
                "threshold": max(0.01, float(target_metrics.get("high_signal_observe_rate", 0.0))),
                "decision": "reject_candidate",
            },
            {
                "metric": "candidate_geometry_route_action_rate",
                "operator": "<=",
                "threshold": float(metrics.get("geometry_route_action_rate", 0.0)),
                "decision": "reject_candidate",
            },
        ])
    elif mutation_kind == "tighten_shape_route_contract":
        gates.extend([
            {
                "metric": "candidate_route_contract_success_rate",
                "operator": "<=",
                "threshold": float(metrics.get("route_contract_success_rate", 0.0)),
                "decision": "reject_candidate",
            },
            {
                "metric": "candidate_route_contract_failure_rate",
                "operator": ">=",
                "threshold": float(metrics.get("route_contract_failure_rate", 1.0)),
                "decision": "reject_candidate",
            },
        ])
    elif mutation_kind == "tighten_cortex_task_harness":
        gates.extend([
            {
                "metric": "candidate_cortex_task_completion_rate",
                "operator": "<=",
                "threshold": float(metrics.get("cortex_task_completion_rate", 0.0)),
                "decision": "reject_candidate",
            },
            {
                "metric": "candidate_cortex_task_failure_rate",
                "operator": ">=",
                "threshold": float(metrics.get("cortex_task_failure_rate", 1.0)),
                "decision": "reject_candidate",
            },
            {
                "metric": "candidate_cortex_shape_observation_rate",
                "operator": "<=",
                "threshold": float(metrics.get("cortex_shape_observation_rate", 0.0)),
                "decision": "needs_shape_worker_repair",
            },
        ])
    elif mutation_kind == "tighten_prompt_contract":
        gates.append({
            "metric": "candidate_avg_prompt_quality",
            "operator": "<=",
            "threshold": float(metrics.get("avg_prompt_quality", 0.0)),
            "decision": "reject_candidate",
        })
    elif "tool" in mutation_kind:
        gates.append({
            "metric": "candidate_tool_activation_rate",
            "operator": "<=",
            "threshold": float(metrics.get("tool_activation_rate", 0.0)),
            "decision": "reject_candidate",
        })
    elif mutation_kind == "exploit_price_feedback_surface":
        gates.extend([
            {
                "metric": "candidate_outcome_direction_hit_rate",
                "operator": "<",
                "threshold": max(0.60, float(metrics.get("outcome_direction_hit_rate", 0.0))),
                "decision": "reject_candidate",
            },
            {
                "metric": "candidate_avg_realized_edge_score",
                "operator": "<=",
                "threshold": float(metrics.get("avg_realized_edge_score", 0.0)),
                "decision": "reject_candidate",
            },
        ])
    elif mutation_kind == "tighten_price_feedback_contract":
        gates.extend([
            {
                "metric": "candidate_outcome_direction_hit_rate",
                "operator": "<=",
                "threshold": float(metrics.get("outcome_direction_hit_rate", 0.0)),
                "decision": "reject_candidate",
            },
            {
                "metric": "candidate_prompt_contract_failure_rate",
                "operator": ">=",
                "threshold": float(metrics.get("prompt_contract_failure_rate", 1.0)),
                "decision": "reject_candidate",
            },
        ])
    elif mutation_kind == "exploit_information_perfusion_pressure":
        gates.extend([
            {
                "metric": "candidate_perfusion_avg_pressure_gradient",
                "operator": "<",
                "threshold": max(0.35, float(metrics.get("perfusion_avg_pressure_gradient", 0.0))),
                "decision": "reject_candidate",
            },
            {
                "metric": "candidate_perfusion_max_dilation_score",
                "operator": "<",
                "threshold": max(0.55, float(metrics.get("perfusion_max_dilation_score", 0.0))),
                "decision": "reject_candidate",
            },
            {
                "metric": "candidate_outcome_threshold_hit_rate",
                "operator": "<",
                "threshold": float(metrics.get("outcome_threshold_hit_rate", 0.0)),
                "decision": "reject_candidate",
            },
        ])
    elif mutation_kind == "oxygenate_information_perfusion_sources":
        gates.extend([
            {
                "metric": "candidate_perfusion_avg_source_oxygenation",
                "operator": "<=",
                "threshold": float(metrics.get("perfusion_avg_source_oxygenation", 0.0)),
                "decision": "reject_candidate",
            },
            {
                "metric": "candidate_perfusion_high_resistance_rate",
                "operator": ">=",
                "threshold": float(metrics.get("perfusion_high_resistance_rate", 1.0)),
                "decision": "reject_candidate",
            },
        ])
    elif "governor" in mutation_kind:
        gates.append({
            "metric": "candidate_governor_gap_repair_rate",
            "operator": "<=",
            "threshold": float(metrics.get("governor_gap_repair_rate", 0.0)),
            "decision": "reject_candidate",
        })
    else:
        gates.append({
            "metric": "candidate_accepted_unique_high_quality_coverage_per_dollar",
            "operator": "<=",
            "threshold": float(metrics.get("accepted_unique_high_quality_coverage_per_dollar", 0.0)),
            "decision": "reject_candidate",
        })
    return gates


def _mutation_kill_signal(mutation_kind: str) -> str:
    if mutation_kind == "retune_geometry_repair_before_verify":
        return "Candidate still routes fragile or under-sourced cells to verifier spend, or source independence falls while score fails to beat control."
    if mutation_kind == "retune_geometry_surface_hidden_edges":
        return "Candidate leaves the same clean high-signal cells in observe or increases noisy routing without score improvement."
    if mutation_kind == "tighten_shape_route_contract":
        return "Candidate does not raise route-contract success rate or reduce missing-edge failures on matched routed seeds."
    if mutation_kind == "tighten_cortex_task_harness":
        return "Candidate does not raise cortex task completion/shape-observation rates or continues failing worker tasks."
    if mutation_kind == "tighten_prompt_contract":
        return "Candidate does not improve prompt quality/pass rate or reduces valid string yield enough to lose the experiment."
    if mutation_kind == "exploit_price_feedback_surface":
        return "Candidate does not preserve or improve price-outcome hit rate and realized edge on matched slices."
    if mutation_kind == "tighten_price_feedback_contract":
        return "Candidate still emits price-matured strings with weak directional hit rate or fails to reduce prompt/route contract failures."
    if mutation_kind == "exploit_information_perfusion_pressure":
        return "Candidate increases pressure-followup spend without preserving dilation score, early outcome hit rate, or useful price-feedback observations."
    if mutation_kind == "oxygenate_information_perfusion_sources":
        return "Candidate fails to improve source oxygenation or reduce resistance in pressured perfusion cells."
    if "tool" in mutation_kind:
        return "Candidate does not improve active/evaluated tool usage or creates more low-EV/tool-failure backlog."
    if "governor" in mutation_kind:
        return "Candidate does not improve gap repair/yield, or wastes more governor-routed scout budget."
    return "Candidate fails hard gates, loses to control out of sample, or creates cost/quality regressions."


def _scoped_geometry_metrics_from_strings(
    rows: list[dict[str, Any]],
    *,
    geometry_weights: Optional[dict[str, Any]] = None,
    routing_thresholds: Optional[dict[str, Any]] = None,
) -> list[dict[str, float]]:
    if not rows:
        return []
    flags = [
        flag
        for row in rows
        for flag in _string_list(row.get("quality_flags"))
    ]
    families = sorted({
        fam
        for row in rows
        for fam in _source_families_for_string_row(row)
        if fam
    })
    evidence_coverage = sum(
        1 for row in rows
        if _string_list(row.get("evidence_refs")) or _string_list(row.get("source_tool_call_ids"))
    ) / max(1, len(rows))
    source_entropy = _entropy_from_values(families)
    source_independence = _clamp01(source_entropy * min(1.0, len(families) / 3.0))
    convictions = [_float(row.get("conviction"), 0.0) for row in rows]
    novelties = [_float(row.get("novelty_score"), 0.0) for row in rows]
    crowdedness = [_float(row.get("crowdedness"), 0.5) for row in rows]
    attention = [_float(row.get("attention_score"), 0.0) for row in rows]
    valid_rate = 1.0 - _flag_rate(flags, (
        "adversarial_decision:quarantine",
        "failed_call_as_evidence",
        "missing_mechanism",
        "missing_depth_layers",
    ))
    fragility = _clamp01(
        (1.0 - evidence_coverage) * 0.32
        + (1.0 - source_independence) * 0.28
        + (1.0 - valid_rate) * 0.40
    )
    support_mass = _clamp01(_avg(attention) * 0.45 + _avg(convictions) * 0.35 + evidence_coverage * 0.20)
    novelty_pressure = _clamp01(_avg(novelties) * (1.0 - _avg(crowdedness)) * 1.25)
    verifier_readiness = _clamp01(
        support_mass * 0.35
        + evidence_coverage * 0.25
        + source_independence * 0.25
        + (1.0 - fragility) * 0.15
    )
    frontier_pressure = _clamp01(
        novelty_pressure * 0.42
        + source_independence * 0.24
        + support_mass * 0.20
        - fragility * 0.22
    )
    trade_scream_score = score_alpha_geometry_components(
        source_independence=source_independence,
        frontier_pressure=frontier_pressure,
        verifier_readiness=verifier_readiness,
        tension=0.0,
        support_mass=support_mass,
        fragility=fragility,
        geometry_weights=geometry_weights,
    )
    thresholds = normalize_routing_thresholds(routing_thresholds)
    return [{
        "geometry_cell_count": 1.0,
        "source_independence": round(source_independence, 4),
        "frontier_pressure": round(frontier_pressure, 4),
        "verifier_readiness": round(verifier_readiness, 4),
        "fragility": round(fragility, 4),
        "trade_scream_score": round(trade_scream_score, 4),
        "frontier_candidate_rate": (
            1.0
            if (
                trade_scream_score >= thresholds["verify_trade_scream_min"]
                and verifier_readiness >= thresholds["verify_readiness_min"]
            )
            else 0.0
        ),
        **{
            f"routing_threshold_{key}": round(value, 4)
            for key, value in thresholds.items()
        },
    }]


def _source_families_for_string_row(row: dict[str, Any]) -> list[str]:
    families: list[str] = []
    for flag in _string_list(row.get("quality_flags")):
        if flag.startswith("source_family:"):
            families.append(flag.split(":", 1)[1])
    for ref in [*_string_list(row.get("evidence_refs")), *_string_list(row.get("source_tool_call_ids"))]:
        fam = _family_from_ref(ref)
        if fam:
            families.append(fam)
    return sorted(set(families))


def _family_from_ref(ref: str) -> str:
    text = str(ref or "").lower()
    if not text:
        return ""
    if any(tok in text for tok in ("farm_grok_x_alpha", "grok", "x_search", "xai", "twitter", "x.com")):
        return "grok_x_alpha"
    if "hydromancer" in text or "wallet" in text or "builder" in text:
        return "hydromancer"
    if "hl_node" in text or "our_hl_node" in text or "reject" in text or "node" in text:
        return "our_hl_node"
    if "orderbook" in text or "funding" in text or "coinalyze" in text or "microstructure" in text:
        return "market_microstructure"
    if "web" in text or "parallel" in text or "gdelt" in text or "news" in text:
        return "web_attention"
    if "sec" in text or "filing" in text or "analyst" in text:
        return "fundamentals_filings"
    if "macro" in text or "fred" in text or "treasury" in text or "fomc" in text:
        return "macro_official"
    if "event" in text:
        return "event_store"
    return ""


def _cost_exploded(control: dict[str, float], candidate: dict[str, float]) -> bool:
    c_cost = control.get("policy_cost_usd", 0.0)
    k_cost = candidate.get("policy_cost_usd", 0.0)
    c_strings = max(1.0, control.get("string_count", 0.0))
    k_strings = max(1.0, candidate.get("string_count", 0.0))
    control_unit = c_cost / c_strings
    candidate_unit = k_cost / k_strings
    return control_unit > 0 and candidate_unit > control_unit * 1.75


def _quality_per_dollar(score: float, cost_usd: float, string_count: int) -> float:
    if cost_usd <= 0:
        return _clamp01(score) if string_count > 0 else 0.0
    return _clamp01((score * max(1, string_count)) / max(1.0, cost_usd * 1000.0))


def _hash_int(raw: str) -> int:
    return int(hashlib.sha256(str(raw).encode()).hexdigest()[:12], 16)


def _entropy_from_values(values: list[str]) -> float:
    values = [v for v in values if v]
    if len(set(values)) <= 1:
        return 0.0
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    total = float(sum(counts.values()))
    import math

    raw = -sum((n / total) * math.log(n / total) for n in counts.values())
    return _clamp01(raw / math.log(len(counts)))


def _flag_rate(flags: list[str], needles: tuple[str, ...]) -> float:
    if not flags:
        return 0.0
    hits = sum(1 for flag in flags if any(needle in flag for needle in needles))
    return min(1.0, hits / max(1, len(flags)))


def _seed_payload(seed: Any) -> dict[str, Any]:
    payload = getattr(seed, "payload", None)
    if not isinstance(payload, dict):
        payload = {}
        try:
            setattr(seed, "payload", payload)
        except Exception:
            pass
    return payload


def _seed_attr(seed: Any, name: str) -> str:
    if hasattr(seed, name):
        value = getattr(seed, name)
    elif isinstance(seed, dict):
        value = seed.get(name)
    else:
        value = None
    return str(value or "").strip()


def _int_between(raw: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(lo, min(hi, value))


def _json_load(raw: Any, default: Any) -> Any:
    if raw is None:
        return copy.deepcopy(default)
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return copy.deepcopy(default)


def _string_list(raw: Any) -> list[str]:
    parsed = _json_load(raw, raw)
    if isinstance(parsed, list):
        return [str(x).strip() for x in parsed if str(x).strip()]
    if isinstance(parsed, str) and parsed.strip():
        return [parsed.strip()]
    return []


def _merged_source_family_targets(raw: Any, additions: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in [*_string_list(raw), *additions]:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out[:12]


def _w(weights: dict[str, Any], key: str) -> float:
    return max(0.0, float(weights.get(key, 0.0) or 0.0))


def _float(raw: Any, default: float) -> float:
    try:
        return float(raw)
    except Exception:
        return default


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _clamp01(raw: float) -> float:
    return max(0.0, min(1.0, float(raw)))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "DEFAULT_MARKET_EVOLVE_GENOME",
    "EVALUATOR_VERSION",
    "MarketEvolveEvaluation",
    "MarketEvolveMutation",
    "MarketEvolveProgram",
    "MarketEvolveStep",
    "apply_market_evolve_policy_to_seeds",
    "choose_market_evolve_prompt_variant",
    "collect_market_evolve_metrics",
    "build_market_evolve_lineage",
    "build_market_evolve_scoreboard",
    "evaluate_market_evolve_program",
    "evaluate_market_evolve_experiments",
    "load_active_market_evolve_program",
    "load_market_evolve_experiment_results",
    "load_market_evolve_experiments",
    "load_market_evolve_policy_applications",
    "load_market_evolve_scoreboards",
    "load_market_evolve_programs",
    "prepare_market_evolve_experiment_seed_pairs",
    "persist_market_evolve_evaluation",
    "persist_market_evolve_mutation",
    "persist_market_evolve_program",
    "persist_market_evolve_scoreboard",
    "propose_market_evolve_mutation",
    "propose_market_evolve_mutations",
    "run_market_evolve_step",
    "score_market_evolve_metrics",
    "seed_default_market_evolve_program",
]
