#!/usr/bin/env python
"""Run a deterministic 100-scout readiness slice.

This is the scale gate before live provider spend: use the real seed
generator, scout runner, store, information map, geometry, governor, and
self-healing worker, while replacing the external model/provider layer with a
deterministic local shim. The output tells us whether the desk is mechanically
ready for a larger DeepSeek Flash run, and where coverage or data plumbing is
still thin.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import tempfile
import time
import types
import uuid
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from talis_desk.information_map import (
    compute_alpha_geometry,
    plan_alpha_geometry_actions,
    recent_information_strings,
    run_information_synthesis,
)
from talis_desk.market_map.coverage_audit import build_coverage_gap_manifest
from talis_desk.market_map.governor import build_market_map_governor_plan
from talis_desk.market_map.self_healing import (
    build_market_map_self_healing_plan,
    execute_market_map_self_healing_tasks,
    post_market_map_self_healing_work_orders,
)
from talis_desk.market_map.universe import build_market_universe
from talis_desk.store import reset_desk_store_for_test
from talis_desk.swarm.scout_runner import run_scouts
from talis_desk.swarm.seed_generator import (
    DEFAULT_ENTITIES,
    SeedCell,
    entity_asset_class,
    generate_seeds,
)
from talis_desk.tool_atlas import regenerate_tool_atlas


DEFAULT_THEMES = [
    "hyperliquid_node_intelligence",
    "mempool_pending_intent",
    "market_structure_break",
    "calendar_catalyst",
    "relative_rotation",
    "liquidity_fragility",
    "smart_wallet_absorption",
    "source_health_gap",
]


def main() -> int:
    args = _parse_args()
    started = time.perf_counter()
    artifact_dir = Path(args.artifact_dir).expanduser().resolve() if args.artifact_dir else _artifact_dir()
    prompt_output_dir = (
        Path(args.prompt_output_dir).expanduser().resolve()
        if args.prompt_output_dir
        else artifact_dir / "prompt_outputs"
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    prompt_output_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TALIS_LEARNED_TOOLS_DIR"] = str(artifact_dir / "learned_tools")

    cycle_id = args.cycle_id or f"cycle_100_scout_readiness_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    db_path = Path(args.db).expanduser().resolve() if args.db else artifact_dir / "desk-100-scout.db"
    store = reset_desk_store_for_test(db_path)

    transcript: dict[str, Any] = {"calls": []}
    _install_deterministic_tic_chat(transcript)

    import talis_desk.tool_atlas as tool_atlas

    original_dispatch = tool_atlas.dispatch_uri
    tool_atlas.dispatch_uri = _deterministic_dispatch_uri
    try:
        atlas = regenerate_tool_atlas()
        universe = build_market_universe(default_entities=DEFAULT_ENTITIES)
        seeds = generate_seeds(
            n_seeds=args.n_scouts,
            cycle_id=cycle_id,
            entities=universe.entity_symbols() or DEFAULT_ENTITIES,
            themes=DEFAULT_THEMES,
            rng_seed=args.seed_rng,
            theme_share=args.theme_share,
        )
        seeds = [_prepare_seed(seed) for seed in seeds]
        seed_path = prompt_output_dir / "100_scout_seeds.json"
        _write_json(seed_path, [_seed_payload(seed) for seed in seeds])

        scouts = run_scouts(
            seeds=seeds,
            cycle_id=cycle_id,
            model=args.model,
            fallback=args.fallback,
            concurrency=args.concurrency,
            cost_cap_usd=args.cost_cap_usd,
        )
        outputs_path = prompt_output_dir / "100_scout_outputs.json"
        _write_json(outputs_path, [_asdict(row) for row in scouts])

        strings = recent_information_strings(cycle_id=cycle_id, conn=store.conn, limit=max(1000, args.n_scouts * 5))
        synthesis = run_information_synthesis(
            cycle_id=cycle_id,
            max_strings=max(200, args.n_scouts * 3),
            use_llm=False,
        )
        geometry = compute_alpha_geometry(cycle_id=cycle_id, conn=store.conn, persist=True)
        action_plan = plan_alpha_geometry_actions(cycle_id=cycle_id, conn=store.conn, limit=128)
        coverage = build_coverage_gap_manifest(cycle_id=cycle_id, conn=store.conn)
        governor = build_market_map_governor_plan(
            cycle_id=cycle_id,
            conn=store.conn,
            coverage_manifest=coverage,
            scout_budget=args.n_scouts,
            use_llm=False,
        )
        trace = _readiness_trace(
            cycle_id=cycle_id,
            scouts=scouts,
            strings=strings,
            geometry=geometry,
            action_plan=action_plan,
            coverage=coverage,
            governor=governor,
        )
        self_healing_plan = build_market_map_self_healing_plan(trace)
        self_healing_dispatch = post_market_map_self_healing_work_orders(
            self_healing_plan,
            cycle_id=cycle_id,
            conn=store.conn,
            limit=12,
        )
        self_healing_worker = execute_market_map_self_healing_tasks(
            cycle_id=cycle_id,
            conn=store.conn,
            limit=12,
        )

        metrics = _metrics(
            cycle_id=cycle_id,
            seeds=seeds,
            scouts=scouts,
            strings=strings,
            synthesis=synthesis,
            geometry=geometry,
            action_plan=action_plan,
            coverage=coverage,
            governor=governor,
            self_healing_plan=self_healing_plan,
            self_healing_dispatch=self_healing_dispatch,
            self_healing_worker=self_healing_worker,
            atlas_n_tools=getattr(atlas, "n_tools", 0),
            atlas_n_sources=getattr(atlas, "n_sources", 0),
            transcript=transcript,
            elapsed_s=time.perf_counter() - started,
        )
        readiness = _readiness_verdict(metrics)
        report = {
            "schema_version": "talis_100_scout_readiness_v1",
            "mode": "offline_deterministic_model_and_tool_shim",
            "cycle_id": cycle_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "db_path": str(db_path),
            "artifact_dir": str(artifact_dir),
            "prompt_output_dir": str(prompt_output_dir),
            "n_scouts_requested": args.n_scouts,
            "seed_rng": args.seed_rng,
            "model": args.model,
            "fallback": args.fallback,
            "concurrency": args.concurrency,
            "cost_cap_usd": args.cost_cap_usd,
            "metrics": metrics,
            "readiness": readiness,
            "artifacts": {
                "seeds": str(seed_path),
                "outputs": str(outputs_path),
            },
            "next_live_gate": {
                "recommendation": (
                    "Run a 10-scout live-provider canary with the same seed packet before a 1,000 scout run."
                    if readiness["ready_for_live_1000"] else
                    "Fix failed readiness gates, then repeat this deterministic slice before live spend."
                ),
                "why": "This harness proves orchestration and storage, not provider quality or live data freshness.",
            },
        }
        report_path = prompt_output_dir / "100_scout_readiness_report.json"
        md_path = prompt_output_dir / "100_scout_readiness_report.md"
        _write_json(report_path, report)
        _write_text(md_path, _render_markdown(report))
        print(f"SCOUT100_STATUS={readiness['status']}")
        print(f"SCOUT100_READY_FOR_LIVE_1000={readiness['ready_for_live_1000']}")
        print(f"SCOUT100_REPORT_JSON={report_path}")
        print(f"SCOUT100_REPORT_MD={md_path}")
        print(f"SCOUT100_PROMPT_OUTPUT_DIR={prompt_output_dir}")
        return 0 if readiness["status"] in {"pass", "warn"} else 1
    finally:
        tool_atlas.dispatch_uri = original_dispatch
        store.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-scouts", type=int, default=100)
    parser.add_argument("--cycle-id", default="")
    parser.add_argument("--db", default="")
    parser.add_argument("--artifact-dir", default="")
    parser.add_argument("--prompt-output-dir", default="")
    parser.add_argument("--seed-rng", type=int, default=20260522)
    parser.add_argument("--theme-share", type=float, default=0.16)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--cost-cap-usd", type=float, default=1.0)
    parser.add_argument("--model", default="deepseek:v4-flash")
    parser.add_argument("--fallback", default="anthropic:claude-haiku-4-5")
    return parser.parse_args()


def _prepare_seed(seed: SeedCell) -> SeedCell:
    payload = dict(seed.payload or {})
    candidates = [str(x) for x in payload.get("tool_candidates") or [] if x]
    preferred = _preferred_tools(seed)
    payload["tool_candidates"] = _unique([*preferred, *candidates])[:12]
    payload["max_evidence_tools"] = 3 if seed.horizon in {"intraday", "1d"} else 2
    payload["max_tool_iterations"] = 1
    payload["max_followup_tools_per_iteration"] = 1
    payload["prompt_contract_pressure"] = "raise"
    payload["prompt_min_information_strings"] = 2
    payload["readiness_slice"] = True
    seed.payload = payload
    return seed


def _preferred_tools(seed: SeedCell) -> list[str]:
    base = [
        "tic://tool/builtin/query_timeseries@v1",
        "tic://tool/builtin/query_source_health@v1",
    ]
    lens = seed.lens
    entity = seed.entity.upper()
    if lens in {"on_chain", "smart_money", "microstructure"} or entity in {"HYPE", "BTC", "ETH", "SOL"}:
        base = [
            "tic://tool/hydromancer/get_hl_pnl_leaderboard@v1",
            "tic://tool/hydromancer/get_builder_fills@v1",
            "tic://tool/builtin/hl_reject_corpus@v1",
            *base,
        ]
    if lens in {"sentiment", "catalyst", "filing", "polymarket"}:
        base = [
            "tic://tool/builtin/query_events_recent@v1",
            "tic://tool/parallel/parallel_search@v1",
            *base,
        ]
    if "jarvis" in str(seed.theme or "") or entity in {"HYPE", "BTC", "ETH", "SOL"}:
        base.append("tic://tool/jarvis_bridge/get_hyperliquid_node_state@v1")
    return _unique(base)


def _install_deterministic_tic_chat(transcript: dict[str, Any]) -> None:
    async def chat(model: str, system: str, user: str, *, max_tokens: int, fallback: str | None = None) -> dict[str, Any]:
        packet = _parse_prompt_packet(user)
        payload = _model_payload(packet, iteration="scout_tool_iteration:" in str(user or ""))
        transcript.setdefault("calls", []).append({
            "model": model,
            "fallback": fallback,
            "max_tokens": max_tokens,
            "entity": packet["entity"],
            "horizon": packet["horizon"],
            "lens": packet["lens"],
            "bias_mode": packet["bias_mode"],
            "iteration": "scout_tool_iteration:" in str(user or ""),
            "evidence_refs": packet["evidence_refs"],
            "allowed_tools": packet["allowed_tools"][:6],
        })
        return {"text": json.dumps(payload), "model_used": model, "provider": "deterministic_readiness"}

    tic = types.ModuleType("tic")
    desk = types.ModuleType("tic.desk")
    models = types.ModuleType("tic.desk.models")
    models.chat = chat
    desk.models = models
    tic.desk = desk
    sys.modules["tic"] = tic
    sys.modules["tic.desk"] = desk
    sys.modules["tic.desk.models"] = models


def _parse_prompt_packet(user: str) -> dict[str, Any]:
    def field(name: str, default: str) -> str:
        match = re.search(rf"^\s*{re.escape(name)}=(.+)$", user, re.MULTILINE)
        return match.group(1).strip() if match else default

    allowed_block = user.split("allowed_tool_candidates:", 1)[-1] if "allowed_tool_candidates:" in user else ""
    allowed = [
        line.strip("- \t")
        for line in allowed_block.splitlines()
        if line.strip().startswith("tic://")
    ]
    refs = re.findall(r"tool_call_log_id=([A-Za-z0-9_:-]+)", user)
    return {
        "entity": field("entity", "UNKNOWN").upper(),
        "horizon": field("horizon", "1d"),
        "lens": field("lens", "anomaly"),
        "bias_mode": field("bias_mode", "frontier"),
        "theme": field("theme", ""),
        "allowed_tools": _unique(allowed),
        "evidence_refs": _unique(refs),
    }


def _model_payload(packet: dict[str, Any], *, iteration: bool) -> dict[str, Any]:
    entity = packet["entity"]
    horizon = packet["horizon"]
    lens = packet["lens"]
    bias = packet["bias_mode"]
    theme = packet.get("theme") or lens
    refs = packet["evidence_refs"] or [f"synthetic_ref_{entity.lower()}_{lens}"]
    allowed = packet["allowed_tools"]
    confidence = _score_from_text(entity + horizon + lens + bias, lo=0.57, hi=0.84)
    novelty = _score_from_text(lens + entity, lo=0.54, hi=0.91)
    crowded = _score_from_text(bias + lens, lo=0.16, hi=0.68)
    mechanism = _mechanism_for_lens(entity, lens, theme)
    hypothesis = (
        f"{entity} becomes verifier-worthy on {horizon} if {lens.replace('_', ' ')} evidence "
        f"confirms {mechanism} before the route decays."
    )[:280]
    observed_at = datetime.now(timezone.utc)
    expires_at = observed_at + _horizon_delta(horizon)
    strings = [
        {
            "title": f"{entity} {lens.replace('_', ' ')} pressure route",
            "thesis": hypothesis,
            "entities_chain": [entity, lens, theme, "verifier route"],
            "mechanism": mechanism,
            "depth_layers": [
                {"layer": 1, "claim": f"{lens.replace('_', ' ')} shifts the local information cell"},
                {"layer": 2, "claim": "independent source confirmation decides whether the signal survives"},
                {"layer": 3, "claim": "the map routes verifier spend only if edge quality improves"},
            ],
            "expected_outcome": f"Trade attention rises only if fresh receipts support {entity} within {horizon}.",
            "time_horizon": horizon,
            "time_scale": horizon,
            "observed_at": observed_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "source_time_basis": "event_time" if lens in {"catalyst", "sentiment", "polymarket", "on_chain"} else "observed_time",
            "extends_or_contradicts": "new",
            "would_change_decision": True,
            "kill_signal": f"No independent {lens.replace('_', ' ')} receipt or opposing source-health flag before the horizon closes.",
            "crowdedness": round(crowded, 2),
            "conviction": round(confidence, 2),
            "novelty_score": round(novelty, 2),
            "evidence_refs": refs[:4],
            "prior_thread_refs": [],
            "quality_flags": ["readiness_slice_string", f"bias:{bias}"],
        },
        {
            "title": f"{entity} missing-edge test",
            "thesis": (
                f"The useful next question for {entity} is whether {lens.replace('_', ' ')} links to a "
                "different source family instead of repeating the same surface."
            ),
            "entities_chain": [entity, lens, "source family", "alpha geometry"],
            "mechanism": "Source-family independence is what turns a scout claim into a durable map edge.",
            "depth_layers": [
                {"layer": 1, "claim": "one evidence surface creates a candidate string"},
                {"layer": 2, "claim": "a second source family raises verifier readiness"},
            ],
            "expected_outcome": "The geometry cell should either widen sources or verify now, rather than sit as generic commentary.",
            "time_horizon": horizon,
            "time_scale": horizon,
            "observed_at": observed_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "source_time_basis": "observed_time",
            "extends_or_contradicts": "extends",
            "would_change_decision": True,
            "kill_signal": "The second source family is stale, missing, or contradicts the first source.",
            "crowdedness": round(max(0.05, crowded - 0.09), 2),
            "conviction": round(max(0.51, confidence - 0.05), 2),
            "novelty_score": round(min(0.95, novelty + 0.04), 2),
            "evidence_refs": refs[:4],
            "prior_thread_refs": [],
            "quality_flags": ["readiness_slice_missing_edge"],
        },
    ]
    tool_requests: list[dict[str, Any]] = []
    if not iteration and allowed and _score_from_text(entity + lens, lo=0.0, hi=1.0) > 0.62:
        requested_tool = allowed[-1]
        tool_requests.append({
            "tool_uri": requested_tool,
            "tool_name": requested_tool.rsplit("/", 1)[-1].split("@", 1)[0],
            "args": {"entity": entity, "limit": 10},
            "why": f"Confirm the highest value missing edge for {entity} {lens}.",
            "priority": "medium",
            "expected_edge": f"{lens}_second_source_confirmation",
            "expected_info_value": 0.68,
            "would_change_decision": True,
            "fallback_if_denied": "Persist a missing-source-family tool proposal.",
        })
    event_payload = []
    node_payload = []
    if lens in {"catalyst", "sentiment", "polymarket", "on_chain", "smart_money"}:
        event_payload.append({
            "entity": entity,
            "headline": f"{entity} {theme or lens} readiness event",
            "event_type": theme or lens,
            "scenario": "conditional_repricing",
            "source_refs": refs[:3],
            "watch_triggers": [
                {"trigger": "source family confirmation", "severity": "medium", "window": horizon}
            ],
            "quality_flags": ["from_readiness_slice"],
        })
    if lens in {"on_chain", "smart_money", "microstructure"} or entity in {"HYPE", "BTC", "ETH", "SOL"}:
        node_payload.append({
            "entity": entity,
            "source_refs": refs[:3],
            "actors": [
                {"label": f"{entity}_informed_wallet_cluster", "role": "absorber", "source_refs": refs[:2]}
            ],
            "observations": [
                {
                    "label": "route_quality",
                    "value": "needs mempool and node confirmation",
                    "source_ref": refs[0],
                    "confidence": 0.72,
                }
            ],
            "coverage": {
                "tool_proposals": [
                    {
                        "tool_name": "mempool_actor_route_reader",
                        "source_family": "node_mempool",
                        "purpose": "Track pending Hyperliquid actor intent before visible market impact.",
                        "promotion_gate": {"would_change_decision": True},
                    }
                ]
            },
            "quality_flags": ["from_readiness_slice"],
        })
    return {
        "hypothesis": hypothesis,
        "confidence": round(confidence, 2),
        "rationale_brief": f"{len(refs)} receipts plus {lens} posture determine whether this cell deserves verifier spend.",
        "suggested_tools": allowed[:3],
        "tool_requests": tool_requests,
        "information_strings": strings,
        "event_intelligence": event_payload,
        "node_intelligence": node_payload,
    }


def _mechanism_for_lens(entity: str, lens: str, theme: str) -> str:
    readable = lens.replace("_", " ")
    if lens in {"on_chain", "smart_money"}:
        return f"{entity} actor-quality, route, and absorption evidence separating real supply from noise"
    if lens in {"microstructure", "vol_surface", "options_flow"}:
        return f"{entity} liquidity, positioning, and convexity pressure moving before price fully reflects it"
    if lens in {"macro", "money_velocity", "rotation", "factor"}:
        return f"{entity} cross-asset flow pressure changing the relative reward for this market cell"
    if lens in {"catalyst", "filing", "sentiment", "polymarket"}:
        return f"{entity} event interpretation changing expected path, probability, or timing"
    return f"{entity} {readable} evidence creating a non-generic causal route"


def _deterministic_dispatch_uri(uri: str, args: dict[str, Any], context: Any) -> Any:
    entity = str(args.get("coin") or args.get("entity") or args.get("symbol") or args.get("entity_symbol") or "MARKET").upper()
    family = _source_family(uri)
    log_id = "tc_readiness_" + uuid.uuid4().hex[:16]
    result = {
        "schema_version": "deterministic_readiness_tool_result_v1",
        "uri": uri,
        "source_family": family,
        "entity": entity,
        "source_timestamp": datetime.now(timezone.utc).isoformat(),
        "observations": [
            {
                "label": f"{family}_edge",
                "entity": entity,
                "value": _score_from_text(uri + entity, lo=0.12, hi=0.93),
                "interpretation": f"{family} provides a bounded read on {entity}.",
            }
        ],
        "source_health": {"status": "ok", "staleness_s": 0, "permission_scope": "read_only"},
        "would_change_decision": True,
    }
    if "query_events" in uri:
        result["events"] = [{"entity": entity, "event_type": "readiness_event", "headline": f"{entity} source event"}]
    if "hydromancer" in uri or "node" in uri or "reject" in uri:
        result["node_observations"] = [{"entity": entity, "actor": "informed_cluster", "route_quality": "needs_confirmation"}]
    return SimpleNamespace(
        ok=True,
        error=None,
        result=result,
        tool_call_log_id=log_id,
        cost_usd=0.0,
    )


def _metrics(
    *,
    cycle_id: str,
    seeds: list[SeedCell],
    scouts: list[Any],
    strings: list[dict[str, Any]],
    synthesis: Any,
    geometry: Any,
    action_plan: dict[str, Any],
    coverage: dict[str, Any],
    governor: dict[str, Any],
    self_healing_plan: dict[str, Any],
    self_healing_dispatch: dict[str, Any],
    self_healing_worker: dict[str, Any],
    atlas_n_tools: int,
    atlas_n_sources: int,
    transcript: dict[str, Any],
    elapsed_s: float,
) -> dict[str, Any]:
    errors = [s for s in scouts if getattr(s, "error", None)]
    ok = [s for s in scouts if not getattr(s, "error", None) and getattr(s, "hypothesis_text", "")]
    info_counts = [len(getattr(s, "information_string_ids", []) or []) for s in scouts]
    evidence_counts = [len(getattr(s, "tool_evidence", []) or []) for s in scouts]
    evidence_ok = sum(
        1
        for scout in scouts
        for ev in (getattr(scout, "tool_evidence", []) or [])
        if ev.get("ok")
    )
    evidence_total = sum(evidence_counts)
    hypotheses = [str(getattr(s, "hypothesis_text", "") or "").strip().lower() for s in scouts]
    duplicate_hypotheses = len(hypotheses) - len(set(hypotheses))
    cells = [_cell_key(seed) for seed in seeds]
    strings_by_cell = Counter(
        "|".join([
            str(row.get("entity") or ""),
            str(row.get("horizon") or ""),
            str(row.get("lens") or ""),
            str(row.get("bias_mode") or ""),
        ])
        for row in strings
    )
    flags = Counter(
        flag
        for scout in scouts
        for flag in (getattr(scout, "quality_flags", []) or [])
    )
    entity_counts = Counter(seed.entity for seed in seeds)
    lens_counts = Counter(seed.lens for seed in seeds)
    horizon_counts = Counter(seed.horizon for seed in seeds)
    bias_counts = Counter(seed.bias_mode for seed in seeds)
    asset_counts = Counter(entity_asset_class(seed.entity) for seed in seeds)
    prompt_variants = Counter(getattr(s, "prompt_variant", "") for s in scouts)
    tool_iterations = sum(1 for s in scouts if int(getattr(s, "tool_iteration_count", 0) or 0) > 0)
    top_cell = asdict(geometry.cells[0]) if getattr(geometry, "cells", None) else {}
    return {
        "cycle_id": cycle_id,
        "elapsed_s": round(elapsed_s, 3),
        "atlas": {"tools": atlas_n_tools, "sources": atlas_n_sources},
        "seeds": {
            "count": len(seeds),
            "unique_cell_count": len(set(cells)),
            "unique_cell_ratio": round(len(set(cells)) / max(1, len(cells)), 4),
            "entities": dict(entity_counts.most_common()),
            "asset_classes": dict(asset_counts.most_common()),
            "lenses": dict(lens_counts.most_common()),
            "horizons": dict(horizon_counts.most_common()),
            "bias_modes": dict(bias_counts.most_common()),
            "theme_count": len({seed.theme for seed in seeds if seed.theme}),
        },
        "scouts": {
            "completed": len(ok),
            "errored": len(errors),
            "success_rate": round(len(ok) / max(1, len(scouts)), 4),
            "total_cost_usd_estimate": round(sum(float(getattr(s, "cost_usd", 0.0) or 0.0) for s in scouts), 6),
            "avg_information_strings_per_scout": round(statistics.mean(info_counts) if info_counts else 0.0, 3),
            "min_information_strings": min(info_counts or [0]),
            "max_information_strings": max(info_counts or [0]),
            "avg_evidence_packets_per_scout": round(statistics.mean(evidence_counts) if evidence_counts else 0.0, 3),
            "evidence_ok_rate": round(evidence_ok / max(1, evidence_total), 4),
            "tool_iteration_scouts": tool_iterations,
            "duplicate_hypothesis_rate": round(duplicate_hypotheses / max(1, len(hypotheses)), 4),
            "prompt_variants": dict(prompt_variants.most_common()),
            "top_quality_flags": dict(flags.most_common(20)),
            "error_samples": [
                {"seed_id": getattr(s, "seed_id", ""), "error": getattr(s, "error", ""), "flags": getattr(s, "quality_flags", [])}
                for s in errors[:8]
            ],
        },
        "information_map": {
            "string_count": len(strings),
            "cells_with_strings": sum(1 for _, count in strings_by_cell.items() if count > 0),
            "synthesis_id": getattr(synthesis, "synthesis_id", ""),
            "confluences": len(getattr(synthesis, "confluences", []) or []),
            "tensions": len(getattr(synthesis, "tensions", []) or []),
            "promoted_hypotheses": len(getattr(synthesis, "promoted_hypotheses", []) or []),
        },
        "geometry": {
            "cell_count": len(getattr(geometry, "cells", []) or []),
            "top_cell": top_cell,
            "routing_queue_count": len(action_plan.get("routing_queue") or []),
            "top_actions": (action_plan.get("actions") or [])[:6],
        },
        "coverage": {
            "valid_cell_count": ((coverage.get("grid") or {}).get("valid_cell_count")),
            "covered_count": ((coverage.get("coverage") or {}).get("covered_count")),
            "missing_count": ((coverage.get("coverage") or {}).get("missing_count")),
            "coverage_ratio": ((coverage.get("coverage") or {}).get("coverage_ratio")),
            "ranked_gaps": len(governor.get("ranked_gaps") or []),
            "suggested_seed_cells": len(governor.get("suggested_seed_cells") or []),
            "budget_lanes": governor.get("budget_lanes") or [],
        },
        "self_healing": {
            "work_orders": len(self_healing_plan.get("work_orders") or []),
            "posted_tasks": self_healing_dispatch.get("posted_count"),
            "completed_tasks": self_healing_worker.get("completed_count"),
            "failed_tasks": self_healing_worker.get("failed_count"),
            "tool_proposals": self_healing_worker.get("tool_proposal_count"),
            "promotion_reports": self_healing_worker.get("promotion_report_count"),
        },
        "model_shim": {
            "calls": len(transcript.get("calls") or []),
            "iteration_calls": sum(1 for call in transcript.get("calls") or [] if call.get("iteration")),
        },
    }


def _readiness_verdict(metrics: dict[str, Any]) -> dict[str, Any]:
    scouts = metrics.get("scouts") or {}
    seeds = metrics.get("seeds") or {}
    info = metrics.get("information_map") or {}
    geometry = metrics.get("geometry") or {}
    self_healing = metrics.get("self_healing") or {}
    gates = {
        "seed_count_100": seeds.get("count") == 100,
        "unique_cell_ratio_ge_0_92": _metric_float(seeds.get("unique_cell_ratio")) >= 0.92,
        "scout_success_rate_ge_0_95": _metric_float(scouts.get("success_rate")) >= 0.95,
        "avg_strings_ge_1_5": _metric_float(scouts.get("avg_information_strings_per_scout")) >= 1.5,
        "evidence_ok_rate_ge_0_95": _metric_float(scouts.get("evidence_ok_rate")) >= 0.95,
        "duplicate_hypothesis_rate_le_0_20": _metric_float(scouts.get("duplicate_hypothesis_rate"), default=1.0) <= 0.20,
        "synthesis_promoted": int(info.get("promoted_hypotheses") or 0) >= 4,
        "geometry_cells_created": int(geometry.get("cell_count") or 0) >= 8,
        "routing_queue_created": int(geometry.get("routing_queue_count") or 0) >= 1,
        "self_healing_completed": int(self_healing.get("completed_tasks") or 0) >= 1,
        "no_self_healing_failures": int(self_healing.get("failed_tasks") or 0) == 0,
    }
    failed = [name for name, ok in gates.items() if not ok]
    ready = not failed
    return {
        "status": "pass" if ready else "warn" if len(failed) <= 2 else "fail",
        "ready_for_live_1000": ready,
        "gates": gates,
        "failed_gates": failed,
        "interpretation": (
            "The orchestration layer is ready for a live canary and then a 1,000-scout run."
            if ready else
            "The slice surfaced specific gates to fix before spending on a full live run."
        ),
    }


def _readiness_trace(
    *,
    cycle_id: str,
    scouts: list[Any],
    strings: list[dict[str, Any]],
    geometry: Any,
    action_plan: dict[str, Any],
    coverage: dict[str, Any],
    governor: dict[str, Any],
) -> dict[str, Any]:
    top_scout = next((s for s in scouts if getattr(s, "information_string_ids", None)), scouts[0] if scouts else None)
    top_cell = asdict(geometry.cells[0]) if getattr(geometry, "cells", None) else {}
    return {
        "cycle_id": cycle_id,
        "input_packet": {
            "cell": {
                "entity": getattr(top_scout, "entity", "MARKET"),
                "horizon": getattr(top_scout, "horizon", "1d"),
                "lens": getattr(top_scout, "lens", "anomaly"),
                "bias_mode": getattr(top_scout, "bias_mode", "frontier"),
            }
        },
        "stage_io": [
            {"stage_id": "tier0_seeds", "output_count": len(scouts)},
            {"stage_id": "tier1_scouts", "output_count": len(strings)},
            {"stage_id": "alpha_geometry", "output_count": len(getattr(geometry, "cells", []) or [])},
            {"stage_id": "map_governor", "output_count": len(governor.get("ranked_gaps") or [])},
        ],
        "persisted_objects": [
            {"surface": "information_strings", "ids": [row.get("id") for row in strings[:20] if row.get("id")]},
        ],
        "market_map_plan": {
            "axes": {
                "entity": {
                    "count": len(set(row.get("entity") for row in strings)),
                    "manifest": {"source_quality": "readiness_slice", "source_counts": {"generated": len(scouts)}},
                }
            },
            "validity": {"valid_cell_count": (coverage.get("grid") or {}).get("valid_cell_count")},
            "data_source_universe": {
                "count": 6,
                "surfaces": [
                    {"key": "mempool_pending_intent", "title": "Mempool Pending Intent", "status": "tool_gap"},
                    {"key": "source_health_citations", "title": "Source Health Citations", "status": "tool_gap"},
                ],
            },
        },
        "final_results": {
            "hypothesis": getattr(top_scout, "hypothesis_text", ""),
            "information_string_ids": getattr(top_scout, "information_string_ids", []) or [],
            "evidence_receipts": [
                ev.get("tool_call_log_id")
                for ev in (getattr(top_scout, "tool_evidence", []) or [])
                if ev.get("tool_call_log_id")
            ],
            "geometry": {"route_directive": top_cell.get("route_directive") or "observe"},
            "data_surface_coverage": {
                "touched": (coverage.get("coverage") or {}).get("covered_count"),
                "total": (coverage.get("grid") or {}).get("valid_cell_count"),
            },
            "recurrent_loop": {
                "status": "ready" if action_plan.get("routing_queue") else "no_route",
                "shape_tool_call_log_id": "tc_readiness_shape_reader",
                "emitted_seed_id": "seed_readiness_next",
                "worker_assignment": (action_plan.get("routing_queue") or [{}])[0],
            },
        },
    }


def _render_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    readiness = report["readiness"]
    lines = [
        "# 100 Scout Readiness Slice",
        "",
        f"- status: `{readiness['status']}`",
        f"- ready_for_live_1000: `{readiness['ready_for_live_1000']}`",
        f"- mode: `{report['mode']}`",
        f"- cycle: `{report['cycle_id']}`",
        f"- scouts: `{metrics['seeds']['count']}`",
        f"- success_rate: `{metrics['scouts']['success_rate']}`",
        f"- avg_strings_per_scout: `{metrics['scouts']['avg_information_strings_per_scout']}`",
        f"- evidence_ok_rate: `{metrics['scouts']['evidence_ok_rate']}`",
        f"- duplicate_hypothesis_rate: `{metrics['scouts']['duplicate_hypothesis_rate']}`",
        f"- geometry_cells: `{metrics['geometry']['cell_count']}`",
        f"- self_healing_completed_tasks: `{metrics['self_healing']['completed_tasks']}`",
        "",
        "## Gates",
        "",
    ]
    for name, ok in readiness["gates"].items():
        lines.append(f"- {'PASS' if ok else 'FAIL'} `{name}`")
    lines.extend([
        "",
        "## Important Boundary",
        "",
        "This proves orchestration, storage, map geometry, and repair routing with an offline deterministic model/tool shim. It does not prove live provider quality, live data freshness, or exchange/API availability.",
        "",
        "## Next Gate",
        "",
        report["next_live_gate"]["recommendation"],
    ])
    return "\n".join(lines) + "\n"


def _artifact_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path(tempfile.gettempdir()) / f"talis-100-scout-readiness-{stamp}"
    root.mkdir(parents=True, exist_ok=False)
    return root


def _seed_payload(seed: SeedCell) -> dict[str, Any]:
    return {
        "seed_id": seed.seed_id,
        "entity": seed.entity,
        "asset_class": entity_asset_class(seed.entity),
        "horizon": seed.horizon,
        "lens": seed.lens,
        "bias_mode": seed.bias_mode,
        "theme": seed.theme,
        "weight": seed.weight,
        "coverage_penalty": seed.coverage_penalty,
        "frontier_boost": seed.frontier_boost,
        "payload": seed.payload,
    }


def _cell_key(seed: SeedCell) -> str:
    return "|".join([seed.entity, seed.horizon, seed.lens, seed.bias_mode, str(seed.theme or "")])


def _source_family(uri: str) -> str:
    text = uri.lower()
    if "hydromancer" in text:
        return "hydromancer"
    if "jarvis" in text or "node" in text or "reject" in text:
        return "our_node"
    if "parallel" in text or "search" in text:
        return "parallel_web"
    if "event" in text:
        return "event_feed"
    if "timeseries" in text:
        return "market_timeseries"
    if "source_health" in text:
        return "source_health"
    return "tool_atlas"


def _horizon_delta(horizon: str) -> timedelta:
    if horizon in {"tick", "second", "minute", "hour", "intraday"}:
        return timedelta(hours=8)
    if horizon == "1d":
        return timedelta(days=1)
    if horizon == "1w":
        return timedelta(days=7)
    if horizon == "1m":
        return timedelta(days=30)
    if horizon == "1q":
        return timedelta(days=90)
    return timedelta(days=180)


def _score_from_text(text: str, *, lo: float, hi: float) -> float:
    raw = sum((i + 1) * ord(ch) for i, ch in enumerate(text or "x")) % 1000
    return lo + (hi - lo) * (raw / 999.0)


def _unique(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        out.append(item)
        seen.add(item)
    return out


def _metric_float(raw: Any, *, default: float = 0.0) -> float:
    try:
        return float(raw)
    except Exception:
        return default


def _asdict(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [_asdict(v) for v in value]
    if isinstance(value, dict):
        return {k: _asdict(v) for k, v in value.items()}
    return value


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
