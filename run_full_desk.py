"""Prod-style full desk run.

Runs the canonical 6-stage loop for ALL registered specialists against
the populated talis-tic data (36k claims, 63k timeseries, 8k entities,
730 semantic-indexed docs at /tic/tic.db). Then composes one
consolidated daily brief that synthesizes hypotheses, trade ideas,
hot debates, and triggered playbooks from EVERY specialist's cycle.

Usage:
    python3 run_full_desk.py [--db PATH] [--scope SCOPE]
                              [--budget 5.0] [--specialists s1,s2,...]
                              [--parallel|--sequential]

By default runs sequentially so each specialist sees the prior ones'
scratchpad messages (cross-talk lift). Parallel mode is faster but
each specialist runs against an identical pre-state.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# talis-tic must be importable before any talis_desk imports that lazy-load it.
# Codex finding #16: centralized in `talis_desk._tic_config`; resolves
# `TALIS_TIC_ROOT` (env) and falls back to the legacy dev path with a
# deprecation warning. Kept as `TIC_PATH` for back-compat with downstream
# references in this script.
from talis_desk._tic_config import ensure_tic_on_path as _ensure_tic_on_path  # noqa: E402
from talis_desk._tic_config import get_tic_root as _get_tic_root  # noqa: E402
_ensure_tic_on_path()
TIC_PATH = str(_get_tic_root())


SPECIALIST_REGISTRY = {
    # specialist_id -> (module_path, register_fn_name)
    "macro_regime": ("talis_desk.specialists.macro_regime",
                      "register_macro_regime_v1"),
    "microstructure": ("talis_desk.specialists.microstructure_v1",
                        "register_microstructure_v1"),
    "smart_money": ("talis_desk.specialists.smart_money_v1",
                     "register_smart_money_v1"),
    "sentiment_event": ("talis_desk.specialists.sentiment_event_v1",
                         "register_sentiment_event_v1"),
    "rrg_rotation": ("talis_desk.specialists.rrg_rotation_v1",
                      "register_rrg_rotation_v1"),
    "options_vol": ("talis_desk.specialists.options_vol_v1",
                     "register_options_vol_v1"),
    "polymarket_divergence": ("talis_desk.specialists.polymarket_divergence_v1",
                               "register_polymarket_divergence_v1"),
    "anomaly_scanner": ("talis_desk.specialists.anomaly_scanner_v1",
                         "register_anomaly_scanner_v1"),
}


def banner(s: str) -> None:
    print()
    print("=" * 80)
    print(s)
    print("=" * 80)


def register_specialists(only: list[str] | None = None) -> dict[str, str]:
    """Register all specialists (or just `only`). Returns dict
    specialist_id -> spst_id."""
    import importlib
    registered: dict[str, str] = {}
    targets = only or list(SPECIALIST_REGISTRY.keys())
    for sid in targets:
        if sid not in SPECIALIST_REGISTRY:
            print(f"[register] {sid}: SKIP — not in registry")
            continue
        mod_path, fn_name = SPECIALIST_REGISTRY[sid]
        try:
            mod = importlib.import_module(mod_path)
            fn = getattr(mod, fn_name)
            state = fn()
            registered[sid] = state.id
            print(f"[register] {sid:18s} -> {state.id} (v{state.persona_version})")
        except Exception as e:
            print(f"[register] {sid:18s} FAIL: {type(e).__name__}: {e}")
    return registered


def run_one_cycle(specialist_id: str, cycle_id: str, budget_usd: float,
                  paper_only: bool) -> dict:
    """Run a single specialist's research cycle. Returns a result dict."""
    from talis_desk.loop import run_research_cycle
    from talis_desk.loop.runner import LoopConfig

    cfg = LoopConfig(max_cost_usd=budget_usd, paper_only=paper_only)
    t0 = datetime.now(timezone.utc)
    try:
        result = run_research_cycle(
            specialist_id=specialist_id,
            cycle_id=f"{cycle_id}__{specialist_id}",
            loop_config=cfg,
        )
        return {
            "specialist_id": specialist_id,
            "ok": True,
            "elapsed_s": result.elapsed_seconds,
            "cost_usd": result.total_cost_usd,
            "tool_calls": result.total_tool_calls,
            "n_hypotheses": len(result.plan.hypotheses) if result.plan else 0,
            "n_trade_ideas": len(result.synthesis.new_trade_ideas),
            "n_resolved": len(result.synthesis.resolved_hypotheses),
            "n_peer_messages": len(result.synthesis.peer_messages),
            "n_debates_triggered": len(result.synthesis.debate_triggers),
            "n_debates_opened": len(result.synthesis.opened_debate_ids),
            "opened_debate_ids": list(result.synthesis.opened_debate_ids),
            "n_reports": len(getattr(result.synthesis, "report_ids", []) or []),
            "report_ids": list(getattr(result.synthesis, "report_ids", []) or []),
            "kill_switch": result.kill_switch_triggered,
            "quality_flags": result.quality_flags,
            "next_state_id": result.next_state_id,
        }
    except Exception as e:
        return {
            "specialist_id": specialist_id,
            "ok": False,
            "elapsed_s": (datetime.now(timezone.utc) - t0).total_seconds(),
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc()[-2000:],
        }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=str, default=None,
                   help="desk.db path; default tempdir/desk.db")
    p.add_argument("--scope", type=str, default="market")
    p.add_argument("--cycle-id", type=str, default=None)
    p.add_argument("--budget", type=float, default=5.0,
                   help="per-specialist USD budget")
    p.add_argument("--specialists", type=str, default=None,
                   help="comma-separated list; default all 4")
    p.add_argument("--parallel", action="store_true",
                   help="run specialists in parallel (default sequential)")
    p.add_argument("--paper-only", action="store_true", default=True)
    p.add_argument("--live", dest="paper_only", action="store_false")
    args = p.parse_args()

    if args.db is None:
        tmpdir = tempfile.mkdtemp(prefix="full_desk_")
        db_path = Path(tmpdir) / "desk.db"
    else:
        db_path = Path(args.db)
        db_path.parent.mkdir(parents=True, exist_ok=True)

    cycle_id = args.cycle_id or f"prod_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}"
    specialists = (args.specialists or "").split(",") if args.specialists else None
    specialists = [s.strip() for s in specialists if s.strip()] if specialists else None

    banner(f"TALIS FULL DESK — {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"  desk.db         = {db_path}")
    print(f"  cycle_id        = {cycle_id}")
    print(f"  scope           = {args.scope}")
    print(f"  budget/special. = ${args.budget}")
    print(f"  paper_only      = {args.paper_only}")
    print(f"  mode            = {'parallel' if args.parallel else 'sequential'}")
    print(f"  tic.db          = {TIC_PATH}/tic/tic.db")

    # Pin desk store + tic store
    from talis_desk.store import DeskStore
    import talis_desk.store as _store_mod
    _store_mod._STORE = DeskStore(db_path=db_path)

    # ------------------------------------------------------------------
    # PREFLIGHT — Regenerate the tool atlas BEFORE specialists run. This is
    # the only place that scans builtin tools / sources / skills into
    # `tool_atlas`. If we skip this, get_atlas_snapshot_for_cycle() returns
    # zero rows and every dispatch fails with `tool_uri_not_in_atlas`.
    banner("PREFLIGHT — regenerate tool atlas")
    from talis_desk.tool_atlas.atlas import regenerate_tool_atlas
    atlas = regenerate_tool_atlas()
    print(f"  n_tools   = {atlas.n_tools}")
    print(f"  n_sources = {atlas.n_sources}")
    print(f"  n_skills  = {atlas.n_skills}")
    print(f"  total rows= {len(atlas.rows)}")
    if not atlas.rows:
        print(
            "FATAL: tool_atlas is empty after regenerate_tool_atlas(). "
            "Expected ~65 builtin tools plus sources + skills. The desk "
            "refuses to run a cycle against an empty atlas because every "
            "dispatch would fail with tool_uri_not_in_atlas. Check that "
            f"talis-tic is importable from {TIC_PATH!r} and that the "
            "builtin tool registry was scanned successfully."
        )
        return 1

    # ------------------------------------------------------------------
    banner("REGISTER specialists")
    registered = register_specialists(only=specialists)
    if not registered:
        print("FATAL: no specialists registered")
        return 1

    # ------------------------------------------------------------------
    banner("RUN cycles")
    results: list[dict] = []
    if args.parallel:
        with ThreadPoolExecutor(max_workers=len(registered)) as ex:
            futs = {
                ex.submit(run_one_cycle, sid, cycle_id, args.budget,
                          args.paper_only): sid
                for sid in registered
            }
            for fut in as_completed(futs):
                r = fut.result()
                results.append(r)
                _print_result_summary(r)
    else:
        for sid in registered:
            print(f"\n[run] starting {sid}...")
            r = run_one_cycle(sid, cycle_id, args.budget, args.paper_only)
            results.append(r)
            _print_result_summary(r)

    # ------------------------------------------------------------------
    banner("DESK TOTALS")
    ok = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    total_cost = sum(r["cost_usd"] for r in ok)
    total_tools = sum(r["tool_calls"] for r in ok)
    total_hyps = sum(r["n_hypotheses"] for r in ok)
    total_ideas = sum(r["n_trade_ideas"] for r in ok)
    total_msgs = sum(r["n_peer_messages"] for r in ok)
    total_debates = sum(r["n_debates_triggered"] for r in ok)
    total_debates_opened = sum(r.get("n_debates_opened", 0) for r in ok)
    total_reports = sum(r.get("n_reports", 0) for r in ok)
    print(f"  specialists_ok     = {len(ok)}/{len(results)}")
    print(f"  specialists_failed = {len(failed)}")
    print(f"  total_cost_usd     = ${total_cost:.4f}")
    print(f"  total_tool_calls   = {total_tools}")
    print(f"  total_hypotheses   = {total_hyps}")
    print(f"  total_trade_ideas  = {total_ideas}")
    print(f"  total_research_reports = {total_reports}")
    print(f"  total_peer_msgs    = {total_msgs}")
    print(f"  total_debate_trigs = {total_debates}")
    print(f"  total_debates_opened = {total_debates_opened}")
    for f in failed:
        print(f"  FAILED  {f['specialist_id']}: {f.get('error','?')}")

    # ------------------------------------------------------------------
    banner("COMPOSE BRIEF (synthesizes ALL specialists' outputs)")
    from talis_desk.brief import compose_brief
    # Each specialist ran under `{base}__{specialist_id}`. The base id alone
    # only matches rows that explicitly carry the base id (currently none),
    # so cycle stats would render zeros. Pass the FULL set of cycle_ids the
    # specialists wrote under, plus the base id for completeness.
    specialist_cycle_ids = [f"{cycle_id}__{sid}" for sid in registered]
    aggregated_cycle_ids = [cycle_id] + specialist_cycle_ids
    brief = compose_brief(
        scope=args.scope,
        cycle_id=cycle_id,
        cycle_ids=aggregated_cycle_ids,
        output_format="markdown",
    )
    print(f"  brief_id            = {brief.id}")
    print(f"  headline_model_used = {brief.headline_model_used}")
    print(f"  quality_flags       = {brief.quality_flags}")
    print(f"  markdown_len        = {len(brief.markdown)} chars")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = Path(f"/tmp/talis_full_desk_brief_{ts}.md")
    out_path.write_text(brief.markdown)
    print(f"  markdown_path       = {out_path}")

    # Also persist a JSON manifest of the run so the user can audit.
    manifest_path = Path(f"/tmp/talis_full_desk_manifest_{ts}.json")
    import json
    manifest = {
        "cycle_id": cycle_id,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "scope": args.scope,
        "desk_db": str(db_path),
        "tic_db": f"{TIC_PATH}/tic/tic.db",
        "registered_specialists": registered,
        "results": [{k: v for k, v in r.items() if k != "traceback"}
                    for r in results],
        "brief_id": brief.id,
        "brief_path": str(out_path),
        "totals": {
            "specialists_ok": len(ok),
            "total_cost_usd": total_cost,
            "total_tool_calls": total_tools,
            "total_hypotheses": total_hyps,
            "total_trade_ideas": total_ideas,
            "total_peer_msgs": total_msgs,
            "total_debate_trigs": total_debates,
            "total_debates_opened": total_debates_opened,
            "total_research_reports": total_reports,
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str))
    print(f"  manifest_path       = {manifest_path}")

    banner("BRIEF")
    print(brief.markdown)

    return 0 if not failed else 2


def _print_result_summary(r: dict) -> None:
    if not r.get("ok"):
        print(f"  [{r['specialist_id']:18s}] FAIL: {r.get('error','?')}")
        return
    print(
        f"  [{r['specialist_id']:18s}] "
        f"cost=${r['cost_usd']:.4f} tools={r['tool_calls']:>3d} "
        f"hyps={r['n_hypotheses']:>2d} ideas={r['n_trade_ideas']:>2d} "
        f"resolved={r['n_resolved']:>2d} "
        f"reports={r.get('n_reports', 0):>2d} "
        f"deb_trig={r['n_debates_triggered']:>2d} "
        f"deb_open={r.get('n_debates_opened', 0):>2d} "
        f"msgs={r['n_peer_messages']:>2d} elapsed={r['elapsed_s']:.1f}s"
    )


if __name__ == "__main__":
    raise SystemExit(main())
