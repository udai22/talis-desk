"""Talis Desk v5 swarm orchestrator.

Replaces the per-specialist sequential pipeline with a 4-tier parallel
swarm. See `docs/REFLECTION_AND_REFACTOR_v5.md` for the architecture
spec.

Tier 0  -> Latin Hypercube seed generation + coverage / theme / density
            modifiers (talis_desk.swarm.seed_generator)
Tier 1  -> 1000 DeepSeek Flash scouts in parallel (asyncio.gather +
            semaphore=50) (talis_desk.swarm.scout_runner)
Tier 1.33 -> information-map synthesis; promotes only high-signal
            confluences/tensions from scout strings
Tier 1.5 -> 3-family verifier council; 2/3 majority graduates to Tier 2
            (talis_desk.swarm.verifier_council)
Tier 2  -> top-50 verified scouts routed to constitutional specialists
            (analyst_pool) - the 21 existing personas, unchanged
Tier 3  -> existing 6-stage adversarial pipeline, scoped to top-20
            analyst outputs (talis_desk.reports.pipeline)
Tier 4  -> Opus brief synthesis via compose_brief
            (talis_desk.swarm.brief_synthesis)

Usage:
    python3 run_swarm.py [--db PATH] [--budget 5.0] [--n-seeds 1000]

Defaults: db = ~/.talis/desk.db, budget = 5.0 USD per cycle, n-seeds = 1000.

NO STUBS. Every LLM call walks the multi-provider fallback chain. If a
tier can't get a real verifier/analyst, outputs are dropped with
`quality_flag=['<tier>_unavailable']` — never fabricate output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# talis-tic must be importable before any talis_desk imports that lazy-load it.
from talis_desk._tic_config import ensure_tic_on_path as _ensure_tic_on_path
from talis_desk._tic_config import get_tic_root as _get_tic_root

_ensure_tic_on_path()
TIC_PATH = str(_get_tic_root())


def banner(s: str) -> None:
    print()
    print("=" * 80)
    print(s)
    print("=" * 80)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=str, default=None,
                   help="desk.db path; default ~/.talis/desk.db")
    p.add_argument("--cycle-id", type=str, default=None)
    p.add_argument("--scope", type=str, default="market")
    p.add_argument("--budget", type=float, default=5.0,
                   help="hard USD cap per cycle")
    p.add_argument("--n-seeds", type=int, default=1000)
    p.add_argument("--seed-rng", type=int, default=None,
                   help="deterministic Tier-0 seed RNG; default derives from cycle/scope/themes")
    p.add_argument("--randomize-seeds", action="store_true",
                   help="use timestamp entropy for exploratory seed generation")
    p.add_argument("--scout-concurrency", type=int, default=50)
    p.add_argument("--stop-after-scouts", action="store_true",
                   help="run Tier 0 + Tier 1 only, then write a scout walkthrough manifest")
    p.add_argument("--skip-information-synthesis", action="store_true",
                   help="skip Tier 1.33 information-map synthesis")
    p.add_argument("--information-synthesis-max-strings", type=int, default=200,
                   help="max scout strings sent into the attention synthesis pass")
    p.add_argument("--information-synthesis-topk", type=int, default=6,
                   help="max synthesized candidates added to verifier input")
    p.add_argument("--information-synthesis-deterministic", action="store_true",
                   help="run synthesis with local deterministic scoring only")
    p.add_argument("--skip-market-map-llm-governor", action="store_true",
                   help="disable the frontier LLM brain that can promote gated market-map seed repairs")
    p.add_argument("--market-map-llm-model", type=str, default="anthropic:claude-opus-4-7",
                   help="frontier model used for the market-map governor brain")
    p.add_argument("--analyst-topk", type=int, default=50)
    p.add_argument("--adversarial-topk", type=int, default=20,
                   help="how many analyst drafts run through Tier 3")
    p.add_argument("--skip-adversarial", action="store_true",
                   help="skip Tier 3 (smoke test path)")
    p.add_argument("--skip-flow-sim", action="store_true",
                   help="skip Tier 3.5 FlowSim (smoke test path)")
    p.add_argument("--flow-sim-topk", type=int, default=10,
                   help="number of top analyst hypotheses to run through FlowSim")
    p.add_argument("--skip-depth-render", action="store_true",
                   help="skip the Tier 4.5 per-asset depth renderer")
    p.add_argument("--skip-director", action="store_true",
                   help="skip Research Director curriculum generation")
    p.add_argument("--director-timeout-s", type=float, default=60.0,
                   help="max wall seconds for the Research Director LLM call")
    p.add_argument("--paper-only", action="store_true", default=True)
    p.add_argument("--live", dest="paper_only", action="store_false")
    args = p.parse_args()

    if args.db is None:
        db_path = Path.home() / ".talis" / "desk.db"
    else:
        db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    cycle_id = args.cycle_id or f"swarm_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}"

    banner(f"TALIS SWARM v5 — {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"  desk.db          = {db_path}")
    print(f"  cycle_id         = {cycle_id}")
    print(f"  scope            = {args.scope}")
    print(f"  budget (USD)     = ${args.budget}")
    print(f"  n_seeds          = {args.n_seeds}")
    print(f"  seed_rng_mode    = {'randomized' if args.randomize_seeds else 'deterministic'}")
    print(f"  scout_concur     = {args.scout_concurrency}")
    print(f"  info_synth_topk  = {args.information_synthesis_topk}")
    print(f"  map_llm_governor = {'off' if args.skip_market_map_llm_governor else args.market_map_llm_model}")
    print(f"  analyst_topk     = {args.analyst_topk}")
    print(f"  adversarial_topk = {args.adversarial_topk}")
    print(f"  tic.db           = {TIC_PATH}/tic/tic.db")

    # ----- Pin desk store -------------------------------------------------
    from talis_desk.store import DeskStore
    import talis_desk.store as _store_mod
    _store_mod._STORE = DeskStore(db_path=db_path)
    from talis_desk.cost_ledger import get_cost_ledger
    _ledger = get_cost_ledger()

    # ----- Storage replication (best-effort) ------------------------------
    try:
        from talis_desk.storage.litestream_runner import maybe_start_or_snapshot
        rep_status = maybe_start_or_snapshot(db_path)
        print(f"  storage_repl     = {rep_status}")
    except Exception as e:
        print(f"  storage_repl     = SKIPPED ({type(e).__name__}: {e})")

    # ----- Preflight: regenerate tool atlas + register specialists --------
    banner("PREFLIGHT — regenerate tool atlas")
    from talis_desk.tool_atlas.atlas import regenerate_tool_atlas
    atlas = regenerate_tool_atlas()
    print(f"  n_tools = {atlas.n_tools}  n_sources = {atlas.n_sources}")
    if not atlas.rows:
        print("FATAL: empty tool_atlas — refuse to run swarm")
        return 1

    # Also register the citation tools (Wave 2.2).
    try:
        from talis_desk.citations import register_citation_tools
        outcomes = register_citation_tools()
        for uri, out in outcomes.items():
            print(f"  citation_tool    {uri} -> {out}")
    except Exception as e:
        print(f"  citation_tools   = SKIPPED ({type(e).__name__}: {e})")

    # Register the 14 order-flow tools (tape / profile / internals / TOP).
    try:
        from talis_desk.order_flow import register_order_flow_tools
        of_outcomes = register_order_flow_tools()
        n_ok = sum(1 for s in of_outcomes.values() if s in {"registered", "in_process"})
        print(f"  order_flow_tools = {n_ok}/{len(of_outcomes)} registered "
              f"(sample: {list(of_outcomes.items())[0][1]})")
    except Exception as e:
        print(f"  order_flow_tools = SKIPPED ({type(e).__name__}: {e})")

    # Register read-only Jarvis Trading Engine bridge tools so scouts can learn
    # from our production execution/node/strategy surfaces without mutating JTE.
    try:
        from talis_desk.jarvis_bridge import register_jarvis_bridge_tools
        jb_outcomes = register_jarvis_bridge_tools()
        n_ok = sum(1 for s in jb_outcomes.values() if s in {"registered", "in_process"})
        print(f"  jarvis_bridge    = {n_ok}/{len(jb_outcomes)} registered")
    except Exception as e:
        print(f"  jarvis_bridge    = SKIPPED ({type(e).__name__}: {e})")

    banner("PREFLIGHT — register specialists (constitutional voices)")
    from run_full_desk import SPECIALIST_REGISTRY, register_specialists
    registered = register_specialists(only=None)
    if not registered:
        print("FATAL: no specialists registered")
        return 1
    print(f"  n_registered = {len(registered)}")

    # ----- Research Director: propose curriculum BEFORE Tier 0 -----------
    banner("RESEARCH DIRECTOR — propose curriculum")
    director_themes: list[str] = []
    if args.skip_director:
        print("  director SKIPPED (--skip-director)")
    else:
        try:
            from talis_desk.director.research_director import run_research_director_cycle
            plan = run_research_director_cycle(
                cycle_id=f"{cycle_id}__director",
                budget_usd=min(0.50, args.budget * 0.05),
                llm_timeout_s=args.director_timeout_s,
            )
            director_themes = list(getattr(plan, "cross_cutting_themes", []) or [])
            print(f"  plan_id          = {plan.plan_id}")
            print(f"  themes_proposed  = {len(director_themes)}")
            for t in director_themes[:10]:
                print(f"    - {t}")
        except Exception as e:
            print(f"  director SKIPPED ({type(e).__name__}: {e})")

    # ----- Tier 0: seed generation ----------------------------------------
    banner("TIER 0 — stratified seed generation")
    from talis_desk.swarm.seed_generator import (
        SeedCell,
        generate_alpha_geometry_route_seeds,
        generate_market_map_governor_seeds,
        generate_seeds,
        load_themes_from_meta,
        narrow_tools_for_seed,
    )
    from talis_desk.coordination import (
        coverage_cell_key,
        post_task,
        touch_coverage_cell,
    )
    t0 = time.perf_counter()
    # Director themes take precedence over the meta/themes_active.json file.
    themes = director_themes or load_themes_from_meta()
    if themes:
        print(f"  injecting {len(themes)} themes: {themes}")
    scout_cost_cap = max(0.5, args.budget * 0.10)  # ~10% of cycle budget
    seed_rng = None
    if not args.randomize_seeds:
        seed_rng = args.seed_rng
        if seed_rng is None:
            seed_rng = _derive_seed_rng({
                "cycle_id": cycle_id,
                "scope": args.scope,
                "n_seeds": args.n_seeds,
                "themes": themes or [],
            })
        print(f"  seed_rng         = {seed_rng}")
    active_market_program = None
    try:
        from talis_desk.information_map import load_active_market_evolve_program

        active_market_program = load_active_market_evolve_program(
            cycle_id=cycle_id,
            conn=_store_mod._STORE.conn,
        )
    except Exception as e:
        print(f"  market_evolve_load SKIPPED ({type(e).__name__}: {e})")
    geometry_route_seeds = generate_alpha_geometry_route_seeds(
        cycle_id=cycle_id,
        n_seed_budget=args.n_seeds,
        program=active_market_program,
        conn=_store_mod._STORE.conn,
    )
    if geometry_route_seeds:
        print(f"  alpha_geometry injected {len(geometry_route_seeds)} route-followup seeds")
    remaining_after_geometry = max(0, args.n_seeds - len(geometry_route_seeds))
    governor_seeds = generate_market_map_governor_seeds(
        cycle_id=cycle_id,
        n_seed_budget=remaining_after_geometry,
        program=active_market_program,
        conn=_store_mod._STORE.conn,
        use_llm=not args.skip_market_map_llm_governor,
        llm_model=args.market_map_llm_model,
        geometry_cycle_id=cycle_id,
    )
    if governor_seeds:
        print(f"  map_governor injected {len(governor_seeds)} frontier/gap-repair seeds")
    base_seed_count = max(0, args.n_seeds - len(geometry_route_seeds) - len(governor_seeds))
    seeds = generate_seeds(
        n_seeds=base_seed_count,
        cycle_id=cycle_id,
        themes=themes or None,
        rng_seed=seed_rng,
    )
    seeds = geometry_route_seeds + governor_seeds + seeds
    calendar_seeds = _calendar_gate_seeds(cycle_id=cycle_id, conn=_store_mod._STORE.conn)
    if calendar_seeds:
        for seed in calendar_seeds:
            seed.payload["tool_candidates"] = narrow_tools_for_seed(seed)
        seeds = calendar_seeds + seeds
        print(f"  calendar_gate injected {len(calendar_seeds)} must-research seeds")
    for seed in seeds:
        seed.payload["seed_rng"] = seed_rng
        seed.payload["seed_rng_mode"] = "randomized" if args.randomize_seeds else "deterministic"
    try:
        from talis_desk.information_map import (
            apply_market_evolve_policy_to_seeds,
            prepare_market_evolve_experiment_seed_pairs,
        )

        paired_experiment_slices = prepare_market_evolve_experiment_seed_pairs(
            seeds,
            cycle_id=cycle_id,
            conn=_store_mod._STORE.conn,
        )
        if paired_experiment_slices:
            print(f"  market_evolve_ab = paired {paired_experiment_slices} matched seed slices")
        active_market_program = apply_market_evolve_policy_to_seeds(
            seeds,
            cycle_id=cycle_id,
            conn=_store_mod._STORE.conn,
        )
        print(
            "  market_evolve    = "
            f"{active_market_program.name} gen={active_market_program.generation} "
            f"id={active_market_program.program_id}"
        )
    except Exception as e:
        print(f"  market_evolve_policy SKIPPED ({type(e).__name__}: {e})")
    # Enrich every seed's tool candidate list with BM25-ranked tools from
    # the atlas (Gap 2 — dynamic tool retrieval). Union with the existing
    # lexical candidates so scouts get wider coverage without exposing
    # the entire atlas.
    try:
        from talis_desk.tool_atlas.retrieval import find_tool_for_query
        _bm25_extra = 0
        for seed in seeds:
            query = " ".join(
                [seed.entity, seed.horizon, seed.lens, seed.bias_mode, seed.theme or ""]
            )
            hits = find_tool_for_query(
                query,
                top_k=5,
                lens=seed.lens,
                entity=seed.entity,
            )
            existing = list(seed.payload.get("tool_candidates") or [])
            seen = set(existing)
            for h in hits:
                if h.tool_uri not in seen:
                    existing.append(h.tool_uri)
                    seen.add(h.tool_uri)
                    _bm25_extra += 1
            try:
                tool_limit = int(seed.payload.get("tool_candidate_limit") or 10)
            except Exception:
                tool_limit = 10
            seed.payload["tool_candidates"] = existing[:max(4, min(24, tool_limit))]
        print(f"  bm25_retrieval added {_bm25_extra} extra tool candidates across {len(seeds)} seeds")
    except Exception as e:
        print(f"  bm25_retrieval SKIPPED ({type(e).__name__}: {e})")
    print(f"  generated {len(seeds)} seeds in {time.perf_counter()-t0:.2f}s")
    for seed in seeds:
        cell_key = coverage_cell_key(
            entity=seed.entity,
            horizon=seed.horizon,
            lens=seed.lens,
            source="tier0_seed",
            bias_mode=seed.bias_mode,
            theme=seed.theme,
        )
        seed.payload["coverage_cell_key"] = cell_key
        seed.payload["task_id"] = post_task(
            topic=f"bb_topic:scout:{seed.lens}:{seed.entity}",
            title=f"Scout {seed.entity} {seed.horizon} via {seed.lens}",
            description=(
                f"Investigate one falsifiable {seed.bias_mode} hypothesis "
                f"for {seed.entity} on {seed.horizon} horizon through {seed.lens}."
            ),
            cycle_id=cycle_id,
            priority=float(seed.weight),
            budget_usd=max(0.0001, scout_cost_cap / max(1, len(seeds))),
            ttl_seconds=1800,
            input_schema={
                "entity": seed.entity,
                "horizon": seed.horizon,
                "lens": seed.lens,
                "bias_mode": seed.bias_mode,
                "theme": seed.theme,
            },
            allowed_tools=[],
            evidence_requirements=["falsifiable_hypothesis", "suggested_tools"],
            promotion_criteria={"verifier_gate": "2_of_3_practical_majority"},
            kill_criteria={"empty_hypothesis": True, "provider_unavailable": True},
            coverage_cell_key=cell_key,
            payload=seed.to_payload(),
        )
        touch_coverage_cell(
            entity=seed.entity,
            horizon=seed.horizon,
            lens=seed.lens,
            source="tier0_seed",
            bias_mode=seed.bias_mode,
            theme=seed.theme,
            novelty_score=min(1.0, float(seed.weight)),
            density_score=1.0 / max(0.01, float(seed.frontier_boost)),
            payload={
                "seed_id": seed.seed_id,
                "task_id": seed.payload["task_id"],
                "seed_source": (seed.payload or {}).get("source"),
                "gap_id": (seed.payload or {}).get("gap_id"),
                "alpha_geometry_cell_key": (seed.payload or {}).get("alpha_geometry_cell_key"),
                "market_map_completion_pressure": (seed.payload or {}).get("market_map_completion_pressure"),
                "missing_surfaces": (seed.payload or {}).get("missing_surfaces"),
                "expected_edges": (seed.payload or {}).get("expected_edges"),
            },
        )
    print(f"  sample:")
    for s in seeds[:3]:
        print(f"    {s.seed_id}: {s.entity} x {s.horizon} x {s.lens} x {s.bias_mode} "
              f"(w={s.weight:.2f}, src={(s.payload or {}).get('source', 'stratified')})")

    # ----- Tier 1: scout swarm --------------------------------------------
    banner("TIER 1 — DeepSeek Flash scout swarm")
    from talis_desk.swarm.scout_runner import run_scouts
    t1 = time.perf_counter()
    scouts = run_scouts(
        seeds=seeds,
        cycle_id=cycle_id,
        concurrency=args.scout_concurrency,
        cost_cap_usd=scout_cost_cap,
    )
    t1_elapsed = time.perf_counter() - t1
    n_scout_ok = sum(1 for s in scouts if s.hypothesis_text and not s.error)
    n_scout_err = sum(1 for s in scouts if s.error)
    scout_cost = sum(s.cost_usd for s in scouts)
    print(f"  scouts_run        = {len(scouts)}")
    print(f"  scouts_ok         = {n_scout_ok}")
    print(f"  scouts_errored    = {n_scout_err}")
    print(f"  scout_total_cost  = ${scout_cost:.4f}")
    print(f"  scout_elapsed     = {t1_elapsed:.1f}s")
    n_info_strings = sum(len(getattr(s, "information_string_ids", []) or []) for s in scouts)
    prompt_variant_counts = {
        v: sum(1 for s in scouts if getattr(s, "prompt_variant", "") == v)
        for v in sorted({getattr(s, "prompt_variant", "") for s in scouts if getattr(s, "prompt_variant", "")})
    }
    prompt_quality_flags = [
        flag
        for s in scouts
        for flag in (getattr(s, "quality_flags", []) or [])
        if str(flag).startswith("prompt_")
    ]
    print(f"  info_strings      = {n_info_strings}")
    if prompt_variant_counts:
        print(f"  prompt_variants   = {prompt_variant_counts}")
    if prompt_quality_flags:
        print(f"  prompt_flags      = {dict((f, prompt_quality_flags.count(f)) for f in sorted(set(prompt_quality_flags)))}")
    try:
        _ledger.record(amount_usd=scout_cost, stage="swarm_tier1_scouts",
                       specialist_id=None, cycle_id=cycle_id)
    except Exception as e:
        print(f"  cost_ledger      = scout record skipped ({type(e).__name__}: {e})")
    learned_tool_promotions = []
    runtime_adapter_work_orders = []
    try:
        tool_policy = dict((getattr(active_market_program, "genome", {}) or {}).get("tool_request_policy") or {})
        if tool_policy.get("auto_promote_high_priority_tools"):
            from talis_desk.tool_atlas import promote_pending_analysis_tool_proposals

            limit = max(1, min(12, int(tool_policy.get("max_tool_promotions_per_cycle") or 3)))
            learned_tool_promotions = promote_pending_analysis_tool_proposals(
                cycle_id=cycle_id,
                limit=limit,
                conn=_store_mod._STORE.conn,
            )
            n_passed = sum(1 for pmt in learned_tool_promotions if pmt.passed)
            print(f"  learned_tools     = promoted {n_passed}/{len(learned_tool_promotions)} pending proposals")
        runtime_adapter_work_orders = _consume_runtime_adapter_backlog(
            tool_policy=tool_policy,
            cycle_id=cycle_id,
            conn=_store_mod._STORE.conn,
        )
        if runtime_adapter_work_orders:
            print(f"  runtime_adapters  = work orders {len(runtime_adapter_work_orders)}")
    except Exception as e:
        print(f"  learned_tools     = promotion skipped ({type(e).__name__}: {e})")

    if args.stop_after_scouts:
        banner("SCOUT WALKTHROUGH — stopping before spend-heavy tiers")
        manifest_path, brief_path = _write_scout_walkthrough_artifacts(
            cycle_id=cycle_id,
            db_path=db_path,
            seeds=seeds,
            scouts=scouts,
            scout_cost=scout_cost,
            prompt_variant_counts=prompt_variant_counts,
            n_info_strings=n_info_strings,
            elapsed_s=t1_elapsed,
            scope=args.scope,
        )
        print(f"  brief.path        = {brief_path}")
        print(f"  manifest         = {manifest_path}")
        return 0

    # ----- Tier 1.25: embedding-based dedup -------------------------------
    banner("TIER 1.25 — embedding dedup (cosine >= 0.92)")
    from talis_desk.swarm.dedup import dedup_scouts
    t125 = time.perf_counter()
    dedup_result = dedup_scouts(scouts)
    t125_elapsed = time.perf_counter() - t125
    print(f"  scouts_in         = {len([s for s in scouts if s.hypothesis_text and not s.error])}")
    print(f"  n_clusters        = {dedup_result.n_clusters}")
    print(f"  dropped_dupes     = {len(dedup_result.dropped)}")
    print(f"  threshold         = {dedup_result.threshold}")
    print(f"  dedup_elapsed     = {t125_elapsed:.1f}s")
    if dedup_result.quality_flags:
        print(f"  dedup_flags       = {dedup_result.quality_flags}")

    # ----- Tier 1.33: information-map attention synthesis ------------------
    promoted_scouts = []
    information_synthesis = None
    if args.skip_information_synthesis:
        print("\n(tier 1.33 information synthesis skipped)")
    else:
        banner("TIER 1.33 — information-map attention synthesis")
        t133 = time.perf_counter()
        try:
            from talis_desk.information_map import load_alpha_geometry, run_information_synthesis
            from talis_desk.swarm.information_bridge import promoted_scouts_from_synthesis

            information_synthesis = run_information_synthesis(
                cycle_id=cycle_id,
                max_strings=args.information_synthesis_max_strings,
                use_llm=not args.information_synthesis_deterministic,
                geometry_weights=(
                    (active_market_program.genome or {}).get("geometry_weights")
                    if active_market_program else None
                ),
                routing_thresholds=(
                    (active_market_program.genome or {}).get("routing_thresholds")
                    if active_market_program else None
                ),
            )
            promoted_scouts = promoted_scouts_from_synthesis(
                information_synthesis,
                existing_scouts=dedup_result.kept,
                max_items=args.information_synthesis_topk,
            )
            t133_elapsed = time.perf_counter() - t133
            print(f"  synthesis_id      = {information_synthesis.synthesis_id}")
            print(f"  confluences       = {len(information_synthesis.confluences)}")
            print(f"  tensions          = {len(information_synthesis.tensions)}")
            print(f"  promoted_raw      = {len(information_synthesis.promoted_hypotheses)}")
            print(f"  promoted_added    = {len(promoted_scouts)}")
            top_geometry = load_alpha_geometry(cycle_id=cycle_id, limit=3)
            print(f"  alpha_geometry    = {len(top_geometry)} cells")
            for cell in top_geometry[:3]:
                print(
                    "    "
                    f"{cell.get('route_directive')} "
                    f"{cell.get('entity')}/{cell.get('horizon')}/{cell.get('lens')} "
                    f"scream={float(cell.get('trade_scream_score') or 0):.2f} "
                    f"ready={float(cell.get('verifier_readiness') or 0):.2f}"
                )
            print(f"  synthesis_cost    = ${information_synthesis.cost_usd:.4f}")
            print(f"  synthesis_flags   = {information_synthesis.quality_flags}")
            print(f"  synthesis_elapsed = {t133_elapsed:.1f}s")
            try:
                _ledger.record(amount_usd=information_synthesis.cost_usd,
                               stage="swarm_tier133_information_synthesis",
                               specialist_id=None, cycle_id=cycle_id)
            except Exception:
                pass
        except Exception as e:
            print(f"  information_synthesis SKIPPED ({type(e).__name__}: {e})")

    # ----- Tier 1.5: verifier council -------------------------------------
    banner("TIER 1.5 — 3-family verifier council")
    from talis_desk.swarm.verifier_council import run_verifier_council
    t15 = time.perf_counter()
    # Run verifiers only against successful, non-deduped scouts to save budget.
    ok_scouts = [
        s for s in dedup_result.kept
        if s.hypothesis_text and not s.error and "deduped" not in s.quality_flags
    ]
    verifier_inputs = ok_scouts + promoted_scouts
    verdicts = run_verifier_council(verifier_inputs)
    t15_elapsed = time.perf_counter() - t15
    n_approved = sum(1 for v in verdicts if v.decision == "approve")
    n_rejected = sum(1 for v in verdicts if v.decision == "reject")
    n_abstained = sum(1 for v in verdicts if v.decision == "abstain")
    verifier_cost = sum(v.cost_usd for v in verdicts)
    print(f"  verifier_run      = {len(verdicts)}")
    print(f"  verifier_inputs   = raw:{len(ok_scouts)} synthesized:{len(promoted_scouts)}")
    print(f"  approved          = {n_approved}")
    print(f"  rejected          = {n_rejected}")
    print(f"  abstained         = {n_abstained}")
    print(f"  verifier_cost     = ${verifier_cost:.4f}")
    print(f"  verifier_elapsed  = {t15_elapsed:.1f}s")
    try:
        _ledger.record(amount_usd=verifier_cost, stage="swarm_tier15_verifier",
                       specialist_id=None, cycle_id=cycle_id)
    except Exception:
        pass

    # ----- Tier 2: analyst pool -------------------------------------------
    banner("TIER 2 — analyst pool (constitutional specialists)")
    from talis_desk.swarm.analyst_pool import run_analyst_pool
    t2 = time.perf_counter()
    analyst_outputs = run_analyst_pool(
        scouts=verifier_inputs,
        verdicts=verdicts,
        topk=args.analyst_topk,
    )
    t2_elapsed = time.perf_counter() - t2
    n_analyst_ok = sum(1 for a in analyst_outputs if a.draft_md and not a.error)
    analyst_cost = sum(a.cost_usd for a in analyst_outputs)
    print(f"  analyst_run       = {len(analyst_outputs)}")
    print(f"  analyst_ok        = {n_analyst_ok}")
    print(f"  analyst_cost      = ${analyst_cost:.4f}")
    print(f"  analyst_elapsed   = {t2_elapsed:.1f}s")
    try:
        _ledger.record(amount_usd=analyst_cost, stage="swarm_tier2_analysts",
                       specialist_id=None, cycle_id=cycle_id)
    except Exception:
        pass

    # ----- Cost gate before Tier 3 ----------------------------------------
    cumulative = scout_cost + verifier_cost + analyst_cost
    print(f"  cumulative cost  = ${cumulative:.4f} / ${args.budget:.4f} budget")
    if cumulative >= args.budget:
        print(f"  COST CAP REACHED — skipping Tier 3 adversarial pipeline")
        args.skip_adversarial = True

    # ----- Tier 3: existing 6-stage adversarial pipeline ------------------
    n_reports = 0
    adversarial_cost = 0.0
    if not args.skip_adversarial:
        banner("TIER 3 — adversarial pipeline (top-20 analyst outputs)")
        n_reports, adversarial_cost = _run_tier3_pipeline(
            analyst_outputs[:args.adversarial_topk], cycle_id,
            remaining_budget=args.budget - cumulative,
        )
        print(f"  reports_written  = {n_reports}")
        print(f"  adversarial_cost = ${adversarial_cost:.4f}")
        try:
            _ledger.record(amount_usd=adversarial_cost, stage="swarm_tier3_adversarial",
                           specialist_id=None, cycle_id=cycle_id)
        except Exception:
            pass
    else:
        print("\n(tier 3 skipped)")

    # ----- Tier 3.5: FlowSim — counterparty-reaction gate -----------------
    flow_forecasts: list = []
    flow_decisions: list = []
    trade_expressions: list = []
    asymmetric_bets: list = []         # SelectedCandidate × ExecutableExpression × RuleSet
    flow_cost = 0.0
    if not args.skip_flow_sim:
        banner("TIER 3.5 — FlowSim (counterparty reaction + PM compile)")
        flow_topk = max(1, min(int(args.flow_sim_topk), len(analyst_outputs)))
        flow_forecasts, flow_decisions, trade_expressions, flow_cost = _run_flow_sim_pass(
            analyst_outputs[:flow_topk],
            cycle_id=cycle_id,
        )
        n_publish = sum(1 for d in flow_decisions if d.verdict == "publish")
        n_size_down = sum(1 for d in flow_decisions if d.verdict == "publish_size_down")
        n_revise = sum(1 for d in flow_decisions if d.verdict == "revise")
        n_kill = sum(1 for d in flow_decisions if d.verdict == "kill")
        print(f"  flow_run         = {len(flow_forecasts)}")
        print(f"  publish          = {n_publish}")
        print(f"  publish_size_dn  = {n_size_down}")
        print(f"  revise           = {n_revise}")
        print(f"  kill             = {n_kill}")
        print(f"  flow_cost        = ${flow_cost:.4f}")
        try:
            _ledger.record(amount_usd=flow_cost, stage="swarm_tier35_flow_sim",
                           specialist_id=None, cycle_id=cycle_id)
        except Exception:
            pass

        # --- Tier 3.6: convex selection + concentration + PM compile ----
        banner("TIER 3.6 — Convex selection (top 0-3) + executable compile")
        asymmetric_bets = _run_concentration_and_compile(
            analyst_outputs[:flow_topk],
            flow_forecasts,
            flow_decisions,
            cycle_id=cycle_id,
        )
        n_asym = len(asymmetric_bets)
        print(f"  asymmetric_bets  = {n_asym}")
        for i, bet in enumerate(asymmetric_bets, 1):
            sel, expr, rs = bet
            print(f"  #{i} {expr.hl_coin:10s} {expr.direction:5s} "
                  f"size=${expr.target_size_usd:.0f} lev={expr.leverage:.1f}x "
                  f"convex={sel.convexity.convexity_score:.2f} "
                  f"protection={expr.protective_order_status}")
    else:
        print("\n(tier 3.5 flow_sim skipped)")

    # ----- Tier 4: brief synthesis ----------------------------------------
    banner("TIER 4 — Opus brief synthesis")
    from talis_desk.swarm.brief_synthesis import synthesize_brief
    try:
        brief = synthesize_brief(cycle_id=cycle_id, scope=args.scope)
        print(f"  brief.id           = {brief.id}")
        print(f"  brief.headline_used = {brief.headline_model_used}")
        print(f"  brief.quality_flags = {brief.quality_flags}")
        print(f"  brief.markdown_len  = {len(brief.markdown)}")

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = Path(f"/tmp/talis_swarm_brief_{ts}.md")
        brief_md = brief.markdown
        # Asymmetric Bets section first (the actionable headline output).
        brief_md = brief_md + "\n\n" + _render_asymmetric_bets(asymmetric_bets)

        # Tier 4.5 — per-asset depth render (Emini Tic voice).
        depth_md = ""
        depth_cost = 0.0
        if asymmetric_bets and not args.skip_depth_render:
            banner("TIER 4.5 — per-asset depth render (Emini Tic voice)")
            try:
                from talis_desk.brief.emini_depth_renderer import render_all_asymmetric_bets
                depth_md, depth_results, depth_cost = render_all_asymmetric_bets(
                    asymmetric_bets, cycle_id=cycle_id,
                )
                print(f"  depth_rendered   = {len(depth_results)}")
                print(f"  depth_cost       = ${depth_cost:.4f}")
                for r in depth_results:
                    print(f"    {r.instrument:8s} model={r.model_used:35s} "
                          f"cost=${r.cost_usd:.3f} flags={r.quality_flags or '[]'}")
                try:
                    _ledger.record(amount_usd=depth_cost, stage="swarm_tier45_depth",
                                   specialist_id="emini_tic", cycle_id=cycle_id)
                except Exception:
                    pass
            except Exception as e:
                print(f"  depth_render SKIPPED ({type(e).__name__}: {e})")
        if depth_md:
            brief_md = brief_md + "\n\n" + depth_md

        # Trade Slate (deprecated alias) for backward-compat readers.
        if trade_expressions:
            brief_md = brief_md + "\n\n" + _render_trade_expressions(trade_expressions)
        out_path.write_text(brief_md)
        print(f"  brief.path         = {out_path}")
    except Exception as e:
        print(f"  brief FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()
        brief = None
        out_path = None

    # ----- Tier 5: topology engine (post-cycle, feeds next cycle's seeds) -
    banner("TIER 5 — topology engine")
    topology_result = None
    try:
        from talis_desk.topology import run_topology_cycle
        topology_result = run_topology_cycle(cycle_id=cycle_id)
        print(f"  n_hypotheses     = {topology_result.n_hypotheses}")
        print(f"  n_regions        = {topology_result.n_regions}")
        print(f"  quality_flags    = {topology_result.quality_flags}")
        print(f"  out_dir          = {topology_result.out_dir}")
    except Exception as e:
        print(f"  topology SKIPPED ({type(e).__name__}: {e})")

    # ----- Tier 5.5: MarketEvolve evaluator -------------------------------
    banner("TIER 5.5 — MarketEvolve evaluator")
    market_evolve_step = None
    evolution_control = None
    try:
        from talis_desk.information_map import run_market_evolve_step
        market_evolve_step = run_market_evolve_step(cycle_id=cycle_id)
        best_eval = market_evolve_step.best_evaluation
        print(f"  programs_eval    = {len(market_evolve_step.evaluations)}")
        if best_eval is not None:
            print(f"  best_program     = {best_eval.program_id}")
            print(f"  evolve_score     = {best_eval.score:.3f}")
            print(f"  evolve_passed    = {best_eval.passed}")
            print(f"  evolve_flags     = {best_eval.quality_flags}")
            print(f"  evolve_reason    = {best_eval.rationale}")
        print(f"  mutations        = {len(market_evolve_step.mutations)}")
        for mutation in market_evolve_step.mutations[:3]:
            print(
                "    "
                f"{mutation.mutation_kind}: child={mutation.child_program_id} "
                f"status={mutation.status}"
            )
        print(f"  experiments      = {len(market_evolve_step.experiment_plans)}")
        print(f"  exp_results      = {len(market_evolve_step.experiment_results)}")
        for result in market_evolve_step.experiment_results[:3]:
            print(
                "    "
                f"{result.get('decision')} "
                f"delta={float(result.get('score_delta') or 0):.3f} "
                f"experiment={result.get('experiment_id')}"
            )
        evolution_control = _build_evolution_control_payload(
            cycle_id=cycle_id,
            active_program=active_market_program,
            market_evolve_step=market_evolve_step,
            conn=_store_mod._STORE.conn,
        )
        try:
            from talis_desk.information_map import post_evolution_control_work_orders

            evolution_dispatch = post_evolution_control_work_orders(
                cycle_id=cycle_id,
                evolution_control=evolution_control,
                conn=_store_mod._STORE.conn,
                source="run_swarm_tier55",
            )
            evolution_control["task_dispatch"] = evolution_dispatch
        except Exception as dispatch_exc:
            evolution_control["task_dispatch"] = {
                "schema_version": "evolution_control_task_dispatch_v1",
                "cycle_id": cycle_id,
                "source": "run_swarm_tier55",
                "status": "error",
                "error": f"{type(dispatch_exc).__name__}: {dispatch_exc}",
                "posted_count": 0,
                "existing_count": 0,
                "task_count": 0,
                "quality_flags": ["evolution_control_dispatch_failed"],
            }
        try:
            task_worker = _run_cortex_task_worker_feedback(
                cycle_id=cycle_id,
                active_program=active_market_program,
                conn=_store_mod._STORE.conn,
            )
            evolution_control["task_execution"] = task_worker["task_execution"]
            evolution_control["task_feedback"] = task_worker["task_feedback"]
        except Exception as worker_exc:
            evolution_control["task_execution"] = {
                "schema_version": "cortex_task_worker_batch_v1",
                "cycle_id": cycle_id,
                "status": "error",
                "error": f"{type(worker_exc).__name__}: {worker_exc}",
                "task_count": 0,
                "completed_count": 0,
                "failed_count": 0,
                "quality_flags": ["cortex_task_worker_failed"],
            }
            evolution_control["task_feedback"] = {
                "schema_version": "cortex_task_feedback_v1",
                "cycle_id": cycle_id,
                "status": "error",
                "error": f"{type(worker_exc).__name__}: {worker_exc}",
                "quality_flags": ["cortex_task_feedback_failed"],
            }
        cortex = evolution_control.get("geometry_cortex_review") or {}
        lineage = evolution_control.get("market_evolve_lineage") or {}
        dispatch = evolution_control.get("task_dispatch") or {}
        task_execution = evolution_control.get("task_execution") or {}
        task_feedback = evolution_control.get("task_feedback") or {}
        feedback_metrics = task_feedback.get("metrics") if isinstance(task_feedback, dict) else {}
        if not isinstance(feedback_metrics, dict):
            feedback_metrics = {}
        diagnostics = [
            str(d.get("code"))
            for d in (cortex.get("diagnostics") or [])
            if isinstance(d, dict) and d.get("code")
        ]
        proposed = cortex.get("proposed_geometry_policy") if isinstance(cortex.get("proposed_geometry_policy"), dict) else {}
        print(f"  cortex_shape     = {cortex.get('status')} diagnostics={diagnostics[:4]}")
        print(f"  cortex_mutation  = {proposed.get('mutation_kind_hint') or 'none'}")
        print(f"  lineage_frontier = {len(lineage.get('frontier') or [])}")
        print(
            "  cortex_tasks     = "
            f"{dispatch.get('posted_count', 0)} posted / "
            f"{dispatch.get('existing_count', 0)} existing"
        )
        print(
            "  cortex_worker    = "
            f"{task_execution.get('completed_count', 0)} completed / "
            f"{task_execution.get('failed_count', 0)} failed"
        )
        print(
            "  cortex_feedback  = "
            f"completion={float(feedback_metrics.get('cortex_task_completion_rate') or 0.0):.2f} "
            f"shape_obs={float(feedback_metrics.get('cortex_shape_observation_rate') or 0.0):.2f}"
        )
    except Exception as e:
        print(f"  market_evolve SKIPPED ({type(e).__name__}: {e})")

    # ----- Tier 6: wiki auto-organize -------------------------------------
    banner("TIER 6 — wiki auto-organize")
    wiki_result = None
    try:
        from talis_desk.wiki.auto_organize import auto_organize
        wiki_result = auto_organize(cycle_id=cycle_id, db_path=db_path)
        print(f"  wiki_root        = {wiki_result.wiki_root}")
        print(f"  pages_written    = {wiki_result.pages_written}")
        print(f"  delta_path       = {wiki_result.delta_path}")
        print(f"  git_committed    = {wiki_result.git_committed}")
        if wiki_result.delta:
            d = wiki_result.delta
            print(f"  delta            = new={len(d.new_hypotheses)} resolved={len(d.resolved_hypotheses)} "
                  f"moves={len(d.posterior_moves)} dead={len(d.dead_theses)}")
    except Exception as e:
        print(f"  wiki SKIPPED ({type(e).__name__}: {e})")

    # ----- Total summary --------------------------------------------------
    banner("SWARM TOTAL")
    synthesis_cost = getattr(information_synthesis, "cost_usd", 0.0) or 0.0
    total_cost = scout_cost + synthesis_cost + verifier_cost + analyst_cost + adversarial_cost + flow_cost
    print(f"  total_cost       = ${total_cost:.4f}")
    print(f"  scout_cost       = ${scout_cost:.4f}")
    print(f"  synthesis_cost   = ${synthesis_cost:.4f}")
    print(f"  verifier_cost    = ${verifier_cost:.4f}")
    print(f"  analyst_cost     = ${analyst_cost:.4f}")
    print(f"  adversarial_cost = ${adversarial_cost:.4f}")
    print(f"  flow_cost        = ${flow_cost:.4f}")
    print(f"  n_hypotheses     = {n_scout_ok}")
    print(f"  n_verified       = {n_approved}")
    print(f"  n_analyzed       = {n_analyst_ok}")
    print(f"  n_reports        = {n_reports}")
    print(f"  n_flow_forecasts = {len(flow_forecasts)}")
    print(f"  n_expressions    = {len(trade_expressions)}")
    print(f"  n_asymmetric_bet = {len(asymmetric_bets)}")

    # ----- Persist swarm manifest ----------------------------------------
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    manifest_path = Path(f"/tmp/talis_swarm_manifest_{ts}.json")
    manifest = {
        "cycle_id": cycle_id,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "scope": args.scope,
        "desk_db": str(db_path),
        "tic_db": f"{TIC_PATH}/tic/tic.db",
        "n_seeds": len(seeds),
        "n_scouts_ok": n_scout_ok,
        "n_information_strings": n_info_strings,
        "prompt_variant_counts": prompt_variant_counts,
        "market_evolve_active_program": (
            {
                "program_id": active_market_program.program_id,
                "name": active_market_program.name,
                "generation": active_market_program.generation,
                "status": active_market_program.status,
                "score": active_market_program.score,
            }
            if active_market_program else None
        ),
        "learned_tool_promotions": [
            {
                "proposal_id": pmt.proposal_id,
                "tool_name": pmt.tool_name,
                "tool_uri": pmt.tool_uri,
                "status": pmt.status,
                "passed": pmt.passed,
                "quality_flags": pmt.quality_flags,
                "eval_report": pmt.eval_report,
                "iteration_proposal_id": getattr(pmt, "iteration_proposal_id", ""),
            }
            for pmt in learned_tool_promotions
        ],
        "runtime_adapter_work_orders": [
            {
                "proposal_id": order.proposal_id,
                "tool_name": order.tool_name,
                "runtime": order.runtime,
                "status": order.status,
                "work_order_path": order.work_order_path,
                "quality_flags": order.quality_flags,
            }
            for order in runtime_adapter_work_orders
        ],
        "information_synthesis": (
            {
                "id": information_synthesis.synthesis_id,
                "summary": information_synthesis.summary,
                "n_confluences": len(information_synthesis.confluences),
                "n_tensions": len(information_synthesis.tensions),
                "n_promoted": len(information_synthesis.promoted_hypotheses),
                "n_promoted_added_to_verifier": len(promoted_scouts),
                "quality_flags": information_synthesis.quality_flags,
                "model_used": information_synthesis.model_used,
            }
            if information_synthesis else None
        ),
        "market_evolve": (
            {
                "n_programs": len(market_evolve_step.programs),
                "n_evaluations": len(market_evolve_step.evaluations),
                "n_mutations": len(market_evolve_step.mutations),
                "n_experiments": len(market_evolve_step.experiment_plans),
                "n_experiment_results": len(market_evolve_step.experiment_results),
                "best": (
                    {
                        "program_id": market_evolve_step.best_evaluation.program_id,
                        "score": market_evolve_step.best_evaluation.score,
                        "passed": market_evolve_step.best_evaluation.passed,
                        "metrics": market_evolve_step.best_evaluation.metrics,
                        "quality_flags": market_evolve_step.best_evaluation.quality_flags,
                        "rationale": market_evolve_step.best_evaluation.rationale,
                    }
                    if market_evolve_step.best_evaluation else None
                ),
                "mutations": [
                    {
                        "mutation_id": m.mutation_id,
                        "parent_program_id": m.parent_program_id,
                        "child_program_id": m.child_program_id,
                        "mutation_kind": m.mutation_kind,
                        "rationale": m.rationale,
                        "status": m.status,
                    }
                    for m in market_evolve_step.mutations[:10]
                ],
                "experiments": [
                    {
                        "id": e.get("id"),
                        "candidate_program_id": e.get("candidate_program_id"),
                        "experiment_kind": e.get("experiment_kind"),
                        "status": e.get("status"),
                        "success_criteria": e.get("success_criteria"),
                    }
                    for e in market_evolve_step.experiment_plans[:10]
                ],
                "experiment_results": [
                    {
                        "id": r.get("id"),
                        "experiment_id": r.get("experiment_id"),
                        "decision": r.get("decision"),
                        "score_delta": r.get("score_delta"),
                        "rationale": r.get("rationale"),
                        "quality_flags": r.get("quality_flags"),
                    }
                    for r in market_evolve_step.experiment_results[:10]
                ],
                "quality_flags": market_evolve_step.quality_flags,
            }
            if market_evolve_step else None
        ),
        "evolution_control": evolution_control,
        "n_verified": n_approved,
        "n_analyzed": n_analyst_ok,
        "n_reports": n_reports,
        "n_flow_forecasts": len(flow_forecasts),
        "n_trade_expressions": len(trade_expressions),
        "n_asymmetric_bets": len(asymmetric_bets),
        "total_cost": total_cost,
        "asymmetric_bets": [
            {
                "instrument": expr.hl_coin,
                "direction": expr.direction,
                "size_usd": expr.target_size_usd,
                "leverage": expr.leverage,
                "convexity_score": sel.convexity.convexity_score,
                "protective_order_status": expr.protective_order_status,
                "is_executable": expr.is_executable(),
                "stop_price": expr.stop_price,
                "tp1_price": expr.tp1_price,
                "tp2_price": expr.tp2_price,
                "stops_from_order_flow": (
                    "stops_from_order_flow_levels" in (expr.quality_flags or [])
                ),
                "specialist_id": sel.candidate.payload.get("specialist_id"),
                "forecast_id": sel.candidate.forecast.forecast_id,
                "scout_id": sel.candidate.analyst_output_id,
            }
            for sel, expr, _ in asymmetric_bets
        ],
        # Drill-down IDs.
        "scout_ids_ok": [s.scout_id for s in scouts if s.hypothesis_text and not s.error][:200],
        "verified_scout_ids": [v.scout_id for v in verdicts if v.decision == "approve"][:100],
        "analyst_specialist_ids": [a.specialist_id for a in analyst_outputs if a.draft_md and not a.error][:100],
        "flow_forecast_ids": [f.forecast_id for f in flow_forecasts],
        "ruleset_summaries": [
            {
                "candidate_id": sel.candidate.analyst_output_id,
                "instrument": expr.hl_coin,
                "n_rules": len(rs.rules),
                "n_kill": sum(1 for r in rs.rules if r.severity == "kill"),
            }
            for sel, expr, rs in asymmetric_bets
        ],
        "flow_verdict_counts": {
            "publish": sum(1 for d in flow_decisions if d.verdict == "publish"),
            "publish_size_down": sum(1 for d in flow_decisions if d.verdict == "publish_size_down"),
            "revise": sum(1 for d in flow_decisions if d.verdict == "revise"),
            "kill": sum(1 for d in flow_decisions if d.verdict == "kill"),
        },
        "cost_breakdown": {
            "scout": scout_cost,
            "information_synthesis": synthesis_cost,
            "verifier": verifier_cost,
            "analyst": analyst_cost,
            "adversarial": adversarial_cost,
            "flow_sim": flow_cost,
            "total": total_cost,
        },
        "brief_id": getattr(brief, "id", None) if brief else None,
        "brief_path": str(out_path) if out_path else None,
        "quality_flags": getattr(brief, "quality_flags", []) if brief else [],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str))
    print(f"  manifest         = {manifest_path}")

    return 0


def _write_scout_walkthrough_artifacts(
    *,
    cycle_id: str,
    db_path: Path,
    seeds: list[Any],
    scouts: list[Any],
    scout_cost: float,
    prompt_variant_counts: dict[str, int],
    n_info_strings: int,
    elapsed_s: float,
    scope: str,
) -> tuple[Path, Path]:
    """Write coherent scout-only artifacts for the monitor.

    This is the microscope mode: it runs the sensory layer, persists raw
    scout outputs + information strings to desk.db, then stops before the
    spend-heavy verifier / analyst / PM layers. The manifest uses the same
    `/tmp/talis_swarm_manifest_*.json` convention as full cycles so the
    visualizer can lock DB, manifest, and markdown to one cycle.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    brief_path = Path(f"/tmp/talis_scout_walkthrough_{ts}.md")
    manifest_path = Path(f"/tmp/talis_swarm_manifest_{ts}.json")
    scout_rows = [_scout_manifest_row(s) for s in scouts]
    ok_rows = [r for r in scout_rows if r["ok"]]
    error_rows = [r for r in scout_rows if not r["ok"]]

    brief_path.write_text(
        _render_scout_walkthrough_markdown(
            cycle_id=cycle_id,
            scope=scope,
            db_path=db_path,
            scouts=ok_rows,
            errors=error_rows,
            scout_cost=scout_cost,
            n_info_strings=n_info_strings,
            elapsed_s=elapsed_s,
            prompt_variant_counts=prompt_variant_counts,
        )
    )
    manifest = {
        "cycle_id": cycle_id,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "run_mode": "scout_walkthrough",
        "desk_db": str(db_path),
        "tic_db": f"{TIC_PATH}/tic/tic.db",
        "n_seeds": len(seeds),
        "n_scouts_ok": len(ok_rows),
        "n_scouts_error": len(error_rows),
        "n_information_strings": n_info_strings,
        "prompt_variant_counts": prompt_variant_counts,
        "n_verified": 0,
        "n_analyzed": 0,
        "n_reports": 0,
        "n_flow_forecasts": 0,
        "n_trade_expressions": 0,
        "n_asymmetric_bets": 0,
        "total_cost": scout_cost,
        "cost_breakdown": {
            "scout": scout_cost,
            "information_synthesis": 0.0,
            "verifier": 0.0,
            "analyst": 0.0,
            "adversarial": 0.0,
            "flow_sim": 0.0,
            "total": scout_cost,
        },
        "scout_ids_ok": [r["scout_id"] for r in ok_rows][:500],
        "scout_preview": ok_rows[:12],
        "brief_id": f"scout_walkthrough_{cycle_id}",
        "brief_path": str(brief_path),
        "quality_flags": ["scout_walkthrough_only"],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str))
    return manifest_path, brief_path


def _build_evolution_control_payload(
    *,
    cycle_id: str,
    active_program: Any = None,
    market_evolve_step: Any = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Compatibility wrapper for the shared evolution-control read model."""
    from talis_desk.information_map import build_evolution_control_payload

    return build_evolution_control_payload(
        cycle_id=cycle_id,
        active_program=active_program,
        market_evolve_step=market_evolve_step,
        conn=conn,
        source="swarm_manifest",
    )


def _run_cortex_task_worker_feedback(
    *,
    cycle_id: str,
    active_program: Any = None,
    conn: Any = None,
    limit: int = 8,
) -> dict[str, Any]:
    """Execute bounded cortex work orders and expose evaluator-ready feedback."""
    from talis_desk.agent_harness import HarnessPolicy, execute_cortex_task_queue
    from talis_desk.information_map import collect_market_evolve_metrics
    from talis_desk.information_map.market_evolve import score_market_evolve_metrics

    cortex_policy = {}
    if active_program is not None:
        try:
            genome = getattr(active_program, "genome", {}) or {}
            cortex_policy = dict((genome.get("cortex_policy") if isinstance(genome, dict) else {}) or {})
        except Exception:
            cortex_policy = {}
    evidence_hard_cap = _bounded_int(
        cortex_policy.get("max_tools_per_task"),
        default=4,
        low=1,
        high=8,
    )
    execute_followup_tools = bool(
        cortex_policy.get("execute_bounded_followup_tools", True)
    )
    batch = execute_cortex_task_queue(
        cycle_id=cycle_id,
        limit=limit,
        policy=HarnessPolicy(evidence_hard_cap=evidence_hard_cap, max_retries=0, retry_backoff_s=0.0),
        execute_followup_tools=execute_followup_tools,
        conn=conn,
    )
    metrics = collect_market_evolve_metrics(cycle_id=cycle_id, conn=conn)
    score_after_worker = None
    if active_program is not None:
        score_after_worker = score_market_evolve_metrics(metrics, program=active_program)
    metric_keys = (
        "cortex_task_count",
        "cortex_task_completed_count",
        "cortex_task_failed_count",
        "cortex_task_pending_count",
        "cortex_task_completion_rate",
        "cortex_task_failure_rate",
        "cortex_task_pending_rate",
        "cortex_shape_observation_rate",
        "cortex_observations_per_task",
        "cortex_deferred_followup_rate",
        "cortex_followup_execution_rate",
        "cortex_followup_observations_per_task",
        "cortex_shape_blocked_followup_rate",
    )
    cortex_metrics = {
        key: float(metrics.get(key) or 0.0)
        for key in metric_keys
    }
    flags: list[str] = ["production_cortex_task_feedback"]
    if not batch.get("task_count"):
        flags.append("no_cortex_tasks_executed")
    return {
        "task_execution": batch,
        "task_feedback": {
            "schema_version": "cortex_task_feedback_v1",
            "cycle_id": cycle_id,
            "status": "ready",
            "score_after_worker": score_after_worker,
            "metrics": cortex_metrics,
            "proof": {
                "evaluator_saw_worker_tasks": cortex_metrics["cortex_task_count"] > 0.0,
                "shape_observation_is_rewarded": cortex_metrics["cortex_shape_observation_rate"] > 0.0,
                "worker_completion_is_rewarded": cortex_metrics["cortex_task_completion_rate"] > 0.0,
                "worker_failures_are_penalizable": "cortex_task_failure_rate" in cortex_metrics,
                "external_followups_enabled": bool(batch.get("execute_followup_tools")),
                "followup_execution_is_rewarded": "cortex_followup_execution_rate" in cortex_metrics,
                "shape_gate_blocks_followups": "cortex_shape_blocked_followup_rate" in cortex_metrics,
            },
            "quality_flags": flags,
        },
    }


def _bounded_int(raw: Any, *, default: int, low: int, high: int) -> int:
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(low, min(high, value))


def _scout_manifest_row(scout: Any) -> dict[str, Any]:
    strings = []
    for info in getattr(scout, "information_strings", []) or []:
        strings.append({
            "id": getattr(info, "string_id", None),
            "title": getattr(info, "title", ""),
            "thesis": getattr(info, "thesis", ""),
            "mechanism": getattr(info, "mechanism", ""),
            "expected_outcome": getattr(info, "expected_outcome", ""),
            "kill_signal": getattr(info, "kill_signal", ""),
            "entities_chain": list(getattr(info, "entities_chain", []) or []),
            "evidence_refs": list(getattr(info, "evidence_refs", []) or []),
            "conviction": float(getattr(info, "conviction", 0.0) or 0.0),
            "novelty_score": float(getattr(info, "novelty_score", 0.0) or 0.0),
            "crowdedness": float(getattr(info, "crowdedness", 0.5) or 0.5),
            "quality_flags": list(getattr(info, "quality_flags", []) or []),
        })
    return {
        "ok": bool(getattr(scout, "hypothesis_text", "") and not getattr(scout, "error", None)),
        "scout_id": getattr(scout, "scout_id", ""),
        "hypothesis_id": getattr(scout, "hypothesis_id", None),
        "seed_id": getattr(scout, "seed_id", ""),
        "entity": getattr(scout, "entity", ""),
        "horizon": getattr(scout, "horizon", ""),
        "lens": getattr(scout, "lens", ""),
        "bias_mode": getattr(scout, "bias_mode", ""),
        "prompt_variant": getattr(scout, "prompt_variant", ""),
        "model_used": getattr(scout, "model_used", ""),
        "provider": getattr(scout, "provider", ""),
        "confidence": float(getattr(scout, "confidence", 0.0) or 0.0),
        "cost_usd": float(getattr(scout, "cost_usd", 0.0) or 0.0),
        "elapsed_s": float(getattr(scout, "elapsed_s", 0.0) or 0.0),
        "hypothesis": getattr(scout, "hypothesis_text", ""),
        "rationale_brief": getattr(scout, "rationale_brief", ""),
        "suggested_tools": list(getattr(scout, "suggested_tools", []) or []),
        "tool_evidence": list(getattr(scout, "tool_evidence", []) or []),
        "information_string_ids": list(getattr(scout, "information_string_ids", []) or []),
        "event_intelligence_ids": list(getattr(scout, "event_intelligence_ids", []) or []),
        "node_intelligence_ids": list(getattr(scout, "node_intelligence_ids", []) or []),
        "tool_proposal_ids": list(getattr(scout, "tool_proposal_ids", []) or []),
        "information_strings": strings,
        "quality_flags": list(getattr(scout, "quality_flags", []) or []),
        "error": getattr(scout, "error", None),
    }


def _render_scout_walkthrough_markdown(
    *,
    cycle_id: str,
    scope: str,
    db_path: Path,
    scouts: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    scout_cost: float,
    n_info_strings: int,
    elapsed_s: float,
    prompt_variant_counts: dict[str, int],
) -> str:
    lines = [
        "# Talis Scout Walkthrough",
        "",
        "This is a scout-only run: Tier 0 generated market slices, Tier 1 ran real DeepSeek/Flash-family scouts, then the pipeline stopped before verifier, analyst, FlowSim, and PM spend.",
        "",
        "## Cycle",
        f"- cycle_id: `{cycle_id}`",
        f"- scope: `{scope}`",
        f"- desk_db: `{db_path}`",
        f"- scouts_ok: `{len(scouts)}`",
        f"- scouts_error: `{len(errors)}`",
        f"- information_strings: `{n_info_strings}`",
        f"- scout_cost: `${scout_cost:.4f}`",
        f"- elapsed: `{elapsed_s:.1f}s`",
        f"- prompt_variants: `{prompt_variant_counts}`",
        "",
        "## Top Scout Outputs",
    ]
    ranked = sorted(
        scouts,
        key=lambda r: (float(r.get("confidence") or 0.0), len(r.get("information_string_ids") or [])),
        reverse=True,
    )
    for i, row in enumerate(ranked[:12], 1):
        lines.extend([
            "",
            f"### {i}. {row['entity']} · {row['horizon']} · {row['lens']} · {row['prompt_variant']}",
            f"- confidence: `{float(row.get('confidence') or 0.0):.2f}` · model: `{row.get('model_used') or 'unknown'}` · cost: `${float(row.get('cost_usd') or 0.0):.4f}`",
            f"- hypothesis: {row.get('hypothesis') or '—'}",
            f"- rationale: {row.get('rationale_brief') or '—'}",
            f"- tools: `{', '.join(row.get('suggested_tools') or []) or '—'}`",
        ])
        strings = row.get("information_strings") or []
        for s in strings[:2]:
            lines.extend([
                f"  - string: {s.get('title') or s.get('thesis') or '—'}",
                f"    mechanism: {s.get('mechanism') or '—'}",
                f"    kill_signal: {s.get('kill_signal') or '—'}",
            ])
    if errors:
        lines.extend(["", "## Errors"])
        for row in errors[:20]:
            lines.append(f"- `{row.get('scout_id')}` {row.get('entity')} {row.get('lens')}: {row.get('error')}")
    return "\n".join(lines) + "\n"


def _run_concentration_and_compile(
    analyst_outputs: list,
    flow_forecasts: list,
    flow_decisions: list,
    cycle_id: str,
) -> list:
    """Tier 3.6 — convex selection + concentration cull + HL-native compile.

    Returns list of (SelectedCandidate, ExecutableExpression, RuleSet) tuples
    for the top 0-3 asymmetric bets.
    """
    if not analyst_outputs or not flow_forecasts:
        return []
    from talis_desk.flow_sim.selection import (
        CandidateBundle,
        cull_and_concentrate,
        published_only,
    )
    from talis_desk.execution import (
        compile_to_executable,
        get_perp_spec,
        validate_preview,
    )
    from talis_desk.watchtower import compile_rules
    from talis_desk.pm_overlay import DEFAULT_HOUSE_RISK_STATE
    from talis_desk.coordination import append_blackboard_event

    # Pair analyst outputs with their forecast + gate decision by ordering.
    pairs = list(zip(analyst_outputs, flow_forecasts, flow_decisions))
    candidates: list = []
    for ao, forecast, decision in pairs:
        spec = get_perp_spec(ao.entity)
        hl_listed = spec is not None
        candidates.append(CandidateBundle(
            analyst_output_id=ao.scout_id,
            instrument=ao.entity,
            sector_tag=None,
            horizon=ao.horizon,
            hl_listed=hl_listed,
            adv_decile=0.8 if hl_listed else None,
            catalyst_clock_hours=None,  # populated upstream if calendar gate fired
            payoff_asymmetry=None,
            funding_8h_bps=None,
            thesis_confidence=ao.confidence,
            desk_direction=_desk_direction_from_forecast(forecast),
            forecast=forecast,
            gate=decision,
            stop_distance_pct=None,
            payload={"draft_md": ao.draft_md[:600]},
        ))

    selected = cull_and_concentrate(candidates, DEFAULT_HOUSE_RISK_STATE)
    pub = published_only(selected)

    out: list = []
    for sel in pub:
        cand = sel.candidate
        if cand.desk_direction is None:
            sel.quality_flags.append("no_directional_hl_perp_expression")
            if abs(getattr(cand.forecast, "net_vol_pressure", 0.0)) >= 0.25:
                sel.quality_flags.append("vol_signal_not_hl_perp_executable")
            continue
        # Resolve mark price (best-effort via HL allMids; fall back if unavailable).
        mark = _resolve_mark_price(cand.instrument)
        if mark <= 0:
            sel.quality_flags.append("mark_price_unavailable")
            continue
        forecast = cand.forecast

        # Per-asset order-flow levels — anchors stops/TPs to real structure
        # when accumulated session history is available.
        of_levels = _compute_per_asset_levels(
            instrument=cand.instrument,
            mark_price=mark,
            intended_direction=cand.desk_direction,
        )

        expr = compile_to_executable(
            forecast_id=forecast.forecast_id,
            selected=sel,
            mark_price=mark,
            funding_8h_bps=None,
            data_freshness_ms=200,
            order_flow_levels=of_levels,
        )
        # Preview: assume Watchtower tracks, no native triggers in this v1.
        validate_preview(
            expr,
            can_attach_hl_native_triggers=False,
            watchtower_will_track=True,
        )
        ruleset = compile_rules(
            executable=expr,
            forecast=forecast,
            expected_holding_days=7,
            catalyst_clock_hours=cand.catalyst_clock_hours,
        )
        out.append((sel, expr, ruleset))
        try:
            append_blackboard_event(
                event_type="asymmetric_bet_published",
                cycle_id=cycle_id,
                topic="bb_topic:asymmetric_bets",
                payload={
                    "candidate_id": cand.analyst_output_id,
                    "instrument": expr.hl_coin,
                    "direction": expr.direction,
                    "target_size_usd": expr.target_size_usd,
                    "leverage": expr.leverage,
                    "convexity_score": sel.convexity.convexity_score,
                    "n_rules": len(ruleset.rules),
                    "protective_order_status": expr.protective_order_status,
                },
            )
        except Exception:
            pass
    return out


def _desk_direction_from_forecast(forecast) -> Optional[str]:
    """Map FlowSim pressure to an HL-perp direction.

    FlowSim can surface directional pressure and volatility pressure. The
    HL-native executable compiler currently supports directional perps only,
    so vol-only signals must not masquerade as executable long/short bets.
    """
    net_buy = float(getattr(forecast, "net_buy_pressure", 0.0) or 0.0)
    if net_buy > 0.05:
        return "long"
    if net_buy < -0.05:
        return "short"
    return None


def _compute_per_asset_levels(
    *,
    instrument: str,
    mark_price: float,
    intended_direction: Optional[str] = None,
    candle_bars: Optional[list[dict]] = None,
):
    """Best-effort per-asset order-flow levels.

    Pulls today's bars (or accepts caller-supplied), loads the asset's
    rolling state, computes a level slate. Returns None on any failure
    so the executable compiler falls back to %-based defaults.
    """
    if not instrument or mark_price <= 0:
        return None
    try:
        from talis_desk.order_flow import compute_asset_levels
    except Exception:
        return None
    bars = candle_bars or _fetch_candles_for_levels(instrument, mark_price)
    if not bars:
        # Even with no bars we can still emit a slate from accumulated
        # naked POCs / HVN bands in state.
        bars = []
    try:
        return compute_asset_levels(
            asset=instrument,
            mark_price=mark_price,
            bars=bars,
            intended_direction=intended_direction,
            save_state=True,
        )
    except Exception as e:
        print(f"  order_flow_levels skipped for {instrument}: {type(e).__name__}: {e}")
        return None


def _fetch_candles_for_levels(instrument: str, mark_price: float) -> list[dict]:
    """Best-effort HL candle snapshot for the level caller. Returns [] on failure."""
    if os.environ.get("TALIS_DISABLE_HL_META", "").lower() in {"1", "true"}:
        return []
    try:
        import json as _json
        import urllib.request
        from datetime import datetime, timedelta, timezone as _tz
        end = int(datetime.now(_tz.utc).timestamp() * 1000)
        start = end - int(timedelta(hours=24).total_seconds() * 1000)
        body = {
            "type": "candleSnapshot",
            "req": {
                "coin": instrument,
                "interval": "5m",
                "startTime": start,
                "endTime": end,
            },
        }
        req = urllib.request.Request(
            "https://api.hyperliquid.xyz/info",
            data=_json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            candles = _json.loads(resp.read())
        if not isinstance(candles, list):
            return []
        out: list[dict] = []
        for c in candles:
            out.append({
                "ts": c.get("t"),
                "open": float(c.get("o") or 0),
                "high": float(c.get("h") or 0),
                "low": float(c.get("l") or 0),
                "close": float(c.get("c") or 0),
                "volume": float(c.get("v") or 0),
            })
        return out
    except Exception:
        return []


def _resolve_mark_price(instrument: str) -> float:
    """Best-effort live mark from HL allMids. Returns 0.0 on failure."""
    if not instrument:
        return 0.0
    if os.environ.get("TALIS_DISABLE_HL_META", "").lower() in {"1", "true"}:
        return 0.0
    try:
        import json as _json
        import urllib.request
        req = urllib.request.Request(
            "https://api.hyperliquid.xyz/info",
            data=_json.dumps({"type": "allMids"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            mids = _json.loads(resp.read())
        if isinstance(mids, dict):
            v = mids.get(instrument) or mids.get(instrument.upper())
            return float(v) if v else 0.0
    except Exception:
        return 0.0
    return 0.0


def _render_asymmetric_bets(bets: list) -> str:
    """Render the "Asymmetric Bets (top 0-3)" section as markdown.

    bets: list of (SelectedCandidate, ExecutableExpression, RuleSet)
    """
    if not bets:
        return (
            "---\n\n"
            "## Asymmetric Bets — None Today\n\n"
            "*Nothing cleared the convexity, survival, and flow gates. "
            "Holding cash today.*\n"
        )
    lines = ["---", "", "## Asymmetric Bets (top 0-3 — FlowSim-gated + survival-checked)", ""]
    for i, (sel, expr, rs) in enumerate(bets, 1):
        if expr.is_executable():
            exec_label = "✅ executable"
        elif expr.protective_order_status == "tracked_by_watchtower":
            exec_label = "🟡 watchtower-tracked"
        else:
            exec_label = "🔴 not-protected"
        stops_from_of = "stops_from_order_flow_levels" in (expr.quality_flags or [])
        stop_source = " (order-flow-anchored)" if stops_from_of else " (6% default)"
        # Flow audit summary: net pressure + simulator confidence + n archetypes.
        fc = sel.candidate.forecast
        nbp = getattr(fc, "net_buy_pressure", 0.0)
        sim_conf = getattr(fc, "simulator_confidence", 0.0)
        n_arch_ok = sum(1 for r in getattr(fc, "archetype_reactions", []) if not r.error)
        n_arch_tot = len(getattr(fc, "archetype_reactions", []))
        lines.extend([
            f"### #{i} — {expr.hl_coin} ({expr.direction.upper()}) · {exec_label}",
            f"- **Size:** ${expr.target_size_usd:.0f} ({sel.survival.target_size_pct_nav*100:.2f}% NAV) at **{expr.leverage:.1f}x**",
            f"- **Convexity:** {sel.convexity.convexity_score:.3f}  ({sel.convexity.rationale})",
            f"- **Flow:** net_buy={nbp:+.2f} · sim_conf={sim_conf:.2f} · archetypes {n_arch_ok}/{n_arch_tot}",
            f"- **Mark:** {expr.mark_price:.4g} · **Stop:** {expr.stop_price:.4g}{stop_source} · **TP1:** {expr.tp1_price:.4g} · **TP2:** {expr.tp2_price:.4g}",
            f"- **Liq est:** {expr.liquidation_price_est:.4g}",
            "- **Entry ladder:**",
        ])
        for rung in expr.entry_ladder:
            lines.append(
                f"  - `{rung.label}` ${rung.size_usd:.0f} @ {rung.price:.4g} (weight {rung.weight:.0%})"
            )
        kill_rules = [r for r in rs.rules if r.severity == "kill"]
        warn_rules = [r for r in rs.rules if r.severity == "warn"]
        lines.append(f"- **Watchtower:** {len(rs.rules)} rules ({len(kill_rules)} kill, {len(warn_rules)} warn)")
        for r in kill_rules:
            lines.append(f"  - `kill` {r.label}")
        if sel.candidate.payload.get("draft_md"):
            lines.append("")
            lines.append(f"> {sel.candidate.payload['draft_md'][:280]}")
        lines.append("")
    return "\n".join(lines)


def _render_trade_expressions(expressions: list) -> str:
    """Render the FlowSim-gated, PM-compiled trade slate as markdown.

    This is the actionable section appended to the brief — instrument,
    direction, size, invalidation, what flips the crowd.
    """
    if not expressions:
        return ""
    lines = [
        "---",
        "",
        "## Trade Slate (FlowSim-gated)",
        "",
        "| Instrument | Kind | Dir | Size | Conf | Flow | Rationale |",
        "|---|---|---|---|---|---|---|",
    ]
    for ao, expr in expressions:
        size_pct = f"{int(round(expr.size_hint_pct_nav * 100 * 10))/10}%"
        conf = f"{int(round(expr.confidence * 100))}%"
        flow = f"{int(round(expr.flow_alignment * 100))}%"
        kind_short = expr.kind.replace("_", " ")
        rationale = (expr.rationale_short or expr.rationale or "")[:80]
        lines.append(
            f"| {expr.instrument} | {kind_short} | {expr.direction} | "
            f"{size_pct} | {conf} | {flow} | {rationale} |"
        )
    lines.extend(["", "### Per-thesis detail", ""])
    for ao, expr in expressions:
        if expr.kind in {"wait", "none"}:
            lines.extend([
                f"#### {expr.instrument} — {expr.kind.upper()}",
                f"- **Verdict:** {expr.kind} ({expr.rationale_short})",
                f"- **Why:** {expr.rationale}",
                "",
            ])
            continue
        lines.extend([
            f"#### {expr.instrument} — {expr.kind.replace('_', ' ').upper()} ({expr.direction})",
            f"- **Specialist:** {ao.specialist_id}",
            f"- **Size hint:** {int(round(expr.size_hint_pct_nav * 100 * 10))/10}% NAV",
            f"- **Holding:** {expr.expected_holding_days or '—'} days",
            f"- **Catalyst clock:** {expr.catalyst_clock or '—'}",
            f"- **Invalidation:** {expr.invalidation_text}",
            f"- **What flips the crowd:** {expr.what_flips_crowd or '—'}",
            f"- **Edge thesis:** {ao.edge_thesis}",
            "",
        ])
    return "\n".join(lines)


def _run_flow_sim_pass(
    analyst_outputs: list,
    cycle_id: str,
) -> tuple[list, list, list, float]:
    """Tier 3.5 — fan each promoted analyst draft through FlowSim, score
    via the publication gate, compile to TradeExpression.

    Returns (forecasts, decisions, expressions, total_cost).
    """
    if not analyst_outputs:
        return [], [], [], 0.0
    from talis_desk.flow_sim import (
        run_flow_sim,
        gate_decision,
        compile_to_expression,
        FlowSimUnavailableError,
    )
    from talis_desk.coordination import (
        append_blackboard_event,
        attribute_failure,
    )

    forecasts: list = []
    decisions: list = []
    expressions: list = []
    total_cost = 0.0

    for ao in analyst_outputs:
        try:
            forecast = run_flow_sim(
                brief_id=f"swarm:{ao.scout_id}",
                cycle_id=cycle_id,
                entity=ao.entity,
                horizon=ao.horizon,
                lens=ao.lens,
                thesis_text=ao.edge_thesis or ao.draft_md[:1600],
                market_context="",
                desk_direction=None,  # let the gate read net pressure
                sector=None,
                catalyst_kind=None,
            )
        except FlowSimUnavailableError as e:
            print(f"  flow_sim unavailable on scout={ao.scout_id}: {e}")
            try:
                attribute_failure(
                    artifact_kind="flow_forecast",
                    artifact_id=f"swarm:{ao.scout_id}",
                    failure_kind="flow_sim_unavailable",
                    cycle_id=cycle_id,
                    specialist_id=ao.specialist_id,
                    severity="yellow",
                    rationale=str(e)[:240],
                )
            except Exception:
                pass
            continue
        except Exception as e:
            print(f"  flow_sim error on scout={ao.scout_id}: {type(e).__name__}: {e}")
            continue
        forecasts.append(forecast)
        total_cost += forecast.cost_usd

        # Gate decision uses analyst confidence as thesis_confidence.
        try:
            decision = gate_decision(
                forecast,
                thesis_confidence=ao.confidence,
                desk_direction=None,
            )
        except Exception as e:
            print(f"  gate error on scout={ao.scout_id}: {type(e).__name__}: {e}")
            continue
        decisions.append(decision)

        # PM compile (deterministic).
        try:
            expr = compile_to_expression(
                instrument=ao.entity,
                forecast=forecast,
                gate=decision,
                thesis_text=ao.edge_thesis,
                desk_direction_hint=None,
                horizon=ao.horizon,
            )
            expressions.append((ao, expr))
        except Exception as e:
            print(f"  pm_compile error on scout={ao.scout_id}: {type(e).__name__}: {e}")
            continue

        try:
            append_blackboard_event(
                event_type="flow_sim_completed",
                cycle_id=cycle_id,
                topic="bb_topic:flow_sim",
                specialist_id=ao.specialist_id,
                payload={
                    "scout_id": ao.scout_id,
                    "forecast_id": forecast.forecast_id,
                    "verdict": decision.verdict,
                    "net_buy_pressure": forecast.net_buy_pressure,
                    "simulator_confidence": forecast.simulator_confidence,
                    "size_multiplier": decision.size_multiplier,
                    "expression_kind": expr.kind,
                },
            )
        except Exception:
            pass

    return forecasts, decisions, expressions, total_cost


def _calendar_gate_seeds(cycle_id: str, conn) -> list:
    """Convert today's hard calendar gate into Tier-0 research seeds.

    The brief composer already forces critical catalysts into the rendered
    brief. This hook moves the same information *upstream* so the swarm
    researches NVDA/FOMC-style events before synthesis instead of merely
    acknowledging them at the end.
    """
    try:
        from talis_desk.brief.calendar_gate import check_calendar_today
        from talis_desk._tic_config import get_tic_root
        from talis_desk.swarm.seed_generator import SeedCell
    except Exception:
        return []
    gate = None
    try:
        tic_db = get_tic_root() / "tic" / "tic.db"
        if tic_db.exists():
            import sqlite3
            with sqlite3.connect(str(tic_db)) as tic_conn:
                tic_conn.row_factory = sqlite3.Row
                gate = check_calendar_today(
                    datetime.now(timezone.utc),
                    tic_conn,
                    include_sec_edgar=False,
                )
    except Exception as e:
        print(f"  calendar_gate TIC lookup skipped ({type(e).__name__}: {e})")
    if gate is None:
        try:
            gate = check_calendar_today(
                datetime.now(timezone.utc),
                conn,
                include_sec_edgar=False,
            )
        except Exception as e:
            print(f"  calendar_gate seed injection skipped ({type(e).__name__}: {e})")
            return []
    triggers = [
        t for t in (getattr(gate, "triggers", []) or [])
        if t.get("severity") in ("critical", "high")
    ]
    out = []
    seen: set[tuple[str, str, str]] = set()
    for i, trig in enumerate(triggers[:20]):
        kind = str(trig.get("kind") or "catalyst")
        headline_upper = str(trig.get("headline") or "").upper()
        if kind == "macro_release" and "FOMC" in headline_upper:
            entity = "FOMC_MINUTES"
        elif kind == "macro_release":
            entity = str(trig.get("release") or "MACRO_RELEASE")
        else:
            entity = str(
                trig.get("ticker")
                or trig.get("release")
                or trig.get("headline")
                or "MARKET"
            )
        entity = (
            entity.upper()
            .replace("[US/HIGH]_", "")
            .replace("[US/CRITICAL]_", "")
            .replace(" ", "_")
        )[:24] or "MARKET"
        severity = str(trig.get("severity") or "high")
        if kind in ("macro_release",):
            lens = "macro"
            bias = "tail_risk" if severity == "critical" else "consensus_confirm"
        elif kind in ("filing", "sec_filing"):
            lens = "filing"
            bias = "frontier"
        elif kind in ("earnings",):
            lens = "catalyst"
            bias = "consensus_confirm"
        else:
            lens = "catalyst"
            bias = "frontier"
        horizon = "intraday" if severity == "critical" else "1d"
        key = (entity, horizon, lens)
        if key in seen:
            continue
        seen.add(key)
        out.append(SeedCell(
            seed_id=f"seed_{cycle_id}_CAL_{i:04d}",
            entity=entity,
            horizon=horizon,
            lens=lens,
            bias_mode=bias,
            theme="calendar_gate",
            weight=3.0 if severity == "critical" else 2.0,
            frontier_boost=1.5 if severity == "critical" else 1.2,
            coverage_penalty=1.0,
            payload={
                "source": "calendar_gate",
                "calendar_trigger": trig,
                "calendar_severity": severity,
                "must_lead_brief_with": getattr(gate, "must_lead_brief_with", None),
            },
        ))
    return out


def _run_tier3_pipeline(
    analyst_outputs: list,
    cycle_id: str,
    remaining_budget: float,
) -> tuple[int, float]:
    """Tier 3 — invoke the 6-stage adversarial pipeline on the top
    analyst outputs. Returns (n_reports_written, total_cost).

    Direct call to talis_desk.reports.pipeline.run_report_pipeline.
    Each promoted analyst hypothesis becomes one full research report.
    """
    if not analyst_outputs:
        return 0, 0.0
    from talis_desk.reports.pipeline import (
        run_report_pipeline,
        ReportPipelineUnavailableError,
    )
    from talis_desk.reports.persist import emit_research_report
    from talis_desk.store import get_desk_store
    from talis_desk.coordination import (
        append_blackboard_event,
        attribute_failure,
        promote_task,
    )

    conn = get_desk_store().conn
    per_report_budget = max(0.10, remaining_budget / max(1, len(analyst_outputs)))
    total_cost = 0.0
    n_written = 0

    for ao in analyst_outputs:
        if total_cost >= remaining_budget:
            print(f"  Tier 3 cost cap reached at {n_written} reports")
            break
        try:
            persona_prompt = _load_persona_prompt(conn, ao.specialist_id)
            hypothesis = _load_hypothesis_for_report(conn, ao)
            tool_evidence = _tool_evidence_for_report(conn, ao)
            primary_artifact = _primary_artifact_for_report(ao)
            sub_cycle = f"{cycle_id}__{ao.specialist_id}"
            print(
                f"  report_start      scout={ao.scout_id} "
                f"specialist={ao.specialist_id} entity={ao.entity}",
                flush=True,
            )

            result = run_report_pipeline(
                specialist_id=ao.specialist_id,
                persona_prompt=persona_prompt,
                hypothesis=hypothesis,
                primary_artifact=primary_artifact,
                tool_evidence=tool_evidence,
                market_snapshot=None,
                source_health={},
                cycle_id=sub_cycle,
                report_kind="watchlist",
                confidence_hint=ao.confidence,
                novelty_score_hint=None,
                max_revision_turns=1,
                conn=conn,
            )
            report = getattr(result, "report", None)
            if report is not None:
                ctx = type("_SwarmReportContext", (), {"conn": conn})()
                try:
                    emit_research_report(report, ctx)
                except Exception as e:
                    print(f"  report persist failed ({type(e).__name__}: {e})")
            n_written += 1
            total_cost += float(getattr(result, "total_cost_usd", 0.0) or 0.0)
            print(
                f"  report_done       scout={ao.scout_id} "
                f"cost=${float(getattr(result, 'total_cost_usd', 0.0) or 0.0):.4f}",
                flush=True,
            )

            # Promote the originating task on the blackboard.
            task_id = getattr(ao, "task_id", None) or (
                hypothesis.get("payload", {}).get("task_id") if isinstance(hypothesis, dict) else None
            )
            if task_id:
                try:
                    promote_task(
                        task_id,
                        specialist_id=ao.specialist_id,
                        payload={"reason": "tier3_report_emitted"},
                    )
                except Exception:
                    pass
            try:
                append_blackboard_event(
                    event_type="report_emitted",
                    cycle_id=cycle_id,
                    topic="bb_topic:tier3:report",
                    task_id=task_id,
                    specialist_id=ao.specialist_id,
                    payload={
                        "scout_id": ao.scout_id,
                        "hypothesis_id": ao.hypothesis_id,
                        "cost_usd": float(getattr(result, "total_cost_usd", 0.0) or 0.0),
                    },
                )
            except Exception:
                pass
        except ReportPipelineUnavailableError as e:
            print(f"  pipeline unavailable on scout={ao.scout_id}: {e}")
            try:
                attribute_failure(
                    artifact_kind="research_report",
                    artifact_id=f"swarm:{ao.scout_id}",
                    failure_kind="report_pipeline_unavailable",
                    cycle_id=cycle_id,
                    specialist_id=ao.specialist_id,
                    severity="yellow",
                    rationale=str(e)[:240],
                )
            except Exception:
                pass
        except Exception as e:
            print(f"  pipeline error on scout={ao.scout_id}: {type(e).__name__}: {e}")
            try:
                attribute_failure(
                    artifact_kind="research_report",
                    artifact_id=f"swarm:{ao.scout_id}",
                    failure_kind=type(e).__name__,
                    cycle_id=cycle_id,
                    specialist_id=ao.specialist_id,
                    severity="red",
                    rationale=str(e)[:240],
                )
            except Exception:
                pass
    return n_written, total_cost


def _load_persona_prompt(conn, specialist_id: str) -> str:
    try:
        row = conn.execute(
            "SELECT state_json FROM specialist_states "
            "WHERE specialist_id = ? AND state_kind = 'persona' "
            "AND transaction_to IS NULL "
            "ORDER BY transaction_from DESC LIMIT 1",
            (specialist_id,),
        ).fetchone()
        if not row:
            return ""
        raw = row["state_json"] if hasattr(row, "keys") else row[0]
        payload = json.loads(raw or "{}")
        return str(payload.get("system_prompt") or "")
    except Exception:
        return ""


def _load_hypothesis_for_report(conn, analyst_output) -> dict:
    hid = analyst_output.hypothesis_id or analyst_output.scout_id
    try:
        row = conn.execute(
            "SELECT * FROM hypotheses WHERE id = ? "
            "ORDER BY transaction_from DESC LIMIT 1",
            (hid,),
        ).fetchone()
    except Exception:
        row = None
    if row:
        d = dict(row)
        payload = _json_obj(d.get("payload"))
        entity_ids = _json_list(d.get("entity_ids")) or [analyst_output.entity]
        claim_ids = _json_list(d.get("claim_ids"))
        tool_call_ids = _json_list(d.get("tool_call_ids"))
        if not tool_call_ids:
            for ev in payload.get("tool_evidence") or []:
                tcid = ev.get("tool_call_log_id")
                if tcid:
                    tool_call_ids.append(tcid)
        return {
            "id": d.get("id") or hid,
            "title": d.get("title") or analyst_output.edge_thesis,
            "hypothesis_text": (
                d.get("hypothesis_text")
                or d.get("text")
                or analyst_output.edge_thesis
            ),
            "posterior_prob": d.get("posterior_prob") or analyst_output.posterior,
            "heat_score": d.get("heat_score") or analyst_output.confidence,
            "status": d.get("status") or "active",
            "entity_ids": entity_ids,
            "claim_ids": claim_ids,
            "tool_call_ids": tool_call_ids,
        }
    return {
        "id": hid,
        "title": analyst_output.edge_thesis or analyst_output.entity,
        "hypothesis_text": analyst_output.edge_thesis or analyst_output.draft_md[:500],
        "posterior_prob": analyst_output.posterior,
        "heat_score": analyst_output.confidence,
        "status": "active",
        "entity_ids": [analyst_output.entity],
        "claim_ids": [],
        "tool_call_ids": [],
    }


def _primary_artifact_for_report(analyst_output) -> dict:
    return {
        "id": f"swarm_watch:{analyst_output.scout_id}",
        "instrument": analyst_output.entity,
        "direction": "watch",
        "watch_condition": analyst_output.edge_thesis,
        "current_posterior": analyst_output.posterior,
        "analyst_confidence": analyst_output.confidence,
    }


def _tool_evidence_for_report(conn, analyst_output) -> list[dict]:
    hid = analyst_output.hypothesis_id
    payload: dict = {}
    if hid:
        try:
            row = conn.execute(
                "SELECT payload FROM hypotheses WHERE id = ? "
                "ORDER BY transaction_from DESC LIMIT 1",
                (hid,),
            ).fetchone()
            if row:
                raw = row["payload"] if hasattr(row, "keys") else row[0]
                payload = _json_obj(raw)
        except Exception:
            payload = {}
    out: list[dict] = []
    for ev in payload.get("tool_evidence") or []:
        out.append({
            "hypothesis_id": hid,
            "tool_call_log_id": ev.get("tool_call_log_id"),
            "tool_uri": ev.get("tool_uri") or ev.get("uri"),
            "posterior_delta": None,
            "contradicts": bool(ev.get("error")),
            "rationale": (
                str(ev.get("summary") or ev.get("error") or "")[:240]
            ),
        })
    if analyst_output.draft_md:
        out.append({
            "hypothesis_id": hid,
            "tool_call_log_id": None,
            "tool_uri": "swarm://tier2/analyst_draft",
            "posterior_delta": None,
            "contradicts": False,
            "rationale": analyst_output.draft_md[:240],
        })
    return out


def _json_obj(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        obj = json.loads(raw or "{}")
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _json_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    try:
        obj = json.loads(raw or "[]")
        return obj if isinstance(obj, list) else []
    except Exception:
        return []


def _consume_runtime_adapter_backlog(
    *,
    tool_policy: dict[str, Any],
    cycle_id: str,
    conn: Any,
) -> list[Any]:
    priority = str(tool_policy.get("runtime_adapter_backlog_priority") or "").lower()
    try:
        limit = int(tool_policy.get("max_runtime_adapter_builds_per_cycle") or 0)
    except Exception:
        limit = 0
    if priority == "high" and limit <= 0:
        limit = 1
    limit = max(0, min(6, limit))
    if limit <= 0:
        return []
    from talis_desk.tool_atlas import create_runtime_adapter_work_orders

    return create_runtime_adapter_work_orders(
        cycle_id=cycle_id,
        limit=limit,
        conn=conn,
    )


def _derive_seed_rng(material: Any) -> int:
    """Stable Tier-0 entropy for replayable seed plans.

    Coverage and density snapshots can still change the sampled plan, but for a
    fixed cycle/scope/theme input and fixed DB state this makes the market slice
    allocation exactly reproducible.
    """
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return int(hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16], 16) % (2**31 - 1)


if __name__ == "__main__":
    raise SystemExit(main())
