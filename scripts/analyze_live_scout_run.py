#!/usr/bin/env python
"""Analyze a completed live scout run and turn it into an evolution plan."""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WEAK_FLAG_PREFIXES = (
    "prompt_missing_",
    "prompt_string_",
    "prompt_invented_tool",
    "scout_empty_hypothesis",
    "scout_json_unparseable",
    "adversarial_review:quarantine",
)


def main() -> int:
    args = _parse_args()
    live_report_path = Path(args.live_report).expanduser().resolve()
    tournament_report_path = Path(args.tournament_report).expanduser().resolve() if args.tournament_report else None
    report = build_live_scout_learning_report(
        live_report_path,
        tournament_report_path=tournament_report_path,
    )
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else live_report_path.parent
    json_path, md_path = write_learning_report_artifacts(report, output_dir=output_dir)
    print(f"LIVE_SCOUT_LEARNING_REPORT_JSON={json_path}")
    print(f"LIVE_SCOUT_LEARNING_REPORT_MD={md_path}")
    print(f"LIVE_SCOUT_LEARNING_NEXT_ALLOWED={report['next_run']['allowed_next_step']}")
    return 0


def build_live_scout_learning_report(
    live_report_path: Path,
    *,
    tournament_report_path: Path | None = None,
) -> dict[str, Any]:
    live_report = _read_json(live_report_path)
    output_dir = live_report_path.parent
    outputs_raw = _read_json(output_dir / "live_scout_canary_outputs.json")
    outputs = [row for row in outputs_raw if isinstance(row, dict)] if isinstance(outputs_raw, list) else []
    transcript = _read_json(output_dir / "live_scout_transcript.json")
    slice_preview = _read_json(output_dir / "live_scout_slice_preview.json")
    tournament = _read_json(tournament_report_path) if tournament_report_path else _read_nearby_tournament(live_report_path)
    metrics = live_report.get("metrics") if isinstance(live_report.get("metrics"), dict) else {}
    winner = tournament.get("winner") if isinstance(tournament.get("winner"), dict) else {}
    decision = tournament.get("promotion_decision") if isinstance(tournament.get("promotion_decision"), dict) else {}
    rows = _row_evaluations(outputs)
    flag_counts = Counter(flag for row in rows for flag in row["quality_flags"])
    prompt_scores = [row["prompt_quality"] for row in rows if row["prompt_quality"] is not None]
    weak_rows = [row for row in rows if row["weakness_score"] > 0]
    failure_modes = _failure_modes(rows, flag_counts)
    source_family_counts = _source_family_counts(outputs, slice_preview)
    geometry = metrics.get("geometry") if isinstance(metrics.get("geometry"), dict) else {}
    market_evolve = metrics.get("market_evolve") if isinstance(metrics.get("market_evolve"), dict) else {}
    learning_summary = _learning_summary(
        live_report=live_report,
        tournament=tournament,
        rows=rows,
        prompt_scores=prompt_scores,
        failure_modes=failure_modes,
    )
    evolution_arms = _evolution_arms(
        failure_modes=failure_modes,
        geometry=geometry,
        market_evolve=market_evolve,
        source_family_counts=source_family_counts,
    )
    return {
        "schema_version": "talis_live_scout_learning_report_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_live_report": str(live_report_path),
        "source_tournament_report": str(tournament_report_path or ""),
        "cycle_id": live_report.get("cycle_id"),
        "summary": learning_summary,
        "scorecard": {
            "requested": int(live_report.get("n_scouts_requested") or len(rows)),
            "provider_calls": int((live_report.get("transcript_summary") or {}).get("call_count") or 0),
            "completed": int(((metrics.get("scouts") or {}) if isinstance(metrics.get("scouts"), dict) else {}).get("completed") or 0),
            "success_rate": _float(((metrics.get("scouts") or {}) if isinstance(metrics.get("scouts"), dict) else {}).get("success_rate")),
            "estimated_cost_usd": _float(((metrics.get("scouts") or {}) if isinstance(metrics.get("scouts"), dict) else {}).get("total_cost_usd_estimate")),
            "strings": int(((metrics.get("information_map") or {}) if isinstance(metrics.get("information_map"), dict) else {}).get("string_count") or 0),
            "geometry_cells": int(geometry.get("cell_count") or 0),
            "routing_queue_count": int(geometry.get("routing_queue_count") or 0),
            "avg_prompt_quality": round(sum(prompt_scores) / max(1, len(prompt_scores)), 4),
            "low_prompt_quality_count": sum(1 for score in prompt_scores if score < 0.70),
            "weak_scout_count": len(weak_rows),
            "duplicate_hypothesis_rate": _float(((metrics.get("scouts") or {}) if isinstance(metrics.get("scouts"), dict) else {}).get("duplicate_hypothesis_rate")),
            "tool_error_rate": _float((winner.get("quality") or {}).get("tool_error_rate")),
        },
        "tournament": {
            "decision": decision.get("decision"),
            "ready_for_live_1000": bool(decision.get("ready_for_live_1000")),
            "ready_for_scheduled_production": bool(decision.get("ready_for_scheduled_production")),
            "reason": decision.get("reason") or "",
        },
        "market_evolve": {
            "paired_seed_slices": market_evolve.get("paired_seed_slices"),
            "arm_counts": market_evolve.get("arm_counts") or {},
            "latest_experiment_decision": market_evolve.get("latest_experiment_decision"),
            "final_score": market_evolve.get("final_score"),
            "final_passed": market_evolve.get("final_passed"),
        },
        "source_family_counts": source_family_counts,
        "failure_modes": failure_modes,
        "weak_scouts": weak_rows[:20],
        "top_geometry_actions": (geometry.get("top_actions") or [])[:10],
        "evolution_arms": evolution_arms,
        "next_run": _next_run(decision, failure_modes, evolution_arms),
        "artifacts": {
            "live_report": str(live_report_path),
            "outputs": str(output_dir / "live_scout_canary_outputs.json"),
            "transcript": str(output_dir / "live_scout_transcript.json"),
            "slice_preview": str(output_dir / "live_scout_slice_preview.json"),
        },
    }


def write_learning_report_artifacts(report: dict[str, Any], *, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "live_scout_learning_report.json"
    md_path = output_dir / "live_scout_learning_report.md"
    _write_json(json_path, report)
    md_path.write_text(render_learning_report_markdown(report), encoding="utf-8")
    return json_path, md_path


def render_learning_report_markdown(report: dict[str, Any]) -> str:
    scorecard = report.get("scorecard") if isinstance(report.get("scorecard"), dict) else {}
    tournament = report.get("tournament") if isinstance(report.get("tournament"), dict) else {}
    lines = [
        "# Live Scout Learning Report",
        "",
        str(report.get("summary") or ""),
        "",
        "## Scorecard",
        "",
    ]
    for key in (
        "requested",
        "provider_calls",
        "completed",
        "success_rate",
        "estimated_cost_usd",
        "strings",
        "geometry_cells",
        "avg_prompt_quality",
        "weak_scout_count",
    ):
        lines.append(f"- {key}: `{scorecard.get(key)}`")
    lines.extend([
        "",
        "## Tournament",
        "",
        f"- decision: `{tournament.get('decision')}`",
        f"- ready_for_live_1000: `{tournament.get('ready_for_live_1000')}`",
        f"- ready_for_scheduled_production: `{tournament.get('ready_for_scheduled_production')}`",
        "",
        "## Failure Modes",
        "",
    ])
    for mode in report.get("failure_modes") or []:
        lines.append(f"- `{mode.get('id')}` count={mode.get('count')}: {mode.get('mitigation')}")
    lines.extend(["", "## Evolution Arms", ""])
    for arm in report.get("evolution_arms") or []:
        lines.append(f"- `{arm.get('id')}`: {arm.get('purpose')} Gate: {arm.get('success_gate')}")
    lines.extend([
        "",
        "## Next Run",
        "",
        f"- allowed_next_step: `{(report.get('next_run') or {}).get('allowed_next_step')}`",
        f"- operator_note: {(report.get('next_run') or {}).get('operator_note')}",
    ])
    return "\n".join(lines) + "\n"


def _row_evaluations(outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in outputs:
        flags = [str(flag) for flag in (row.get("quality_flags") or [])]
        prompt_quality = _prompt_quality(flags)
        weak_flags = [
            flag for flag in flags
            if flag.startswith(WEAK_FLAG_PREFIXES) or flag in {"prompt_invented_tool", "adversarial_review:quarantine"}
        ]
        if row.get("error"):
            weak_flags.append(str(row.get("error")))
        if not str(row.get("hypothesis_text") or "").strip():
            weak_flags.append("empty_hypothesis_text")
        weakness_score = len(set(weak_flags))
        rows.append({
            "seed_id": row.get("seed_id"),
            "scout_id": row.get("scout_id"),
            "entity": row.get("entity"),
            "horizon": row.get("horizon"),
            "lens": row.get("lens"),
            "bias_mode": row.get("bias_mode"),
            "prompt_quality": prompt_quality,
            "hypothesis_text": str(row.get("hypothesis_text") or "")[:320],
            "confidence": row.get("confidence"),
            "string_count": len(row.get("information_string_ids") or []),
            "tool_evidence_count": len(row.get("tool_evidence") or []),
            "tool_iteration_count": row.get("tool_iteration_count") or 0,
            "error": row.get("error"),
            "quality_flags": flags,
            "weak_flags": sorted(set(weak_flags)),
            "weakness_score": weakness_score,
            "suggested_tools_count": len(row.get("suggested_tools") or []),
            "tool_request_count": len(row.get("tool_requests") or []),
        })
    rows.sort(key=lambda item: (item["weakness_score"], -(item["prompt_quality"] or 0)), reverse=True)
    return rows


def _failure_modes(rows: list[dict[str, Any]], flag_counts: Counter[str]) -> list[dict[str, Any]]:
    mode_specs = [
        ("json_unparseable", ["scout_json_unparseable"], "fallback_json_repair_before_drop", "One fallback-model JSON repair is now attempted before a non-calendar scout is dropped."),
        ("empty_hypothesis", ["prompt_missing_hypothesis", "scout_empty_hypothesis", "empty_hypothesis_text"], "hypothesis_string_consistency", "Keep strict pressure: if strings exist, hypothesis must summarize the best valid string."),
        ("missing_information_strings", ["prompt_missing_information_strings"], "gap_string_instead_of_empty", "Stale/thin data should become a low-conviction gap string with evidence refs, not an empty packet."),
        ("invented_tools", ["prompt_invented_tool"], "suggested_tool_allowlist_filter", "Suggested tools are filtered against allowed_tool_candidates before persistence."),
        ("missing_evidence_refs", ["prompt_string_missing_evidence_refs"], "evidence_ref_hard_gate", "Strings without provided refs should become tool_requests or be rejected by prompt quality."),
        ("stale_date_directionality", ["prompt_string_stale_date_reference"], "stale_data_gap_prompt", "Stale evidence should map source-health gaps instead of directional trade claims."),
        ("adversarial_quarantine", ["adversarial_review:quarantine"], "source_repair_before_verify", "Quarantined strings should route to source repair or independent scout replication before verifier spend."),
        ("node_not_promoted", ["node_intelligence_not_promoted"], "node_contract_upgrade", "Node payloads are captured but need stronger actor/source coverage before promotion into trade strings."),
    ]
    out = []
    for mode_id, flags, mitigation_id, mitigation in mode_specs:
        count = sum(flag_counts.get(flag, 0) for flag in flags)
        if mode_id == "empty_hypothesis":
            count += sum(1 for row in rows if "empty_hypothesis_text" in row.get("weak_flags", []))
        examples = [
            {
                "entity": row.get("entity"),
                "horizon": row.get("horizon"),
                "lens": row.get("lens"),
                "seed_id": row.get("seed_id"),
                "prompt_quality": row.get("prompt_quality"),
                "hypothesis_text": row.get("hypothesis_text"),
                "weak_flags": row.get("weak_flags"),
            }
            for row in rows
            if any(flag in row.get("weak_flags", []) or flag in row.get("quality_flags", []) for flag in flags)
        ][:5]
        out.append({
            "id": mode_id,
            "count": int(count),
            "severity": "red" if mode_id in {"json_unparseable", "invented_tools"} and count else "yellow" if count else "green",
            "mitigation_id": mitigation_id,
            "mitigation": mitigation,
            "examples": examples,
        })
    out.sort(key=lambda item: (item["severity"] != "green", item["count"]), reverse=True)
    return out


def _evolution_arms(
    *,
    failure_modes: list[dict[str, Any]],
    geometry: dict[str, Any],
    market_evolve: dict[str, Any],
    source_family_counts: dict[str, int],
) -> list[dict[str, Any]]:
    counts = {row["id"]: int(row.get("count") or 0) for row in failure_modes}
    arms = []
    if counts.get("json_unparseable") or counts.get("invented_tools"):
        arms.append({
            "id": "harness_repair_arm",
            "type": "harness",
            "purpose": "Reduce preventable formatting/tool-contract waste before the 1,000 ramp.",
            "changes": ["fallback JSON repair", "suggested tool allowlist filtering"],
            "success_gate": "json_unparseable_rate == 0 and invented_tool_persist_rate == 0",
        })
    if counts.get("missing_evidence_refs") or counts.get("stale_date_directionality") or counts.get("missing_information_strings"):
        arms.append({
            "id": "source_freshness_gap_arm",
            "type": "prompt_and_tooling",
            "purpose": "Convert stale/thin evidence into auditable gap strings and follow-up tool requests.",
            "changes": ["stale data cannot imply direction", "gap strings must name missing source and repair condition"],
            "success_gate": "low_prompt_quality_rate <= 0.05 and stale_directionality_flags <= 0.01",
        })
    if counts.get("node_not_promoted") or source_family_counts.get("our_node", 0) < source_family_counts.get("market_timeseries", 0) * 0.5:
        arms.append({
            "id": "node_intelligence_promotion_arm",
            "type": "source_surface",
            "purpose": "Make Hydromancer/HL-node observations first-class graph edges instead of sidecar context.",
            "changes": ["tighten node_intelligence contract", "rank actor quality and reject behavior", "promote only source-backed actor/flow strings"],
            "success_gate": "node_promoted_string_rate increases without adversarial quarantine rising",
        })
    top_actions = [row for row in (geometry.get("top_actions") or []) if isinstance(row, dict)]
    if top_actions:
        arms.append({
            "id": "geometry_replication_arm",
            "type": "routing",
            "purpose": "Let the map shape choose independent follow-up scouts for high tension/frontier cells.",
            "changes": ["replicate top trade-scream cells", "widen independent source families", "resolve contradiction clusters"],
            "success_gate": "top geometry cells receive independent scout confirmation, contradiction, or kill signal",
            "top_cells": [
                {
                    "cell_key": row.get("cell_key"),
                    "route_directive": row.get("route_directive"),
                    "trade_scream_score": ((row.get("metrics") or {}) if isinstance(row.get("metrics"), dict) else {}).get("trade_scream_score"),
                }
                for row in top_actions[:5]
            ],
        })
    if market_evolve.get("latest_experiment_decision"):
        arms.append({
            "id": "market_evolve_policy_arena",
            "type": "evaluator",
            "purpose": "Continue matched control/candidate policy testing instead of hand-picking a prompt by taste.",
            "changes": ["keep 20+ matched seed pairs at 100+ scale", "promote only evaluator-positive mutations"],
            "success_gate": "candidate improves accepted unique high-quality coverage per dollar without worsening low-EV tool rate",
            "latest_decision": market_evolve.get("latest_experiment_decision"),
        })
    return arms


def _next_run(decision: dict[str, Any], failure_modes: list[dict[str, Any]], evolution_arms: list[dict[str, Any]]) -> dict[str, Any]:
    ready_1000 = bool(decision.get("ready_for_live_1000"))
    red_modes = [mode["id"] for mode in failure_modes if mode.get("severity") == "red" and int(mode.get("count") or 0) > 0]
    return {
        "allowed_next_step": "live_1000_scout_ramp" if ready_1000 else "repair_then_repeat_100",
        "requires_explicit_spend_authorization": True,
        "recommended_command": (
            "PYTHONPATH=. python scripts/run_scout_system_launch_gate.py --allow-live-spend "
            "--live-scouts 1000 --live-cost-cap-usd 5.00 --live-concurrency 8 --max-tool-iterations 1"
            if ready_1000 else
            "PYTHONPATH=. python scripts/run_scout_system_launch_gate.py --allow-live-spend "
            "--live-scouts 100 --live-cost-cap-usd 1.00 --live-concurrency 4 --max-tool-iterations 1"
        ),
        "operator_note": (
            "The 1,000 ramp is allowed by tournament evidence, but scheduled production remains blocked until two independent 1,000-scout shadow runs pass repeatability gates."
            if ready_1000 else
            "Repeat the 100-scout distribution after repair arms reduce red failure modes."
        ),
        "watch_before_promoting": red_modes or [arm["id"] for arm in evolution_arms[:3]],
    }


def _learning_summary(
    *,
    live_report: dict[str, Any],
    tournament: dict[str, Any],
    rows: list[dict[str, Any]],
    prompt_scores: list[float],
    failure_modes: list[dict[str, Any]],
) -> str:
    decision = (tournament.get("promotion_decision") or {}).get("decision") if isinstance(tournament.get("promotion_decision"), dict) else "unknown"
    avg_quality = round(sum(prompt_scores) / max(1, len(prompt_scores)), 3)
    weak = sum(1 for row in rows if row.get("weakness_score", 0) > 0)
    top = [mode["id"] for mode in failure_modes if int(mode.get("count") or 0) > 0][:3]
    return (
        f"The live run earned `{decision}` with average prompt quality {avg_quality} and "
        f"{weak}/{len(rows)} scouts carrying at least one repair signal. The system should "
        f"treat the next scale step as an evaluator-gated ramp, not production; the main "
        f"repair pockets are {', '.join(top) if top else 'none'}."
    )


def _source_family_counts(outputs: list[dict[str, Any]], slice_preview: dict[str, Any]) -> dict[str, int]:
    distributions = slice_preview.get("distributions") if isinstance(slice_preview.get("distributions"), dict) else {}
    source_counts = distributions.get("source_family") if isinstance(distributions.get("source_family"), dict) else {}
    counter = Counter({str(k): int(v) for k, v in source_counts.items()})
    for row in outputs:
        for info in row.get("information_strings") or []:
            if not isinstance(info, dict):
                continue
            for flag in info.get("quality_flags") or []:
                text = str(flag)
                if text.startswith("source_family:"):
                    counter[text.split(":", 1)[1]] += 1
    return dict(counter.most_common())


def _prompt_quality(flags: list[str]) -> float | None:
    for flag in flags:
        if str(flag).startswith("prompt_quality:"):
            return _float(str(flag).split(":", 1)[1], default=None)
    return None


def _read_nearby_tournament(live_report_path: Path) -> dict[str, Any]:
    candidates = [
        live_report_path.parent.parent.parent / "tournament" / "live_scout_tournament_report.json",
        live_report_path.parent.parent / "tournament" / "live_scout_tournament_report.json",
        live_report_path.parent / "live_scout_tournament_report.json",
    ]
    for path in candidates:
        if path.exists():
            return _read_json(path)
    return {}


def _read_json(path: Path | None) -> Any:
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def _float(raw: Any, default: float | None = 0.0) -> float | None:
    try:
        return float(raw)
    except Exception:
        return default


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("live_report", help="Path to live_scout_canary_report.json")
    parser.add_argument("--tournament-report", default="", help="Optional live_scout_tournament_report.json")
    parser.add_argument("--output-dir", default="", help="Where to write live_scout_learning_report.{json,md}")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
