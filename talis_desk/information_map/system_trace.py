"""Canonical layer-by-layer trace artifacts for information-map runs.

The static prompt viewer is intentionally beautiful and opinionated. This
module is the colder audit spine behind it: a deterministic JSON object that
records what entered each layer, what left it, what IDs were persisted, and
which downstream layer consumes those IDs.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SYSTEM_TRACE_SCHEMA_VERSION = "system_trace_v1"


PROMPT_TRACE_FILENAMES = {
    "prompt_gate_system_prompt": "prompt_gate_system_prompt.md",
    "prompt_gate_sample_output": "prompt_gate_sample_output.json",
    "prompt_gate_quality": "prompt_gate_quality.json",
    "live_scout_system_prompt": "live_scout_system_prompt.md",
    "live_scout_user_prompt": "live_scout_user_prompt.md",
    "live_scout_tool_evidence": "live_scout_tool_evidence.json",
    "live_scout_model_output": "live_scout_model_output.json",
    "live_scout_model_response_envelope": "live_scout_model_response_envelope.json",
    "live_scout_persisted_output": "live_scout_persisted_output.json",
    "live_scout_monitor_row": "live_scout_monitor_row.json",
    "alpha_geometry": "alpha_geometry.json",
    "alpha_geometry_cortex_review": "alpha_geometry_cortex_review.json",
    "evolution_control_task_dispatch": "evolution_control_task_dispatch.json",
    "cortex_task_worker_execution": "cortex_task_worker_execution.json",
    "cortex_task_feedback_evaluator": "cortex_task_feedback_evaluator.json",
    "market_evolve_step": "market_evolve_step.json",
    "shape_recurrent_loop": "shape_recurrent_loop.json",
    "market_evolve_hard_experiment": "market_evolve_hard_experiment.json",
    "market_evolve_lineage": "market_evolve_lineage.json",
    "monitor_payload": "monitor_payload.json",
    "market_universe_manifest": "market_universe_manifest.json",
    "market_map_governor": "market_map_governor.json",
    "market_map_self_healing": "market_map_self_healing.json",
    "coverage_gap_manifest": "coverage_gap_manifest.json",
}


def read_prompt_trace_artifacts(prompt_output_dir: Path | str) -> dict[str, dict[str, Any]]:
    """Read the smoke-run prompt artifacts in the same shape used by the viewer."""
    root = Path(prompt_output_dir)
    return {
        key: _read_artifact(root / filename)
        for key, filename in PROMPT_TRACE_FILENAMES.items()
    }


def build_system_trace(
    *,
    report: dict[str, Any],
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build the canonical trace for a ground-up scout/intelligence run."""
    layers = [row for row in report.get("layers", []) if isinstance(row, dict)]
    layer_by_name = {str(row.get("name") or ""): row for row in layers}

    prompt = _parse_user_prompt(_artifact_text(artifacts, "live_scout_user_prompt"))
    evidence = _artifact_list(artifacts, "live_scout_tool_evidence")
    model = _artifact_json(artifacts, "live_scout_model_output")
    persisted = _artifact_json(artifacts, "live_scout_persisted_output")
    geometry = _artifact_json(artifacts, "alpha_geometry")
    task_execution = _artifact_json(artifacts, "cortex_task_worker_execution")
    task_feedback = _artifact_json(artifacts, "cortex_task_feedback_evaluator")
    evolve = _artifact_json(artifacts, "market_evolve_step")
    recurrent = _artifact_json(artifacts, "shape_recurrent_loop")
    hard_experiment = _artifact_json(artifacts, "market_evolve_hard_experiment")
    monitor = _artifact_json(artifacts, "monitor_payload")

    live_layer = layer_by_name.get("live_scout_execution_and_monitor_join") or {}
    event_layer = layer_by_name.get("event_intelligence_ingest") or {}
    node_layer = layer_by_name.get("node_intelligence_ingest") or {}
    tool_layer = layer_by_name.get("analysis_tool_creation_iteration") or {}
    synthesis_layer = layer_by_name.get("information_synthesis_budget_gate") or {}
    geometry_layer = layer_by_name.get("alpha_geometry_market_evolve") or {}
    monitor_layer = layer_by_name.get("monitor_payload_surface") or {}

    allowed_tools = [str(x) for x in prompt.get("allowed_tools", []) if x]
    evidence_refs = _evidence_refs(evidence)
    info_ids = _string_list(
        persisted.get("information_string_ids")
        or live_layer.get("information_string_ids")
        or report.get("information_string_ids")
    )
    event_ids = _string_list(
        persisted.get("event_intelligence_ids")
        or live_layer.get("event_intelligence_ids")
        or event_layer.get("event_bundle_id")
    )
    node_ids = _string_list(
        persisted.get("node_intelligence_ids")
        or live_layer.get("node_intelligence_ids")
        or node_layer.get("node_snapshot_id")
    )
    proposal_ids = _string_list(
        persisted.get("tool_proposal_ids")
        or tool_layer.get("proposal_ids")
        or report.get("tool_proposal_ids")
    )

    top_cell = geometry.get("top_cell") if isinstance(geometry.get("top_cell"), dict) else {}
    top_metrics = top_cell.get("metrics") if isinstance(top_cell.get("metrics"), dict) else {}
    best_eval = evolve.get("best_evaluation") if isinstance(evolve.get("best_evaluation"), dict) else {}
    mutation = _first_dict(evolve.get("mutations"))
    experiment = _first_dict(evolve.get("experiment_plans"))
    first_string = _first_information_string(model=model, monitor=monitor)
    data_surface_coverage = _parse_data_surface_coverage(
        _quality_flags_from(model=model, persisted=persisted, monitor=monitor)
    )

    cell = {
        "entity": _first_non_empty(prompt.get("entity"), persisted.get("entity"), report.get("entity")),
        "horizon": _first_non_empty(prompt.get("horizon"), persisted.get("horizon")),
        "lens": _first_non_empty(prompt.get("lens"), persisted.get("lens")),
        "bias_mode": _first_non_empty(prompt.get("bias_mode"), persisted.get("bias_mode")),
        "theme": _first_non_empty(prompt.get("theme"), first_string.get("theme"), "unknown"),
        "as_of_utc": prompt.get("as_of_utc"),
    }

    final_results = {
        "status": report.get("status"),
        "verdict": _verdict(report=report, geometry=geometry, geometry_layer=geometry_layer),
        "boundary": "Routed research object, not final trade approval.",
        "layers_passed": sum(1 for row in layers if row.get("status") == "pass"),
        "layers_total": len(layers),
        "hypothesis_id": _first_non_empty(persisted.get("hypothesis_id"), live_layer.get("hypothesis_id")),
        "hypothesis": _first_non_empty(persisted.get("hypothesis_text"), model.get("hypothesis")),
        "confidence": _first_non_empty(persisted.get("confidence"), model.get("confidence")),
        "information_string_ids": info_ids,
        "first_information_string": {
            "title": first_string.get("title"),
            "thesis": first_string.get("thesis"),
            "mechanism": first_string.get("mechanism"),
            "expected_outcome": first_string.get("expected_outcome"),
            "kill_signal": first_string.get("kill_signal"),
            "entities_chain": first_string.get("entities_chain") or [],
            "conviction": first_string.get("conviction"),
            "novelty_score": first_string.get("novelty_score"),
            "crowdedness": first_string.get("crowdedness"),
        },
        "evidence_receipts": evidence_refs,
        "data_surface_coverage": data_surface_coverage,
        "monitor_summary": monitor.get("summary") if isinstance(monitor.get("summary"), dict) else {},
        "synthesis": {
            "synthesis_id": synthesis_layer.get("synthesis_id"),
            "promoted_string_ids": _string_list(synthesis_layer.get("promoted_string_ids")),
            "promoted_hypotheses": synthesis_layer.get("promoted_hypotheses"),
            "confluences": synthesis_layer.get("confluences"),
            "tensions": synthesis_layer.get("tensions"),
        },
        "geometry": {
            "top_cell_key": _first_non_empty(top_cell.get("cell_key"), geometry_layer.get("top_cell_key")),
            "route_directive": _first_non_empty(top_cell.get("route_directive"), geometry_layer.get("top_route_directive")),
            "trade_scream_score": _first_non_empty(top_metrics.get("trade_scream_score"), geometry_layer.get("top_trade_scream_score")),
            "verifier_readiness": _first_non_empty(top_metrics.get("verifier_readiness"), geometry_layer.get("top_verifier_readiness")),
            "coordinates": top_cell.get("coordinates") if isinstance(top_cell.get("coordinates"), dict) else {},
        },
        "evolution": {
            "program_id": best_eval.get("program_id"),
            "evaluation_id": best_eval.get("evaluation_id"),
            "score": best_eval.get("score"),
            "passed": best_eval.get("passed"),
            "mutation_id": mutation.get("mutation_id"),
            "mutation_kind": mutation.get("mutation_kind"),
            "experiment_id": experiment.get("id") or experiment.get("experiment_id"),
        },
        "recurrent_loop": _recurrent_loop_summary(recurrent),
        "cortex_worker": _cortex_worker_summary(task_execution),
        "cortex_feedback": _cortex_feedback_summary(task_feedback),
        "hard_experiment": _hard_experiment_summary(hard_experiment),
    }

    trace = {
        "schema_version": SYSTEM_TRACE_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cycle_id": report.get("cycle_id") or persisted.get("cycle_id"),
        "status": report.get("status"),
        "summary": {
            "one_line": final_results["verdict"],
            "cell_key": _cell_key(cell),
            "artifact_dir": report.get("artifact_dir"),
            "db_path": report.get("db_path"),
            "log_path": report.get("log_path"),
        },
        "input_packet": {
            "cell": cell,
            "seed": {
                "seed_id": persisted.get("seed_id"),
                "scout_id": _first_non_empty(persisted.get("scout_id"), live_layer.get("scout_id")),
                "prompt_variant": persisted.get("prompt_variant"),
                "model_used": _first_non_empty(persisted.get("model_used"), model.get("model_used")),
                "provider": persisted.get("provider"),
            },
            "policy": {
                "allowed_tool_count": len(allowed_tools),
                "allowed_tools": allowed_tools,
                "scale_gate": (layer_by_name.get("prompt_contract_and_scale_gate") or {}).get("scale_gate"),
                "prompt_quality": (layer_by_name.get("prompt_contract_and_scale_gate") or {}).get("prompt_quality"),
            },
            "evidence_receipts": evidence_refs,
        },
        "market_map_plan": _market_map_plan(report=report, cell=cell, prompt=prompt),
        "stage_io": _stage_io(
            report=report,
            layers=layers,
            layer_by_name=layer_by_name,
            cell=cell,
            allowed_tools=allowed_tools,
            evidence_refs=evidence_refs,
            info_ids=info_ids,
            event_ids=event_ids,
            node_ids=node_ids,
            proposal_ids=proposal_ids,
            persisted=persisted,
            model=model,
            first_string=first_string,
            final_results=final_results,
            recurrent=recurrent,
            task_execution=task_execution,
            task_feedback=task_feedback,
            hard_experiment=hard_experiment,
        ),
        "persisted_objects": _persisted_objects(
            persisted=persisted,
            first_string=first_string,
            evidence_refs=evidence_refs,
            info_ids=info_ids,
            event_ids=event_ids,
            node_ids=node_ids,
            proposal_ids=proposal_ids,
            synthesis_layer=synthesis_layer,
            geometry=geometry,
            geometry_layer=geometry_layer,
            evolve=evolve,
        ),
        "final_results": final_results,
        "artifact_manifest": _artifact_manifest(artifacts),
        "quality_flags": _trace_quality_flags(
            report=report,
            evidence_refs=evidence_refs,
            info_ids=info_ids,
            geometry=geometry,
            evolve=evolve,
            monitor=monitor,
            final_results=final_results,
        ),
    }
    return trace


def _market_map_plan(
    *,
    report: dict[str, Any],
    cell: dict[str, Any],
    prompt: dict[str, Any],
) -> dict[str, Any]:
    """Describe how Tier 0 creates market coverage and how to audit completeness."""
    try:
        from ..swarm.seed_generator import (
            BIAS_MODES,
            DEFAULT_ENTITIES,
            HORIZONS,
            LENSES,
            entity_asset_class,
            valid_lenses_for_entity,
        )
        from ..market_map.universe import build_market_universe
        from .data_substrate import DATA_SURFACES
    except Exception:
        return {
            "status": "unavailable",
            "reason": "seed_generator import failed",
            "current_cell": cell,
        }

    universe = build_market_universe(default_entities=DEFAULT_ENTITIES)
    entity_symbols = universe.entity_symbols() or list(DEFAULT_ENTITIES)
    valid_counts_by_asset_class: dict[str, int] = {}
    valid_cell_count = 0
    for entity in entity_symbols:
        asset_class = entity_asset_class(entity)
        n_valid = len(valid_lenses_for_entity(entity)) * len(HORIZONS) * len(BIAS_MODES)
        valid_counts_by_asset_class[asset_class] = valid_counts_by_asset_class.get(asset_class, 0) + n_valid
        valid_cell_count += n_valid

    default_budget = 1000
    theme_share = 0.10
    return {
        "status": "available",
        "current_cell": cell,
        "axes": {
            "entity": {
                "count": len(entity_symbols),
                "values": entity_symbols,
                "sample_values": entity_symbols[:80],
                "source": "MarketUniverseManifest: static desk watchlist plus live/snapshot Hyperliquid tradeable perps",
                "manifest": {
                    "generated_at": universe.generated_at,
                    "source_quality": universe.source_quality,
                    "source_counts": universe.source_counts,
                    "errors": list(universe.errors),
                },
            },
            "horizon": {"count": len(HORIZONS), "values": HORIZONS},
            "lens": {"count": len(LENSES), "values": LENSES},
            "bias_mode": {"count": len(BIAS_MODES), "values": BIAS_MODES},
            "theme": {
                "source": "Research Director cross-cutting themes or meta/themes_active.json",
                "current": prompt.get("theme") or cell.get("theme"),
            },
        },
        "validity": {
            "rule": "asset-class gates remove nonsensical lenses before sampling",
            "valid_cell_count": valid_cell_count,
            "valid_counts_by_asset_class": valid_counts_by_asset_class,
        },
        "sampling_policy": {
            "base_sampler": "Latin hypercube over valid entity x horizon x lens x bias cells",
            "default_seed_budget": default_budget,
            "theme_injection": f"{int(default_budget * theme_share)} dedicated theme scouts when themes exist",
            "stratified_budget": f"{default_budget - int(default_budget * theme_share)} broad-market scouts before calendar/geometry injections",
            "determinism": "rng_seed can make the slice replayable; otherwise cycle/scope time seeds the generator",
        },
        "routing_overlays": [
            {
                "name": "coverage penalty",
                "source": "coverage_log",
                "effect": "recently covered cells with no downstream publish are downweighted",
            },
            {
                "name": "topology frontier boost",
                "source": "topology_density_map",
                "effect": "sparse/frontier regions are oversampled, dense consensus regions are underweighted",
            },
            {
                "name": "alpha-geometry route seeds",
                "source": "information_geometry_snapshots from the prior cycle",
                "effect": "verify, repair, resolve-tension, widen-source, and widen-scout cells get explicit next-cycle scouts",
            },
            {
                "name": "calendar gate",
                "source": "calendar_gate / catalyst clock",
                "effect": "must-research event cells are prepended before the normal random budget",
            },
            {
                "name": "BM25 tool retrieval",
                "source": "tool_atlas",
                "effect": "each seed receives lexical candidates plus dynamic tool/source matches for its query text",
            },
        ],
        "completion_model": {
            "honest_claim": "One 1,000-scout cycle is strategic coverage, not mathematical proof that every market state was exhausted.",
            "full_map_requirement": [
                "Every valid cell has a coverage_log row with freshness and outcome state.",
                "Every active theme has dedicated cells across affected entities and horizons.",
                "Every geometry route directive is either consumed by a later seed or explicitly expired.",
                "Every high-value data surface has at least one live receipt path or a tool proposal explaining the gap.",
                "Coverage dashboards show blind zones, stale zones, crowded zones, and frontier zones separately.",
            ],
            "proof_artifacts": [
                "seed_manifest.json for generated cells",
                "coverage_log table",
                "tool_atlas retrieval receipts",
                "information_geometry_snapshots",
                "system_trace.json",
            ],
        },
        "data_source_universe": {
            "count": len(DATA_SURFACES),
            "surfaces": [
                {
                    "key": surface.key,
                    "title": surface.title,
                    "source_family": surface.source_family,
                    "status": surface.status,
                    "example_tools": list(surface.example_tools),
                    "edge_types": list(surface.edge_types),
                }
                for surface in DATA_SURFACES
            ],
            "honest_claim": (
                "This is the known source taxonomy, not every possible source on earth. "
                "Unknown or missing surfaces must become tool proposals with explicit gaps."
            ),
        },
    }


def render_system_trace_markdown(trace: dict[str, Any]) -> str:
    """Render a compact human-readable trace beside the canonical JSON."""
    final = trace.get("final_results") if isinstance(trace.get("final_results"), dict) else {}
    input_packet = trace.get("input_packet") if isinstance(trace.get("input_packet"), dict) else {}
    cell = input_packet.get("cell") if isinstance(input_packet.get("cell"), dict) else {}
    lines = [
        "# Talis System Trace",
        "",
        f"- schema: `{trace.get('schema_version')}`",
        f"- status: `{trace.get('status')}`",
        f"- cycle: `{trace.get('cycle_id')}`",
        f"- cell: `{_cell_key(cell)}`",
        f"- verdict: {final.get('verdict') or ''}",
        f"- boundary: {final.get('boundary') or ''}",
        "",
        "## Market Map Generation",
        "",
    ]
    market_map = trace.get("market_map_plan") if isinstance(trace.get("market_map_plan"), dict) else {}
    axes = market_map.get("axes") if isinstance(market_map.get("axes"), dict) else {}
    validity = market_map.get("validity") if isinstance(market_map.get("validity"), dict) else {}
    sampling = market_map.get("sampling_policy") if isinstance(market_map.get("sampling_policy"), dict) else {}
    lines.extend([
        f"- entities: `{((axes.get('entity') or {}) if isinstance(axes.get('entity'), dict) else {}).get('count')}`",
        f"- horizons: `{((axes.get('horizon') or {}) if isinstance(axes.get('horizon'), dict) else {}).get('values')}`",
        f"- lenses: `{((axes.get('lens') or {}) if isinstance(axes.get('lens'), dict) else {}).get('count')}`",
        f"- bias modes: `{((axes.get('bias_mode') or {}) if isinstance(axes.get('bias_mode'), dict) else {}).get('values')}`",
        f"- valid cells: `{validity.get('valid_cell_count')}`",
        f"- sampler: `{sampling.get('base_sampler')}`",
        f"- honest claim: {((market_map.get('completion_model') or {}) if isinstance(market_map.get('completion_model'), dict) else {}).get('honest_claim') or ''}",
        "",
        "## Data In / Data Out",
        "",
    ])
    for stage in trace.get("stage_io") or []:
        if not isinstance(stage, dict):
            continue
        lines.extend([
            f"### {stage.get('stage_id')} - {stage.get('party')}",
            f"- in: `{_short(stage.get('inputs'))}`",
            f"- out: `{_short(stage.get('outputs'))}`",
            f"- stored: `{_short(stage.get('stored'))}`",
            f"- consumer: `{stage.get('consumer') or ''}`",
            "",
        ])
    lines.extend([
        "## Final Results",
        "",
        f"- hypothesis_id: `{final.get('hypothesis_id')}`",
        f"- hypothesis: {final.get('hypothesis') or ''}",
        f"- strings: `{_short(final.get('information_string_ids'))}`",
        f"- route: `{(final.get('geometry') or {}).get('route_directive')}`",
        f"- market_evolve: `{(final.get('evolution') or {}).get('mutation_kind')}`",
        f"- recurrent_loop: `{_short(final.get('recurrent_loop'))}`",
        f"- cortex_worker: `{_short(final.get('cortex_worker'))}`",
        f"- cortex_feedback: `{_short(final.get('cortex_feedback'))}`",
        f"- hard_experiment: `{_short(final.get('hard_experiment'))}`",
        "",
        "## Stored Geometry",
        "",
    ])
    for obj in trace.get("persisted_objects") or []:
        if not isinstance(obj, dict):
            continue
        lines.append(
            f"- `{obj.get('surface')}`: `{_short(obj.get('ids'))}` -> {obj.get('consumer') or ''}"
        )
    lines.append("")
    flags = trace.get("quality_flags") or []
    lines.extend([
        "## Trace Quality",
        "",
        f"- flags: `{_short(flags)}`",
        "",
    ])
    return "\n".join(lines)


def _recurrent_loop_summary(recurrent: dict[str, Any]) -> dict[str, Any]:
    if not recurrent:
        return {
            "status": "missing",
            "shape_tool_called": False,
            "route_changed_from_observe": False,
        }
    tool_call = recurrent.get("native_tool_call") if isinstance(recurrent.get("native_tool_call"), dict) else {}
    route = recurrent.get("route_decision") if isinstance(recurrent.get("route_decision"), dict) else {}
    worker = recurrent.get("worker_assignment") if isinstance(recurrent.get("worker_assignment"), dict) else {}
    proof = recurrent.get("proof") if isinstance(recurrent.get("proof"), dict) else {}
    shape = recurrent.get("shape_observed") if isinstance(recurrent.get("shape_observed"), dict) else {}
    top_action = shape.get("top_action") if isinstance(shape.get("top_action"), dict) else {}
    cortex_next_step = (
        recurrent.get("cortex_next_step")
        if isinstance(recurrent.get("cortex_next_step"), dict)
        else {}
    )
    return {
        "status": "ready" if proof.get("tool_call_logged") and proof.get("route_changed_from_observe") else "partial",
        "shape_tool_called": bool(tool_call.get("tool_call_log_id")),
        "shape_tool_uri": tool_call.get("tool_uri"),
        "shape_tool_call_log_id": tool_call.get("tool_call_log_id"),
        "shape_status": shape.get("status"),
        "top_action": top_action.get("action"),
        "top_route_directive": top_action.get("route_directive"),
        "cortex_primary_owner": cortex_next_step.get("primary_owner"),
        "cortex_primary_action": cortex_next_step.get("primary_action"),
        "cortex_tool_sequence": _string_list(cortex_next_step.get("immediate_tool_sequence")),
        "cortex_seed_template": (
            cortex_next_step.get("seed_template")
            if isinstance(cortex_next_step.get("seed_template"), dict)
            else {}
        ),
        "route_changed_from_observe": bool(proof.get("route_changed_from_observe")),
        "emitted_seed_id": route.get("emitted_seed_id"),
        "emitted_cell": {
            "entity": route.get("entity"),
            "horizon": route.get("horizon"),
            "lens": route.get("lens"),
            "bias_mode": route.get("bias_mode"),
            "directive": route.get("directive"),
        },
        "worker_assignment": worker,
    }


def _hard_experiment_summary(episode: dict[str, Any]) -> dict[str, Any]:
    if not episode:
        return {
            "status": "missing",
            "candidate_promoted": False,
        }
    proof = episode.get("proof") if isinstance(episode.get("proof"), dict) else {}
    return {
        "status": "ready" if proof.get("candidate_program_active") else "partial",
        "experiment_id": episode.get("experiment_id"),
        "candidate_program_id": episode.get("candidate_program_id"),
        "final_decision": episode.get("final_decision"),
        "final_score_delta": episode.get("final_score_delta"),
        "cycle_count": len(episode.get("cycles") or []),
        "candidate_promoted": bool(proof.get("candidate_program_active")),
        "paired_seed_slices": bool(proof.get("paired_seed_slices")),
        "candidate_won_two_cycles": bool(proof.get("candidate_won_two_cycles")),
        "active_program_after": (episode.get("active_program_after") or {}).get("program_id")
            if isinstance(episode.get("active_program_after"), dict) else None,
    }


def _cortex_worker_summary(task_execution: dict[str, Any]) -> dict[str, Any]:
    if not task_execution:
        return {
            "status": "missing",
            "task_count": 0,
            "completed_count": 0,
            "failed_count": 0,
            "shape_tool_observed": False,
        }
    execution = (
        task_execution.get("execution")
        if isinstance(task_execution.get("execution"), dict)
        else {}
    )
    proof = task_execution.get("proof") if isinstance(task_execution.get("proof"), dict) else {}
    return {
        "status": "ready" if proof.get("worker_completed_tasks") and proof.get("shape_tool_observed") else "partial",
        "task_count": execution.get("task_count"),
        "claimed_count": execution.get("claimed_count"),
        "completed_count": execution.get("completed_count"),
        "failed_count": execution.get("failed_count"),
        "shape_tool_observed": bool(proof.get("shape_tool_observed")),
        "worker_claimed_tasks": bool(proof.get("worker_claimed_tasks")),
        "worker_completed_tasks": bool(proof.get("worker_completed_tasks")),
    }


def _cortex_feedback_summary(task_feedback: dict[str, Any]) -> dict[str, Any]:
    if not task_feedback:
        return {
            "status": "missing",
            "evaluator_saw_worker_tasks": False,
        }
    metrics = task_feedback.get("metrics") if isinstance(task_feedback.get("metrics"), dict) else {}
    proof = task_feedback.get("proof") if isinstance(task_feedback.get("proof"), dict) else {}
    return {
        "status": "ready" if proof.get("evaluator_saw_worker_tasks") else "partial",
        "score": task_feedback.get("score"),
        "task_count": metrics.get("cortex_task_count"),
        "completion_rate": metrics.get("cortex_task_completion_rate"),
        "failure_rate": metrics.get("cortex_task_failure_rate"),
        "pending_rate": metrics.get("cortex_task_pending_rate"),
        "shape_observation_rate": metrics.get("cortex_shape_observation_rate"),
        "evaluator_saw_worker_tasks": bool(proof.get("evaluator_saw_worker_tasks")),
        "worker_completion_is_rewarded": bool(proof.get("worker_completion_is_rewarded")),
        "worker_failures_are_penalizable": bool(proof.get("worker_failures_are_penalizable")),
    }


def _stage_io(
    *,
    report: dict[str, Any],
    layers: list[dict[str, Any]],
    layer_by_name: dict[str, dict[str, Any]],
    cell: dict[str, Any],
    allowed_tools: list[str],
    evidence_refs: list[str],
    info_ids: list[str],
    event_ids: list[str],
    node_ids: list[str],
    proposal_ids: list[str],
    persisted: dict[str, Any],
    model: dict[str, Any],
    first_string: dict[str, Any],
    final_results: dict[str, Any],
    recurrent: dict[str, Any],
    task_execution: dict[str, Any],
    task_feedback: dict[str, Any],
    hard_experiment: dict[str, Any],
) -> list[dict[str, Any]]:
    live_layer = layer_by_name.get("live_scout_execution_and_monitor_join") or {}
    event_layer = layer_by_name.get("event_intelligence_ingest") or {}
    node_layer = layer_by_name.get("node_intelligence_ingest") or {}
    tool_layer = layer_by_name.get("analysis_tool_creation_iteration") or {}
    synthesis_layer = layer_by_name.get("information_synthesis_budget_gate") or {}
    geometry_layer = layer_by_name.get("alpha_geometry_market_evolve") or {}
    recurrent_layer = layer_by_name.get("shape_recurrent_loop") or {}
    worker_layer = layer_by_name.get("cortex_task_worker_execution") or {}
    feedback_layer = layer_by_name.get("cortex_task_feedback_evaluator") or {}
    hard_experiment_layer = layer_by_name.get("market_evolve_hard_experiment") or {}
    monitor_layer = layer_by_name.get("monitor_payload_surface") or {}
    prompt_layer = layer_by_name.get("prompt_contract_and_scale_gate") or {}
    schema_layer = layer_by_name.get("schema_migrations") or {}

    def stage(
        stage_id: str,
        party: str,
        inputs: Any,
        outputs: Any,
        *,
        stored: Any = None,
        consumer: str = "",
        source_artifacts: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "stage_id": stage_id,
            "party": party,
            "inputs": inputs,
            "outputs": outputs,
            "stored": stored,
            "consumer": consumer,
            "source_artifacts": source_artifacts or [],
        }

    return [
        stage(
            "00_schema",
            "Schema Migrator",
            {"required_tables": schema_layer.get("required_tables")},
            {"schema_version": schema_layer.get("schema_version"), "status": schema_layer.get("status")},
            stored={"db_path": report.get("db_path")},
            consumer="all downstream persistence",
        ),
        stage(
            "01_prompt_gate",
            "Prompt Lab",
            {"prompt_contract": "deep_scout_prompt", "sample_size": (prompt_layer.get("scale_gate") or {}).get("n")},
            {"quality": prompt_layer.get("prompt_quality"), "scale_gate": prompt_layer.get("scale_gate")},
            stored={"artifacts": ["prompt_gate_system_prompt", "prompt_gate_sample_output", "prompt_gate_quality"]},
            consumer="scout scale-up controller",
            source_artifacts=["prompt_gate_system_prompt", "prompt_gate_sample_output", "prompt_gate_quality"],
        ),
        stage(
            "02_seed_slice",
            "Seed Router",
            {"cycle_id": report.get("cycle_id"), "market_grid": "entity x horizon x lens x bias x theme"},
            {"seed_id": persisted.get("seed_id"), "cell": cell},
            stored={"coverage_cell_key": first_string.get("coverage_cell_key")},
            consumer="tool atlas and scout harness",
        ),
        stage(
            "03_tool_harness",
            "Tool Atlas + Harness",
            {"cell": cell, "allowed_tools": allowed_tools},
            {"tool_call_log_ids": evidence_refs, "receipt_count": len(evidence_refs)},
            stored={"event_bundle_ids": event_ids, "node_snapshot_ids": node_ids},
            consumer="model prompt and source provenance graph",
            source_artifacts=["live_scout_tool_evidence"],
        ),
        stage(
            "04_model_call",
            "Deep Scout Model",
            {
                "system_prompt": "live_scout_system_prompt.md",
                "user_prompt": "live_scout_user_prompt.md",
                "evidence_refs": evidence_refs,
            },
            {
                "hypothesis": model.get("hypothesis"),
                "information_strings": len(model.get("information_strings") or []),
                "suggested_tools": model.get("suggested_tools"),
                "tool_requests": model.get("tool_requests"),
            },
            stored={"response_envelope": "live_scout_model_response_envelope.json"},
            consumer="normalizer and persistor",
            source_artifacts=["live_scout_system_prompt", "live_scout_user_prompt", "live_scout_model_output"],
        ),
        stage(
            "05_persistence",
            "Normalizer + Persistor",
            {"model_json": "live_scout_model_output", "review_flags": persisted.get("quality_flags")},
            {
                "scout_id": _first_non_empty(persisted.get("scout_id"), live_layer.get("scout_id")),
                "hypothesis_id": final_results.get("hypothesis_id"),
                "information_string_ids": info_ids,
                "event_intelligence_ids": event_ids,
                "node_intelligence_ids": node_ids,
                "tool_proposal_ids": proposal_ids,
            },
            stored={
                "hypotheses": final_results.get("hypothesis_id"),
                "information_strings": info_ids,
                "market_event_intelligence": event_ids,
                "node_intelligence_snapshots": node_ids,
                "analysis_tool_proposals": proposal_ids,
            },
            consumer="monitor, synthesis, geometry, verifier",
            source_artifacts=["live_scout_persisted_output", "live_scout_monitor_row"],
        ),
        stage(
            "06_tool_creation",
            "Analysis Tool Creator",
            {"quality_flags": tool_layer.get("quality_flags"), "existing_proposals": proposal_ids},
            {"learned_tool": tool_layer.get("learned_tool"), "iterated_proposal_ids": tool_layer.get("iterated_proposal_ids")},
            stored={"analysis_tool_proposals": proposal_ids},
            consumer="learned runtime and future scout atlas retrieval",
        ),
        stage(
            "07_attention",
            "Information Synthesis Gate",
            {"cycle_strings": synthesis_layer.get("strings"), "information_string_ids": info_ids},
            {
                "synthesis_id": synthesis_layer.get("synthesis_id"),
                "confluences": synthesis_layer.get("confluences"),
                "tensions": synthesis_layer.get("tensions"),
                "promoted_hypotheses": synthesis_layer.get("promoted_hypotheses"),
            },
            stored={"information_syntheses": synthesis_layer.get("synthesis_id")},
            consumer="verifier and specialist budget router",
        ),
        stage(
            "08_geometry",
            "Alpha Geometry",
            {"strings": info_ids, "source_families": _source_families(first_string)},
            final_results.get("geometry"),
            stored={"information_geometry_snapshots": (final_results.get("geometry") or {}).get("top_cell_key")},
            consumer="next-cycle seed directives and trade-scream scanner",
            source_artifacts=["alpha_geometry"],
        ),
        stage(
            "09_evolution",
            "MarketEvolve Evaluator",
            {"geometry": final_results.get("geometry"), "layer_results": len(layers)},
            final_results.get("evolution"),
            stored={"market_evolve_tables": ["programs", "evaluations", "mutations", "experiments"]},
            consumer="research policy selection and A/B harness",
            source_artifacts=["market_evolve_step"],
        ),
        stage(
            "10_shape_recurrent_loop",
            "Cortex Shape Reader",
            {
                "geometry": final_results.get("geometry"),
                "native_tool": "tic://tool/talis_native/plan_alpha_geometry_actions@v1",
            },
            final_results.get("recurrent_loop"),
            stored={
                "tool_call_log_id": recurrent_layer.get("shape_tool_call_log_id")
                    or ((recurrent.get("native_tool_call") or {}).get("tool_call_log_id")
                        if isinstance(recurrent.get("native_tool_call"), dict) else None),
                "artifact": recurrent_layer.get("artifact"),
            },
            consumer="next-cycle seed router and market-map self-healing workers",
            source_artifacts=["shape_recurrent_loop"],
        ),
        stage(
            "10b_cortex_task_worker",
            "Cortex Task Worker",
            {
                "shape_reader": final_results.get("recurrent_loop"),
                "dispatchable_work_orders": True,
            },
            final_results.get("cortex_worker"),
            stored={
                "artifact": worker_layer.get("artifact"),
                "completed_count": worker_layer.get("completed_count")
                    or ((task_execution.get("execution") or {}).get("completed_count")
                        if isinstance(task_execution.get("execution"), dict) else None),
            },
            consumer="MarketEvolve worker-feedback evaluator",
            source_artifacts=["cortex_task_worker_execution"],
        ),
        stage(
            "10c_cortex_feedback",
            "Cortex Worker Feedback Evaluator",
            {"worker_outcome": final_results.get("cortex_worker")},
            final_results.get("cortex_feedback"),
            stored={"artifact": feedback_layer.get("artifact")},
            consumer="MarketEvolve objective score and harness-policy mutation",
            source_artifacts=["cortex_task_feedback_evaluator"],
        ),
        stage(
            "11_hard_experiment",
            "MarketEvolve Hard Experiment",
            {
                "experiment_id": hard_experiment_layer.get("experiment_id")
                    or hard_experiment.get("experiment_id"),
                "candidate_program_id": hard_experiment_layer.get("candidate_program_id")
                    or hard_experiment.get("candidate_program_id"),
            },
            final_results.get("hard_experiment"),
            stored={
                "artifact": hard_experiment_layer.get("artifact"),
                "experiment_results": [
                    ((cycle.get("result") or {}).get("id") if isinstance(cycle.get("result"), dict) else None)
                    for cycle in (hard_experiment.get("cycles") or [])
                    if isinstance(cycle, dict)
                ],
            },
            consumer="active research-policy selector and next mutation cycle",
            source_artifacts=["market_evolve_hard_experiment"],
        ),
        stage(
            "12_monitor",
            "Monitor + Product Surface",
            {"stored_ids": {"strings": info_ids, "events": event_ids, "node": node_ids, "tools": proposal_ids}},
            {"summary": monitor_layer.get("summary") or final_results.get("monitor_summary")},
            stored={"monitor_payload": monitor_layer.get("monitor_payload_artifact")},
            consumer="phone visualization and human audit",
            source_artifacts=["monitor_payload"],
        ),
    ]


def _persisted_objects(
    *,
    persisted: dict[str, Any],
    first_string: dict[str, Any],
    evidence_refs: list[str],
    info_ids: list[str],
    event_ids: list[str],
    node_ids: list[str],
    proposal_ids: list[str],
    synthesis_layer: dict[str, Any],
    geometry: dict[str, Any],
    geometry_layer: dict[str, Any],
    evolve: dict[str, Any],
) -> list[dict[str, Any]]:
    top_cell = geometry.get("top_cell") if isinstance(geometry.get("top_cell"), dict) else {}
    best_eval = evolve.get("best_evaluation") if isinstance(evolve.get("best_evaluation"), dict) else {}
    mutation = _first_dict(evolve.get("mutations"))
    experiment = _first_dict(evolve.get("experiment_plans"))
    return [
        {
            "surface": "hypotheses",
            "ids": _string_list(persisted.get("hypothesis_id")),
            "geometry": "claim point",
            "consumer": "dedup, verifier, monitor",
        },
        {
            "surface": "information_strings",
            "ids": info_ids,
            "geometry": "causal vector with horizon, novelty, conviction, crowdedness",
            "consumer": "synthesis, alpha geometry, context retrieval",
        },
        {
            "surface": "information_string_evidence",
            "ids": evidence_refs,
            "geometry": "source edges from string to tool receipt",
            "consumer": "audit, verifier, source repair",
        },
        {
            "surface": "information_map_nodes_edges",
            "ids": first_string.get("entities_chain") or [],
            "geometry": "typed path across entity, actor, mechanism, and theme",
            "consumer": "neighbor retrieval and frontier-gap scanning",
        },
        {
            "surface": "market_event_intelligence",
            "ids": event_ids,
            "geometry": "event-time node with scenarios and watch triggers",
            "consumer": "event scouts and monitor",
        },
        {
            "surface": "node_intelligence_snapshots",
            "ids": node_ids,
            "geometry": "node-native observation field",
            "consumer": "node discovery, mempool/tool proposals, source independence scoring",
        },
        {
            "surface": "analysis_tool_proposals",
            "ids": proposal_ids,
            "geometry": "missing-capability vector",
            "consumer": "tool builder and learned runtime",
        },
        {
            "surface": "information_syntheses",
            "ids": _string_list(synthesis_layer.get("synthesis_id")),
            "geometry": "attention clusters and tensions",
            "consumer": "verifier budget gate",
        },
        {
            "surface": "information_geometry_snapshots",
            "ids": _string_list(top_cell.get("cell_key") or geometry_layer.get("top_cell_key")),
            "geometry": "market cell coordinates plus route directive",
            "consumer": "next-cycle seed planning and visual scanner",
        },
        {
            "surface": "market_evolve_lineage",
            "ids": _string_list([best_eval.get("evaluation_id"), mutation.get("mutation_id"), experiment.get("id")]),
            "geometry": "research-policy fitness and mutation path",
            "consumer": "prompt/tool/routing evolution",
        },
    ]


def _trace_quality_flags(
    *,
    report: dict[str, Any],
    evidence_refs: list[str],
    info_ids: list[str],
    geometry: dict[str, Any],
    evolve: dict[str, Any],
    monitor: dict[str, Any],
    final_results: dict[str, Any],
) -> list[str]:
    flags: list[str] = []
    if report.get("status") != "pass":
        flags.append("report_not_pass")
    if not final_results.get("hypothesis_id"):
        flags.append("missing_hypothesis_id")
    if not info_ids:
        flags.append("missing_information_string_ids")
    if not evidence_refs:
        flags.append("missing_evidence_receipts")
    if not geometry:
        flags.append("missing_alpha_geometry")
    if not evolve:
        flags.append("missing_market_evolve")
    if not monitor:
        flags.append("missing_monitor_payload")
    failed = [
        str(row.get("name") or "unknown")
        for row in report.get("layers", [])
        if isinstance(row, dict) and row.get("status") != "pass"
    ]
    flags.extend(f"failed_layer:{name}" for name in failed)
    return sorted(set(flags))


def _artifact_manifest(artifacts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key, artifact in artifacts.items():
        if not isinstance(artifact, dict):
            continue
        out.append({
            "key": key,
            "filename": artifact.get("filename"),
            "path": artifact.get("path"),
            "kind": artifact.get("kind"),
            "bytes": artifact.get("bytes") or 0,
            "present": bool(artifact.get("bytes")),
        })
    return sorted(out, key=lambda row: str(row.get("key") or ""))


def _read_artifact(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    parsed: Any = None
    if path.suffix == ".json" and text:
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
    return {
        "path": str(path),
        "filename": path.name,
        "kind": path.suffix.lstrip(".") or "text",
        "text": text,
        "json": parsed,
        "bytes": len(text.encode("utf-8")),
    }


def _artifact_text(artifacts: dict[str, dict[str, Any]], key: str) -> str:
    return str((artifacts.get(key) or {}).get("text") or "")


def _artifact_json(artifacts: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
    raw = (artifacts.get(key) or {}).get("json")
    return raw if isinstance(raw, dict) else {}


def _artifact_list(artifacts: dict[str, dict[str, Any]], key: str) -> list[Any]:
    raw = (artifacts.get(key) or {}).get("json")
    return raw if isinstance(raw, list) else []


def _parse_user_prompt(prompt_text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    allowed: list[str] = []
    in_allowed = False
    for raw in prompt_text.splitlines():
        line = raw.strip()
        if line.startswith("as_of_utc="):
            out["as_of_utc"] = line.split("=", 1)[1]
        if line in {"allowed_tool_candidates:", "allowed_tool_candidates"}:
            in_allowed = True
            continue
        if in_allowed:
            if not line or line.startswith("Return "):
                in_allowed = False
            elif line.startswith("tic://"):
                allowed.append(line)
        for key in ("entity", "horizon", "lens", "bias_mode", "theme"):
            prefix = f"{key}="
            if line.startswith(prefix):
                out[key] = line.split("=", 1)[1]
    out["allowed_tools"] = allowed
    return out


def _first_information_string(*, model: dict[str, Any], monitor: dict[str, Any]) -> dict[str, Any]:
    model_strings = model.get("information_strings")
    if isinstance(model_strings, list):
        for item in model_strings:
            if isinstance(item, dict):
                return item
    monitor_strings = monitor.get("strings")
    if isinstance(monitor_strings, list):
        for item in monitor_strings:
            if isinstance(item, dict):
                return item
    return {}


def _evidence_refs(evidence: list[Any]) -> list[str]:
    refs: list[str] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        ref = item.get("tool_call_log_id") or item.get("uri")
        if ref:
            refs.append(str(ref))
    return refs


def _quality_flags_from(*, model: dict[str, Any], persisted: dict[str, Any], monitor: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    for raw in [persisted.get("quality_flags"), model.get("quality_flags")]:
        flags.extend(_string_list(raw))
    for item in monitor.get("strings") or []:
        if isinstance(item, dict):
            flags.extend(_string_list(item.get("quality_flags")))
    return flags


def _parse_data_surface_coverage(flags: list[str]) -> dict[str, Any]:
    for flag in flags:
        text = str(flag)
        prefix = "data_surface_coverage:"
        if not text.startswith(prefix):
            continue
        value = text[len(prefix):]
        parts = value.split("/", 1)
        if len(parts) == 2:
            try:
                touched = int(parts[0])
                total = int(parts[1])
            except ValueError:
                break
            return {"touched": touched, "total": total, "label": value}
    return {"touched": None, "total": None, "label": ""}


def _source_families(first_string: dict[str, Any]) -> list[str]:
    families: list[str] = []
    for flag in _string_list(first_string.get("quality_flags")):
        if flag.startswith("source_family:"):
            families.append(flag.split(":", 1)[1])
    return sorted(set(families))


def _verdict(
    *,
    report: dict[str, Any],
    geometry: dict[str, Any],
    geometry_layer: dict[str, Any],
) -> str:
    route = (
        (geometry.get("top_cell") or {}).get("route_directive")
        if isinstance(geometry.get("top_cell"), dict)
        else None
    ) or geometry_layer.get("top_route_directive") or "observe"
    if report.get("status") == "pass":
        return f"Ground-up scout smoke passed and routed the cell to {str(route).replace('_', ' ')}."
    return "Ground-up scout smoke did not pass; inspect failed layers before trusting outputs."


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, (list, tuple, set)):
        return [str(x) for x in raw if x not in (None, "")]
    return [str(raw)]


def _first_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                return item
    if isinstance(raw, dict):
        return raw
    return {}


def _cell_key(cell: dict[str, Any]) -> str:
    return "|".join(
        str(cell.get(key) or "?")
        for key in ("entity", "horizon", "lens", "bias_mode", "theme")
    )


def _short(value: Any, limit: int = 320) -> str:
    try:
        text = json.dumps(value, sort_keys=True, default=str)
    except Exception:
        text = str(value)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "..."
    return text
