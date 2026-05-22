#!/usr/bin/env python
"""Summarize a live scout canary/shadow run for review and phone viewers."""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any


def main() -> int:
    args = _parse_args()
    prompt_dir = _resolve_prompt_dir(Path(args.path).expanduser().resolve())
    audit = build_audit(prompt_dir)
    out_json = prompt_dir / "live_scout_performance_audit.json"
    out_md = prompt_dir / "live_scout_performance_audit.md"
    out_json.write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")
    out_md.write_text(render_markdown(audit), encoding="utf-8")
    print(f"LIVE_SCOUT_AUDIT_JSON={out_json}")
    print(f"LIVE_SCOUT_AUDIT_MD={out_md}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="Run artifact directory or prompt_outputs directory.")
    return parser.parse_args()


def _resolve_prompt_dir(path: Path) -> Path:
    if path.name == "prompt_outputs":
        return path
    candidate = path / "prompt_outputs"
    if candidate.exists():
        return candidate
    return path


def build_audit(prompt_dir: Path) -> dict[str, Any]:
    canary = _read_json(prompt_dir / "live_scout_canary_report.json")
    guarded = _read_json(prompt_dir / "guarded_shadow_production_report.json")
    transcript = _read_json(prompt_dir / "live_scout_canary_transcript_progress.json")
    outputs = _read_json(prompt_dir / "live_scout_canary_outputs.json")

    metrics = canary.get("metrics") if isinstance(canary.get("metrics"), dict) else {}
    scouts = metrics.get("scouts") if isinstance(metrics.get("scouts"), dict) else {}
    info = metrics.get("information_map") if isinstance(metrics.get("information_map"), dict) else {}
    geometry = metrics.get("geometry") if isinstance(metrics.get("geometry"), dict) else {}
    coverage = metrics.get("coverage") if isinstance(metrics.get("coverage"), dict) else {}
    seeds = metrics.get("seeds") if isinstance(metrics.get("seeds"), dict) else {}
    preflight = canary.get("preflight") if isinstance(canary.get("preflight"), dict) else {}
    verdict = canary.get("verdict") if isinstance(canary.get("verdict"), dict) else {}
    scale = canary.get("scale_decision") if isinstance(canary.get("scale_decision"), dict) else {}
    safety = guarded.get("safety_policy") if isinstance(guarded.get("safety_policy"), dict) else {}
    refreshed = guarded.get("refreshed_tournament") if isinstance(guarded.get("refreshed_tournament"), dict) else {}

    db_path = Path(str(canary.get("db_path") or prompt_dir.parent / "desk-live-canary.db"))
    db = _sqlite_summary(db_path)
    transcript_summary = _transcript_summary(transcript)
    output_summary = _output_summary(outputs)
    ready_for_shadow = (
        verdict.get("status") == "pass"
        and guarded.get("status") in {"pass", "dry_run"}
        and not (guarded.get("failed_gates") or [])
        and (refreshed.get("promotion_decision") or {}).get("ready_for_scheduled_production") is True
    )
    issues = []
    if scouts.get("errored"):
        issues.append(f"{scouts.get('errored')} scout error(s) need inspection.")
    if transcript_summary["fallback_after_primary_error"]:
        issues.append(
            f"{transcript_summary['fallback_after_primary_error']} primary provider timeout(s) used fallback."
        )
    top_quality = scouts.get("top_quality_flags") if isinstance(scouts.get("top_quality_flags"), dict) else {}
    if top_quality.get("prompt_missing_hypothesis"):
        issues.append(f"{top_quality.get('prompt_missing_hypothesis')} scouts missed the hypothesis field.")
    if top_quality.get("prompt_missing_information_strings"):
        issues.append(f"{top_quality.get('prompt_missing_information_strings')} scouts missed information strings.")
    if top_quality.get("scout_json_unparseable"):
        issues.append(f"{top_quality.get('scout_json_unparseable')} scout output was JSON-unparseable.")

    return {
        "schema_version": "talis_live_scout_performance_audit_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "prompt_output_dir": str(prompt_dir),
        "artifact_dir": str(prompt_dir.parent),
        "cycle_id": canary.get("cycle_id"),
        "readiness": {
            "ready_for_guarded_scheduled_shadow": ready_for_shadow,
            "ready_for_trade_execution": False,
            "trade_execution_reason": "The safety policy is explicitly shadow-only; verification and execution gates are separate.",
            "canary_status": verdict.get("status"),
            "guarded_status": guarded.get("status"),
            "scale_decision": scale.get("decision"),
            "failed_gates": list(verdict.get("failed_gates") or []) + list(guarded.get("failed_gates") or []),
            "issues_to_watch": issues,
        },
        "preflight": {
            "market_universe": preflight.get("market_universe"),
            "tool_atlas": preflight.get("tool_atlas"),
            "provider_import_ok": preflight.get("provider_import_ok"),
            "tool_atlas_ok": preflight.get("tool_atlas_ok"),
            "market_universe_ok": preflight.get("market_universe_ok"),
        },
        "run_metrics": {
            "n_scouts_requested": canary.get("n_scouts_requested"),
            "completed": scouts.get("completed"),
            "errored": scouts.get("errored"),
            "success_rate": scouts.get("success_rate"),
            "information_strings": info.get("string_count"),
            "information_strings_per_scout": scouts.get("avg_information_strings_per_scout"),
            "duplicate_hypothesis_rate": scouts.get("duplicate_hypothesis_rate"),
            "evidence_ok_rate": scouts.get("evidence_ok_rate"),
            "cost_usd_estimate": scouts.get("total_cost_usd_estimate"),
            "elapsed_s": metrics.get("elapsed_s"),
            "geometry_cells": geometry.get("cell_count"),
            "routing_queue_count": geometry.get("routing_queue_count"),
            "covered_cells": coverage.get("covered_count"),
            "valid_cell_count": coverage.get("valid_cell_count"),
            "coverage_ratio": coverage.get("coverage_ratio"),
            "promoted_hypotheses": info.get("promoted_hypotheses"),
            "confluences": info.get("confluences"),
            "tensions": info.get("tensions"),
        },
        "provider_metrics": transcript_summary,
        "seed_distribution": {
            "asset_classes": seeds.get("asset_classes"),
            "horizons": seeds.get("horizons"),
            "lenses": seeds.get("lenses"),
            "bias_modes": seeds.get("bias_modes"),
            "unique_cell_count": seeds.get("unique_cell_count"),
            "unique_cell_ratio": seeds.get("unique_cell_ratio"),
        },
        "quality": {
            "top_quality_flags": scouts.get("top_quality_flags"),
            "error_samples": scouts.get("error_samples"),
            "output_summary": output_summary,
        },
        "storage": db,
        "geometry": {
            "top_cell": geometry.get("top_cell"),
            "top_actions": (geometry.get("top_actions") or [])[:8],
        },
        "next_step": {
            "system_recommendation": scale.get("next_step"),
            "human_recommendation": (
                "Run the scheduled 1,000-scout shadow job with the same guardrail wrapper, "
                "then compare its provider timeout rate, JSON error rate, string yield, and geometry cells "
                "against the two already-promoted 1,000-scout shadow runs."
            ),
        },
        "safety_policy": safety,
    }


def _read_json(path: Path) -> Any:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _transcript_summary(transcript: Any) -> dict[str, Any]:
    calls = transcript.get("calls") if isinstance(transcript, dict) else []
    calls = [c for c in calls or [] if isinstance(c, dict)]
    providers = collections.Counter(str((c.get("response_envelope") or {}).get("provider") or "unknown") for c in calls)
    models = collections.Counter(str((c.get("response_envelope") or {}).get("model_used") or "unknown") for c in calls)
    primary_errors = collections.Counter(str(c.get("primary_error") or "") for c in calls if c.get("primary_error"))
    latencies = sorted(float(c.get("elapsed_s") or 0.0) for c in calls)
    return {
        "call_count": len(calls),
        "provider_counts": dict(providers),
        "model_counts": dict(models),
        "fallback_after_primary_error": sum(1 for c in calls if c.get("fallback_after_primary_error")),
        "primary_error_counts": dict(primary_errors),
        "latency_s": {
            "min": _round(latencies[0]) if latencies else None,
            "avg": _round(mean(latencies)) if latencies else None,
            "p50": _percentile(latencies, 0.50),
            "p90": _percentile(latencies, 0.90),
            "p95": _percentile(latencies, 0.95),
            "max": _round(latencies[-1]) if latencies else None,
        },
    }


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    idx = min(len(values) - 1, max(0, round(pct * (len(values) - 1))))
    return _round(values[idx])


def _round(value: float) -> float:
    return round(float(value), 4)


def _output_summary(outputs: Any) -> dict[str, Any]:
    rows = outputs if isinstance(outputs, list) else []
    if isinstance(outputs, dict):
        rows = outputs.get("outputs") or outputs.get("results") or outputs.get("scout_outputs") or []
    rows = [r for r in rows if isinstance(r, dict)]
    providers = collections.Counter(str(r.get("provider") or "unknown") for r in rows)
    entities = collections.Counter(str(r.get("entity") or "unknown") for r in rows)
    sample = []
    for row in rows:
        if len(sample) >= 5:
            break
        sample.append({
            "entity": row.get("entity"),
            "horizon": row.get("horizon"),
            "lens": row.get("lens"),
            "bias_mode": row.get("bias_mode"),
            "confidence": row.get("confidence"),
            "hypothesis_text": row.get("hypothesis_text"),
            "information_string_ids": row.get("information_string_ids"),
            "quality_flags": row.get("quality_flags"),
        })
    return {
        "row_count": len(rows),
        "provider_counts": dict(providers),
        "top_entities": entities.most_common(12),
        "sample_outputs": sample,
    }


def _sqlite_summary(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"db_path": str(db_path), "exists": False}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    table_names = [
        "hypotheses",
        "information_strings",
        "information_map_nodes",
        "information_map_edges",
        "information_string_evidence",
        "information_geometry_snapshots",
        "promoted_candidates",
        "task_contracts",
        "tool_call_log",
        "tool_atlas",
        "node_intelligence_snapshots",
        "node_intelligence_observations",
        "analysis_tool_proposals",
        "coverage_cells",
        "coverage_log",
    ]
    counts = {}
    for table in table_names:
        try:
            counts[table] = conn.execute(f"select count(*) from {table}").fetchone()[0]
        except sqlite3.Error:
            counts[table] = None
    top_geometry = [
        dict(row)
        for row in conn.execute(
            """
            select cell_key, entity, horizon, lens, route_directive,
                   trade_scream_score, verifier_readiness, quality_flags
            from information_geometry_snapshots
            order by trade_scream_score desc
            limit 8
            """
        )
    ]
    promoted = [
        dict(row)
        for row in conn.execute(
            """
            select entity, horizon, lens, hypothesis, confidence, promotion_score, status, quality_flags
            from promoted_candidates
            order by promotion_score desc
            limit 8
            """
        )
    ]
    tool_proposals = [
        dict(row)
        for row in conn.execute(
            """
            select tool_name, proposal_kind, entity, horizon, lens, source_family, trigger, priority, status
            from analysis_tool_proposals
            order by case priority when 'high' then 0 when 'medium' then 1 else 2 end, created_at desc
            limit 8
            """
        )
    ]
    return {
        "db_path": str(db_path),
        "exists": True,
        "table_counts": counts,
        "top_geometry_cells": top_geometry,
        "promoted_candidates": promoted,
        "tool_proposals": tool_proposals,
    }


def render_markdown(audit: dict[str, Any]) -> str:
    readiness = audit.get("readiness") or {}
    metrics = audit.get("run_metrics") or {}
    provider = audit.get("provider_metrics") or {}
    storage = ((audit.get("storage") or {}).get("table_counts") or {})
    issues = readiness.get("issues_to_watch") or []
    issue_lines = "\n".join(f"- {issue}" for issue in issues) or "- None."
    return f"""# Live Scout Performance Audit

Generated: `{audit.get('generated_at')}`

## Readiness

- Guarded scheduled shadow: `{readiness.get('ready_for_guarded_scheduled_shadow')}`
- Trade execution: `{readiness.get('ready_for_trade_execution')}`
- Canary status: `{readiness.get('canary_status')}`
- Guarded wrapper status: `{readiness.get('guarded_status')}`
- Scale decision: `{readiness.get('scale_decision')}`

## Run Metrics

- Scouts: `{metrics.get('completed')}/{metrics.get('n_scouts_requested')}` completed, `{metrics.get('errored')}` errored
- Information strings: `{metrics.get('information_strings')}`
- Geometry cells: `{metrics.get('geometry_cells')}`
- Routing tasks: `{metrics.get('routing_queue_count')}`
- Duplicate hypothesis rate: `{metrics.get('duplicate_hypothesis_rate')}`
- Evidence OK rate: `{metrics.get('evidence_ok_rate')}`
- Estimated cost: `${metrics.get('cost_usd_estimate')}`

## Provider Behavior

- Calls: `{provider.get('call_count')}`
- Providers: `{provider.get('provider_counts')}`
- Primary fallback count: `{provider.get('fallback_after_primary_error')}`
- Primary errors: `{provider.get('primary_error_counts')}`
- Latency seconds: `{provider.get('latency_s')}`

## Storage Proof

- Hypotheses: `{storage.get('hypotheses')}`
- Information strings: `{storage.get('information_strings')}`
- Information map nodes: `{storage.get('information_map_nodes')}`
- Information map edges: `{storage.get('information_map_edges')}`
- Tool calls: `{storage.get('tool_call_log')}`
- Node observations: `{storage.get('node_intelligence_observations')}`
- Analysis tool proposals: `{storage.get('analysis_tool_proposals')}`

## Issues To Watch

{issue_lines}

## Next Step

{(audit.get('next_step') or {}).get('human_recommendation')}
"""


if __name__ == "__main__":
    raise SystemExit(main())
