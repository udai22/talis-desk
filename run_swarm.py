"""Talis Desk v5 swarm orchestrator.

Replaces the per-specialist sequential pipeline with a 4-tier parallel
swarm. See `docs/REFLECTION_AND_REFACTOR_v5.md` for the architecture
spec.

Tier 0  -> Latin Hypercube seed generation + coverage / theme / density
            modifiers (talis_desk.swarm.seed_generator)
Tier 1  -> 1000 DeepSeek Flash scouts in parallel (asyncio.gather +
            semaphore=50) (talis_desk.swarm.scout_runner)
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
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path


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
    p.add_argument("--scout-concurrency", type=int, default=50)
    p.add_argument("--analyst-topk", type=int, default=50)
    p.add_argument("--adversarial-topk", type=int, default=20,
                   help="how many analyst drafts run through Tier 3")
    p.add_argument("--skip-adversarial", action="store_true",
                   help="skip Tier 3 (smoke test path)")
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
    print(f"  scout_concur     = {args.scout_concurrency}")
    print(f"  analyst_topk     = {args.analyst_topk}")
    print(f"  adversarial_topk = {args.adversarial_topk}")
    print(f"  tic.db           = {TIC_PATH}/tic/tic.db")

    # ----- Pin desk store -------------------------------------------------
    from talis_desk.store import DeskStore
    import talis_desk.store as _store_mod
    _store_mod._STORE = DeskStore(db_path=db_path)

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
    try:
        from talis_desk.director.research_director import run_research_director_cycle
        plan = run_research_director_cycle(
            cycle_id=f"{cycle_id}__director",
            budget_usd=min(0.50, args.budget * 0.05),
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
    from talis_desk.swarm.seed_generator import generate_seeds, load_themes_from_meta
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
    seeds = generate_seeds(
        n_seeds=args.n_seeds,
        cycle_id=cycle_id,
        themes=themes or None,
    )
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
            payload={"seed_id": seed.seed_id, "task_id": seed.payload["task_id"]},
        )
    print(f"  sample:")
    for s in seeds[:3]:
        print(f"    {s.seed_id}: {s.entity} x {s.horizon} x {s.lens} x {s.bias_mode} "
              f"(w={s.weight:.2f})")

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

    # ----- Tier 1.5: verifier council -------------------------------------
    banner("TIER 1.5 — 3-family verifier council")
    from talis_desk.swarm.verifier_council import run_verifier_council
    t15 = time.perf_counter()
    # Run verifiers only against successful scouts to save budget.
    ok_scouts = [s for s in scouts if s.hypothesis_text and not s.error]
    verdicts = run_verifier_council(ok_scouts)
    t15_elapsed = time.perf_counter() - t15
    n_approved = sum(1 for v in verdicts if v.decision == "approve")
    n_rejected = sum(1 for v in verdicts if v.decision == "reject")
    n_abstained = sum(1 for v in verdicts if v.decision == "abstain")
    verifier_cost = sum(v.cost_usd for v in verdicts)
    print(f"  verifier_run      = {len(verdicts)}")
    print(f"  approved          = {n_approved}")
    print(f"  rejected          = {n_rejected}")
    print(f"  abstained         = {n_abstained}")
    print(f"  verifier_cost     = ${verifier_cost:.4f}")
    print(f"  verifier_elapsed  = {t15_elapsed:.1f}s")

    # ----- Tier 2: analyst pool -------------------------------------------
    banner("TIER 2 — analyst pool (constitutional specialists)")
    from talis_desk.swarm.analyst_pool import run_analyst_pool
    t2 = time.perf_counter()
    analyst_outputs = run_analyst_pool(
        scouts=ok_scouts,
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
    else:
        print("\n(tier 3 skipped)")

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
        out_path.write_text(brief.markdown)
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
    total_cost = scout_cost + verifier_cost + analyst_cost + adversarial_cost
    print(f"  total_cost       = ${total_cost:.4f}")
    print(f"  scout_cost       = ${scout_cost:.4f}")
    print(f"  verifier_cost    = ${verifier_cost:.4f}")
    print(f"  analyst_cost     = ${analyst_cost:.4f}")
    print(f"  adversarial_cost = ${adversarial_cost:.4f}")
    print(f"  n_hypotheses     = {n_scout_ok}")
    print(f"  n_verified       = {n_approved}")
    print(f"  n_analyzed       = {n_analyst_ok}")
    print(f"  n_reports        = {n_reports}")

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
        "n_verified": n_approved,
        "n_analyzed": n_analyst_ok,
        "n_reports": n_reports,
        "cost_breakdown": {
            "scout": scout_cost,
            "verifier": verifier_cost,
            "analyst": analyst_cost,
            "adversarial": adversarial_cost,
            "total": total_cost,
        },
        "brief_id": getattr(brief, "id", None) if brief else None,
        "brief_path": str(out_path) if out_path else None,
        "quality_flags": getattr(brief, "quality_flags", []) if brief else [],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str))
    print(f"  manifest         = {manifest_path}")

    return 0


def _run_tier3_pipeline(
    analyst_outputs: list,
    cycle_id: str,
    remaining_budget: float,
) -> tuple[int, float]:
    """Tier 3 — invoke the existing 6-stage adversarial pipeline on the
    top analyst outputs. Returns (n_reports_written, total_cost).

    We feed each analyst output as a synthetic 'hypothesis' to the
    pipeline, capped at per-report budget == remaining_budget / N.

    The pipeline is the unchanged talis_desk.reports.pipeline module —
    we just call its public surface. If anything fails per-report we
    skip it and continue.
    """
    if not analyst_outputs:
        return 0, 0.0
    per_report_budget = max(0.10, remaining_budget / max(1, len(analyst_outputs)))
    total_cost = 0.0
    n_written = 0
    try:
        from talis_desk.reports.pipeline import run_pipeline_for_hypothesis  # type: ignore
        has_pipeline = True
    except Exception:
        has_pipeline = False
    if not has_pipeline:
        # Look for alternate surfaces in reports.pipeline.
        try:
            from talis_desk.reports import pipeline as _pipe  # type: ignore
            candidates = [
                "run_adversarial_pipeline",
                "produce_report",
                "run_report_pipeline",
                "run_pipeline",
            ]
            run_fn = None
            for name in candidates:
                if hasattr(_pipe, name):
                    run_fn = getattr(_pipe, name)
                    break
            if run_fn is None:
                print(
                    "  WARNING: adversarial pipeline entry point not found; "
                    "skipping Tier 3."
                )
                return 0, 0.0
        except Exception as e:
            print(f"  WARNING: pipeline import failed: {e}; skipping Tier 3.")
            return 0, 0.0
    else:
        run_fn = run_pipeline_for_hypothesis  # type: ignore

    for ao in analyst_outputs:
        if total_cost >= remaining_budget:
            print(f"  Tier 3 cost cap reached at {n_written} reports")
            break
        try:
            # Best-effort: the pipeline historically took (specialist_id,
            # hypothesis_id, cycle_id, budget). We pass what we can; if
            # the signature differs we fall back to a no-op.
            result = _call_pipeline_safely(
                run_fn, ao, cycle_id, per_report_budget,
            )
            if result:
                n_written += 1
                total_cost += float(getattr(result, "cost_usd", 0.0) or 0.0)
        except Exception as e:
            print(f"  pipeline error on scout={ao.scout_id}: {type(e).__name__}: {e}")
    return n_written, total_cost


def _call_pipeline_safely(run_fn, analyst_output, cycle_id: str,
                          budget_usd: float):
    """Call the pipeline entry point with whichever signature it accepts."""
    import inspect
    sig = inspect.signature(run_fn)
    params = sig.parameters
    kwargs = {}
    if "specialist_id" in params:
        kwargs["specialist_id"] = analyst_output.specialist_id
    if "hypothesis_id" in params:
        kwargs["hypothesis_id"] = analyst_output.hypothesis_id
    if "cycle_id" in params:
        kwargs["cycle_id"] = f"{cycle_id}__{analyst_output.specialist_id}"
    if "budget_usd" in params:
        kwargs["budget_usd"] = budget_usd
    if "budget" in params:
        kwargs["budget"] = budget_usd
    if "max_cost_usd" in params:
        kwargs["max_cost_usd"] = budget_usd
    try:
        return run_fn(**kwargs)
    except TypeError as e:
        # Pipeline signature didn't match; surface but don't crash.
        print(f"  pipeline sig mismatch ({e}); skipping {analyst_output.scout_id}")
        return None


if __name__ == "__main__":
    raise SystemExit(main())
