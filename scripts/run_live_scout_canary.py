#!/usr/bin/env python
"""Run a live-provider scout canary/ramp before scaling scout spend.

The deterministic readiness slice proves orchestration with a local model/tool
shim. This runner proves the next layer: real provider import, real prompt
formatting, real tool atlas, real evidence dispatch, real string storage, and
the same map/geometry/governor path under a hard cost cap. It can run tiny
canaries, 100-scout distribution ramps, and 1,000-scout shadow candidates; the
tournament evaluator remains the promotion authority.

Live calls require ``--allow-live-spend`` on purpose. Running without it still
writes a preflight report and viewer artifacts, but it will not call a model.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from talis_desk._tic_config import ensure_tic_on_path, get_tic_root
from talis_desk.information_map import (
    compute_alpha_geometry,
    plan_alpha_geometry_actions,
    recent_information_strings,
    run_information_synthesis,
)
from talis_desk.information_map.deep_scout_prompt import build_deep_scout_system_prompt
from talis_desk.market_map.coverage_audit import build_coverage_gap_manifest
from talis_desk.market_map.governor import build_market_map_governor_plan
from talis_desk.market_map.self_healing import (
    build_market_map_self_healing_plan,
    execute_market_map_self_healing_tasks,
    post_market_map_self_healing_work_orders,
)
from talis_desk.market_map.universe import build_market_universe
from talis_desk.store import reset_desk_store_for_test
from talis_desk.swarm.scout_runner import (
    _apply_prompt_contract_pressure,
    _build_user_prompt,
    run_scouts,
)
from talis_desk.swarm.seed_generator import (
    DEFAULT_ENTITIES,
    SeedCell,
    generate_seeds,
)
from talis_desk.tool_atlas import regenerate_tool_atlas

from scripts.run_100_scout_readiness_slice import (
    DEFAULT_THEMES,
    _asdict,
    _metrics,
    _prepare_seed,
    _readiness_trace,
    _seed_payload,
    _write_json,
    _write_text,
)


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

    cycle_id = args.cycle_id or f"cycle_live_scout_canary_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    db_path = Path(args.db).expanduser().resolve() if args.db else artifact_dir / "desk-live-canary.db"
    store = reset_desk_store_for_test(db_path)
    restore_chat: Callable[[], None] | None = None
    try:
        preflight = _preflight(cycle_id=cycle_id)
        universe = build_market_universe(default_entities=DEFAULT_ENTITIES)
        seeds = generate_seeds(
            n_seeds=args.n_scouts,
            cycle_id=cycle_id,
            entities=universe.entity_symbols() or DEFAULT_ENTITIES,
            themes=DEFAULT_THEMES,
            rng_seed=args.seed_rng,
            theme_share=args.theme_share,
        )
        seeds = [_prepare_live_seed(seed, args=args) for seed in seeds]
        seed_path = prompt_output_dir / "live_scout_canary_seeds.json"
        _write_json(seed_path, [_seed_payload(seed) for seed in seeds])

        if not args.allow_live_spend:
            report = _blocked_report(
                args=args,
                cycle_id=cycle_id,
                db_path=db_path,
                artifact_dir=artifact_dir,
                prompt_output_dir=prompt_output_dir,
                seed_path=seed_path,
                preflight=preflight,
                reason="explicit_live_spend_flag_missing",
                elapsed_s=time.perf_counter() - started,
            )
            _write_canary_report(prompt_output_dir, report)
            _print_report_paths(prompt_output_dir, report)
            return 0

        if not preflight.get("provider_import_ok"):
            report = _blocked_report(
                args=args,
                cycle_id=cycle_id,
                db_path=db_path,
                artifact_dir=artifact_dir,
                prompt_output_dir=prompt_output_dir,
                seed_path=seed_path,
                preflight=preflight,
                reason="provider_import_failed",
                elapsed_s=time.perf_counter() - started,
            )
            _write_canary_report(prompt_output_dir, report)
            _print_report_paths(prompt_output_dir, report)
            return 0

        transcript: dict[str, Any] = {"calls": []}
        restore_chat = _install_live_chat_recorder(
            transcript,
            timeout_s=args.provider_timeout_s,
            progress_path=prompt_output_dir / "live_scout_canary_transcript_progress.json",
        )
        atlas = regenerate_tool_atlas()
        scouts = run_scouts(
            seeds=seeds,
            cycle_id=cycle_id,
            model=args.model,
            fallback=args.fallback,
            concurrency=args.concurrency,
            cost_cap_usd=args.cost_cap_usd,
        )
        outputs_path = prompt_output_dir / "live_scout_canary_outputs.json"
        _write_json(outputs_path, [_asdict(row) for row in scouts])
        _write_primary_live_artifacts(
            prompt_output_dir=prompt_output_dir,
            seeds=seeds,
            scouts=scouts,
            transcript=transcript,
        )

        strings = recent_information_strings(cycle_id=cycle_id, conn=store.conn, limit=max(200, args.n_scouts * 10))
        synthesis = run_information_synthesis(
            cycle_id=cycle_id,
            max_strings=max(50, args.n_scouts * 3),
            use_llm=False,
        )
        geometry = compute_alpha_geometry(cycle_id=cycle_id, conn=store.conn, persist=True)
        action_plan = plan_alpha_geometry_actions(cycle_id=cycle_id, conn=store.conn, limit=64)
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
            limit=args.self_healing_limit,
        )
        self_healing_worker = execute_market_map_self_healing_tasks(
            cycle_id=cycle_id,
            conn=store.conn,
            limit=args.self_healing_limit,
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
        transcript_summary = _transcript_summary(transcript)
        verdict = _live_canary_verdict(
            metrics,
            cost_cap_usd=args.cost_cap_usd,
            n_scouts=args.n_scouts,
            transcript_summary=transcript_summary,
        )
        report = {
            "schema_version": "talis_live_scout_canary_v1",
            "mode": "live_provider_cost_capped",
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
            "provider_timeout_s": args.provider_timeout_s,
            "prompt_variant_override": args.prompt_variant,
            "max_tool_iterations": args.max_tool_iterations,
            "preflight": preflight,
            "metrics": metrics,
            "verdict": verdict,
            "artifacts": {
                "seeds": str(seed_path),
                "outputs": str(outputs_path),
            },
            "transcript_summary": transcript_summary,
            "scale_decision": _scale_decision(verdict, n_scouts=args.n_scouts),
        }
        _write_canary_report(prompt_output_dir, report)
        _print_report_paths(prompt_output_dir, report)
        return 0 if verdict["status"] in {"pass", "warn"} else 1
    finally:
        if restore_chat is not None:
            restore_chat()
        store.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-scouts", type=int, default=10)
    parser.add_argument("--cycle-id", default="")
    parser.add_argument("--db", default="")
    parser.add_argument("--artifact-dir", default="")
    parser.add_argument("--prompt-output-dir", default="")
    parser.add_argument("--seed-rng", type=int, default=20260522)
    parser.add_argument("--theme-share", type=float, default=0.20)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--cost-cap-usd", type=float, default=0.10)
    parser.add_argument("--self-healing-limit", type=int, default=6)
    parser.add_argument("--provider-timeout-s", type=float, default=45.0)
    parser.add_argument("--prompt-variant", default="", help="Optional forced scout prompt variant for this canary.")
    parser.add_argument("--max-tool-iterations", type=int, default=1)
    parser.add_argument("--model", default="deepseek:v4-flash")
    parser.add_argument("--fallback", default="anthropic:claude-haiku-4-5")
    parser.add_argument(
        "--allow-live-spend",
        action="store_true",
        help="Actually call the configured live model provider under the cost cap.",
    )
    return parser.parse_args()


def _prepare_live_seed(seed: SeedCell, *, args: argparse.Namespace) -> SeedCell:
    seed = _prepare_seed(seed)
    payload = dict(seed.payload or {})
    if args.prompt_variant:
        payload["prompt_variant"] = args.prompt_variant
    payload["max_tool_iterations"] = max(0, int(args.max_tool_iterations or 0))
    seed.payload = payload
    return seed


def _preflight(*, cycle_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "cycle_id": cycle_id,
        "tic_root_ok": False,
        "provider_import_ok": False,
        "tool_atlas_ok": False,
        "market_universe_ok": False,
    }
    try:
        root = get_tic_root()
        out["tic_root_ok"] = True
        out["tic_root"] = str(root)
    except Exception as exc:
        out["tic_root_error"] = f"{type(exc).__name__}: {exc}"
    try:
        ensure_tic_on_path()
        from tic.desk.models import chat as _chat  # type: ignore

        out["provider_import_ok"] = callable(_chat)
        out["provider_import"] = "tic.desk.models.chat"
    except Exception as exc:
        out["provider_import_error"] = f"{type(exc).__name__}: {exc}"
    try:
        atlas = regenerate_tool_atlas()
        out["tool_atlas_ok"] = bool(getattr(atlas, "n_tools", 0) or getattr(atlas, "n_sources", 0))
        out["tool_atlas"] = {
            "tools": getattr(atlas, "n_tools", 0),
            "sources": getattr(atlas, "n_sources", 0),
            "skills": getattr(atlas, "n_skills", 0),
        }
    except Exception as exc:
        out["tool_atlas_error"] = f"{type(exc).__name__}: {exc}"
    try:
        universe = build_market_universe(default_entities=DEFAULT_ENTITIES)
        out["market_universe_ok"] = bool(universe.entity_symbols())
        out["market_universe"] = {
            "entity_count": len(universe.entity_symbols()),
            "source_quality": getattr(universe, "source_quality", ""),
        }
    except Exception as exc:
        out["market_universe_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _install_live_chat_recorder(
    transcript: dict[str, Any],
    *,
    timeout_s: float,
    progress_path: Path,
) -> Callable[[], None]:
    ensure_tic_on_path()
    from tic.desk import models as tic_models  # type: ignore

    real_chat = tic_models.chat

    async def recorded_chat(
        model: str,
        system: str,
        user: str,
        *,
        max_tokens: int,
        fallback: str | None = None,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        call: dict[str, Any] = {
            "index": len(transcript.setdefault("calls", [])),
            "model": model,
            "fallback": fallback,
            "max_tokens": max_tokens,
            "system_prompt": system,
            "user_prompt": user,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            res = await asyncio.wait_for(
                real_chat(model, system, user, max_tokens=max_tokens, fallback=fallback),
                timeout=max(1.0, timeout_s),
            )
        except Exception as exc:
            call["elapsed_s"] = round(time.perf_counter() - t0, 3)
            call["error"] = f"{type(exc).__name__}: {exc}"
            transcript.setdefault("calls", []).append(call)
            _safe_write_progress(progress_path, transcript)
            raise
        call["elapsed_s"] = round(time.perf_counter() - t0, 3)
        call["response_envelope"] = {k: v for k, v in res.items() if k != "text"}
        call["text"] = str(res.get("text") or "")
        transcript.setdefault("calls", []).append(call)
        _safe_write_progress(progress_path, transcript)
        return res

    tic_models.chat = recorded_chat

    def restore() -> None:
        tic_models.chat = real_chat

    return restore


def _write_primary_live_artifacts(
    *,
    prompt_output_dir: Path,
    seeds: list[SeedCell],
    scouts: list[Any],
    transcript: dict[str, Any],
) -> None:
    first_call = next((c for c in transcript.get("calls") or [] if isinstance(c, dict)), {})
    first_scout = next((s for s in scouts if not getattr(s, "error", None)), scouts[0] if scouts else None)
    first_seed = next(
        (seed for seed in seeds if first_scout is not None and seed.seed_id == getattr(first_scout, "seed_id", "")),
        seeds[0] if seeds else None,
    )
    system_prompt = str(first_call.get("system_prompt") or "")
    user_prompt = str(first_call.get("user_prompt") or "")
    if first_seed is not None and first_scout is not None:
        evidence = getattr(first_scout, "tool_evidence", []) or []
        if not user_prompt:
            user_prompt = _build_user_prompt(first_seed, tool_evidence=evidence)
        if not system_prompt:
            system_prompt = _apply_prompt_contract_pressure(
                build_deep_scout_system_prompt(getattr(first_scout, "prompt_variant", "") or "receptive_field_v1"),
                first_seed,
            )
    else:
        evidence = []
    response_text = str(first_call.get("text") or "")
    parsed_response: Any = None
    if response_text:
        parsed_response = _extract_first_json(response_text)
    response_envelope = {
        **{k: v for k, v in first_call.get("response_envelope", {}).items() if k != "text"},
        "text": response_text,
    }
    _write_text(prompt_output_dir / "live_scout_system_prompt.md", system_prompt)
    _write_text(prompt_output_dir / "live_scout_user_prompt.md", user_prompt)
    _write_json(prompt_output_dir / "live_scout_tool_evidence.json", evidence)
    _write_json(prompt_output_dir / "live_scout_model_output.json", parsed_response if parsed_response is not None else {"raw_text": response_text})
    _write_json(prompt_output_dir / "live_scout_model_response_envelope.json", response_envelope)
    _write_json(prompt_output_dir / "live_scout_persisted_output.json", _asdict(first_scout) if first_scout is not None else {})
    _write_json(prompt_output_dir / "live_scout_transcript.json", transcript)


def _live_canary_verdict(
    metrics: dict[str, Any],
    *,
    cost_cap_usd: float,
    n_scouts: int,
    transcript_summary: dict[str, Any],
) -> dict[str, Any]:
    scouts = metrics.get("scouts") if isinstance(metrics.get("scouts"), dict) else {}
    info = metrics.get("information_map") if isinstance(metrics.get("information_map"), dict) else {}
    geometry = metrics.get("geometry") if isinstance(metrics.get("geometry"), dict) else {}
    self_healing = metrics.get("self_healing") if isinstance(metrics.get("self_healing"), dict) else {}
    cost = _to_float(scouts.get("total_cost_usd_estimate"))
    flags = scouts.get("top_quality_flags") if isinstance(scouts.get("top_quality_flags"), dict) else {}
    provider_error_count = sum(
        int(v)
        for k, v in flags.items()
        if "provider" in str(k).lower() or "json_unparseable" in str(k).lower()
    )
    scout_error_count = int(scouts.get("errored") or 0)
    sample_n = int(n_scouts or 0)
    stage_error_budget = max(1, int(sample_n * 0.02))
    gates = {
        "sample_size_ge_10": sample_n >= 10,
        "cost_below_cap": cost <= max(0.0, cost_cap_usd),
        "provider_call_errors_eq_0": len(transcript_summary.get("errors") or []) == 0,
        "scout_success_rate_ge_0_70": _to_float(scouts.get("success_rate")) >= 0.70,
        "avg_strings_ge_1_00": _to_float(scouts.get("avg_information_strings_per_scout")) >= 1.00,
        "evidence_ok_rate_ge_0_60": _to_float(scouts.get("evidence_ok_rate")) >= 0.60,
        "duplicate_hypothesis_rate_le_0_35": _to_float(scouts.get("duplicate_hypothesis_rate"), default=1.0) <= 0.35,
        "provider_json_errors_within_stage_budget": provider_error_count <= stage_error_budget,
        "scout_errors_within_stage_budget": scout_error_count <= stage_error_budget,
        "information_strings_created": int(info.get("string_count") or 0) >= 1,
        "synthesis_promoted": int(info.get("promoted_hypotheses") or 0) >= 1,
        "geometry_cells_created": int(geometry.get("cell_count") or 0) >= 1,
        "self_healing_no_failures": int(self_healing.get("failed_tasks") or 0) == 0,
    }
    failed = [name for name, ok in gates.items() if not ok]
    status = "pass" if not failed else "warn" if len(failed) <= 2 else "fail"
    ready_for_next = status == "pass" and sample_n >= 10
    ready_for_100 = ready_for_next and sample_n < 100
    ready_for_tournament = ready_for_next and sample_n >= 100
    if ready_for_next and sample_n >= 1000:
        interpretation = (
            "The 1,000-scout live shadow candidate has clean raw canary gates. "
            "Run the tournament evaluator next; only the tournament can promote it "
            "to a repeat shadow trial, and scheduled production stays blocked until "
            "repeatability is proven."
        )
    elif ready_for_tournament:
        interpretation = (
            "The 100-scout live ramp is clean. Run the tournament evaluator before "
            "any 1,000-scout spend."
        )
    elif ready_for_100:
        interpretation = (
            "The live provider canary is clean. Run a 100-scout live ramp next, "
            "then promote to 1,000 only if the same quality curve holds."
        )
    elif status in {"pass", "warn"} and sample_n < 10:
        interpretation = "This was a useful live smoke, but not enough to open the next spend gate."
    else:
        interpretation = "The live canary found provider/data-quality issues. Fix these before increasing spend."
    return {
        "status": status,
        "ready_for_next_live_100": ready_for_100,
        "ready_for_live_1000_tournament": ready_for_tournament,
        "ready_for_direct_live_1000": False,
        "gates": gates,
        "failed_gates": failed,
        "interpretation": interpretation,
    }


def _blocked_report(
    *,
    args: argparse.Namespace,
    cycle_id: str,
    db_path: Path,
    artifact_dir: Path,
    prompt_output_dir: Path,
    seed_path: Path,
    preflight: dict[str, Any],
    reason: str,
    elapsed_s: float,
) -> dict[str, Any]:
    gates = {
        "tic_root_ok": bool(preflight.get("tic_root_ok")),
        "provider_import_ok": bool(preflight.get("provider_import_ok")),
        "tool_atlas_ok": bool(preflight.get("tool_atlas_ok")),
        "market_universe_ok": bool(preflight.get("market_universe_ok")),
        "explicit_live_spend_allowed": bool(args.allow_live_spend),
    }
    return {
        "schema_version": "talis_live_scout_canary_v1",
        "mode": "preflight_no_live_spend",
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
        "provider_timeout_s": args.provider_timeout_s,
        "prompt_variant_override": args.prompt_variant,
        "max_tool_iterations": args.max_tool_iterations,
        "preflight": preflight,
        "verdict": {
            "status": "blocked",
            "reason": reason,
            "ready_for_next_live_100": False,
            "ready_for_direct_live_1000": False,
            "gates": gates,
            "failed_gates": [name for name, ok in gates.items() if not ok],
            "interpretation": "No live model calls were made. This is an anti-waste preflight artifact.",
        },
        "metrics": {"elapsed_s": round(elapsed_s, 3)},
        "artifacts": {"seeds": str(seed_path)},
        "scale_decision": {
            "decision": "do_not_scale_yet",
            "next_step": "Run this same script with --allow-live-spend under a tiny cost cap, then inspect the live canary gates.",
        },
    }


def _scale_decision(verdict: dict[str, Any], *, n_scouts: int) -> dict[str, Any]:
    if verdict.get("ready_for_live_1000_tournament") and int(n_scouts or 0) >= 1000:
        return {
            "decision": "evaluate_shadow_production_trial",
            "next_step": (
                "Run the live scout tournament evaluator over this 1,000-scout report. "
                "Promote only to a repeat 1,000-scout shadow trial if scale gates pass; "
                "scheduled production requires repeatability across independent 1,000-scout runs."
            ),
            "why_not_scheduled_production": (
                "A single 1,000-scout pass proves scale shape, not operational repeatability. "
                "The next proof is a second independent 1,000-scout shadow run with stable "
                "provider reliability, prompt structure, geometry, and coverage deltas."
            ),
        }
    if verdict.get("ready_for_live_1000_tournament"):
        return {
            "decision": "evaluate_live_1000_ramp_next",
            "next_step": "Run the live scout tournament evaluator over the 100-scout report. Promote to 1,000 only if the distribution gates pass.",
            "why_not_direct_1000": "A 100-scout ramp proves a broader distribution than the 10-scout canary, but the tournament still needs to check provider reliability, redundancy, prompt quality, temporal structure, and geometry before a 1,000-scout run.",
        }
    if verdict.get("ready_for_next_live_100"):
        return {
            "decision": "run_live_100_ramp_next",
            "next_step": "Run 100 live scouts with the same prompt-output capture and stop immediately if canary gates regress.",
            "why_not_direct_1000": f"A {n_scouts}-scout canary proves provider compatibility and quality shape, but the next paid step should validate distributional stability at 100.",
        }
    if verdict.get("status") in {"pass", "warn"} and int(n_scouts or 0) < 10:
        return {
            "decision": "finish_10_scout_live_canary",
            "next_step": "Run the full 10-scout live canary with the same hard cap and transcript capture before a 100-scout ramp.",
        }
    return {
        "decision": "do_not_scale_yet",
        "next_step": "Inspect failed live canary gates, repair prompt/tool/data failures, and repeat the 10-scout canary.",
    }


def _transcript_summary(transcript: dict[str, Any]) -> dict[str, Any]:
    calls = [c for c in transcript.get("calls") or [] if isinstance(c, dict)]
    return {
        "call_count": len(calls),
        "models": sorted({str(c.get("model") or "") for c in calls if c.get("model")}),
        "providers": sorted({
            str((c.get("response_envelope") or {}).get("provider") or "")
            for c in calls
            if isinstance(c.get("response_envelope"), dict) and (c.get("response_envelope") or {}).get("provider")
        }),
        "errors": [str(c.get("error")) for c in calls if c.get("error")],
        "prompt_chars": sum(len(str(c.get("system_prompt") or "")) + len(str(c.get("user_prompt") or "")) for c in calls),
        "response_chars": sum(len(str(c.get("text") or "")) for c in calls),
    }


def _write_canary_report(prompt_output_dir: Path, report: dict[str, Any]) -> None:
    _write_json(prompt_output_dir / "live_scout_canary_report.json", report)
    _write_text(prompt_output_dir / "live_scout_canary_report.md", _render_markdown(report))


def _safe_write_progress(path: Path, transcript: dict[str, Any]) -> None:
    try:
        _write_json(path, transcript)
    except Exception:
        pass


def _render_markdown(report: dict[str, Any]) -> str:
    verdict = report.get("verdict") if isinstance(report.get("verdict"), dict) else {}
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    scouts = metrics.get("scouts") if isinstance(metrics.get("scouts"), dict) else {}
    info = metrics.get("information_map") if isinstance(metrics.get("information_map"), dict) else {}
    lines = [
        "# Live Scout Canary",
        "",
        f"- status: `{verdict.get('status')}`",
        f"- mode: `{report.get('mode')}`",
        f"- cycle: `{report.get('cycle_id')}`",
        f"- scouts: `{report.get('n_scouts_requested')}`",
        f"- cost_cap_usd: `{report.get('cost_cap_usd')}`",
        f"- estimated_cost_usd: `{scouts.get('total_cost_usd_estimate', 0)}`",
        f"- success_rate: `{scouts.get('success_rate', 0)}`",
        f"- avg_strings_per_scout: `{scouts.get('avg_information_strings_per_scout', 0)}`",
        f"- information_strings: `{info.get('string_count', 0)}`",
        "",
        "## Gates",
        "",
    ]
    for name, ok in (verdict.get("gates") or {}).items():
        lines.append(f"- {'PASS' if ok else 'FAIL'} `{name}`")
    lines.extend([
        "",
        "## Decision",
        "",
        str((report.get("scale_decision") or {}).get("decision") or ""),
        "",
        str((report.get("scale_decision") or {}).get("next_step") or ""),
    ])
    return "\n".join(lines) + "\n"


def _print_report_paths(prompt_output_dir: Path, report: dict[str, Any]) -> None:
    verdict = report.get("verdict") if isinstance(report.get("verdict"), dict) else {}
    print(f"LIVE_CANARY_STATUS={verdict.get('status')}")
    print(f"LIVE_CANARY_READY_FOR_NEXT_100={verdict.get('ready_for_next_live_100')}")
    print(f"LIVE_CANARY_REPORT_JSON={prompt_output_dir / 'live_scout_canary_report.json'}")
    print(f"LIVE_CANARY_REPORT_MD={prompt_output_dir / 'live_scout_canary_report.md'}")
    print(f"LIVE_CANARY_PROMPT_OUTPUT_DIR={prompt_output_dir}")


def _artifact_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path(tempfile.gettempdir()) / f"talis-live-scout-canary-{stamp}"
    root.mkdir(parents=True, exist_ok=False)
    return root


def _to_float(raw: Any, *, default: float = 0.0) -> float:
    try:
        return float(raw)
    except Exception:
        return default


def _extract_first_json(text: str) -> Any:
    start = (text or "").find("{")
    if start < 0:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


if __name__ == "__main__":
    raise SystemExit(main())
