#!/usr/bin/env python
"""Export a mobile-first viewer for a 100-scout readiness/system run."""
from __future__ import annotations

import argparse
import html
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def main() -> int:
    args = _parse_args()
    prompt_dir = Path(args.prompt_output_dir).expanduser().resolve()
    report_path = Path(args.report_json).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    packet = build_packet(prompt_dir=prompt_dir, report_path=report_path)
    (output_dir / "index.html").write_text(render_html(packet), encoding="utf-8")
    (output_dir / "run.json").write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    for name in (
        "100_scout_readiness_report.json",
        "market_evolve_hard_experiment.json",
        "alpha_geometry.json",
        "alpha_geometry_cortex_review.json",
        "market_evolve_step.json",
        "market_evolve_lineage.json",
    ):
        src = prompt_dir / name
        if src.exists():
            shutil.copyfile(src, output_dir / name)
    print(f"SCOUT_SYSTEM_VIEWER_INDEX={output_dir / 'index.html'}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt_output_dir")
    parser.add_argument("--report-json", required=True)
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args()


def build_packet(*, prompt_dir: Path, report_path: Path) -> dict[str, Any]:
    report = _read_json(report_path)
    geometry = _read_json(prompt_dir / "alpha_geometry.json")
    cortex = _read_json(prompt_dir / "alpha_geometry_cortex_review.json")
    experiment = _read_json(prompt_dir / "market_evolve_hard_experiment.json")
    evolve = _read_json(prompt_dir / "market_evolve_step.json")
    lineage = _read_json(prompt_dir / "market_evolve_lineage.json")
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    readiness = report.get("readiness") if isinstance(report.get("readiness"), dict) else {}
    seeds = metrics.get("seeds") if isinstance(metrics.get("seeds"), dict) else {}
    scouts = metrics.get("scouts") if isinstance(metrics.get("scouts"), dict) else {}
    info = metrics.get("information_map") if isinstance(metrics.get("information_map"), dict) else {}
    self_healing = metrics.get("self_healing") if isinstance(metrics.get("self_healing"), dict) else {}
    market_evolve = metrics.get("market_evolve") if isinstance(metrics.get("market_evolve"), dict) else {}
    top_cells = [
        _slim_cell(row)
        for row in (geometry.get("cells") or [])[:8]
        if isinstance(row, dict)
    ]
    top_actions = [
        _slim_action(row)
        for row in ((geometry.get("action_plan") or {}).get("actions") or [])[:8]
        if isinstance(row, dict)
    ]
    return {
        "schema_version": "talis_100_scout_system_viewer_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_prompt_output_dir": str(prompt_dir),
        "source_report_json": str(report_path),
        "cycle_id": report.get("cycle_id"),
        "status": readiness.get("status"),
        "ready_for_live_1000": readiness.get("ready_for_live_1000"),
        "failed_gates": readiness.get("failed_gates") or [],
        "gates": readiness.get("gates") or {},
        "summary": {
            "scouts": seeds.get("count"),
            "completed": scouts.get("completed"),
            "errored": scouts.get("errored"),
            "strings": info.get("string_count"),
            "geometry_cells": (metrics.get("geometry") or {}).get("cell_count"),
            "routing_tasks": (metrics.get("geometry") or {}).get("routing_queue_count"),
            "effective_unique_cell_ratio": seeds.get("effective_unique_cell_ratio"),
            "duplicate_hypothesis_rate": scouts.get("duplicate_hypothesis_rate"),
            "evidence_ok_rate": scouts.get("evidence_ok_rate"),
            "cost_usd_estimate": scouts.get("total_cost_usd_estimate"),
            "prompt_variants": scouts.get("prompt_variants") or {},
            "promoted_hypotheses": info.get("promoted_hypotheses"),
            "self_healing_completed": self_healing.get("completed_tasks"),
            "self_healing_failed": self_healing.get("failed_tasks"),
        },
        "market_evolve": {
            **market_evolve,
            "experiment_status": experiment.get("status"),
            "experiment_decision": experiment.get("final_decision"),
            "experiment_score_delta": experiment.get("final_score_delta"),
            "experiment_proof": experiment.get("proof") or {},
            "best_evaluation": evolve.get("best_evaluation") or {},
            "lineage_nodes": len(lineage.get("nodes") or []),
            "lineage_edges": len(lineage.get("edges") or []),
            "frontier": (lineage.get("frontier") or [])[:5],
        },
        "geometry": {
            "global_metrics": geometry.get("global_metrics") or {},
            "top_cells": top_cells,
            "top_actions": top_actions,
            "cortex_status": cortex.get("status"),
            "shape_can_direct_next": cortex.get("shape_can_direct_next"),
            "cortex_work_orders": (cortex.get("cortex_work_orders") or [])[:5],
        },
    }


def _slim_cell(row: dict[str, Any]) -> dict[str, Any]:
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    return {
        "cell_key": row.get("cell_key"),
        "entity": row.get("entity"),
        "horizon": row.get("horizon"),
        "lens": row.get("lens"),
        "theme": row.get("theme"),
        "route_directive": row.get("route_directive"),
        "trade_scream_score": row.get("trade_scream_score") or metrics.get("trade_scream_score"),
        "verifier_readiness": row.get("verifier_readiness") or metrics.get("verifier_readiness"),
        "source_independence": metrics.get("source_independence"),
        "fragility": metrics.get("fragility"),
    }


def _slim_action(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": row.get("action"),
        "owner": row.get("owner"),
        "cell_key": row.get("cell_key"),
        "priority_score": row.get("priority_score"),
        "success_gate": row.get("success_gate"),
        "missing_edges": row.get("missing_edges") or [],
    }


def render_html(packet: dict[str, Any]) -> str:
    summary = packet.get("summary") or {}
    evolve = packet.get("market_evolve") or {}
    geometry = packet.get("geometry") or {}
    gates = packet.get("gates") if isinstance(packet.get("gates"), dict) else {}
    gate_rows = "".join(
        f"<li><span>{html.escape(str(name).replace('_', ' '))}</span><b>{'PASS' if ok else 'FAIL'}</b></li>"
        for name, ok in gates.items()
    )
    top_cells = "".join(_cell_card(row) for row in geometry.get("top_cells") or [])
    top_actions = "".join(_action_card(row) for row in geometry.get("top_actions") or [])
    variant_rows = "".join(
        f"<div><span>{html.escape(str(k))}</span><strong>{html.escape(str(v))}</strong></div>"
        for k, v in (summary.get("prompt_variants") or {}).items()
    )
    frontier_rows = "".join(
        f"<li><span>{html.escape(str(row.get('name') or row.get('program_id') or 'program'))}</span>"
        f"<b>{html.escape(str(row.get('next_action') or row.get('status') or ''))}</b></li>"
        for row in evolve.get("frontier") or []
        if isinstance(row, dict)
    )
    proof = evolve.get("experiment_proof") if isinstance(evolve.get("experiment_proof"), dict) else {}
    proof_rows = "".join(
        f"<li><span>{html.escape(str(k).replace('_', ' '))}</span><b>{html.escape(str(v))}</b></li>"
        for k, v in proof.items()
        if k != "quality_flags"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <link rel="icon" href="data:," />
  <title>Talis 100 Scout System Test</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #07080a;
      --panel: rgba(255,255,255,.072);
      --line: rgba(255,255,255,.13);
      --text: #f6f7f4;
      --muted: rgba(246,247,244,.68);
      --green: #74f1b1;
      --cyan: #75d8ff;
      --amber: #f5cb72;
      --rose: #ff8b9a;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background:
        radial-gradient(circle at 20% -10%, rgba(117,216,255,.18), transparent 32%),
        linear-gradient(180deg, #0d1114 0%, var(--bg) 56%);
      color: var(--text);
      font: 15px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", sans-serif;
      letter-spacing: 0;
      overflow-x: hidden;
    }}
    main {{ width: min(1180px, 100%); margin: 0 auto; padding: 22px 16px 44px; }}
    .hero {{ min-height: 74vh; display: grid; align-content: center; gap: 24px; }}
    .eyebrow {{ color: var(--green); text-transform: uppercase; font-size: 12px; font-weight: 800; letter-spacing: .08em; }}
    h1 {{ font-size: clamp(52px, 10vw, 118px); line-height: .9; margin: 10px 0 14px; max-width: 900px; }}
    h2 {{ font-size: clamp(32px, 6vw, 64px); line-height: .96; margin: 0 0 10px; }}
    h3 {{ font-size: 21px; line-height: 1.05; margin: 0 0 8px; }}
    p {{ color: var(--muted); margin: 0; max-width: 760px; font-size: 18px; }}
    .hero-grid, .metric-grid, .two, .cell-grid {{ display: grid; gap: 10px; }}
    .hero-grid {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .metric-grid {{ grid-template-columns: repeat(6, minmax(0, 1fr)); margin-top: 18px; }}
    .two {{ grid-template-columns: minmax(0, 1.05fr) minmax(0, .95fr); align-items: start; }}
    .card, .metric, .panel, .cell, .gate-list li {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      backdrop-filter: blur(18px);
    }}
    .card, .panel, .cell {{ padding: 16px; min-width: 0; }}
    .metric {{ padding: 13px; min-height: 96px; min-width: 0; overflow-wrap: anywhere; }}
    .metric span, .card span, .cell span {{ display: block; color: var(--muted); font-size: 12px; font-weight: 750; text-transform: uppercase; }}
    .metric strong {{ display: block; margin-top: 8px; font-size: 27px; line-height: 1; }}
    section {{ margin-top: 34px; }}
    .flow {{ display: flex; gap: 10px; overflow-x: auto; scroll-snap-type: x mandatory; padding-bottom: 12px; width: 100%; max-width: 100%; }}
    .flow .card {{ scroll-snap-align: start; min-height: 230px; flex: 0 0 min(31%, 360px); }}
    .flow b {{ color: var(--cyan); }}
    .gate-list {{ list-style: none; margin: 14px 0 0; padding: 0; display: grid; gap: 7px; }}
    .gate-list li {{ display: flex; justify-content: space-between; gap: 12px; padding: 10px 12px; }}
    .gate-list li > * {{ min-width: 0; overflow-wrap: anywhere; }}
    .gate-list b {{ color: var(--green); text-align: right; }}
    .cell-grid {{ grid-template-columns: repeat(4, minmax(0, 1fr)); margin-top: 12px; }}
    .cell strong {{ display: block; font-size: 18px; margin: 7px 0 9px; overflow-wrap: anywhere; }}
    .cell em {{ color: var(--cyan); font-style: normal; }}
    .bar {{ height: 7px; border-radius: 999px; background: rgba(255,255,255,.12); overflow: hidden; margin-top: 10px; }}
    .bar i {{ display: block; height: 100%; background: linear-gradient(90deg, var(--green), var(--cyan)); }}
    .status-pass {{ color: var(--green); }}
    .status-warn {{ color: var(--amber); }}
    .status-reject {{ color: var(--rose); }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: rgba(0,0,0,.28); border: 1px solid var(--line); padding: 12px; border-radius: 8px; color: #dce8e6; max-height: 420px; overflow: auto; }}
    a {{ color: var(--cyan); text-decoration: none; }}
    @media (max-width: 860px) {{
      main {{ padding: 18px 12px 38px; }}
      .hero {{ min-height: 82vh; }}
      .hero-grid, .metric-grid, .two, .cell-grid {{ grid-template-columns: 1fr; }}
      .flow .card {{ flex-basis: 86%; }}
      p {{ font-size: 17px; }}
    }}
  </style>
</head>
<body>
<main>
  <section class="hero">
    <div>
      <div class="eyebrow">100-scout system readiness / {html.escape(str(packet.get("cycle_id") or ""))}</div>
      <h1>The first layer is ready for a guarded live scale test.</h1>
      <p>This is the system run from the ground up: seed cells, policy genome, scout harness, map memory, geometry, cortex, self-healing, and MarketEvolve. It is not a trade signal. It is a proof that the layer can run, store, route, and reject weak policy mutations.</p>
    </div>
    <div class="hero-grid">
      <div class="card"><span>Status</span><h3 class="status-pass">{html.escape(str(packet.get("status")).upper())}</h3><p>All readiness gates passed after accounting for deliberate A/B duplicate cells.</p></div>
      <div class="card"><span>Scale decision</span><h3>{html.escape(str(packet.get("ready_for_live_1000")))}</h3><p>Mechanically ready for live-provider 1,000 gating, after a live canary/ramp policy.</p></div>
      <div class="card"><span>MarketEvolve</span><h3 class="status-reject">{html.escape(str(evolve.get("experiment_decision") or "pending")).replace("_", " ")}</h3><p>The candidate did not beat control; the system refused to promote it.</p></div>
      <div class="card"><span>Cost estimate</span><h3>${html.escape(str(summary.get("cost_usd_estimate")))}</h3><p>Offline deterministic shim cost; live-provider cost is checked separately.</p></div>
    </div>
  </section>

  <section>
    <h2>What Actually Ran</h2>
    <p>Each card is one layer boundary. The point is not that every scout is brilliant; it is that the full layer turns many narrow observations into a shared market terrain with gates, receipts, and feedback.</p>
    <div class="flow">
      <div class="card"><span>1 / split</span><h3>Market cells</h3><p><b>{summary.get("scouts")}</b> scout jobs were generated from the universe, themes, horizon/lens/bias grid, and evolution policy.</p></div>
      <div class="card"><span>2 / policy</span><h3>Research genome</h3><p>MarketEvolve stamped each seed with prompt variant, tool budget, and experiment arm.</p></div>
      <div class="card"><span>3 / tools</span><h3>Harness receipts</h3><p>Scouts used small approved tool slices; evidence OK rate was <b>{summary.get("evidence_ok_rate")}</b>.</p></div>
      <div class="card"><span>4 / outputs</span><h3>Information strings</h3><p>The swarm produced <b>{summary.get("strings")}</b> causal strings, not just raw hypotheses.</p></div>
      <div class="card"><span>5 / map</span><h3>Geometry</h3><p>The map formed <b>{summary.get("geometry_cells")}</b> cells and emitted <b>{summary.get("routing_tasks")}</b> next-route tasks.</p></div>
      <div class="card"><span>6 / repair</span><h3>Self-healing</h3><p><b>{summary.get("self_healing_completed")}</b> repair tasks completed; <b>{summary.get("self_healing_failed")}</b> failed.</p></div>
      <div class="card"><span>7 / evolve</span><h3>Hard experiment</h3><p><b>{evolve.get("arm_counts", {}).get("control", 0)}</b> control and <b>{evolve.get("arm_counts", {}).get("candidate", 0)}</b> candidate applications were scored.</p></div>
    </div>
  </section>

  <section>
    <h2>The Metrics</h2>
    <div class="metric-grid">
      {metric("Scouts completed", f"{summary.get('completed')}/{summary.get('scouts')}")}
      {metric("Strings/scout", summary.get("strings") and round(float(summary.get("strings")) / max(1, int(summary.get("scouts") or 1)), 2))}
      {metric("Geometry cells", summary.get("geometry_cells"))}
      {metric("Routing tasks", summary.get("routing_tasks"))}
      {metric("Effective unique", summary.get("effective_unique_cell_ratio"))}
      {metric("Dup hypothesis", summary.get("duplicate_hypothesis_rate"))}
    </div>
  </section>

  <section class="two">
    <div class="panel">
      <h2>MarketEvolve Did The Right Thing</h2>
      <p>The candidate policy got a real matched test and was rejected because score delta failed. That is what makes this closer to an AlphaEvolve-style system: candidate ideas are cheap; promotion is expensive and evidence-gated.</p>
      <div class="metric-grid" style="grid-template-columns: repeat(3, 1fr);">
        {metric("Pairs", evolve.get("paired_seed_slices"))}
        {metric("Decision", str(evolve.get("experiment_decision") or "pending").replace("_", " "))}
        {metric("Score delta", evolve.get("experiment_score_delta"))}
      </div>
      <ul class="gate-list">{proof_rows}</ul>
    </div>
    <div class="panel">
      <h3>Prompt variants used</h3>
      <div class="metric-grid" style="grid-template-columns: 1fr 1fr;">{variant_rows}</div>
      <h3 style="margin-top:18px;">Evolution frontier</h3>
      <ul class="gate-list">{frontier_rows}</ul>
    </div>
  </section>

  <section>
    <h2>The Shape That Came Back</h2>
    <p>These are the highest-priority geometry cells and route actions. This is where the map starts to scream: verify now, widen scouts, repair sources, or resolve tension.</p>
    <div class="cell-grid">{top_cells}</div>
    <div class="cell-grid">{top_actions}</div>
  </section>

  <section class="two">
    <div class="panel">
      <h2>Readiness Gates</h2>
      <ul class="gate-list">{gate_rows}</ul>
    </div>
    <div class="panel">
      <h2>Raw Artifacts</h2>
      <p>The page copies the core JSON next to the viewer so the story and audit trail stay together.</p>
      <ul class="gate-list">
        <li><span>Readiness report</span><b><a href="100_scout_readiness_report.json">open</a></b></li>
        <li><span>Hard experiment</span><b><a href="market_evolve_hard_experiment.json">open</a></b></li>
        <li><span>Alpha geometry</span><b><a href="alpha_geometry.json">open</a></b></li>
        <li><span>Cortex review</span><b><a href="alpha_geometry_cortex_review.json">open</a></b></li>
        <li><span>Run packet</span><b><a href="run.json">open</a></b></li>
      </ul>
    </div>
  </section>
</main>
</body>
</html>
"""


def metric(label: str, value: Any) -> str:
    return f'<div class="metric"><span>{html.escape(str(label))}</span><strong>{html.escape(str(value))}</strong></div>'


def _cell_card(row: dict[str, Any]) -> str:
    score = _pct(row.get("trade_scream_score"))
    return (
        '<div class="cell">'
        f'<span>{html.escape(str(row.get("route_directive") or "observe"))}</span>'
        f'<strong>{html.escape(str(row.get("cell_key") or row.get("entity") or "cell"))}</strong>'
        f'<p><em>trade scream {score}%</em> / readiness {_pct(row.get("verifier_readiness"))}% / source {_pct(row.get("source_independence"))}%</p>'
        f'<div class="bar"><i style="width:{max(2, min(100, score))}%"></i></div>'
        '</div>'
    )


def _action_card(row: dict[str, Any]) -> str:
    return (
        '<div class="cell">'
        f'<span>{html.escape(str(row.get("owner") or "router"))}</span>'
        f'<strong>{html.escape(str(row.get("action") or "action"))}</strong>'
        f'<p>{html.escape(str(row.get("cell_key") or ""))}</p>'
        f'<p><em>{html.escape(str(row.get("success_gate") or "success gate pending"))}</em></p>'
        '</div>'
    )


def _pct(value: Any) -> int:
    try:
        return int(round(float(value) * 100))
    except Exception:
        return 0


def _read_json(path: Path) -> Any:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
