#!/usr/bin/env python
"""Export a mobile-friendly static prompt/output viewer.

The ground-up smoke harness writes raw prompt artifacts. This script turns one
run into a self-contained HTML page suitable for GitHub Pages or any static
host.
"""
from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from talis_desk.information_map import summarize_data_substrate


FILENAMES = {
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
    "geometry_cortex_mutation_path": "geometry_cortex_mutation_path.json",
    "evolution_control_task_dispatch": "evolution_control_task_dispatch.json",
    "cortex_task_worker_execution": "cortex_task_worker_execution.json",
    "cortex_task_feedback_evaluator": "cortex_task_feedback_evaluator.json",
    "market_evolve_step": "market_evolve_step.json",
    "shape_recurrent_loop": "shape_recurrent_loop.json",
    "market_evolve_hard_experiment": "market_evolve_hard_experiment.json",
    "market_evolve_lineage": "market_evolve_lineage.json",
    "monitor_payload": "monitor_payload.json",
    "research_evolution_payload": "research_evolution_payload.json",
    "system_trace": "system_trace.json",
    "market_universe_manifest": "market_universe_manifest.json",
    "market_map_governor": "market_map_governor.json",
    "market_map_self_healing": "market_map_self_healing.json",
    "coverage_gap_manifest": "coverage_gap_manifest.json",
}


REFERENCE_POST = {
    "source": "X",
    "author": "Jason Goldberg (@betashop)",
    "post_url": "https://x.com/betashop/status/2057488257139581110?s=46",
    "article_url": "https://x.com/i/article/2057477003679313920",
    "observed_text": "The public oEmbed returned a post containing a link to an X Article.",
    "lesson": (
        "Treat social posts as entry points, not evidence. Resolve the linked primary artifact, "
        "store the post and article separately, then make the scout state what was directly observed "
        "versus what still needs authenticated/source-level retrieval."
    ),
}


SYSTEM_PARTS = [
    {
        "title": "Signal Intake",
        "summary": "The front door. Posts, event feeds, node data, wallet traces, charts, filings, calendars, and future mempool feeds arrive here as untrusted clues.",
        "input": "External and node-native data",
        "output": "Timestamped evidence refs",
        "why": "Never let a screenshot become truth. Resolve it into source-linked evidence first.",
        "layers": ["event_intelligence_ingest", "node_intelligence_ingest"],
    },
    {
        "title": "Tool Atlas",
        "summary": "The approved instrument wall. Scouts can draw from any read-only tool or source with schemas, costs, permissions, and logs.",
        "input": "Tool candidates and source needs",
        "output": "Budgeted tool/source calls and logs",
        "why": "Wide scouts only work if they can reach the whole approved estate without spraying random calls.",
        "layers": ["analysis_tool_creation_iteration", "scout_wiring_fallbacks"],
    },
    {
        "title": "Prompt Lab",
        "summary": "The quality-control room. Prompts must prove they create useful strings before thousands of cheap calls are allowed.",
        "input": "Prompt contract and sample outputs",
        "output": "Passed or blocked scale gate",
        "why": "DeepSeek Flash gets scaled only after the prompt proves it returns usable intelligence.",
        "layers": ["prompt_contract_and_scale_gate"],
    },
    {
        "title": "Scout Swarm",
        "summary": "The wide sensing layer. Each scout studies one tiny slice of the market and explains the causal chain it sees.",
        "input": "Entity, horizon, lens, evidence",
        "output": "Hypothesis and strings",
        "why": "Thousands of cheap scouts map the market without pretending every observation is actionable.",
        "layers": ["live_scout_execution_and_monitor_join", "scout_wiring_fallbacks"],
    },
    {
        "title": "Information Map",
        "summary": "The memory. Claims become queryable objects with source refs, time horizons, quality flags, and links to related claims.",
        "input": "Scout outputs",
        "output": "Queryable intelligence graph",
        "why": "The map becomes memory and routing infrastructure, not a sidecar notebook.",
        "layers": ["event_intelligence_ingest", "node_intelligence_ingest"],
    },
    {
        "title": "Attention Gate",
        "summary": "The triage desk. It compares many strings, finds overlaps and contradictions, and chooses what deserves deeper work.",
        "input": "Many information strings",
        "output": "Promoted hypotheses",
        "why": "This is the anti-spam layer between broad sensing and expensive verification.",
        "layers": ["information_synthesis_budget_gate"],
    },
    {
        "title": "Alpha Geometry + Evolution",
        "summary": "The shape layer. Strings become market cells with coordinates, route directives, evaluator scores, mutations, and A/B experiment plans.",
        "input": "Information strings, tool receipts, source families",
        "output": "Map shape, routing directive, evolved research policy",
        "why": "The desk should improve its own prompting, slicing, and tool use from measured outcomes.",
        "layers": ["alpha_geometry_market_evolve", "shape_recurrent_loop", "cortex_task_worker_execution"],
    },
    {
        "title": "Verifier + Tool Builders",
        "summary": "The due-diligence room and workshop. Strong claims get checked; missing capabilities become new tools.",
        "input": "Promoted claims and gaps",
        "output": "Validated claims or new tools",
        "why": "Agents should improve the desk itself, especially around node discovery and mempool intelligence.",
        "layers": ["analysis_tool_creation_iteration"],
    },
    {
        "title": "Monitor + PM",
        "summary": "The user surface. It turns the machinery into a calm explanation, alert, or portfolio-aware decision path.",
        "input": "Stored graph and verifier outputs",
        "output": "Human-readable product surface",
        "why": "This is where the machine becomes legible enough to trust from a phone.",
        "layers": ["monitor_payload_surface"],
    },
]


def main() -> int:
    args = _parse_args()
    prompt_dir = Path(args.prompt_output_dir).expanduser().resolve()
    report_path = Path(args.report_json).expanduser().resolve() if args.report_json else prompt_dir.parent / "groundup_report.json"
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    report = _read_json(report_path)
    artifacts = {key: _read_artifact(prompt_dir / filename) for key, filename in FILENAMES.items()}
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_path": str(report_path),
        "prompt_output_dir": str(prompt_dir),
        "report": report,
        "artifacts": artifacts,
        "reference_post": REFERENCE_POST,
    }
    html_text = render_html(data)
    (output_dir / "index.html").write_text(html_text, encoding="utf-8")
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    (output_dir / "run.json").write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    print(f"PROMPT_VIEWER_INDEX={output_dir / 'index.html'}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt_output_dir", help="Directory emitted by smoke_node_intelligence_groundup.py")
    parser.add_argument("--report-json", default="", help="Optional explicit groundup_report.json path")
    parser.add_argument("--output-dir", required=True, help="Directory to write static viewer files")
    return parser.parse_args()


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


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def render_html(data: dict[str, Any]) -> str:
    blob = json.dumps(data, ensure_ascii=True)
    report = data.get("report") or {}
    layers = report.get("layers") or []
    summary = _monitor_summary(data)
    story = _story(data)
    card_html = _render_metric_cards(report, layers, summary, story)
    system_map_html = _render_system_map(layers)
    part_details_html = _render_system_part_details()
    journey_html = _render_signal_journey(data, story, summary)
    scout_view_html = _render_scout_view(data, story)
    conclusion_html = _render_conclusion_logic(data, story)
    source_html = _render_source_cards(data)
    layer_html = "\n".join(_render_layer(row) for row in layers)
    raw_panels = "\n".join([
        _render_panel("system", "System Prompt", data, "live_scout_system_prompt", active=True),
        _render_panel("user", "User Prompt", data, "live_scout_user_prompt"),
        _render_panel("evidence", "Tool Evidence", data, "live_scout_tool_evidence"),
        _render_panel("model", "Model Output", data, "live_scout_model_output"),
        _render_panel("persisted", "Persisted Scout Output", data, "live_scout_persisted_output"),
        _render_panel("geometry", "Alpha Geometry", data, "alpha_geometry"),
        _render_panel("tasks", "Evolution-Control Tasks", data, "evolution_control_task_dispatch"),
        _render_panel("worker", "Cortex Worker", data, "cortex_task_worker_execution"),
        _render_panel("feedback", "Cortex Feedback", data, "cortex_task_feedback_evaluator"),
            _render_panel("evolve", "MarketEvolve Step", data, "market_evolve_step"),
            _render_panel("recurrent", "Shape Recurrent Loop", data, "shape_recurrent_loop"),
            _render_panel("experiment", "Hard Experiment", data, "market_evolve_hard_experiment"),
            _render_panel("lineage", "Evolution Lineage", data, "market_evolve_lineage"),
            _render_panel("monitor", "Monitor Payload", data, "monitor_payload"),
        _render_panel("research", "Research Evolution Payload", data, "research_evolution_payload"),
        _render_panel("trace", "System Trace", data, "system_trace"),
        _render_panel("universe", "Market Universe", data, "market_universe_manifest"),
        _render_panel("governor", "Map Governor", data, "market_map_governor"),
        _render_panel("selfheal", "Self-Healing Plan", data, "market_map_self_healing"),
        _render_panel("coverage", "Coverage Gaps", data, "coverage_gap_manifest"),
    ])
    cohesive_story_html = _render_cohesive_story(
        data=data,
        story=story,
        summary=summary,
        layers=layers,
        source_html=source_html,
        raw_panels=raw_panels,
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Talis Scout Lab</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #050506;
      --ink: #f5f6f8;
      --muted: #a8acb4;
      --soft: #737984;
      --glass: rgba(25, 27, 32, .68);
      --glass-2: rgba(255, 255, 255, .065);
      --line: rgba(255, 255, 255, .12);
      --line-strong: rgba(255, 255, 255, .22);
      --green: #6ee7b7;
      --mint: #5eead4;
      --amber: #f6c453;
      --blue: #8ec5ff;
      --rose: #ff9aa7;
      --red: #ff6b6b;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      --sans: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", Inter, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background:
        linear-gradient(160deg, rgba(38, 62, 68, .86) 0%, rgba(8, 10, 12, .96) 38%, #050506 100%);
      color: var(--ink);
      font-family: var(--sans);
      letter-spacing: 0;
      overflow-x: hidden;
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image: linear-gradient(rgba(255,255,255,.045) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px);
      background-size: 42px 42px;
      mask-image: linear-gradient(to bottom, rgba(0,0,0,.4), transparent 54%);
    }}
    a {{ color: var(--mint); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .shell {{
      width: 100%;
      max-width: 1180px;
      margin: 0 auto;
      padding: max(16px, env(safe-area-inset-top)) 14px 58px;
      overflow-x: hidden;
    }}
    header, section, article, .hero, .glass, .answer, .map, .story-card, .source-card, .scout-card, .prompt-window, .logic-card {{
      min-width: 0;
      max-width: 100%;
    }}
    p, h1, h2, h3, strong, small {{
      overflow-wrap: anywhere;
    }}
    header {{
      display: grid;
      gap: 16px;
      padding: 14px 0 18px;
    }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      font-weight: 700;
    }}
    .dot {{
      width: 9px;
      height: 9px;
      border-radius: 999px;
      background: var(--green);
      box-shadow: 0 0 0 4px rgba(64, 209, 154, .12);
    }}
    h1 {{
      margin: 0;
      max-width: 960px;
      font-size: clamp(42px, 14vw, 104px);
      line-height: .88;
      letter-spacing: 0;
      overflow-wrap: anywhere;
    }}
    .lede {{
      margin: 0;
      max-width: 820px;
      color: var(--muted);
      font-size: clamp(16px, 2.2vw, 21px);
      line-height: 1.45;
    }}
    .toolbar {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
      margin-top: 8px;
      max-width: 100%;
    }}
    .pill, button {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,.07);
      color: var(--ink);
      border-radius: 8px;
      padding: 10px 12px;
      font: 700 13px var(--sans);
      backdrop-filter: blur(20px);
    }}
    .pill {{
      max-width: 100%;
      white-space: normal;
      overflow-wrap: anywhere;
    }}
    button {{ cursor: pointer; }}
    button.active {{ border-color: var(--mint); color: var(--mint); }}
    .concierge {{
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(290px, .9fr);
      gap: 12px;
      margin: 8px 0 18px;
    }}
    .concierge-main, .concierge-side {{
      border: 1px solid var(--line);
      background: var(--glass);
      border-radius: 8px;
      padding: 18px;
      backdrop-filter: blur(26px) saturate(130%);
    }}
    .concierge-main h2 {{
      margin: 0 0 12px;
      font-size: clamp(30px, 7vw, 64px);
      line-height: .96;
    }}
    .concierge-main p, .concierge-side p {{
      margin: 0;
      color: var(--muted);
      font-size: 15px;
      line-height: 1.5;
    }}
    .concierge-side {{
      display: grid;
      gap: 10px;
      align-content: start;
    }}
    .plain-row {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,.055);
      border-radius: 8px;
      padding: 12px;
    }}
    .plain-row span {{
      display: block;
      color: var(--soft);
      font-size: 11px;
      font-weight: 900;
      text-transform: uppercase;
      margin-bottom: 6px;
    }}
    .plain-row strong {{
      display: block;
      font-size: 15px;
      line-height: 1.28;
    }}
    .storyline {{
      display: grid;
      gap: 14px;
    }}
    .story-hero {{
      position: relative;
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(260px, .62fr);
      gap: 14px;
      min-height: 580px;
      align-items: end;
      border: 1px solid var(--line);
      background:
        linear-gradient(145deg, rgba(255,255,255,.07), rgba(255,255,255,.018)),
        radial-gradient(circle at 76% 18%, rgba(94,234,212,.18), transparent 34%),
        rgba(13,17,20,.7);
      border-radius: 8px;
      padding: clamp(18px, 4vw, 34px);
      overflow: hidden;
      backdrop-filter: blur(26px) saturate(130%);
    }}
    .story-hero::after {{
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background: linear-gradient(90deg, transparent, rgba(255,255,255,.04), transparent);
      transform: translateX(-100%);
      animation: sheen 8s ease-in-out infinite;
    }}
    @keyframes sheen {{
      0%, 58% {{ transform: translateX(-100%); }}
      82%, 100% {{ transform: translateX(100%); }}
    }}
    .hero-copy {{
      position: relative;
      z-index: 1;
      display: grid;
      gap: 18px;
    }}
    .hero-copy h1 {{
      max-width: 820px;
      font-size: clamp(54px, 10vw, 118px);
      line-height: .88;
    }}
    .hero-copy p {{
      max-width: 720px;
      margin: 0;
      color: var(--muted);
      font-size: clamp(17px, 2.3vw, 24px);
      line-height: 1.38;
    }}
    .hero-proof {{
      position: relative;
      z-index: 1;
      display: grid;
      gap: 9px;
    }}
    .moment {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,.06);
      border-radius: 8px;
      padding: 13px;
    }}
    .moment span {{
      display: block;
      color: var(--soft);
      font-size: 11px;
      font-weight: 900;
      text-transform: uppercase;
      margin-bottom: 7px;
    }}
    .moment strong {{
      display: block;
      font-size: 16px;
      line-height: 1.28;
    }}
    .swipe-section {{
      border-top: 1px solid var(--line);
      padding: 24px 0 30px;
    }}
    .swipe-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: end;
      margin-bottom: 12px;
    }}
    .swipe-head h2 {{
      margin: 0;
      font-size: clamp(28px, 6vw, 48px);
      line-height: 1;
      max-width: 780px;
    }}
    .swipe-head p {{
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.45;
      max-width: 760px;
    }}
    .swipe-hint {{
      flex: 0 0 auto;
      color: var(--mint);
      border: 1px solid rgba(94,234,212,.34);
      background: rgba(94,234,212,.08);
      border-radius: 999px;
      padding: 8px 10px;
      font-size: 11px;
      font-weight: 900;
      text-transform: uppercase;
      white-space: nowrap;
    }}
    .swipe-deck {{
      display: grid;
      grid-auto-flow: column;
      grid-auto-columns: minmax(280px, 78%);
      gap: 10px;
      overflow-x: auto;
      padding: 2px 2px 16px;
      scroll-snap-type: x mandatory;
      -webkit-overflow-scrolling: touch;
      scrollbar-color: rgba(255,255,255,.28) transparent;
    }}
    .swipe-card {{
      min-height: 430px;
      border: 1px solid var(--line);
      background:
        linear-gradient(155deg, rgba(255,255,255,.10), rgba(255,255,255,.035)),
        var(--glass);
      border-radius: 8px;
      padding: 16px;
      scroll-snap-align: start;
      display: grid;
      grid-template-rows: auto auto 1fr auto;
      gap: 12px;
      backdrop-filter: blur(22px);
    }}
    .swipe-card .kicker {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      color: var(--mint);
      font-size: 11px;
      font-weight: 900;
      text-transform: uppercase;
    }}
    .swipe-card .kicker b {{
      display: grid;
      place-items: center;
      min-width: 30px;
      height: 30px;
      border-radius: 50%;
      background: var(--green);
      color: #07100d;
      font-size: 12px;
    }}
    .swipe-card h3 {{
      margin: 0;
      font-size: clamp(24px, 6vw, 38px);
      line-height: 1;
    }}
    .swipe-card p {{
      margin: 0;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.5;
    }}
    .swipe-proof {{
      align-self: end;
      display: grid;
      gap: 8px;
    }}
    .swipe-proof div {{
      border: 1px solid var(--line);
      background: rgba(0,0,0,.18);
      border-radius: 8px;
      padding: 10px;
    }}
    .swipe-proof span {{
      display: block;
      color: var(--soft);
      font-size: 10px;
      font-weight: 900;
      text-transform: uppercase;
      margin-bottom: 5px;
    }}
    .swipe-proof strong {{
      display: block;
      font-size: 13px;
      line-height: 1.3;
      overflow-wrap: anywhere;
    }}
    .swipe-dots {{
      display: flex;
      gap: 6px;
      margin-top: 2px;
    }}
    .swipe-dots i {{
      width: 22px;
      height: 3px;
      border-radius: 999px;
      background: rgba(255,255,255,.24);
    }}
    .swipe-dots i.on {{
      background: var(--green);
    }}
    .chapter {{
      border-top: 1px solid var(--line);
      padding: 34px 0;
    }}
    .chapter-head {{
      display: grid;
      gap: 8px;
      margin-bottom: 14px;
      max-width: 840px;
    }}
    .chapter-label {{
      color: var(--mint);
      font-size: 12px;
      font-weight: 900;
      text-transform: uppercase;
    }}
    .chapter h2 {{
      margin: 0;
      font-size: clamp(28px, 6vw, 56px);
      line-height: 1;
    }}
    .chapter-head p {{
      margin: 0;
      color: var(--muted);
      font-size: 15px;
      line-height: 1.52;
    }}
    .first-layer {{
      display: grid;
      grid-template-columns: minmax(0, .9fr) minmax(0, 1.1fr);
      gap: 10px;
    }}
    .run-recipe, .workbench-panel, .brew-panel, .output-panel {{
      border: 1px solid var(--line);
      background: var(--glass);
      border-radius: 8px;
      padding: 14px;
      backdrop-filter: blur(22px);
    }}
    .run-recipe {{
      display: grid;
      gap: 10px;
      align-content: start;
    }}
    .recipe-step {{
      display: grid;
      grid-template-columns: 34px 1fr;
      gap: 10px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.045);
      border-radius: 8px;
      padding: 11px;
    }}
    .recipe-step i {{
      width: 28px;
      height: 28px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      background: var(--green);
      color: #07100d;
      font-style: normal;
      font-weight: 900;
      font-size: 12px;
    }}
    .recipe-step h3, .workbench-panel h3, .brew-panel h3, .output-panel h3 {{
      margin: 0 0 5px;
      font-size: 16px;
      line-height: 1.2;
    }}
    .recipe-step p, .workbench-panel p, .brew-panel p, .output-panel p {{
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }}
    .workbench-grid {{
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 8px;
      margin-top: 10px;
    }}
    .workbench-grid div {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,.052);
      border-radius: 8px;
      padding: 10px;
    }}
    .workbench-grid span, .evidence-strip span, .brew-panel span, .output-panel span {{
      display: block;
      color: var(--soft);
      font-size: 10px;
      font-weight: 900;
      text-transform: uppercase;
      margin-bottom: 6px;
    }}
    .workbench-grid strong {{
      display: block;
      font-size: 13px;
      line-height: 1.25;
    }}
        .prompt-slice {{
          margin-top: 10px;
          max-height: 260px;
      overflow: auto;
      border: 1px solid var(--line);
      background: rgba(0,0,0,.18);
      border-radius: 8px;
      padding: 11px;
      white-space: pre-wrap;
      word-break: break-word;
      color: #d9e1ea;
      font-family: var(--mono);
          font-size: 11px;
          line-height: 1.45;
        }}
        .contract-grid {{
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: 8px;
          margin-top: 10px;
        }}
        .contract-grid div {{
          border: 1px solid var(--line);
          background: rgba(255,255,255,.052);
          border-radius: 8px;
          padding: 10px;
        }}
        .contract-grid span {{
          display: block;
          color: var(--soft);
          font-size: 10px;
          font-weight: 900;
          text-transform: uppercase;
          margin-bottom: 6px;
        }}
        .contract-grid strong {{
          display: block;
          font-size: 13px;
          line-height: 1.25;
        }}
        .contract-list {{
          margin-top: 10px;
          border-top: 1px solid var(--line);
          padding-top: 10px;
        }}
        .contract-list h4 {{
          margin: 0 0 8px;
          font-size: 12px;
          text-transform: uppercase;
          color: var(--mint);
        }}
        .contract-list ul {{
          list-style: none;
          display: grid;
          gap: 7px;
          margin: 0;
          padding: 0;
        }}
        .contract-list li {{
          border: 1px solid var(--line);
          background: rgba(0,0,0,.16);
          border-radius: 8px;
          padding: 9px;
        }}
        .contract-list li strong {{
          display: block;
          font-size: 12px;
          line-height: 1.25;
        }}
        .contract-list li span {{
          display: block;
          margin-top: 4px;
          color: var(--muted);
          font-size: 11px;
          line-height: 1.35;
          text-transform: none;
        }}
        .evidence-strip {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 10px;
    }}
    .evidence-card {{
      min-height: 174px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.05);
      border-radius: 8px;
      padding: 13px;
    }}
    .evidence-card h3 {{
      margin: 0 0 8px;
      font-size: 16px;
      line-height: 1.18;
    }}
    .evidence-card p {{
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.42;
    }}
    .brew-grid {{
      display: grid;
      gap: 9px;
    }}
    .brew-row {{
      display: grid;
      grid-template-columns: minmax(0, .95fr) 46px minmax(0, 1.05fr);
      gap: 8px;
      align-items: stretch;
    }}
    .brew-arrow {{
      display: grid;
      place-items: center;
      color: #07100d;
      background: var(--green);
      border-radius: 8px;
      font-weight: 900;
      min-height: 62px;
    }}
    .score-tape {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 8px;
      margin-top: 10px;
    }}
    .score-tape div {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,.052);
      border-radius: 8px;
      padding: 10px;
    }}
    .score-tape strong {{
      display: block;
      font-size: 24px;
      line-height: 1;
    }}
    .score-tape small {{
      display: block;
      color: var(--muted);
      font-size: 11px;
      margin-top: 6px;
    }}
    .trace-board {{
      display: grid;
      grid-template-columns: minmax(0, .95fr) minmax(0, 1.05fr);
      gap: 10px;
      align-items: start;
    }}
    .trace-spine {{
      display: grid;
      gap: 9px;
    }}
    .trace-step {{
      display: grid;
      grid-template-columns: 38px 1fr;
      gap: 10px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.052);
      border-radius: 8px;
      padding: 11px;
    }}
    .trace-step .num {{
      width: 30px;
      height: 30px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      background: var(--green);
      color: #07100d;
      font-size: 12px;
      font-weight: 900;
    }}
    .trace-step h3 {{
      margin: 0 0 5px;
      font-size: 15px;
      line-height: 1.18;
    }}
    .trace-step p {{
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.42;
    }}
    .trace-step code {{
      display: inline-block;
      max-width: 100%;
      overflow-wrap: anywhere;
      margin-top: 7px;
      color: var(--ink);
      background: rgba(255,255,255,.06);
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 5px 7px;
      font-family: var(--mono);
      font-size: 10px;
      line-height: 1.3;
    }}
    .trace-meta {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
      margin-top: 10px;
    }}
    .trace-meta div {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,.052);
      border-radius: 8px;
      padding: 10px;
    }}
    .trace-meta span, .payload-row span {{
      display: block;
      color: var(--soft);
      font-size: 10px;
      font-weight: 900;
      text-transform: uppercase;
      margin-bottom: 5px;
    }}
    .trace-meta strong {{
      display: block;
      font-size: 12px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }}
    .payload-table {{
      display: grid;
      gap: 7px;
      margin-top: 10px;
    }}
    .payload-row {{
      display: grid;
      grid-template-columns: minmax(92px, .34fr) minmax(0, 1fr);
      gap: 8px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.045);
      border-radius: 8px;
      padding: 10px;
    }}
    .payload-row strong {{
      display: block;
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .id-strip {{
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
      margin-top: 10px;
    }}
    .id-strip code {{
      color: var(--ink);
      background: rgba(255,255,255,.06);
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 7px 9px;
      font-family: var(--mono);
      font-size: 10px;
      overflow-wrap: anywhere;
    }}
    .persist-grid {{
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 10px;
      margin-top: 10px;
    }}
    .persist-card {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,.052);
      border-radius: 8px;
      padding: 12px;
    }}
    .persist-card span {{
      display: block;
      color: var(--soft);
      font-size: 10px;
      font-weight: 900;
      text-transform: uppercase;
      margin-bottom: 6px;
    }}
    .persist-card h3 {{
      margin: 0 0 6px;
      font-size: 14px;
      line-height: 1.22;
      overflow-wrap: anywhere;
    }}
    .persist-card p {{
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.42;
    }}
    .geometry-plane {{
      position: relative;
      min-height: 360px;
      margin-top: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      background:
        linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px),
        linear-gradient(rgba(255,255,255,.035) 1px, transparent 1px),
        radial-gradient(circle at 82% 20%, rgba(110,231,183,.14), transparent 34%),
        rgba(0,0,0,.15);
      background-size: 48px 48px, 48px 48px, auto, auto;
    }}
    .geo-axis {{
      position: absolute;
      z-index: 1;
      color: var(--soft);
      font-size: 10px;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: 0;
    }}
    .geo-x {{ left: 12px; right: 12px; bottom: 9px; text-align: right; }}
    .geo-y {{ left: 10px; top: 10px; writing-mode: vertical-rl; transform: rotate(180deg); }}
    .geo-cross {{
      position: absolute;
      inset: 12% 8% 12% 8%;
      border-left: 1px solid rgba(255,255,255,.16);
      border-bottom: 1px solid rgba(255,255,255,.16);
    }}
    .geo-dot {{
      position: absolute;
      z-index: 2;
      display: grid;
      place-items: center;
      transform: translate(-50%, 50%);
      border-radius: 50%;
      border: 1px solid rgba(110,231,183,.74);
      background: rgba(110,231,183,.18);
      box-shadow: 0 0 34px rgba(94,234,212,.16);
    }}
    .geo-dot.verify_now {{ border-color: rgba(246,196,83,.92); background: rgba(246,196,83,.18); }}
    .geo-dot.repair_sources {{ border-color: rgba(255,154,167,.85); background: rgba(255,154,167,.15); }}
    .geo-dot b {{
      font-size: 11px;
      line-height: 1;
    }}
    .geo-dot span {{
      position: absolute;
      left: 50%;
      top: calc(100% + 6px);
      width: max-content;
      max-width: 120px;
      transform: translateX(-50%);
      color: var(--muted);
      font-size: 10px;
      font-weight: 800;
      text-align: center;
      line-height: 1.15;
      text-transform: uppercase;
    }}
    .output-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 10px;
    }}
    .next-flow {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 10px;
    }}
    .next-flow article {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,.05);
      border-radius: 8px;
      padding: 13px;
    }}
    .next-flow h3 {{
      margin: 0 0 7px;
      font-size: 15px;
    }}
    .next-flow p {{
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.42;
    }}
    .hero {{
      display: grid;
      grid-template-columns: minmax(0, 1.05fr) minmax(300px, .95fr);
      gap: 12px;
      align-items: stretch;
      margin: 8px 0 14px;
    }}
    .glass {{
      border: 1px solid var(--line);
      background: var(--glass);
      border-radius: 8px;
      box-shadow: 0 30px 90px rgba(0,0,0,.38);
      backdrop-filter: blur(26px) saturate(130%);
    }}
    .answer {{
      padding: 18px;
      display: grid;
      gap: 14px;
      overflow: hidden;
    }}
    .answer-label {{
      color: var(--mint);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
    }}
    .answer h2 {{
      margin: 0;
      font-size: clamp(27px, 7vw, 58px);
      line-height: .98;
      letter-spacing: 0;
      overflow-wrap: anywhere;
    }}
    .answer p {{
      margin: 0;
      color: var(--muted);
      font-size: 15px;
      line-height: 1.48;
    }}
    .decision-row {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
    }}
    .decision {{
      background: rgba(255,255,255,.055);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 11px;
    }}
    .decision span {{
      display: block;
      color: var(--soft);
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
      margin-bottom: 7px;
    }}
    .decision strong {{
      display: block;
      font-size: 15px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }}
    .map {{
      position: relative;
      min-height: 440px;
      overflow: hidden;
      padding: 14px;
    }}
    .map canvas {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
    }}
    .map-title {{
      position: relative;
      z-index: 2;
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
    }}
    .node {{
      position: absolute;
      z-index: 3;
      width: 132px;
      min-height: 78px;
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      padding: 10px;
      background: rgba(9, 11, 14, .66);
      backdrop-filter: blur(20px);
      box-shadow: 0 18px 36px rgba(0,0,0,.22);
    }}
    .node b {{ display: block; font-size: 13px; margin-bottom: 5px; }}
    .node small {{ color: var(--muted); font-size: 11px; line-height: 1.25; display: block; overflow-wrap: anywhere; }}
    .n1 {{ left: 8%; top: 22%; }}
    .n2 {{ right: 8%; top: 21%; }}
    .n3 {{ left: 50%; top: 45%; transform: translateX(-50%); border-color: rgba(110,231,183,.55); }}
    .n4 {{ left: 8%; bottom: 10%; }}
    .n5 {{ right: 8%; bottom: 10%; }}
    .score-ring {{
      position: relative;
      width: 126px;
      height: 126px;
      display: grid;
      place-items: center;
      border-radius: 50%;
      background: conic-gradient(var(--green) calc(var(--score) * 1%), rgba(255,255,255,.1) 0);
      margin: 6px 0;
    }}
    .score-ring::after {{
      content: "";
      position: absolute;
      inset: 9px;
      border-radius: 50%;
      background: #111418;
    }}
    .score-ring strong {{
      position: relative;
      z-index: 1;
      font-size: 28px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(6, 1fr);
      gap: 10px;
      margin: 18px 0;
    }}
    .metric {{
      min-height: 112px;
      border: 1px solid var(--line);
      background: var(--glass);
      border-radius: 8px;
      padding: 14px;
      display: grid;
      align-content: space-between;
      backdrop-filter: blur(22px);
    }}
    .metric span, .metric small {{ color: var(--muted); }}
    .metric span {{ font-size: 12px; font-weight: 700; text-transform: uppercase; }}
    .metric strong {{ font-size: 30px; line-height: 1; }}
    .metric small {{ font-size: 12px; line-height: 1.35; }}
    .section {{
      border-top: 1px solid var(--line);
      padding: 24px 0;
    }}
    h2 {{
      margin: 0 0 12px;
      font-size: 22px;
      letter-spacing: 0;
    }}
    .section-kicker {{
      color: var(--soft);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      margin: 0 0 8px;
    }}
    .section-copy {{
      margin: -4px 0 14px;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.5;
      max-width: 780px;
    }}
    .scout-desk {{
      display: grid;
      grid-template-columns: .9fr 1.1fr;
      gap: 10px;
    }}
    .scout-stack {{
      display: grid;
      gap: 10px;
    }}
    .scout-card, .prompt-window, .logic-card {{
      border: 1px solid var(--line);
      background: var(--glass);
      border-radius: 8px;
      padding: 14px;
      backdrop-filter: blur(24px);
    }}
    .scout-card span, .prompt-window span, .logic-card span {{
      display: block;
      color: var(--soft);
      font-size: 11px;
      font-weight: 900;
      text-transform: uppercase;
      margin-bottom: 8px;
    }}
    .scout-card h3, .logic-card h3 {{
      margin: 0 0 8px;
      font-size: 18px;
      line-height: 1.18;
    }}
    .scout-card p, .logic-card p {{
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }}
    .cell-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }}
    .cell-grid div {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,.052);
      border-radius: 8px;
      padding: 9px;
    }}
    .cell-grid b {{
      display: block;
      font-size: 13px;
      line-height: 1.25;
    }}
    .tool-list {{
      display: grid;
      gap: 7px;
    }}
    .tool-list code {{
      display: block;
      white-space: normal;
      overflow-wrap: anywhere;
      color: var(--ink);
      background: rgba(255,255,255,.06);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px;
      font-family: var(--mono);
      font-size: 11px;
      line-height: 1.35;
    }}
    .prompt-window {{
      padding: 0;
      overflow: hidden;
    }}
    .prompt-top {{
      display: flex;
      justify-content: space-between;
      gap: 8px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: rgba(255,255,255,.04);
    }}
    .prompt-top b {{ font-size: 14px; }}
    .prompt-body {{
      margin: 0;
      max-height: 420px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      padding: 14px;
      font-family: var(--mono);
      color: #d9e1ea;
      font-size: 11px;
      line-height: 1.45;
    }}
    .logic-board {{
      display: grid;
      gap: 10px;
    }}
    .logic-row {{
      display: grid;
      grid-template-columns: minmax(0, .95fr) 48px minmax(0, 1.05fr);
      gap: 10px;
      align-items: stretch;
    }}
    .logic-operator {{
      display: grid;
      place-items: center;
      color: #06100d;
      background: var(--green);
      border-radius: 8px;
      font-weight: 900;
      font-size: 13px;
      min-height: 64px;
    }}
    .score-row {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 8px;
    }}
    .score-box {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,.052);
      border-radius: 8px;
      padding: 10px;
    }}
    .score-box strong {{
      display: block;
      font-size: 22px;
      line-height: 1;
    }}
    .score-box small {{
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 11px;
    }}
    .source-grid {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 10px;
    }}
    .source-card {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,.055);
      border-radius: 8px;
      padding: 13px;
      min-height: 168px;
      display: grid;
      gap: 9px;
      align-content: start;
    }}
    .source-card .source-type {{
      color: var(--mint);
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
    }}
    .source-card h3 {{ margin: 0; font-size: 15px; line-height: 1.2; }}
    .source-card p {{ margin: 0; color: var(--muted); font-size: 12px; line-height: 1.42; }}
    .story-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }}
    .system-board {{
      border: 1px solid var(--line);
      background: var(--glass);
      border-radius: 8px;
      padding: 14px;
      overflow: hidden;
      backdrop-filter: blur(24px);
    }}
    .system-strip {{
      display: grid;
      grid-template-columns: repeat(9, minmax(96px, 1fr));
      gap: 8px;
      align-items: stretch;
      overflow-x: auto;
      padding-bottom: 4px;
      -webkit-overflow-scrolling: touch;
    }}
    .system-step {{
      position: relative;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.055);
      border-radius: 8px;
      padding: 11px;
      min-height: 128px;
    }}
    .system-step::after {{
      content: "";
      position: absolute;
      right: -7px;
      top: 50%;
      width: 7px;
      height: 1px;
      background: rgba(94,234,212,.55);
    }}
    .system-step:last-child::after {{ display: none; }}
    .system-step.active {{
      border-color: rgba(110,231,183,.58);
      box-shadow: inset 0 0 0 1px rgba(110,231,183,.14);
    }}
    .system-step .num {{
      color: var(--mint);
      font-size: 11px;
      font-weight: 900;
      margin-bottom: 8px;
    }}
    .system-step h3 {{ margin: 0 0 8px; font-size: 14px; line-height: 1.15; }}
    .system-step p {{ margin: 0; color: var(--muted); font-size: 12px; line-height: 1.36; }}
    .part-grid {{
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 10px;
      margin-top: 12px;
    }}
    .part-card {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,.05);
      border-radius: 8px;
      padding: 14px;
      display: grid;
      gap: 10px;
    }}
    .part-card h3 {{ margin: 0; font-size: 17px; }}
    .part-card p {{ margin: 0; color: var(--muted); font-size: 13px; line-height: 1.45; }}
    .part-meta {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }}
    .part-meta div {{
      border: 1px solid var(--line);
      background: rgba(0,0,0,.16);
      border-radius: 8px;
      padding: 9px;
    }}
    .part-meta span {{
      color: var(--soft);
      display: block;
      font-size: 10px;
      font-weight: 900;
      text-transform: uppercase;
      margin-bottom: 5px;
    }}
    .part-meta strong {{
      display: block;
      font-size: 12px;
      line-height: 1.3;
    }}
    .run-chip {{
      width: fit-content;
      max-width: 100%;
      color: var(--mint);
      border: 1px solid rgba(94,234,212,.34);
      background: rgba(94,234,212,.09);
      border-radius: 999px;
      padding: 6px 9px;
      font-size: 11px;
      font-weight: 800;
    }}
    .journey {{
      display: grid;
      gap: 10px;
    }}
    .journey-card {{
      display: grid;
      grid-template-columns: 44px 1fr;
      gap: 12px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.052);
      border-radius: 8px;
      padding: 13px;
    }}
    .journey-card .mark {{
      width: 34px;
      height: 34px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      color: #06100d;
      background: var(--green);
      font-weight: 900;
      font-size: 12px;
    }}
    .journey-card h3 {{
      margin: 0 0 6px;
      font-size: 16px;
      line-height: 1.2;
    }}
    .journey-card p {{
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }}
    .journey-card em {{
      display: block;
      margin-top: 8px;
      color: var(--ink);
      font-style: normal;
      font-size: 13px;
      line-height: 1.35;
    }}
    .story-card {{
      border: 1px solid var(--line);
      background: var(--glass);
      border-radius: 8px;
      padding: 15px;
    }}
    .story-card span {{
      display: block;
      color: var(--soft);
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
      margin-bottom: 8px;
    }}
    .story-card p {{ margin: 0; color: var(--ink); font-size: 15px; line-height: 1.45; }}
    .chain {{
      display: flex;
      gap: 7px;
      flex-wrap: wrap;
    }}
    .chain span {{
      color: var(--ink);
      background: rgba(255,255,255,.08);
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 8px 10px;
      font-size: 12px;
      font-weight: 700;
    }}
    .timeline {{
      display: grid;
      gap: 8px;
    }}
    .step {{
      display: grid;
      grid-template-columns: 28px 1fr;
      gap: 10px;
      align-items: start;
    }}
    .step i {{
      width: 28px;
      height: 28px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      color: #07100d;
      background: var(--green);
      font-style: normal;
      font-size: 12px;
      font-weight: 900;
    }}
    .step p {{ margin: 2px 0 0; color: var(--muted); font-size: 13px; line-height: 1.4; }}
    .inspection {{
      border: 1px solid var(--line);
      background: var(--glass);
      border-radius: 8px;
      overflow: hidden;
    }}
    .inspection summary {{
      cursor: pointer;
      padding: 16px;
      font-weight: 800;
      list-style: none;
    }}
    .inspection summary::-webkit-details-marker {{ display: none; }}
    .tabs {{
      display: flex;
      overflow-x: auto;
      gap: 8px;
      padding: 0 14px 12px;
      -webkit-overflow-scrolling: touch;
    }}
    .panel {{
      display: none;
      border-top: 1px solid var(--line);
    }}
    .panel.active {{ display: block; }}
    .panel-head {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      border-bottom: 1px solid var(--line);
      padding: 12px 14px;
      background: rgba(255,255,255,.04);
    }}
    .panel-head h3 {{ margin: 0; font-size: 15px; }}
    .panel-head small {{ color: var(--muted); }}
    pre {{
      margin: 0;
      padding: 14px;
      overflow: auto;
      max-height: 72vh;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: var(--mono);
      font-size: 12px;
      line-height: 1.45;
      color: #d7dee8;
    }}
    .layers {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 10px;
    }}
    .layer {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,.055);
      border-radius: 8px;
      padding: 12px;
    }}
    .layer h3 {{ margin: 0 0 8px; font-size: 14px; }}
    .layer .pass {{ color: var(--green); }}
    .layer .fail {{ color: var(--red); }}
    .layer p {{ margin: 0; color: var(--muted); font-size: 12px; line-height: 1.4; }}
    .reference {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }}
    .note {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,.055);
      border-radius: 8px;
      padding: 14px;
      color: var(--muted);
      line-height: 1.5;
    }}
    .note strong {{ color: var(--text); }}
    .footer {{
      color: var(--soft);
      font-size: 12px;
      padding: 18px 0;
    }}
    @media (max-width: 860px) {{
      .shell {{ padding-left: 12px; padding-right: 12px; }}
      .swipe-head {{ display: grid; }}
      .swipe-deck {{ grid-auto-columns: minmax(286px, 88%); }}
      .swipe-card {{ min-height: 470px; }}
      .concierge {{ grid-template-columns: 1fr; }}
      .hero {{ grid-template-columns: 1fr; }}
      .toolbar {{ display: grid; grid-template-columns: 1fr; align-items: stretch; }}
      .pill {{ width: 100%; }}
      .grid {{ grid-template-columns: repeat(2, 1fr); }}
      .layers, .reference, .story-grid, .source-grid, .part-grid, .part-meta, .scout-desk, .cell-grid, .logic-row, .score-row {{ grid-template-columns: 1fr; }}
      .logic-operator {{ min-height: 34px; }}
      .system-strip {{ grid-template-columns: 1fr; overflow-x: visible; }}
      .system-step::after {{
        left: 24px;
        right: auto;
        top: auto;
        bottom: -9px;
        width: 1px;
        height: 9px;
      }}
      .decision-row {{ grid-template-columns: 1fr; }}
      .map {{ min-height: 360px; }}
      h1 {{ font-size: clamp(44px, 13.5vw, 58px); line-height: .92; }}
      .answer h2 {{ font-size: clamp(28px, 8.8vw, 36px); line-height: 1.02; }}
      .story-hero, .first-layer, .evidence-strip, .brew-row, .output-grid, .next-flow, .workbench-grid, .score-tape {{
        grid-template-columns: 1fr;
      }}
      .story-hero {{
        min-height: auto;
        padding: 18px;
        gap: 12px;
      }}
      .hero-copy {{ gap: 12px; }}
      .hero-copy h1 {{
        font-size: clamp(38px, 10.5vw, 50px);
        line-height: .96;
      }}
      .hero-copy p {{
        font-size: 15px;
        line-height: 1.42;
      }}
      .hero-proof {{
        gap: 8px;
      }}
      .moment {{
        padding: 10px;
      }}
      .moment strong {{
        font-size: 14px;
      }}
      .brew-arrow {{ min-height: 34px; }}
      .trace-board, .trace-meta, .persist-grid, .payload-row {{
        grid-template-columns: 1fr;
      }}
      .geometry-plane {{ min-height: 320px; }}
      .node {{ width: 118px; min-height: 70px; padding: 9px; }}
      .n1 {{ left: 3%; top: 24%; }}
      .n2 {{ right: 3%; top: 24%; }}
      .n3 {{ top: 47%; }}
      .n4 {{ left: 3%; bottom: 8%; }}
      .n5 {{ right: 3%; bottom: 8%; }}
      .metric {{ min-height: 98px; }}
      pre {{ max-height: 68vh; font-size: 11px; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    {cohesive_story_html}
  </main>

  <script id="run-data" type="application/json">{html.escape(blob)}</script>
  <script>
    const buttons = [...document.querySelectorAll('[data-tab]')];
    const panels = [...document.querySelectorAll('.panel')];
    buttons.forEach((button) => {{
      button.addEventListener('click', () => {{
        buttons.forEach((b) => b.classList.toggle('active', b === button));
        panels.forEach((p) => p.classList.toggle('active', p.dataset.panel === button.dataset.tab));
      }});
    }});
    document.querySelectorAll('[data-copy]').forEach((button) => {{
      button.addEventListener('click', async () => {{
        const pre = button.closest('.panel').querySelector('pre');
        await navigator.clipboard.writeText(pre.textContent);
        const old = button.textContent;
        button.textContent = 'Copied';
        setTimeout(() => button.textContent = old, 900);
      }});
    }});
    const canvas = document.getElementById('flowCanvas');
    if (canvas) {{
      const ctx = canvas.getContext('2d');
      function drawFlow() {{
        const rect = canvas.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.floor(rect.width * dpr);
        canvas.height = Math.floor(rect.height * dpr);
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, rect.width, rect.height);
        const nodes = [
          [rect.width * .22, rect.height * .34],
          [rect.width * .78, rect.height * .34],
          [rect.width * .50, rect.height * .55],
          [rect.width * .22, rect.height * .82],
          [rect.width * .78, rect.height * .82],
        ];
        const links = [[0,2],[1,2],[2,3],[2,4],[3,4]];
        const t = performance.now() / 1100;
        links.forEach(([a,b], idx) => {{
          const [x1,y1] = nodes[a];
          const [x2,y2] = nodes[b];
          const grad = ctx.createLinearGradient(x1,y1,x2,y2);
          grad.addColorStop(0, 'rgba(94,234,212,.15)');
          grad.addColorStop(.55, 'rgba(142,197,255,.65)');
          grad.addColorStop(1, 'rgba(246,196,83,.18)');
          ctx.strokeStyle = grad;
          ctx.lineWidth = 1.4;
          ctx.beginPath();
          ctx.moveTo(x1,y1);
          ctx.bezierCurveTo(x1, (y1+y2)/2, x2, (y1+y2)/2, x2, y2);
          ctx.stroke();
          const p = (Math.sin(t + idx * .75) + 1) / 2;
          const x = x1 + (x2 - x1) * p;
          const y = y1 + (y2 - y1) * p;
          ctx.fillStyle = 'rgba(110,231,183,.92)';
          ctx.beginPath();
          ctx.arc(x, y, 3.4, 0, Math.PI * 2);
          ctx.fill();
        }});
        nodes.forEach(([x,y], idx) => {{
          const r = idx === 2 ? 16 : 11;
          ctx.fillStyle = idx === 2 ? 'rgba(110,231,183,.15)' : 'rgba(255,255,255,.08)';
          ctx.strokeStyle = idx === 2 ? 'rgba(110,231,183,.78)' : 'rgba(255,255,255,.22)';
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.arc(x, y, r, 0, Math.PI * 2);
          ctx.fill();
          ctx.stroke();
        }});
        requestAnimationFrame(drawFlow);
      }}
      drawFlow();
      window.addEventListener('resize', drawFlow);
    }}
  </script>
</body>
</html>
"""


def _story(data: dict[str, Any]) -> dict[str, Any]:
    model = _artifact_json(data, "live_scout_model_output")
    persisted = _artifact_json(data, "live_scout_persisted_output")
    strings = model.get("information_strings") or persisted.get("information_strings") or []
    first = strings[0] if strings and isinstance(strings[0], dict) else {}
    confidence = float(model.get("confidence") or persisted.get("confidence") or 0.0)
    prompt_variant = str(persisted.get("prompt_variant") or "?").replace("_", " ")
    return {
        "headline": str(model.get("hypothesis") or persisted.get("hypothesis_text") or "Scout produced a persisted intelligence string."),
        "thesis": str(first.get("thesis") or model.get("hypothesis") or ""),
        "mechanism": str(first.get("mechanism") or model.get("rationale_brief") or persisted.get("rationale_brief") or ""),
        "expected": str(first.get("expected_outcome") or "Verifier should inspect the routed evidence."),
        "kill_signal": str(first.get("kill_signal") or "No kill signal emitted."),
        "entities": [str(x) for x in first.get("entities_chain") or [persisted.get("entity") or "HYPE"]],
        "depth_layers": first.get("depth_layers") or [],
        "confidence": confidence,
        "confidence_label": f"{round(confidence * 100)}% model confidence",
        "novelty": _pct(first.get("novelty_score")),
        "crowdedness": _pct(first.get("crowdedness")),
        "conviction": _pct(first.get("conviction")),
        "prompt_variant": prompt_variant,
    }


def _render_cohesive_story(
    *,
    data: dict[str, Any],
    story: dict[str, Any],
    summary: dict[str, Any],
    layers: list[dict[str, Any]],
    source_html: str,
    raw_panels: str,
) -> str:
    prompt_text = str(((data.get("artifacts") or {}).get("live_scout_user_prompt") or {}).get("text") or "")
    prompt = _parse_user_prompt(prompt_text)
    evidence = [x for x in _artifact_list(data, "live_scout_tool_evidence") if isinstance(x, dict)]
    facts = _conclusion_facts(evidence)
    model = _artifact_json(data, "live_scout_model_output")
    persisted = _artifact_json(data, "live_scout_persisted_output")
    geometry = _artifact_json(data, "alpha_geometry")
    system_trace = _artifact_json(data, "system_trace")
    top_cell = geometry.get("top_cell") if isinstance(geometry.get("top_cell"), dict) else {}
    route = str(top_cell.get("route_directive") or "observe").replace("_", " ")
    strings = model.get("information_strings") or persisted.get("information_strings") or []
    first_string = strings[0] if strings and isinstance(strings[0], dict) else {}
    geometry_evolution_html = _render_geometry_evolution_chapter(data)
    layer_pass = sum(1 for row in layers if row.get("status") == "pass")
    layer_total = len(layers)
    prompt_excerpt = _prompt_excerpt(prompt_text)
    tool_html = "".join(f"<code>{html.escape(str(tool))}</code>" for tool in prompt.get("allowed_tools") or [])
    data_substrate = summarize_data_substrate(
        evidence,
        allowed_tools=prompt.get("allowed_tools") or [],
        entity=str(prompt.get("entity") or ""),
        horizon=str(prompt.get("horizon") or ""),
        lens=str(prompt.get("lens") or ""),
    )
    neural_architecture_html = _render_neural_architecture_chapter(
        data=data,
        trace=system_trace,
        prompt=prompt,
        evidence=evidence,
        layers=layers,
        data_substrate=data_substrate,
        story=story,
    )
    market_map_generation_html = _render_market_map_generation_chapter(
        data=data,
        trace=system_trace,
        prompt=prompt,
    )
    governor_html = _render_market_map_governor_chapter(data=data)
    self_healing_html = _render_self_healing_chapter(data=data)
    touched_surfaces = [row for row in data_substrate.touched if row.touched]
    untouched_surfaces = [row for row in data_substrate.touched if not row.touched]
    data_surface_html = "".join(
        '<article class="output-panel">'
        f'<span>{"Touched" if row.touched else html.escape(row.surface.status.replace("_", " "))}</span>'
        f'<h3>{html.escape(row.surface.title)}</h3>'
        f'<p>{html.escape(row.surface.role)}. {html.escape(row.surface.question)}</p>'
        + (f'<p><strong>Receipts:</strong> {html.escape(", ".join(row.receipt_ids))}</p>' if row.receipt_ids else "")
        + '</article>'
        for row in [*touched_surfaces, *untouched_surfaces[:4]]
    )
    expansion_html = "".join(
        '<article class="output-panel">'
        f'<span>{html.escape(exp.priority)} priority</span>'
        f'<h3>{html.escape(exp.title)}</h3>'
        f'<p>{html.escape(exp.why)}</p>'
        f'<p><strong>Edge:</strong> {html.escape(exp.expected_edge)}</p>'
        f'<div class="tool-list">{"".join(f"<code>{html.escape(tool)}</code>" for tool in exp.suggested_tools)}</div>'
        '</article>'
        for exp in data_substrate.expansions[:4]
    )
    edge_html = "".join(
        f'<div><strong>{html.escape(edge)}</strong><small>formed by this run</small></div>'
        for edge in data_substrate.connection_edges
    ) or '<div><strong>No typed edges yet</strong><small>needs more evidence</small></div>'
    design_field_html = "".join(
        f"<div><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>"
        for label, value in [
            ("Entity", "the object being sensed"),
            ("Horizon", "the expiry window"),
            ("Lens", "the allowed data universe"),
            ("Bias", "the scout posture"),
            ("Theme", "the causal pattern"),
            ("Clock", "the freshness boundary"),
        ]
    )
    cell_html = "".join(
        f"<div><span>{html.escape(label)}</span><strong>{html.escape(str(value))}</strong></div>"
        for label, value in [
            ("Entity", prompt.get("entity") or "HYPE"),
            ("Horizon", prompt.get("horizon") or "intraday"),
            ("Lens", prompt.get("lens") or "on_chain"),
            ("Bias", prompt.get("bias_mode") or "frontier"),
            ("Theme", prompt.get("theme") or "validator_unstake"),
            ("Clock", prompt.get("as_of_utc") or "?"),
        ]
    )
    evidence_cards = "".join(
        '<article class="evidence-card">'
        f'<span>{html.escape(str(item.get("tool_call_log_id") or "evidence"))}</span>'
        f'<h3>{html.escape(_source_title(str(item.get("uri") or "")))}</h3>'
        f'<p>{html.escape(_evidence_fact(item))}</p>'
        '</article>'
        for item in evidence[:4]
    )
    contract_rows = [
        (
            "Narrow receptive field",
            "The scout gets one cell, not the whole market.",
            f"{prompt.get('entity') or 'HYPE'} / {prompt.get('horizon') or 'intraday'} / {prompt.get('lens') or 'on_chain'} / {prompt.get('theme') or 'validator_unstake'}",
        ),
        (
            "Receipts before opinion",
            "Tool output is attached before the model writes.",
            f"{len(evidence)} real evidence packets were included in the user prompt.",
        ),
        (
            "Budgeted atlas slice",
            "The scout can use the approved read-only atlas, then suggest only the relevant exposed URIs.",
            f"{len(prompt.get('allowed_tools') or [])} selected tool/source URIs were exposed.",
        ),
        (
            "Structured output",
            "The answer must become machine memory.",
            "hypothesis, rationale, suggested_tools, information_strings, evidence_refs, kill_signal",
        ),
    ]
    contract_html = "".join(
        '<article class="output-panel">'
        f'<span>{html.escape(kicker)}</span>'
        f'<h3>{html.escape(title)}</h3>'
        f'<p>{html.escape(detail)}</p>'
        '</article>'
        for kicker, title, detail in contract_rows
    )
    brew_rows = [
        (
            "The cell says what counts",
            f"{prompt.get('entity') or 'HYPE'} is the object. {prompt.get('horizon') or 'intraday'} is the window. {prompt.get('lens') or 'on_chain'} is the evidence lane.",
            "The scout is prevented from drifting into broad market commentary.",
        ),
        (
            "The receipts say what happened",
            facts.get("event", "An event feed packet enters the desk."),
            "This becomes only a candidate observation until the rest of the evidence can support or weaken it.",
        ),
        (
            "The market plumbing says whether it can matter",
            facts.get("market", "Market state gives depth and positioning context."),
            "The scout must connect source fact to liquidity, route, positioning, and horizon.",
        ),
        (
            "Node and actor quality say what to inspect next",
            " | ".join(x for x in [facts.get("hydro", ""), facts.get("node", "")] if x) or "Hydromancer and node evidence add actor and execution-quality context.",
            "The output becomes a verifier-ready conditional claim, not a final decision.",
        ),
    ]
    brew_html = "".join(
        '<div class="brew-row">'
        f'<article class="brew-panel"><span>Observed input</span><h3>{html.escape(title)}</h3><p>{html.escape(observed)}</p></article>'
        '<div class="brew-arrow">then</div>'
        f'<article class="brew-panel"><span>System transformation</span><p>{html.escape(inference)}</p></article>'
        '</div>'
        for title, observed, inference in brew_rows
    )
    score_html = "".join(
        f'<div><strong>{html.escape(value)}</strong><small>{html.escape(label)}</small></div>'
        for label, value in [
            ("confidence", story["confidence_label"].split(" ")[0]),
            ("conviction", story["conviction"]),
            ("novelty", story["novelty"]),
            ("crowdedness", story["crowdedness"]),
        ]
    )
    output_excerpt = json.dumps(
        {
            "hypothesis": model.get("hypothesis") or persisted.get("hypothesis_text"),
            "confidence": model.get("confidence") or persisted.get("confidence"),
            "rationale_brief": model.get("rationale_brief") or persisted.get("rationale_brief"),
            "first_information_string": {
                "title": first_string.get("title"),
                "thesis": first_string.get("thesis"),
                "mechanism": first_string.get("mechanism"),
                "expected_outcome": first_string.get("expected_outcome"),
                "kill_signal": first_string.get("kill_signal"),
                "evidence_refs": first_string.get("evidence_refs"),
            },
        },
        indent=2,
        ensure_ascii=True,
    )
    swipe_deck_html = _render_swipe_deck(
        prompt=prompt,
        evidence=evidence,
        persisted=persisted,
        data_substrate=data_substrate,
        layers=layers,
        story=story,
        first_string=first_string,
        data=data,
    )
    system_trace_html = _render_system_trace_chapter(
        data=data,
        prompt=prompt,
        evidence=evidence,
        persisted=persisted,
        data_substrate=data_substrate,
        layers=layers,
        story=story,
        first_string=first_string,
    )
    final_results_html = _render_final_results_chapter(
        data=data,
        prompt=prompt,
        evidence=evidence,
        persisted=persisted,
        data_substrate=data_substrate,
        layers=layers,
        story=story,
        first_string=first_string,
    )
    return f"""
      <div class="storyline">
        <section class="story-hero">
          <div class="hero-copy">
            <div class="eyebrow"><span class="dot"></span> Layer 1 run, with real inputs</div>
            <h1>One scout run, decoded.</h1>
            <p>Follow the actual data packet from market slice to tool receipts, model output, persistent map memory, geometry, and the next research instruction.</p>
          </div>
          <aside class="hero-proof">
            <div class="moment"><span>What this is</span><strong>A first-layer scout: one narrow research cell, not a finished trade call.</strong></div>
            <div class="moment"><span>What it saw</span><strong>A timestamped cell, selected atlas tools, four receipts, and prior map context.</strong></div>
            <div class="moment"><span>What happened</span><strong>{layer_pass}/{layer_total} layers passed. {len(touched_surfaces)}/{data_substrate.total_surfaces} data surfaces touched. Geometry said: {html.escape(route)}.</strong></div>
          </aside>
        </section>

        {neural_architecture_html}

        {market_map_generation_html}

        {governor_html}

        {self_healing_html}

        {swipe_deck_html}

        <section class="chapter">
          <div class="chapter-head">
            <div class="chapter-label">01 / why this layer exists</div>
            <h2>The market is too wide for one prompt.</h2>
            <p>Layer 1 solves that by running many narrow scouts. Each scout gets one receptive field, a small tool budget, and a strict output contract. The goal is not instant genius; the goal is high-quality coverage that compounds into a shared map.</p>
          </div>
          <div class="first-layer">
            <div class="run-recipe">
              <div class="recipe-step"><i>1</i><div><h3>Cut the market into cells</h3><p>Each scout receives one entity, horizon, lens, bias, theme, and clock.</p></div></div>
              <div class="recipe-step"><i>2</i><div><h3>Fetch evidence before reasoning</h3><p>The desk scores the full approved atlas, selects a tiny call budget, and attaches compact receipts.</p></div></div>
              <div class="recipe-step"><i>3</i><div><h3>Prompt against a contract</h3><p>The model must return JSON with thesis, mechanism, kill signal, scores, and refs.</p></div></div>
              <div class="recipe-step"><i>4</i><div><h3>Validate the response</h3><p>Weak strings, invented tools, missing refs, and uncalibrated scores are waste signals.</p></div></div>
              <div class="recipe-step"><i>5</i><div><h3>Route to memory</h3><p>Every useful finding enters the information map as evidence, strings, nodes, edges, and missing-edge prompts.</p></div></div>
            </div>
            <article class="workbench-panel">
              <h3>The reusable field of view</h3>
              <p>Every scout is framed by the same six fields. That makes thousands of cheap calls comparable, replayable, and measurable.</p>
              <div class="workbench-grid">{design_field_html}</div>
              <pre class="prompt-slice">Layer 1 is allowed to say: "inside this cell, these receipts imply this conditional string."
Layer 1 is not allowed to say: "I generally feel bullish or bearish."</pre>
            </article>
          </div>
        </section>

        <section class="chapter">
          <div class="chapter-head">
            <div class="chapter-label">02 / the real input packet</div>
            <h2>Here is exactly what this scout was handed.</h2>
            <p>The scout did not receive a vague request to be smart. It received a precise market cell, a clock, a scored subset of approved tools, and evidence receipts that can be audited later.</p>
          </div>
          <div class="first-layer">
            <article class="workbench-panel">
              <h3>Assigned cell</h3>
              <p>This is the scout's world for the call.</p>
              <div class="workbench-grid">{cell_html}</div>
            </article>
            <article class="workbench-panel">
              <h3>Selected atlas slice</h3>
              <p>The scout can draw from the approved read-only atlas. This call exposes the relevant subset and keeps spend under a hard cap.</p>
              <div class="tool-list">{tool_html}</div>
            </article>
          </div>
        </section>

        <section class="chapter">
          <div class="chapter-head">
            <div class="chapter-label">03 / the bigger data substrate</div>
            <h2>The scout sees a selected slice of a much larger estate.</h2>
            <p>This proof run touched only part of the data layer. That is intentional: the harness exposes the most relevant sources first, records what was touched, and names the important surfaces still missing. More data only helps when it closes a map edge.</p>
          </div>
          <div class="score-tape">
            <div><strong>{len(touched_surfaces)}/{data_substrate.total_surfaces}</strong><small>data surfaces touched by this run</small></div>
            <div><strong>{data_substrate.active_receipts}</strong><small>active receipt links</small></div>
            <div><strong>{len(data_substrate.connection_edges)}</strong><small>typed edges formed</small></div>
            <div><strong>{round(data_substrate.coverage_score * 100)}%</strong><small>substrate coverage score</small></div>
          </div>
          <div class="output-grid" style="margin-top:10px">{data_surface_html}</div>
        </section>

        <section class="chapter">
          <div class="chapter-head">
            <div class="chapter-label">04 / connection expansion</div>
            <h2>The next call is chosen by the missing connection.</h2>
            <p>The system should not spend calls just to feel busy. It should ask: which missing edge would change the map? Actor route, builder flow, source health, pending intent, order-flow expression. If the edge needs a tool we do not have, the gap becomes a tool proposal.</p>
          </div>
          <div class="score-tape">{edge_html}</div>
          <div class="output-grid" style="margin-top:10px">{expansion_html}</div>
        </section>

        <section class="chapter">
          <div class="chapter-head">
            <div class="chapter-label">05 / what the scout sees</div>
            <h2>The prompt is the model's cockpit.</h2>
            <p>The user prompt carries the cell, clock, evidence, prior strings, and allowed tools. The system prompt carries the contract. Together they make the model operate like an instrument instead of an open-ended chat.</p>
          </div>
          <div class="first-layer">
            <article class="workbench-panel">
              <h3>Real prompt excerpt</h3>
              <p>This is the scout's actual input surface: timestamp, cell, evidence, and permitted tools.</p>
              <pre class="prompt-slice">{html.escape(prompt_excerpt)}</pre>
            </article>
            <div class="output-grid">{contract_html}</div>
          </div>
        </section>

        <section class="chapter">
          <div class="chapter-head">
            <div class="chapter-label">06 / real receipts</div>
            <h2>The scout reasons from receipts, not vibes.</h2>
            <p>These are the four receipts in this run: an event-style feed, Hydromancer actor context, our node state, and market time series. The mix matters because node intelligence is strongest when actor quality, source state, and market plumbing meet.</p>
          </div>
          <div class="evidence-strip">{evidence_cards}</div>
        </section>

        <section class="chapter">
          <div class="chapter-head">
            <div class="chapter-label">07 / how it draws the conclusion</div>
            <h2>The conclusion is shown as an audit ladder.</h2>
            <p>We are not exposing hidden model thought. We are exposing the structure the system can audit: input fact, allowed transformation, emitted conditional string, and kill signal.</p>
          </div>
          <div class="brew-grid">{brew_html}</div>
        </section>

        <section class="chapter">
          <div class="chapter-head">
            <div class="chapter-label">08 / what came out</div>
            <h2>The output is a memory object, not a final answer.</h2>
            <p>The scout emits a conditional claim plus information strings. Those objects carry mechanism, expected outcome, kill signal, evidence refs, scores, and quality flags so the rest of Talis can compare, verify, expire, or promote them.</p>
          </div>
          <div class="output-grid">
            <article class="output-panel">
              <span>Emitted hypothesis</span>
              <h3>{html.escape(story["headline"])}</h3>
              <p>{html.escape(story["mechanism"])}</p>
            </article>
            <article class="output-panel">
              <span>Verifier handoff</span>
              <h3>{html.escape(story["expected"])}</h3>
              <p>Kill signal: {html.escape(story["kill_signal"])}</p>
            </article>
          </div>
          <div class="score-tape">{score_html}</div>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Actual structured fragment</h3>
            <p>This is the machine-readable surface the map can store and the next agents can inspect.</p>
            <pre class="prompt-slice">{html.escape(output_excerpt)}</pre>
          </article>
        </section>

        <section class="chapter">
          <div class="chapter-head">
            <div class="chapter-label">09 / how it fits the full system</div>
            <h2>The first layer becomes the routing substrate.</h2>
            <p>After Layer 1, the claim is no longer trapped in a transcript. Tool receipts, source refs, strings, coverage gaps, nodes, and edges land in the information map. Synthesis compares them; verifiers attack them; tool-builder agents repair missing capabilities.</p>
          </div>
          <div class="next-flow">
            <article><h3>Information map</h3><p>Stores strings with source refs, time windows, quality flags, entity chains, and map links.</p></article>
            <article><h3>Attention gate</h3><p>Finds confluences and contradictions before expensive verification spend.</p></article>
            <article><h3>Verifier agents</h3><p>Receive the hypothesis, evidence refs, expected outcome, and kill signal.</p></article>
            <article><h3>Tool builders</h3><p>When analysis is missing, agents propose, test, and promote better node-discovery tools.</p></article>
          </div>
        </section>

        {system_trace_html}

        {geometry_evolution_html}

        {final_results_html}

        <section class="chapter">
          <div class="chapter-head">
            <div class="chapter-label">13 / audit drawer</div>
            <h2>The beautiful layer stays traceable.</h2>
            <p>The phone view should feel calm and designed. Underneath, every claim still has receipts. The raw system prompt, user prompt, evidence, model output, persisted row, and monitor payload remain one tap away.</p>
          </div>
          <details class="inspection">
            <summary>Open raw prompt and output audit trail</summary>
            <div class="tabs" role="tablist">
              <button class="active" data-tab="system">System</button>
              <button data-tab="user">User</button>
              <button data-tab="evidence">Evidence</button>
              <button data-tab="model">Model</button>
              <button data-tab="persisted">Persisted</button>
                  <button data-tab="geometry">Geometry</button>
                  <button data-tab="tasks">Tasks</button>
                  <button data-tab="worker">Worker</button>
                  <button data-tab="feedback">Feedback</button>
                  <button data-tab="evolve">Evolve</button>
                  <button data-tab="recurrent">Recurrent</button>
                  <button data-tab="experiment">Experiment</button>
                  <button data-tab="lineage">Lineage</button>
                  <button data-tab="monitor">Monitor</button>
              <button data-tab="research">Research</button>
              <button data-tab="trace">Trace</button>
              <button data-tab="universe">Universe</button>
              <button data-tab="governor">Governor</button>
              <button data-tab="selfheal">Self-Heal</button>
              <button data-tab="coverage">Coverage</button>
            </div>
            {raw_panels}
          </details>
        </section>
      </div>
    """ 


def _render_swipe_deck(
    *,
    prompt: dict[str, Any],
    evidence: list[dict[str, Any]],
    persisted: dict[str, Any],
    data_substrate: Any,
    layers: list[dict[str, Any]],
    story: dict[str, Any],
    first_string: dict[str, Any],
    data: dict[str, Any],
) -> str:
    """Phone-first, swipeable understanding path for the first-layer run."""
    layer_by_name = {
        str(row.get("name") or ""): row
        for row in layers
        if isinstance(row, dict)
    }
    geometry = _artifact_json(data, "alpha_geometry")
    cortex_review = _artifact_json(data, "alpha_geometry_cortex_review")
    cortex_mutation_path = _artifact_json(data, "geometry_cortex_mutation_path")
    task_dispatch = _artifact_json(data, "evolution_control_task_dispatch")
    task_execution = _artifact_json(data, "cortex_task_worker_execution")
    task_feedback = _artifact_json(data, "cortex_task_feedback_evaluator")
    evolve = _artifact_json(data, "market_evolve_step")
    hard_experiment = _artifact_json(data, "market_evolve_hard_experiment")
    lineage = _artifact_json(data, "market_evolve_lineage")
    top_cell = geometry.get("top_cell") if isinstance(geometry.get("top_cell"), dict) else {}
    top_metrics = top_cell.get("metrics") if isinstance(top_cell.get("metrics"), dict) else {}
    route_label = str(top_cell.get("route_directive") or "observe").replace("_", " ")
    best_eval = evolve.get("best_evaluation") if isinstance(evolve.get("best_evaluation"), dict) else {}
    mutation_rows = evolve.get("mutations") if isinstance(evolve.get("mutations"), list) else []
    mutation = (mutation_rows or [{}])[0]
    lineage_frontier = [row for row in (lineage.get("frontier") or []) if isinstance(row, dict)]
    synthesis = layer_by_name.get("information_synthesis_budget_gate") or {}

    entity = str(prompt.get("entity") or persisted.get("entity") or "HYPE")
    horizon = str(prompt.get("horizon") or persisted.get("horizon") or "intraday")
    lens = str(prompt.get("lens") or persisted.get("lens") or "on_chain")
    bias = str(prompt.get("bias_mode") or persisted.get("bias_mode") or "frontier")
    theme = str(prompt.get("theme") or "validator_unstake")
    info_ids = [str(x) for x in (persisted.get("information_string_ids") or [])]
    evidence_ids = [
        str(item.get("tool_call_log_id") or item.get("uri") or "")
        for item in evidence
        if isinstance(item, dict)
    ]
    source_mix = ", ".join(_source_title(str(item.get("uri") or "")) for item in evidence[:3])
    if len(evidence) > 3:
        source_mix += f", +{len(evidence) - 3} more"

    cards = [
        {
            "label": "Start",
            "title": "A market cell is born.",
            "body": "Input: market universe, horizon grid, lens rules, themes, and coverage state. Output: one typed research cell that says exactly what this scout is allowed to care about.",
            "proof": [
                ("cell", f"{entity} / {horizon} / {lens} / {bias}"),
                ("theme", theme),
                ("clock", prompt.get("as_of_utc") or "exported run clock"),
            ],
        },
        {
            "label": "Policy",
            "title": "The research genome shapes the cell.",
            "body": "Input: the active MarketEvolve program. Output: prompt variant, tool menu width, evidence budget, source targets, and experiment lineage stamped onto the cell.",
            "proof": [
                ("prompt variant", persisted.get("prompt_variant") or "unknown"),
                ("tool menu", f"{len(prompt.get('allowed_tools') or [])} exposed URIs"),
                ("policy score", _pct(best_eval.get("score"))),
            ],
        },
        {
            "label": "Tools",
            "title": "Receipts arrive before opinion.",
            "body": "Input: the cell plus approved atlas candidates. Output: compact tool receipts with tool_call_log IDs that the model can cite and the monitor can audit.",
            "proof": [
                ("evidence calls", len(evidence)),
                ("receipt ids", ", ".join(evidence_ids[:3]) or "none"),
                ("source mix", source_mix or "none"),
            ],
        },
        {
            "label": "Prompt",
            "title": "The model is boxed into a contract.",
            "body": "Input: system contract, user packet, receipts, prior strings, and allowed tools. Output: strict JSON only, with hypothesis, strings, scores, refs, and tool requests.",
            "proof": [
                ("model", persisted.get("model_used") or "deepseek:v4-flash"),
                ("confidence", _pct(persisted.get("confidence"))),
                ("quality", ", ".join(str(x) for x in (persisted.get("quality_flags") or [])[:2])),
            ],
        },
        {
            "label": "Memory",
            "title": "The output becomes map memory.",
            "body": "Input: parsed model JSON. Output: durable rows for the hypothesis, information strings, event intelligence, node intelligence, evidence refs, and tool proposals.",
            "proof": [
                ("scout id", persisted.get("scout_id") or "unknown"),
                ("hypothesis id", persisted.get("hypothesis_id") or "unknown"),
                ("string ids", ", ".join(info_ids[:3]) or "none"),
            ],
        },
        {
            "label": "Attention",
            "title": "Many strings compete for budget.",
            "body": "Input: recent information strings for the cycle. Output: confluences, tensions, promoted string IDs, and a smaller verifier-ready candidate set.",
            "proof": [
                ("synthesis", synthesis.get("synthesis_id") or "exported synthesis"),
                ("strings read", synthesis.get("strings", "?")),
                ("promoted", synthesis.get("promoted_hypotheses", "?")),
            ],
        },
        {
            "label": "Shape",
            "title": "The map forms a visible geometry.",
            "body": "Input: stored strings and their source families. Output: market cells with coordinates, scores, and a route directive that tells the next layer what to do.",
            "proof": [
                ("top route", route_label),
                ("trade scream", _pct(top_metrics.get("trade_scream_score"))),
                ("readiness", _pct(top_metrics.get("verifier_readiness"))),
            ],
        },
            {
                "label": "Learn",
                "title": "The system edits its future behavior.",
                "body": "Input: cycle metrics, geometry, tool usage, and learned-tool outcomes. Output: an evaluator score, a candidate mutation, and a hard experiment plan.",
                "proof": [
                    ("mutation", mutation.get("mutation_kind") or "none"),
                    ("why", mutation.get("rationale") or "no rationale exported"),
                    ("data coverage", f"{len(getattr(data_substrate, 'touched', []) or [])} surfaces tracked"),
                ],
            },
            {
                "label": "Arena",
                "title": "Candidate policies must earn promotion.",
                "body": "Input: one evaluation plus the bounded population policy. Output: candidate children with proof contracts, kill signals, population gates, and matched A/B evidence before any child can become active.",
                "proof": [
                    ("candidate children", len(mutation_rows)),
                    ("frontier queue", len(lineage_frontier)),
                    ("latest verdict", hard_experiment.get("final_decision") or "planned"),
                ],
            },
        ]
    card_html = []
    n = len(cards)
    for idx, card in enumerate(cards, start=1):
        proof = "".join(
            "<div>"
            f"<span>{html.escape(str(label))}</span>"
            f"<strong>{html.escape(_compact_value(value, limit=150))}</strong>"
            "</div>"
            for label, value in card["proof"]
        )
        dots = "".join(
            f'<i class="{"on" if dot_idx == idx else ""}"></i>'
            for dot_idx in range(1, n + 1)
        )
        card_html.append(
            '<article class="swipe-card">'
            f'<div class="kicker"><span>{html.escape(card["label"])}</span><b>{idx}</b></div>'
            f'<h3>{html.escape(card["title"])}</h3>'
            f'<p>{html.escape(card["body"])}</p>'
            f'<div class="swipe-proof">{proof}</div>'
            f'<div class="swipe-dots">{dots}</div>'
            '</article>'
        )
    return f"""
        <section class="swipe-section" aria-label="Swipeable system walkthrough">
          <div class="swipe-head">
            <div>
              <div class="chapter-label">Swipe walkthrough</div>
              <h2>Read this like a deck.</h2>
              <p>Swipe card by card. Each card says what went in, what came out, and which real receipt proves it happened.</p>
            </div>
            <div class="swipe-hint">Swipe sideways</div>
          </div>
          <div class="swipe-deck">{''.join(card_html)}</div>
        </section>
    """


def _render_neural_architecture_chapter(
    *,
    data: dict[str, Any],
    trace: dict[str, Any],
    prompt: dict[str, Any],
    evidence: list[dict[str, Any]],
    layers: list[dict[str, Any]],
    data_substrate: Any,
    story: dict[str, Any],
) -> str:
    """Render the system as a real research network, not a one-off prompt."""
    final = trace.get("final_results") if isinstance(trace.get("final_results"), dict) else {}
    input_packet = trace.get("input_packet") if isinstance(trace.get("input_packet"), dict) else {}
    policy = input_packet.get("policy") if isinstance(input_packet.get("policy"), dict) else {}
    seed = input_packet.get("seed") if isinstance(input_packet.get("seed"), dict) else {}
    stages = [row for row in (trace.get("stage_io") or []) if isinstance(row, dict)]
    persisted_objects = [row for row in (trace.get("persisted_objects") or []) if isinstance(row, dict)]
    geometry = final.get("geometry") if isinstance(final.get("geometry"), dict) else {}
    evolution = final.get("evolution") if isinstance(final.get("evolution"), dict) else {}
    info_ids = [str(x) for x in (final.get("information_string_ids") or [])]
    route = str(geometry.get("route_directive") or "observe").replace("_", " ")
    mutation = str(evolution.get("mutation_kind") or "none").replace("_", " ")
    layer_pass = sum(1 for row in layers if row.get("status") == "pass")
    layer_total = len(layers)
    touched_count = len([row for row in data_substrate.touched if row.touched])
    trace_state = "canonical trace artifact" if trace else "reconstructed from raw artifacts"
    recurrent = _artifact_json(data, "shape_recurrent_loop")
    hard_experiment = _artifact_json(data, "market_evolve_hard_experiment")

    organs = [
        (
            "Sensory organs",
            f"{len(evidence)} receipts",
            "Event feed, Hydromancer, our node, market state, and future mempool streams enter as source-linked observations.",
        ),
        (
            "Receptive field",
            str(prompt.get("entity") or "?") + " / " + str(prompt.get("horizon") or "?") + " / " + str(prompt.get("lens") or "?"),
            "Each scout sees one market cell so thousands of calls can tile the market instead of duplicating attention.",
        ),
        (
            "Activation",
            f"{len(info_ids)} strings",
            "The model output becomes causal strings with mechanism, expected outcome, kill signal, scores, and evidence refs.",
        ),
        (
            "Memory",
            f"{len(persisted_objects)} surfaces",
            "The run writes separate objects for hypotheses, strings, evidence edges, graph nodes, node intelligence, tools, geometry, and evolution.",
        ),
        (
            "Attention",
            _compact_value((final.get("synthesis") or {}).get("synthesis_id")),
            "Synthesis promotes only cross-string confluence or tension into expensive verifier/specialist budget.",
        ),
        (
            "Latent geometry",
            route,
            "The map shape becomes a routing instruction: verify, widen scouts, widen sources, repair sources, or resolve tension.",
        ),
        (
            "Reward signal",
            _pct(evolution.get("score")),
            "MarketEvolve scores the research policy itself, then creates mutations and hard experiments instead of blindly trusting vibes.",
        ),
        (
            "Tool creation",
            mutation,
            "Missing edges become tool proposals; useful tools are evaluated, promoted, and returned to the scout harness.",
        ),
    ]
    organ_html = "".join(
        '<article class="output-panel">'
        f'<span>{html.escape(label)}</span>'
        f'<h3>{html.escape(value)}</h3>'
        f'<p>{html.escape(body)}</p>'
        '</article>'
        for label, value, body in organs
    )

    network_metrics = "".join(
        f'<div><strong>{html.escape(value)}</strong><small>{html.escape(label)}</small></div>'
        for label, value in [
            ("system layers passed", f"{layer_pass}/{layer_total}"),
            ("trace source", trace_state),
            ("allowed tools", str(policy.get("allowed_tool_count") or len(prompt.get("allowed_tools") or []))),
            ("data surface coverage", f"{touched_count}/{data_substrate.total_surfaces}"),
        ]
    )

    stage_html = "".join(
        '<article>'
        f'<h3>{html.escape(str(stage.get("stage_id") or ""))}</h3>'
        f'<p>{html.escape(str(stage.get("party") or ""))}</p>'
        f'<small>{html.escape(_compact_value(stage.get("consumer"), limit=120))}</small>'
        '</article>'
        for stage in stages[:8]
    )
    if not stage_html:
        stage_html = (
            '<article><h3>Trace pending</h3>'
            '<p>Run the ground-up smoke harness after this refactor to emit the canonical stage graph.</p></article>'
        )

    memory_html = "".join(
        '<article class="persist-card">'
        f'<span>{html.escape(str(obj.get("surface") or ""))}</span>'
        f'<h3>{html.escape(_compact_value(obj.get("ids"), limit=160))}</h3>'
        f'<p>{html.escape(str(obj.get("geometry") or ""))}</p>'
        '</article>'
        for obj in persisted_objects[:8]
    )
    if not memory_html:
        memory_html = '<article class="persist-card"><span>trace</span><h3>pending</h3><p>The next smoke run writes the memory-surface ledger.</p></article>'

    activation_packet = {
        "seed": seed,
        "route": route,
        "mutation": mutation,
        "boundary": final.get("boundary") or "Routed research object, not final trade approval.",
    }
    recurrent_steps = []
    if recurrent:
        shape = recurrent.get("shape_observed") if isinstance(recurrent.get("shape_observed"), dict) else {}
        top_action = shape.get("top_action") if isinstance(shape.get("top_action"), dict) else {}
        tool_call = recurrent.get("native_tool_call") if isinstance(recurrent.get("native_tool_call"), dict) else {}
        cortex_next_step = (
            recurrent.get("cortex_next_step")
            if isinstance(recurrent.get("cortex_next_step"), dict)
            else {}
        )
        route_decision = recurrent.get("route_decision") if isinstance(recurrent.get("route_decision"), dict) else {}
        worker = recurrent.get("worker_assignment") if isinstance(recurrent.get("worker_assignment"), dict) else {}
        route_eval = (
            recurrent.get("route_contract_evaluation")
            if isinstance(recurrent.get("route_contract_evaluation"), dict)
            else {}
        )
        recurrent_steps = [
            ("Shape Observed", str(top_action.get("route_directive") or shape.get("status") or "ready"), str(top_action.get("reason") or "The alpha-geometry field was read as a routing object.")),
            ("Tool Called", str(tool_call.get("tool_uri") or ""), f"log={tool_call.get('tool_call_log_id') or 'missing'}"),
            ("Cortex Decides", str(cortex_next_step.get("primary_action") or ""), str(cortex_next_step.get("success_gate") or "")),
            ("Route Selected", str(route_decision.get("directive") or ""), str(route_decision.get("why_this_seed_exists") or "")),
            ("Work Emitted", str(worker.get("owner") or ""), str(worker.get("action") or "")),
            ("Scout Executes", str(route_eval.get("status") or "pending"), _compact_value(route_eval.get("quality_flags"), limit=180)),
        ]
    route_eval_packet = (
        recurrent.get("route_contract_evaluation")
        if isinstance(recurrent.get("route_contract_evaluation"), dict)
        else {}
    )
    emitted_seed = recurrent.get("emitted_seed") if isinstance(recurrent.get("emitted_seed"), dict) else {}
    emitted_payload = emitted_seed.get("payload") if isinstance(emitted_seed.get("payload"), dict) else {}
    route_seed_contract = {
        "seed_id": emitted_seed.get("seed_id"),
        "cell_key": emitted_payload.get("alpha_geometry_cell_key"),
        "shape_reader_first": (
            (emitted_payload.get("tool_candidates") or [""])[0]
            if isinstance(emitted_payload.get("tool_candidates"), list)
            else ""
        ),
        "action": emitted_payload.get("alpha_geometry_action"),
        "owner": emitted_payload.get("alpha_geometry_action_owner"),
        "success_gate": emitted_payload.get("alpha_geometry_success_gate"),
        "missing_edges": emitted_payload.get("alpha_geometry_missing_edges"),
        "next_tools": emitted_payload.get("alpha_geometry_suggested_next_tools"),
    } if emitted_seed else {}
    routed_prompt_preview = str(recurrent.get("scout_prompt_preview") or "")
    recurrent_html = "".join(
        '<article>'
        f'<h3>{html.escape(title)}</h3>'
        f'<p>{html.escape(value)}</p>'
        f'<small>{html.escape(body)}</small>'
        '</article>'
        for title, value, body in recurrent_steps
    )
    if not recurrent_html:
        recurrent_html = (
            '<article><h3>Shape loop pending</h3>'
            '<p>Run the latest smoke harness to emit shape_recurrent_loop.json.</p>'
            '<small>The loop should prove shape observed -> tool called -> route emitted.</small></article>'
        )
    experiment_steps = []
    if hard_experiment:
        cycles = [row for row in (hard_experiment.get("cycles") or []) if isinstance(row, dict)]
        final_decision = str(hard_experiment.get("final_decision") or "")
        experiment_steps = [
            ("Experiment", str(hard_experiment.get("experiment_kind") or "matched_policy_ab"), str(hard_experiment.get("experiment_id") or "")),
            ("Matched Slices", str(sum(int(c.get("pair_count") or 0) for c in cycles)), "paired control/candidate seed cells"),
            ("Outcomes", final_decision, f"delta={_compact_value(hard_experiment.get('final_score_delta'))}"),
            ("Active Policy", _compact_value(((hard_experiment.get("active_program_after") or {}).get("name") if isinstance(hard_experiment.get("active_program_after"), dict) else ""), limit=120), str(hard_experiment.get("candidate_program_id") or "")),
        ]
    experiment_html = "".join(
        '<article>'
        f'<h3>{html.escape(title)}</h3>'
        f'<p>{html.escape(value)}</p>'
        f'<small>{html.escape(body)}</small>'
        '</article>'
        for title, value, body in experiment_steps
    )
    if not experiment_html:
        experiment_html = (
            '<article><h3>Hard experiment pending</h3>'
            '<p>The current artifact set does not yet contain a matched A/B policy episode.</p>'
            '<small>The proof should show candidate-vs-control outcomes and active-policy promotion.</small></article>'
        )

    return f"""
        <section class="chapter">
          <div class="chapter-head">
            <div class="chapter-label">00 / neural-net shape</div>
            <h2>Talis is a research network, not one smart prompt.</h2>
            <p>Think of the desk as a market-native neural system: sensors feed narrow receptive fields; scout outputs become activations; graph memory stores the latent state; attention promotes the important clusters; geometry routes spend; reward loops evolve prompts, tools, and routing. This page shows that shape using the real run packet.</p>
          </div>
          <div class="score-tape">{network_metrics}</div>
          <div class="output-grid" style="margin-top:10px">{organ_html}</div>
          <div class="chapter-head" style="margin-top:22px">
            <div class="chapter-label">00b / recurrent loop</div>
            <h2>The system learns by changing the harness around the model.</h2>
            <p>The model is one organ. The intelligence comes from the loop: tool receipts constrain the scout, map memory changes the next prompt, geometry changes the next slice, evaluator scores mutate the policy, and successful tools become new senses.</p>
          </div>
          <div class="next-flow">{stage_html}</div>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Shape to next action</h3>
            <p>This is the recurrence we care about: the map is read by a native tool, the route changes, and a seed or work order is emitted for the next pass.</p>
            <div class="next-flow" style="margin-top:10px">{recurrent_html}</div>
            <p style="margin-top:12px">The emitted seed now carries the same contract the cortex sees: which edge is missing, who owns it, which tools should run after the shape reader, and what would count as success.</p>
            <pre class="prompt-slice">{html.escape(json.dumps(route_seed_contract, indent=2, sort_keys=True, ensure_ascii=True, default=str))}</pre>
            <p style="margin-top:12px">The routed scout is then graded against the geometry contract. This is the closed loop: the shape directs work, the scout must move the missing edge, and the evaluator tells the cortex whether the route succeeded.</p>
            <pre class="prompt-slice">{html.escape(json.dumps(route_eval_packet, indent=2, sort_keys=True, ensure_ascii=True, default=str))}</pre>
            <p style="margin-top:12px">This is the prompt preview the next scout receives from that routed seed.</p>
            <pre class="prompt-slice">{html.escape(routed_prompt_preview[:6000] if routed_prompt_preview else "Prompt preview missing from this run artifact.")}</pre>
            <pre class="prompt-slice">{html.escape(json.dumps((recurrent.get("cortex_next_step") if isinstance(recurrent, dict) else {}) or {}, indent=2, sort_keys=True, ensure_ascii=True, default=str))}</pre>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Evaluator-guided evolution</h3>
            <p>This is the AlphaEvolve-like part: a candidate research policy must beat the active policy on matched scout slices, satisfy hard gates, and only then become the active program.</p>
            <div class="next-flow" style="margin-top:10px">{experiment_html}</div>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>The stored latent state</h3>
            <p>This is what replaces a hidden-weight latent vector: explicit graph objects with IDs, edges, surfaces, consumers, and a geometry that can be scanned.</p>
            <div class="persist-grid">{memory_html}</div>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Current activation</h3>
            <p>{html.escape(story.get("headline") or "No activation headline exported.")}</p>
            <pre class="prompt-slice">{html.escape(json.dumps(activation_packet, indent=2, sort_keys=True, ensure_ascii=True, default=str))}</pre>
          </article>
        </section>
    """


def _render_market_map_generation_chapter(
    *,
    data: dict[str, Any],
    trace: dict[str, Any],
    prompt: dict[str, Any],
) -> str:
    market_map = trace.get("market_map_plan") if isinstance(trace.get("market_map_plan"), dict) else {}
    axes = market_map.get("axes") if isinstance(market_map.get("axes"), dict) else {}
    validity = market_map.get("validity") if isinstance(market_map.get("validity"), dict) else {}
    sampling = market_map.get("sampling_policy") if isinstance(market_map.get("sampling_policy"), dict) else {}
    completion = market_map.get("completion_model") if isinstance(market_map.get("completion_model"), dict) else {}
    overlays = [row for row in (market_map.get("routing_overlays") or []) if isinstance(row, dict)]
    data_sources = market_map.get("data_source_universe") if isinstance(market_map.get("data_source_universe"), dict) else {}
    coverage_gap = _artifact_json(data, "coverage_gap_manifest")
    coverage = coverage_gap.get("coverage") if isinstance(coverage_gap.get("coverage"), dict) else {}

    def axis_count(name: str) -> str:
        axis = axes.get(name) if isinstance(axes.get(name), dict) else {}
        return str(axis.get("count") or len(axis.get("values") or []) or "?")

    current_cell = market_map.get("current_cell") if isinstance(market_map.get("current_cell"), dict) else {}
    cell_line = " / ".join(
        str(current_cell.get(k) or prompt.get(k) or "?")
        for k in ("entity", "horizon", "lens", "bias_mode", "theme")
    )
    axis_cards = "".join(
        '<article class="output-panel">'
        f'<span>{html.escape(label)}</span>'
        f'<h3>{html.escape(value)}</h3>'
        f'<p>{html.escape(body)}</p>'
        '</article>'
        for label, value, body in [
            ("entities", axis_count("entity"), "The instrument universe. Today it is the default desk universe unless the run scope overrides it."),
            ("horizons", axis_count("horizon"), "Intraday through structural time windows; the horizon decides freshness and kill-signal expectations."),
            ("lenses", axis_count("lens"), "Macro, microstructure, options, sentiment, rotation, on-chain, catalyst, anomaly, and other evidence lanes."),
            ("bias modes", axis_count("bias_mode"), "Contrarian, consensus, frontier, tail-risk, mean-reversion, and momentum postures."),
            ("valid cells", _compact_value(validity.get("valid_cell_count")), "The theoretical grid after asset-class validity removes nonsensical combinations."),
            ("covered cells", _compact_value(coverage.get("covered_count")), "Cells currently proven touched in coverage_cells within this smoke database."),
            ("missing cells", _compact_value(coverage.get("missing_count")), "Deterministic gap count: valid cells minus covered cells, before intentional exclusions."),
            ("source universe", _compact_value(data_sources.get("count")), "Known source surfaces: node, Hydromancer, web, filings, macro, options, prediction markets, mempool, social, and experimental priors."),
            ("current cell", cell_line, "This page follows one specimen cell from that larger market lattice."),
        ]
    )
    overlay_html = "".join(
        '<article>'
        f'<h3>{html.escape(str(row.get("name") or ""))}</h3>'
        f'<p>{html.escape(str(row.get("effect") or ""))}</p>'
        f'<small>{html.escape(str(row.get("source") or ""))}</small>'
        '</article>'
        for row in overlays
    )
    requirements = completion.get("full_map_requirement") if isinstance(completion.get("full_map_requirement"), list) else []
    requirement_html = "".join(
        '<article class="persist-card">'
        f'<span>coverage proof</span>'
        f'<h3>{html.escape(str(idx).zfill(2))}</h3>'
        f'<p>{html.escape(str(item))}</p>'
        '</article>'
        for idx, item in enumerate(requirements, start=1)
    )
    proof_artifacts = completion.get("proof_artifacts") if isinstance(completion.get("proof_artifacts"), list) else []
    proof_html = "".join(f"<code>{html.escape(str(item))}</code>" for item in proof_artifacts)

    return f"""
        <section class="chapter">
          <div class="chapter-head">
            <div class="chapter-label">00c / market map generation</div>
            <h2>The full map is a lattice plus a coverage ledger.</h2>
            <p>The desk does not know the market is covered because it launched 1,000 scouts. It knows coverage by comparing the generated seed lattice against logged cells, freshness, source receipts, geometry routes, and missing surfaces. A cycle is a strategic sweep; the map is complete only when the ledger proves no important blind zone is stale or unexplained.</p>
          </div>
          <div class="output-grid">{axis_cards}</div>
          <div class="chapter-head" style="margin-top:22px">
            <div class="chapter-label">split policy</div>
            <h2>How 1,000 scouts are allocated.</h2>
            <p>{html.escape(str(sampling.get("base_sampler") or "Latin hypercube over valid cells"))}. Themes reserve budget, then the remaining scouts spread across the valid grid. Coverage penalties reduce recently covered dead zones; topology and alpha-geometry pull scouts toward frontiers, tensions, source repair, and verifier-ready cells; calendar gates prepend must-research events.</p>
          </div>
          <div class="next-flow">{overlay_html}</div>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>What would prove the map is full?</h3>
            <p>{html.escape(str(completion.get("honest_claim") or "One cycle is strategic coverage, not absolute proof."))}</p>
            <p>Current deterministic coverage ratio in this smoke run: <strong>{html.escape(_compact_value(coverage.get("coverage_ratio")))}</strong>. That low number is expected for a one-cell smoke; a production 1,000-scout run should move this ledger, not merely print successful calls.</p>
            <div class="persist-grid">{requirement_html}</div>
            <div class="tool-list" style="margin-top:12px">{proof_html}</div>
            <p style="margin-top:12px">{html.escape(str(data_sources.get("honest_claim") or ""))}</p>
          </article>
        </section>
    """


def _render_market_map_governor_chapter(*, data: dict[str, Any]) -> str:
    plan = _artifact_json(data, "market_map_governor")
    ranked = [row for row in (plan.get("ranked_gaps") or []) if isinstance(row, dict)]
    lanes = [row for row in (plan.get("budget_lanes") or []) if isinstance(row, dict)]
    surfaces = [row for row in (plan.get("source_surface_priorities") or []) if isinstance(row, dict)]
    full = plan.get("full_market_definition") if isinstance(plan.get("full_market_definition"), dict) else {}
    coverage = plan.get("coverage_state") if isinstance(plan.get("coverage_state"), dict) else {}
    llm = plan.get("llm_governor") if isinstance(plan.get("llm_governor"), dict) else {}
    geometry = plan.get("alpha_geometry_context") if isinstance(plan.get("alpha_geometry_context"), dict) else {}
    action_plan = geometry.get("action_plan") if isinstance(geometry.get("action_plan"), dict) else {}
    shape_actions = [row for row in (action_plan.get("actions") or []) if isinstance(row, dict)]
    shape_tools = [row for row in (action_plan.get("tool_requests") or []) if isinstance(row, dict)]
    prompt = str(llm.get("prompt") or "")

    top_gap_cards = "".join(
        '<article class="output-panel">'
        f'<span>{html.escape(str(gap.get("priority_band") or ""))} / {html.escape(str(gap.get("status") or ""))}</span>'
        f'<h3>{html.escape(" / ".join(str(gap.get(k) or "") for k in ("entity", "horizon", "lens", "bias_mode")))}</h3>'
        f'<p>{html.escape(str(gap.get("reason") or ""))}</p>'
        f'<p><strong>Surfaces:</strong> {html.escape(", ".join(str(x) for x in (gap.get("missing_surfaces") or [])[:5]))}</p>'
        '</article>'
        for gap in ranked[:6]
    )
    if not top_gap_cards:
        top_gap_cards = '<article class="output-panel"><span>no gaps</span><h3>No ranked gaps exported.</h3><p>Run the governor after the coverage audit to see scout allocation priorities.</p></article>'

    lane_cards = "".join(
        '<article>'
        f'<h3>{html.escape(str(lane.get("scout_count") or 0))} scouts</h3>'
        f'<p>{html.escape(str(lane.get("entity_focus") or ""))} / {html.escape(str(lane.get("horizon_focus") or ""))} / {html.escape(str(lane.get("lens_focus") or ""))}</p>'
        f'<small>{html.escape(str(lane.get("reason") or ""))}</small>'
        '</article>'
        for lane in lanes[:6]
    )
    surface_cards = "".join(
        '<article class="persist-card">'
        f'<span>{html.escape(str(surface.get("source_family") or ""))}</span>'
        f'<h3>{html.escape(str(surface.get("title") or surface.get("surface_key") or ""))}</h3>'
        f'<p>{html.escape(str(surface.get("promotion_gate") or ""))}</p>'
        '</article>'
        for surface in surfaces[:6]
    )
    seed_cells = plan.get("suggested_seed_cells") if isinstance(plan.get("suggested_seed_cells"), list) else []
    seed_preview = json.dumps(seed_cells[:5], indent=2, sort_keys=True, ensure_ascii=True, default=str)
    llm_response = llm.get("response") if isinstance(llm.get("response"), dict) else {}
    parsed = llm_response.get("parsed") if isinstance(llm_response.get("parsed"), dict) else {}
    assessment = parsed.get("market_state_assessment") if isinstance(parsed.get("market_state_assessment"), dict) else {}
    shape_cards = "".join(
        '<article class="output-panel">'
        f'<span>{html.escape(str(action.get("owner") or ""))} / {html.escape(str(action.get("route_directive") or ""))}</span>'
        f'<h3>{html.escape(str(action.get("action") or ""))}</h3>'
        f'<p>{html.escape(str(action.get("reason") or ""))}</p>'
        f'<p><strong>Cell:</strong> {html.escape(str(action.get("entity") or ""))} / {html.escape(str(action.get("horizon") or ""))} / {html.escape(str(action.get("lens") or ""))}</p>'
        f'<p><strong>Gate:</strong> {html.escape(str(action.get("success_gate") or ""))}</p>'
        '</article>'
        for action in shape_actions[:6]
    )
    if not shape_cards:
        shape_cards = '<article class="output-panel"><span>no action</span><h3>No geometry action plan yet.</h3><p>The map needs persisted strings before the field itself can route the next move.</p></article>'
    shape_tool_text = json.dumps(shape_tools[:8], indent=2, sort_keys=True, ensure_ascii=True, default=str)

    return f"""
        <section class="chapter">
          <div class="chapter-head">
            <div class="chapter-label">00d / market-map governor</div>
            <h2>The gap ledger becomes an allocation brain.</h2>
            <p>The coverage audit is deterministic math. The governor sits above it and ranks the frontier: which missing cells deserve the next scouts, which source surfaces those scouts should touch, and what compact context a model can use to propose repair work without pretending the whole market is known.</p>
          </div>
          <div class="score-tape">
            <div><strong>{html.escape(_compact_value(full.get("entity_count")))}</strong><small>known entities</small></div>
            <div><strong>{html.escape(_compact_value(full.get("valid_cell_count")))}</strong><small>valid cells</small></div>
            <div><strong>{html.escape(_compact_value(coverage.get("missing_count")))}</strong><small>missing cells</small></div>
            <div><strong>{html.escape(str(plan.get("completion_pressure") or "unknown"))}</strong><small>completion pressure</small></div>
            <div><strong>{html.escape(str(geometry.get("status") or "unknown"))}</strong><small>geometry field</small></div>
            <div><strong>{html.escape(str(action_plan.get("status") or "unknown"))}</strong><small>shape action plan</small></div>
          </div>
          <div class="output-grid" style="margin-top:10px">{top_gap_cards}</div>
          <div class="chapter-head" style="margin-top:22px">
            <div class="chapter-label">cortex + geometry</div>
            <h2>The shape tells the cortex where to move next.</h2>
            <p>The market map is not just a checklist. Alpha geometry turns strings into coordinates: source independence, frontier pressure, tension, fragility, support, and verifier readiness. The action planner translates that field into concrete next moves: verify, repair sources, resolve tension, widen sources, or replicate with independent scouts.</p>
          </div>
          <div class="output-grid">{shape_cards}</div>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Native self-inspection tool</h3>
            <p>Geometry-routed and cortex-routed scouts now receive <code>tic://tool/talis_native/plan_alpha_geometry_actions@v1</code> in their harness. That means an agent can ask the desk itself: “what does the current shape say I should do next?” before spending more external calls.</p>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Shape tools requested by the field</h3>
            <p>These are the tools the geometry thinks would move a coordinate or close a missing edge.</p>
            <pre class="prompt-slice">{html.escape(shape_tool_text[:3000])}</pre>
          </article>
          <div class="chapter-head" style="margin-top:22px">
            <div class="chapter-label">allocation</div>
            <h2>How the next 1,000 agents get sent out.</h2>
            <p>Budget lanes group the ranked gaps by asset class, horizon, and lens. This keeps broad coverage while still over-weighting live tradeable frontiers, node/Hydromancer gaps, fresh horizons, and missing source edges.</p>
          </div>
          <div class="next-flow">{lane_cards}</div>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Source surfaces the governor wants to improve</h3>
            <p>These are not vague research wishes. Each surface needs typed receipts, timestamps, raw artifact refs, and evidence IDs before it can become map memory.</p>
            <div class="persist-grid">{surface_cards}</div>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Seed-cell preview</h3>
            <p>The governor emits concrete seed packets that can be handed to the scout harness.</p>
            <pre class="prompt-slice">{html.escape(seed_preview[:5000])}</pre>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>LLM governor context</h3>
            <p>A frontier model may improve the plan, but only inside this context and only if deterministic promotion gates accept the output. In this run: enabled={html.escape(str(llm.get("enabled")))}; promoted seeds={html.escape(str(llm.get("promoted_seed_count") or 0))}; assessment={html.escape(str(assessment.get("coverage_posture") or "not run"))}.</p>
            <pre class="prompt-slice">{html.escape(prompt[:5000])}</pre>
          </article>
        </section>
    """


def _render_self_healing_chapter(*, data: dict[str, Any]) -> str:
    plan = _artifact_json(data, "market_map_self_healing")
    orders = [row for row in (plan.get("work_orders") or []) if isinstance(row, dict)]
    context = plan.get("context_packet") if isinstance(plan.get("context_packet"), dict) else {}
    order_cards = "".join(
        '<article class="output-panel">'
        f'<span>{html.escape(str(order.get("owner") or ""))} / {html.escape(str(order.get("priority") or ""))}</span>'
        f'<h3>{html.escape(str(order.get("action") or ""))}</h3>'
        f'<p>{html.escape(str(order.get("reason") or ""))}</p>'
        f'<p><strong>Output:</strong> {html.escape(str(order.get("expected_output") or ""))}</p>'
        '</article>'
        for order in orders[:6]
    )
    if not order_cards:
        order_cards = '<article class="output-panel"><span>no orders</span><h3>No repair work emitted.</h3><p>The map did not find an actionable repair gap in this run.</p></article>'
    context_rows = "".join(
        f'<div><span>{html.escape(label)}</span><strong>{html.escape(_compact_value(value, limit=180))}</strong></div>'
        for label, value in [
            ("cycle", context.get("cycle_id")),
            ("route", context.get("route")),
            ("info strings", context.get("information_string_ids")),
            ("evidence", context.get("evidence_receipts")),
            ("market universe", context.get("market_universe")),
            ("data universe", context.get("data_source_universe")),
        ]
    )
    prompt = str(plan.get("llm_gap_prompt") or "")
    return f"""
        <section class="chapter">
          <div class="chapter-head">
            <div class="chapter-label">00e / self-healing map</div>
            <h2>The map assigns workers to repair itself.</h2>
            <p>Coverage gaps should become action, not dashboard guilt. The self-healing planner reads the canonical trace and emits worker orders for seed routing, tool building, source integrity, context expansion, verifier routing, and learned-tool promotion. An LLM can improve the plan from a compact context packet, while deterministic rails keep every order tied to a missing edge and a success gate.</p>
          </div>
          <div class="score-tape">
            <div><strong>{html.escape(str(len(orders)))}</strong><small>repair / expansion orders</small></div>
            <div><strong>{html.escape(str(plan.get("status") or "unknown"))}</strong><small>planner status</small></div>
            <div><strong>{html.escape(_compact_value(context.get("route")))}</strong><small>geometry route</small></div>
            <div><strong>{html.escape(str(len(prompt)))}</strong><small>LLM context prompt chars</small></div>
          </div>
          <div class="output-grid" style="margin-top:10px">{order_cards}</div>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Worker context packet</h3>
            <p>This is the compact state a repair worker receives before it acts.</p>
            <div class="trace-meta">{context_rows}</div>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>LLM repair-planner prompt</h3>
            <p>The next version can call a cheap or strong model here; the output must be strict JSON and pass deterministic gates before workers run.</p>
            <pre class="prompt-slice">{html.escape(prompt[:5000])}</pre>
          </article>
        </section>
    """


def _render_system_trace_chapter(
    *,
    data: dict[str, Any],
    prompt: dict[str, Any],
    evidence: list[dict[str, Any]],
    persisted: dict[str, Any],
    data_substrate: Any,
    layers: list[dict[str, Any]],
    story: dict[str, Any],
    first_string: dict[str, Any],
) -> str:
    """Render the exact party-by-party packet trace for the exported run."""
    report = data.get("report") if isinstance(data.get("report"), dict) else {}
    layer_by_name = {
        str(row.get("name") or ""): row
        for row in layers
        if isinstance(row, dict)
    }
    geometry = _artifact_json(data, "alpha_geometry")
    evolve = _artifact_json(data, "market_evolve_step")
    monitor = _artifact_json(data, "monitor_payload")
    top_cell = geometry.get("top_cell") if isinstance(geometry.get("top_cell"), dict) else {}
    top_metrics = top_cell.get("metrics") if isinstance(top_cell.get("metrics"), dict) else {}
    best_eval = evolve.get("best_evaluation") if isinstance(evolve.get("best_evaluation"), dict) else {}
    mutation = (evolve.get("mutations") or [{}])[0] if isinstance(evolve.get("mutations"), list) else {}
    experiment = (evolve.get("experiment_plans") or [{}])[0] if isinstance(evolve.get("experiment_plans"), list) else {}
    synthesis_layer = layer_by_name.get("information_synthesis_budget_gate") or {}
    event_layer = layer_by_name.get("event_intelligence_ingest") or {}
    node_layer = layer_by_name.get("node_intelligence_ingest") or {}
    tool_layer = layer_by_name.get("analysis_tool_creation_iteration") or {}
    live_layer = layer_by_name.get("live_scout_execution_and_monitor_join") or {}
    geometry_layer = layer_by_name.get("alpha_geometry_market_evolve") or {}

    allowed_tools = [str(x) for x in (prompt.get("allowed_tools") or [])]
    evidence_ids = [
        str(item.get("tool_call_log_id") or item.get("uri") or "evidence")
        for item in evidence
        if isinstance(item, dict)
    ]
    info_ids = [
        str(x)
        for x in (
            persisted.get("information_string_ids")
            or live_layer.get("information_string_ids")
            or report.get("information_string_ids")
            or []
        )
    ]
    event_ids = [str(x) for x in (persisted.get("event_intelligence_ids") or live_layer.get("event_intelligence_ids") or [])]
    node_ids = [str(x) for x in (persisted.get("node_intelligence_ids") or live_layer.get("node_intelligence_ids") or [])]
    proposal_ids = [
        str(x)
        for x in (
            persisted.get("tool_proposal_ids")
            or tool_layer.get("proposal_ids")
            or report.get("tool_proposal_ids")
            or []
        )
    ]
    entity = str(prompt.get("entity") or persisted.get("entity") or report.get("entity") or "")
    horizon = str(prompt.get("horizon") or persisted.get("horizon") or "")
    lens = str(prompt.get("lens") or persisted.get("lens") or "")
    bias = str(prompt.get("bias_mode") or persisted.get("bias_mode") or "")
    theme = str(prompt.get("theme") or "validator_unstake")
    cell = " / ".join(x for x in [entity, horizon, lens, bias, theme] if x)

    def chips(items: list[str], empty: str = "none") -> str:
        values = [x for x in items if x]
        if not values:
            return f"<code>{html.escape(empty)}</code>"
        return "".join(f"<code>{html.escape(x)}</code>" for x in values[:12])

    def meta(rows: list[tuple[str, Any]]) -> str:
        return "".join(
            "<div>"
            f"<span>{html.escape(label)}</span>"
            f"<strong>{html.escape(_compact_value(value))}</strong>"
            "</div>"
            for label, value in rows
        )

    def payload_rows(rows: list[tuple[str, Any]]) -> str:
        return "".join(
            '<div class="payload-row">'
            f"<span>{html.escape(label)}</span>"
            f"<strong>{html.escape(_compact_value(value, limit=420))}</strong>"
            "</div>"
            for label, value in rows
        )

    parties = [
        (
            "Research Director + Theme Loader",
            "Receives cycle scope and optional cross-cutting themes; hands a curriculum into Tier 0 so the seed plan is not random wandering.",
            f"theme={theme}",
        ),
        (
            "Tier 0 Seed Router",
            "Splits the market into entity x horizon x lens x bias x theme cells, applies validity rules by asset class, coverage penalties, topology/frontier boosts, calendar inserts, and deterministic seed entropy.",
            cell,
        ),
        (
            "MarketEvolve Policy",
            "Stamps the active research-policy genome onto the seed: prompt variant, tool menu width, evidence budget, source-family targets, learned-tool preference, and experiment arm when A/B is active.",
            f"prompt_variant={persisted.get('prompt_variant') or '?'}",
        ),
        (
            "Tool Atlas + Scout Harness",
            "Ranks the approved read-only atlas for this cell, executes a tiny evidence slice, logs receipts, and lets the model request at most a bounded follow-up loop.",
            f"{len(evidence)} receipts / {len(allowed_tools)} exposed tools",
        ),
        (
            "Deep Scout Model",
            "Receives the system contract plus the cell packet, evidence summaries, prior strings, and allowed tools. It returns only strict JSON.",
            str(persisted.get("model_used") or "deepseek:v4-flash"),
        ),
        (
            "Normalizer + Persistor",
            "Normalizes model JSON into hypothesis, information strings, event intelligence, node intelligence, and tool proposals. Invalid or weak fields become quality flags rather than silent truth.",
            f"scout_id={persisted.get('scout_id') or live_layer.get('scout_id') or '?'}",
        ),
        (
            "Information Map",
            "Stores strings as causal objects, writes evidence refs, and upserts graph nodes/edges from the entity chain and mechanism.",
            f"{len(info_ids)} strings",
        ),
        (
            "Attention Synthesis",
            "Reads many strings for the cycle, finds confluences and tensions, and promotes only a small set into verification budget.",
            f"synthesis_id={synthesis_layer.get('synthesis_id') or '?'}",
        ),
        (
            "Alpha Geometry",
            "Projects stored strings into market cells. Coordinates encode source independence, frontier pressure, tension, fragility, support, and verifier readiness.",
            f"{geometry_layer.get('top_route_directive') or top_cell.get('route_directive') or 'observe'}",
        ),
        (
            "MarketEvolve Evaluator",
            "Scores the research policy itself, mutates prompt/routing/tool policy, and creates a matched hard experiment instead of blindly adopting the change.",
            str(mutation.get("mutation_kind") or "no mutation"),
        ),
        (
            "Monitor + Phone Surface",
            "Joins the persisted objects back together so the user can inspect the story, IDs, receipts, prompt, output, graph shape, and next route from one screen.",
            f"{(monitor.get('summary') or {}).get('strings', '?')} monitor strings",
        ),
    ]
    party_html = "".join(
        '<article class="trace-step">'
        f'<div class="num">{idx}</div>'
        f'<div><h3>{html.escape(title)}</h3><p>{html.escape(body)}</p><code>{html.escape(receipt)}</code></div>'
        '</article>'
        for idx, (title, body, receipt) in enumerate(parties, start=1)
    )

    split_html = meta([
        ("market cell", cell),
        ("determinism", "fixed smoke seed; full swarm uses stable rng unless randomized"),
        ("theme injection", theme),
        ("coverage control", "coverage_cell_key + coverage_log penalty in SeedCell payload"),
        ("frontier control", "topology/alpha-geometry route seeds and density boost"),
        ("event gate", ", ".join(event_ids) or event_layer.get("event_bundle_id") or "none"),
        ("tool retrieval", "lexical lens terms + BM25 atlas retrieval + source-family targets"),
        ("tool budget", f"{len(evidence)} evidence calls in this run; hard cap 8 per scout"),
        ("policy lineage", best_eval.get("program_id") or "default active program"),
    ])

    handoff_html = "".join(
        '<article class="output-panel">'
        f'<span>{html.escape(label)}</span>'
        f'<h3>{html.escape(title)}</h3>'
        f'<p>{html.escape(body)}</p>'
        '</article>'
        for label, title, body in [
            (
                "Seed packet",
                "Cell plus policy becomes the scout job",
                f"{cell}; prompt={persisted.get('prompt_variant')}; allowed_tools={len(allowed_tools)}.",
            ),
            (
                "Evidence packet",
                "Receipts are attached before the model call",
                ", ".join(evidence_ids) or "No evidence IDs exported.",
            ),
            (
                "Model packet",
                "The model emits structured JSON",
                f"hypothesis_id={persisted.get('hypothesis_id')}; confidence={persisted.get('confidence')}.",
            ),
            (
                "Map packet",
                "The JSON becomes durable graph memory",
                f"strings={len(info_ids)}, event={len(event_ids)}, node={len(node_ids)}, tool_proposals={len(proposal_ids)}.",
            ),
            (
                "Route packet",
                "Geometry says what to do next",
                f"top_cell={top_cell.get('cell_key') or geometry_layer.get('top_cell_key')}; route={top_cell.get('route_directive') or geometry_layer.get('top_route_directive')}.",
            ),
            (
                "Evolution packet",
                "The policy learns from the run",
                f"score={_pct(best_eval.get('score'))}; mutation={mutation.get('mutation_kind') or 'none'}; experiment={experiment.get('id') or 'planned'}."
            ),
        ]
    )

    io_rows = [
        (
            "Tier 0 seed routing",
            "cycle scope, entity universe, horizons, lenses, bias modes, themes, coverage and topology state",
            f"seed_id={persisted.get('seed_id')}; cell={cell}",
            "A single scout job with a stable replayable slice.",
        ),
        (
            "Policy application",
            "active MarketEvolve program plus any matched experiment assignment",
            f"prompt_variant={persisted.get('prompt_variant')}; tool_budget={len(allowed_tools)} exposed tools",
            "The research genome touches the live scout before spend happens.",
        ),
        (
            "Tool harness",
            "cell, atlas rows, source-family targets, argument inference, hard evidence cap",
            f"tool_call_log_ids={', '.join(evidence_ids) or 'none'}",
            "The model receives receipts, not raw unlimited tool freedom.",
        ),
        (
            "Model contract",
            "system prompt, user prompt, evidence receipts, prior strings, allowed tool list",
            f"hypothesis={_compact_value(persisted.get('hypothesis_text'), limit=120)}",
            "The model output is parseable into durable objects.",
        ),
        (
            "Persistence",
            "parsed JSON, normalized strings, event/node fallbacks, quality review",
            f"hypothesis={persisted.get('hypothesis_id')}; strings={len(info_ids)}; events={len(event_ids)}; node={len(node_ids)}",
            "Everything important gets an ID and a consumer.",
        ),
        (
            "Attention synthesis",
            "recent cycle strings ordered by attention score",
            f"synthesis={synthesis_layer.get('synthesis_id')}; promoted={synthesis_layer.get('promoted_hypotheses', '?')}",
            "Broad scout output is narrowed before verifier spend.",
        ),
        (
            "Alpha geometry",
            "strings, evidence refs, source families, scout counts, novelty/crowdedness/conviction",
            f"top_cell={top_cell.get('cell_key') or geometry_layer.get('top_cell_key')}; route={top_cell.get('route_directive') or geometry_layer.get('top_route_directive')}",
            "The map shape becomes a routing instruction.",
        ),
        (
            "MarketEvolve",
            "cycle metrics, geometry quality, learned-tool usage, proposal backlog, hard experiment history",
            f"score={_pct(best_eval.get('score'))}; mutation={mutation.get('mutation_kind') or 'none'}",
            "The research policy changes only through measured evidence.",
        ),
        (
            "Monitor",
            "database rows joined by scout_id, string IDs, event IDs, node IDs, proposal IDs",
            f"strings={(monitor.get('summary') or {}).get('strings', '?')}; proposals={len(monitor.get('tool_proposals') or [])}",
            "The user gets a legible surface with the raw trail one tap away.",
        ),
    ]
    io_html = "".join(
        '<article class="output-panel">'
        f'<span>{html.escape(stage)}</span>'
        f'<h3>{html.escape(result)}</h3>'
        f'<p><strong>In:</strong> {html.escape(_compact_value(inputs, limit=260))}</p>'
        f'<p><strong>Out:</strong> {html.escape(_compact_value(outputs, limit=260))}</p>'
        '</article>'
        for stage, inputs, outputs, result in io_rows
    )

    persisted_rows = [
        ("hypotheses", persisted.get("hypothesis_id"), "Scout output. Consumed by dedup, verifier, analyst, monitor."),
        ("information_strings", info_ids, "Causal strings. Consumed by synthesis, geometry, context retrieval, verifier handoff."),
        ("information_string_evidence", evidence_ids, "Receipt edges. Consumed by audit, source-family scoring, verifier repair."),
        ("information_map_nodes/edges", first_string.get("entities_chain") or story.get("entities"), "Entity, mechanism, and theme graph. Consumed by context retrieval and geometry."),
        ("market_event_intelligence", event_ids or event_layer.get("event_bundle_id"), "Event object plus data points/watch triggers. Consumed by scouts and monitor."),
        ("node_intelligence_snapshots", node_ids or node_layer.get("node_snapshot_id"), "Hydromancer/node-native observation bundle. Consumed by map, tool proposals, monitor."),
        ("analysis_tool_proposals", proposal_ids, "Missing-capability requests. Consumed by learned-tool eval and runtime-adapter work orders."),
        ("information_syntheses", synthesis_layer.get("synthesis_id"), "Confluences/tensions/promoted hypotheses. Consumed by verifier budget gate."),
        ("information_geometry_snapshots", top_cell.get("cell_key") or geometry_layer.get("top_cell_key"), "Cell coordinates and route directive. Consumed by next seed plan."),
        ("market_evolve_*", [best_eval.get("evaluation_id"), mutation.get("mutation_id"), experiment.get("id")], "Program score, mutation, and hard experiment lineage."),
    ]
    persist_html = "".join(
        '<article class="persist-card">'
        f'<span>{html.escape(str(table))}</span>'
        f'<h3>{html.escape(_compact_value(ids, limit=260))}</h3>'
        f'<p>{html.escape(desc)}</p>'
        '</article>'
        for table, ids, desc in persisted_rows
    )

    packet = {
        "cycle_id": report.get("cycle_id") or persisted.get("cycle_id"),
        "db_path": report.get("db_path"),
        "cell": {
            "entity": entity,
            "horizon": horizon,
            "lens": lens,
            "bias_mode": bias,
            "theme": theme,
            "as_of_utc": prompt.get("as_of_utc"),
        },
        "seed": {
            "seed_id": persisted.get("seed_id"),
            "prompt_variant": persisted.get("prompt_variant"),
            "allowed_tool_count": len(allowed_tools),
        },
        "evidence": {
            "tool_call_log_ids": evidence_ids,
            "source_titles": [_source_title(str(item.get("uri") or "")) for item in evidence],
        },
        "output_ids": {
            "scout_id": persisted.get("scout_id"),
            "hypothesis_id": persisted.get("hypothesis_id"),
            "information_string_ids": info_ids,
            "event_intelligence_ids": event_ids,
            "node_intelligence_ids": node_ids,
            "tool_proposal_ids": proposal_ids,
        },
        "geometry": {
            "top_cell_key": top_cell.get("cell_key") or geometry_layer.get("top_cell_key"),
            "route_directive": top_cell.get("route_directive") or geometry_layer.get("top_route_directive"),
            "trade_scream_score": top_metrics.get("trade_scream_score") or geometry_layer.get("top_trade_scream_score"),
            "verifier_readiness": top_metrics.get("verifier_readiness") or geometry_layer.get("top_verifier_readiness"),
            "coordinates": top_cell.get("coordinates") or {},
        },
        "evolution": {
            "program_id": best_eval.get("program_id"),
            "score": best_eval.get("score"),
            "mutation_kind": mutation.get("mutation_kind"),
            "mutation_id": mutation.get("mutation_id"),
            "experiment_id": experiment.get("id"),
        },
    }

    packet_html = payload_rows([
        ("cycle", packet["cycle_id"]),
        ("seed", packet["seed"]["seed_id"]),
        ("scout", packet["output_ids"]["scout_id"]),
        ("hypothesis", packet["output_ids"]["hypothesis_id"]),
        ("info strings", info_ids),
        ("evidence refs", evidence_ids),
        ("route", packet["geometry"]["route_directive"]),
        ("policy mutation", packet["evolution"]["mutation_kind"]),
    ])

    return f"""
        <section class="chapter">
          <div class="chapter-head">
            <div class="chapter-label">10 / exact packet trace</div>
            <h2>Every party, every handoff, every persisted object.</h2>
            <p>This is the step-by-step system trace for the exported run. The left side is the production choreography; the right side is the actual packet from this run, with IDs you can chase in the raw drawer.</p>
          </div>
          <div class="trace-board">
            <div class="trace-spine">{party_html}</div>
            <div>
              <article class="workbench-panel">
                <h3>How the slice is chosen</h3>
                <p>The full swarm uses these gates to cover the market strategically. This smoke export shows one specimen cell, but the fields are the same for 1,000 scouts.</p>
                <div class="trace-meta">{split_html}</div>
              </article>
              <article class="workbench-panel" style="margin-top:10px">
                <h3>The live packet</h3>
                <p>These are the IDs and control fields that move through the system.</p>
                <div class="payload-table">{packet_html}</div>
                <div class="id-strip">{chips(info_ids + event_ids + node_ids + proposal_ids, empty="no persisted ids")}</div>
              </article>
            </div>
          </div>
          <div class="chapter-head" style="margin-top:22px">
            <div class="chapter-label">10b / handoff contract</div>
            <h2>The packet changes shape at each boundary.</h2>
            <p>Each party receives a narrower or richer object, not a vague blob. That is what makes the layer inspectable and lets future agents improve it without breaking the whole system.</p>
          </div>
          <div class="output-grid">{handoff_html}</div>
          <div class="chapter-head" style="margin-top:22px">
            <div class="chapter-label">10c / data in, data out</div>
            <h2>The real layer ledger.</h2>
            <p>This is the clean version of the raw logs: every layer gets a typed input and emits a typed output. The IDs are the connective tissue.</p>
          </div>
          <div class="output-grid">{io_html}</div>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Where the data lands</h3>
            <p>Storage is the product architecture: strings, receipts, nodes, geometry, tools, and evolution lineage are separate surfaces with explicit consumers.</p>
            <div class="persist-grid">{persist_html}</div>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Compact run packet</h3>
            <p>This is the complete trace in one JSON-shaped object for quick audit.</p>
            <pre class="prompt-slice">{html.escape(json.dumps(packet, indent=2, sort_keys=True, ensure_ascii=True, default=str))}</pre>
          </article>
        </section>
    """


def _render_final_results_chapter(
    *,
    data: dict[str, Any],
    prompt: dict[str, Any],
    evidence: list[dict[str, Any]],
    persisted: dict[str, Any],
    data_substrate: Any,
    layers: list[dict[str, Any]],
    story: dict[str, Any],
    first_string: dict[str, Any],
) -> str:
    report = data.get("report") if isinstance(data.get("report"), dict) else {}
    monitor = _artifact_json(data, "monitor_payload")
    monitor_summary = monitor.get("summary") if isinstance(monitor.get("summary"), dict) else {}
    geometry = _artifact_json(data, "alpha_geometry")
    top_cell = geometry.get("top_cell") if isinstance(geometry.get("top_cell"), dict) else {}
    top_metrics = top_cell.get("metrics") if isinstance(top_cell.get("metrics"), dict) else {}
    evolve = _artifact_json(data, "market_evolve_step")
    best_eval = evolve.get("best_evaluation") if isinstance(evolve.get("best_evaluation"), dict) else {}
    mutation = (evolve.get("mutations") or [{}])[0] if isinstance(evolve.get("mutations"), list) else {}
    experiment = (evolve.get("experiment_plans") or [{}])[0] if isinstance(evolve.get("experiment_plans"), list) else {}
    layer_by_name = {
        str(row.get("name") or ""): row
        for row in layers
        if isinstance(row, dict)
    }
    synthesis = layer_by_name.get("information_synthesis_budget_gate") or {}
    info_ids = [str(x) for x in (persisted.get("information_string_ids") or [])]
    event_ids = [str(x) for x in (persisted.get("event_intelligence_ids") or [])]
    node_ids = [str(x) for x in (persisted.get("node_intelligence_ids") or [])]
    proposal_ids = [str(x) for x in (persisted.get("tool_proposal_ids") or report.get("tool_proposal_ids") or [])]
    evidence_ids = [
        str(item.get("tool_call_log_id") or item.get("uri") or "")
        for item in evidence
        if isinstance(item, dict)
    ]
    layer_pass = sum(1 for row in layers if row.get("status") == "pass")
    layer_total = len(layers)
    route = str(top_cell.get("route_directive") or "observe")
    route_display = route.replace("_", " ")
    result_sentence = (
        "This run passed the ground-up system smoke and produced a routed research object. "
        f"It did not declare a final trade; geometry routed the cell to {route_display}."
    )
    top_line_cards = "".join(
        f'<div><strong>{html.escape(value)}</strong><small>{html.escape(label)}</small></div>'
        for label, value in [
            ("system layers passed", f"{layer_pass}/{layer_total}"),
            ("data surfaces touched", f"{len([row for row in data_substrate.touched if row.touched])}/{data_substrate.total_surfaces}"),
            ("monitor strings", str(monitor_summary.get("strings", "?"))),
            ("top route", route_display),
        ]
    )
    final_cards = "".join(
        '<article class="output-panel">'
        f'<span>{html.escape(label)}</span>'
        f'<h3>{html.escape(title)}</h3>'
        f'<p>{html.escape(body)}</p>'
        '</article>'
        for label, title, body in [
            (
                "Scout conclusion",
                _compact_value(story.get("headline"), limit=140),
                f"Confidence {_pct(persisted.get('confidence'))}; kill signal: {_compact_value(story.get('kill_signal'), limit=220)}",
            ),
            (
                "Information string",
                _compact_value(first_string.get("title") or first_string.get("thesis"), limit=140),
                f"Mechanism: {_compact_value(first_string.get('mechanism') or story.get('mechanism'), limit=260)}",
            ),
            (
                "Evidence receipts",
                f"{len(evidence_ids)} tool receipts",
                ", ".join(evidence_ids) or "No evidence IDs exported.",
            ),
            (
                "Persisted outputs",
                f"{len(info_ids)} strings, {len(event_ids)} event bundle, {len(node_ids)} node snapshot, {len(proposal_ids)} tool proposal",
                f"scout_id={persisted.get('scout_id')}; hypothesis_id={persisted.get('hypothesis_id')}",
            ),
            (
                "Synthesis",
                f"{synthesis.get('promoted_hypotheses', '?')} promoted hypothesis",
                f"synthesis_id={synthesis.get('synthesis_id')}; confluences={synthesis.get('confluences', '?')}; tensions={synthesis.get('tensions', '?')}",
            ),
            (
                "Geometry",
                f"{route.replace('_', ' ')}",
                f"trade_scream={_pct(top_metrics.get('trade_scream_score'))}; readiness={_pct(top_metrics.get('verifier_readiness'))}; top_cell={_compact_value(top_cell.get('cell_key'), limit=160)}",
            ),
            (
                "Policy evolution",
                _compact_value(mutation.get("mutation_kind") or "no mutation", limit=120),
                f"evaluator={_pct(best_eval.get('score'))}; experiment={experiment.get('id') or 'planned'}; rationale={_compact_value(mutation.get('rationale'), limit=220)}",
            ),
            (
                "User surface",
                "Static phone viewer generated",
                "The clean story, swipe deck, exact packet trace, final results, and raw audit drawer are all in this page.",
            ),
        ]
    )
    id_rows = "".join(
        '<article class="persist-card">'
        f'<span>{html.escape(label)}</span>'
        f'<h3>{html.escape(_compact_value(value, limit=240))}</h3>'
        f'<p>{html.escape(meaning)}</p>'
        '</article>'
        for label, value, meaning in [
            ("cycle", report.get("cycle_id") or persisted.get("cycle_id"), "The run boundary tying all rows together."),
            ("seed", persisted.get("seed_id"), "The precise market slice fed to the scout."),
            ("scout", persisted.get("scout_id"), "The Layer 1 model run and harness output."),
            ("hypothesis", persisted.get("hypothesis_id"), "The scout's normalized claim row."),
            ("strings", info_ids, "Causal memory objects used by synthesis and geometry."),
            ("event intelligence", event_ids, "Event bundle derived from tool evidence."),
            ("node intelligence", node_ids, "Node/Hydromancer observation bundle."),
            ("tool proposals", proposal_ids, "Missing or improvable tool surfaces for tool-creation agents."),
            ("geometry cell", top_cell.get("cell_key"), "The top projected map cell."),
            ("mutation", mutation.get("mutation_id"), "The proposed next research-policy change."),
        ]
    )
    final_snapshot = {
        "verdict": "ground_up_smoke_passed" if layer_pass == layer_total else "ground_up_smoke_has_failures",
        "important_boundary": "This is a routed research result, not a final trade recommendation.",
        "cycle_id": report.get("cycle_id") or persisted.get("cycle_id"),
        "input_cell": {
            "entity": prompt.get("entity") or persisted.get("entity"),
            "horizon": prompt.get("horizon") or persisted.get("horizon"),
            "lens": prompt.get("lens") or persisted.get("lens"),
            "bias_mode": prompt.get("bias_mode") or persisted.get("bias_mode"),
            "theme": prompt.get("theme") or "validator_unstake",
            "as_of_utc": prompt.get("as_of_utc"),
        },
        "tool_inputs": {
            "allowed_tool_count": len(prompt.get("allowed_tools") or []),
            "evidence_receipts": evidence_ids,
        },
        "model_outputs": {
            "hypothesis": persisted.get("hypothesis_text"),
            "confidence": persisted.get("confidence"),
            "information_string_ids": info_ids,
            "event_intelligence_ids": event_ids,
            "node_intelligence_ids": node_ids,
            "tool_proposal_ids": proposal_ids,
            "quality_flags": persisted.get("quality_flags"),
        },
        "synthesis": {
            "synthesis_id": synthesis.get("synthesis_id"),
            "promoted_hypotheses": synthesis.get("promoted_hypotheses"),
            "promoted_string_ids": synthesis.get("promoted_string_ids"),
        },
        "geometry": {
            "top_cell": top_cell.get("cell_key"),
            "route_directive": route,
            "trade_scream_score": top_metrics.get("trade_scream_score"),
            "verifier_readiness": top_metrics.get("verifier_readiness"),
            "quality_flags": top_cell.get("quality_flags"),
        },
        "evolution": {
            "evaluator_score": best_eval.get("score"),
            "mutation_kind": mutation.get("mutation_kind"),
            "mutation_id": mutation.get("mutation_id"),
            "experiment_id": experiment.get("id"),
        },
    }
    return f"""
        <section class="chapter">
          <div class="chapter-head">
            <div class="chapter-label">12 / final results</div>
            <h2>What actually came out of the run.</h2>
            <p>{html.escape(result_sentence)}</p>
          </div>
          <div class="score-tape">{top_line_cards}</div>
          <div class="output-grid" style="margin-top:10px">{final_cards}</div>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>All result IDs</h3>
            <p>These are the durable handles you can chase from the pretty story down to raw data.</p>
            <div class="persist-grid">{id_rows}</div>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Final run snapshot</h3>
            <p>The same result as a compact object: inputs, outputs, stored IDs, route, and evolution result.</p>
            <pre class="prompt-slice">{html.escape(json.dumps(final_snapshot, indent=2, sort_keys=True, ensure_ascii=True, default=str))}</pre>
          </article>
        </section>
    """


def _render_geometry_evolution_chapter(data: dict[str, Any]) -> str:
    geometry = _artifact_json(data, "alpha_geometry")
    cortex_review = _artifact_json(data, "alpha_geometry_cortex_review")
    cortex_mutation_path = _artifact_json(data, "geometry_cortex_mutation_path")
    task_dispatch = _artifact_json(data, "evolution_control_task_dispatch")
    task_execution = _artifact_json(data, "cortex_task_worker_execution")
    task_feedback = _artifact_json(data, "cortex_task_feedback_evaluator")
    evolve = _artifact_json(data, "market_evolve_step")
    hard_experiment = _artifact_json(data, "market_evolve_hard_experiment")
    lineage = _artifact_json(data, "market_evolve_lineage")
    research_evolution = _artifact_json(data, "research_evolution_payload")
    evolution_control = (
        research_evolution.get("evolution_control")
        if isinstance(research_evolution.get("evolution_control"), dict)
        else {}
    )
    control_proof = (
        evolution_control.get("proof")
        if isinstance(evolution_control.get("proof"), dict)
        else {}
    )
    cells = [x for x in (geometry.get("cells") or []) if isinstance(x, dict)]
    top = geometry.get("top_cell") if isinstance(geometry.get("top_cell"), dict) else (cells[0] if cells else {})
    metrics = top.get("metrics") if isinstance(top.get("metrics"), dict) else {}
    coords = top.get("coordinates") if isinstance(top.get("coordinates"), dict) else {}
    best = evolve.get("best_evaluation") if isinstance(evolve.get("best_evaluation"), dict) else {}
    evaluator_metrics = best.get("metrics") if isinstance(best.get("metrics"), dict) else {}
    mutations = [x for x in (evolve.get("mutations") or []) if isinstance(x, dict)]
    plans = [x for x in (evolve.get("experiment_plans") or []) if isinstance(x, dict)]
    directives = [x for x in (geometry.get("directives") or []) if isinstance(x, dict)]
    global_metrics = geometry.get("global_metrics") if isinstance(geometry.get("global_metrics"), dict) else {}
    action_plan = geometry.get("action_plan") if isinstance(geometry.get("action_plan"), dict) else {}
    routing_queue = [
        x for x in (
            geometry.get("routing_queue")
            or action_plan.get("routing_queue")
            or []
        )
        if isinstance(x, dict)
    ]
    cortex_next_step = (
        geometry.get("cortex_next_step")
        if isinstance(geometry.get("cortex_next_step"), dict)
        else action_plan.get("cortex_next_step") if isinstance(action_plan.get("cortex_next_step"), dict) else {}
    )
    cortex_toolkit = [
        x for x in (
            geometry.get("cortex_toolkit")
            or action_plan.get("cortex_toolkit")
            or []
        )
        if isinstance(x, dict)
    ]
    shape_health = (
        cortex_review.get("shape_health")
        if isinstance(cortex_review.get("shape_health"), dict)
        else {}
    )
    cortex_diagnostics = [
        x for x in (cortex_review.get("diagnostics") or [])
        if isinstance(x, dict)
    ]
    cortex_work_orders = [
        x for x in (cortex_review.get("cortex_work_orders") or [])
        if isinstance(x, dict)
    ]
    proposed_policy = (
        cortex_review.get("proposed_geometry_policy")
        if isinstance(cortex_review.get("proposed_geometry_policy"), dict)
        else {}
    )

    if not cells and not evolve:
        return """
        <section class="chapter">
          <div class="chapter-head">
            <div class="chapter-label">11 / geometry and evolution</div>
            <h2>The shape layer has not been exported for this run yet.</h2>
            <p>Run the latest ground-up smoke harness to emit alpha_geometry.json and market_evolve_step.json.</p>
          </div>
        </section>
        """

    dot_html = []
    for idx, cell in enumerate(cells[:10], start=1):
        cell_coords = cell.get("coordinates") if isinstance(cell.get("coordinates"), dict) else {}
        cell_metrics = cell.get("metrics") if isinstance(cell.get("metrics"), dict) else {}
        x = _clamp(float(cell_coords.get("x_source_independence") or 0.0), 0.0, 1.0)
        y = _clamp(float(cell_coords.get("y_frontier_pressure") or 0.0), 0.0, 1.0)
        support = _clamp(float(cell_coords.get("size_support_mass") or cell_metrics.get("support_mass") or 0.2), 0.0, 1.0)
        fragility = _clamp(float(cell_coords.get("color_fragility") or cell_metrics.get("fragility") or 0.0), 0.0, 1.0)
        size = round(18 + support * 34)
        left = round(7 + x * 84, 2)
        bottom = round(9 + y * 76, 2)
        opacity = round(0.92 - fragility * 0.42, 3)
        route = str(cell.get("route_directive") or "observe")
        label = str(cell.get("theme") or cell.get("cell_key") or f"cell {idx}")
        dot_html.append(
            f'<div class="geo-dot {html.escape(route)}" '
            f'style="left:{left}%;bottom:{bottom}%;width:{size}px;height:{size}px;opacity:{opacity}" '
            f'title="{html.escape(label)} | {html.escape(route)}">'
            f'<b>{idx}</b><span>{html.escape(route.replace("_", " "))}</span></div>'
        )
    plane_html = "".join(dot_html)

    route_cards = "".join(
        '<article class="output-panel">'
        f'<span>{html.escape(str(d.get("route_directive") or "route").replace("_", " "))}</span>'
        f'<h3>{html.escape(str(d.get("theme") or d.get("cell_key") or "market cell"))}</h3>'
        f'<p>{html.escape(str(d.get("why") or "The geometry requested another pass."))}</p>'
        '</article>'
        for d in directives[:4]
    ) or (
        '<article class="output-panel"><span>observe</span><h3>No urgent route directive</h3>'
        '<p>The exported cells are being watched, but none crossed the verify/repair/widen threshold.</p></article>'
    )

    mutation = mutations[0] if mutations else {}
    mutation_kind = str(mutation.get("mutation_kind") or "no mutation exported")
    mutation_payload = mutation.get("mutation") if isinstance(mutation.get("mutation"), dict) else {}
    experiment = plans[0] if plans else {}
    criteria = experiment.get("success_criteria") if isinstance(experiment.get("success_criteria"), dict) else {}
    mutation_proof = (
        mutation_payload.get("_evolution_proof")
        if isinstance(mutation_payload.get("_evolution_proof"), dict)
        else criteria.get("mutation_intent") if isinstance(criteria.get("mutation_intent"), dict) else {}
    )
    matched = experiment.get("matched_slice") if isinstance(experiment.get("matched_slice"), dict) else {}
    top_story = _geometry_route_sentence(str(top.get("route_directive") or "observe"))
    score_cards = "".join(
        f'<div><strong>{html.escape(value)}</strong><small>{html.escape(label)}</small></div>'
        for label, value in [
            ("geometry cells", str(len(cells))),
            ("top scream", _pct(metrics.get("trade_scream_score") or top.get("trade_scream_score"))),
            ("verifier readiness", _pct(metrics.get("verifier_readiness") or top.get("verifier_readiness"))),
            ("evaluator score", _pct(best.get("score"))),
        ]
    )
    coordinate_cards = "".join(
        f'<div><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>'
        for label, value in [
            ("X", f"source independence {_pct(coords.get('x_source_independence'))}"),
            ("Y", f"frontier pressure {_pct(coords.get('y_frontier_pressure'))}"),
            ("Z", f"tension {_pct(coords.get('z_tension'))}"),
            ("Color", f"fragility {_pct(coords.get('color_fragility'))}"),
            ("Size", f"support mass {_pct(coords.get('size_support_mass'))}"),
            ("Route", str(top.get("route_directive") or "observe").replace("_", " ")),
        ]
    )
    geometry_control_cards = "".join(
        f'<div><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>'
        for label, value in [
            ("action rate", _pct(evaluator_metrics.get("geometry_route_action_rate"))),
            ("observe rate", _pct(evaluator_metrics.get("geometry_observe_rate"))),
            ("route entropy", _pct(evaluator_metrics.get("geometry_route_entropy"))),
            ("fragile verify", _pct(evaluator_metrics.get("fragile_verify_rate"))),
            ("quiet high-signal", _pct(evaluator_metrics.get("high_signal_observe_rate"))),
        ]
    )
    route_queue_card_parts = []
    for idx, item in enumerate(routing_queue[:3], start=1):
        queue_payload = {
            "route_task_id": item.get("route_task_id"),
            "route_directive": item.get("route_directive"),
            "must_call_first": item.get("must_call_first"),
            "tool_sequence": item.get("tool_sequence"),
            "missing_edges": item.get("missing_edges"),
            "success_gate": item.get("success_gate"),
        }
        route_queue_card_parts.append(
            '<article class="output-panel">'
            f'<span>rank {html.escape(str(item.get("rank") or idx))} / {html.escape(str(item.get("owner") or "cortex"))}</span>'
            f'<h3>{html.escape(str(item.get("action") or "route next").replace("_", " "))}</h3>'
            f'<p>{html.escape(str(item.get("reason") or "The shape selected this cell for the next pass."))}</p>'
            f'<pre class="prompt-slice">{html.escape(json.dumps(queue_payload, indent=2, sort_keys=True, ensure_ascii=True, default=str))}</pre>'
            '</article>'
        )
    route_queue_cards = "".join(route_queue_card_parts) or (
        '<article class="output-panel"><span>routing queue</span><h3>No routed work exported</h3>'
        '<p>The shape did not emit a ranked cortex work order in this artifact.</p></article>'
    )
    cortex_toolkit_cards = "".join(
        '<div>'
        f'<span>{html.escape(str(item.get("purpose") or "tool"))}</span>'
        f'<strong>{html.escape(str(item.get("tool_uri") or ""))}</strong>'
        '</div>'
        for item in cortex_toolkit[:6]
    )
    shape_health_cards = "".join(
        f'<div><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>'
        for label, value in [
            ("routed cells", str(shape_health.get("routed_cell_count", ""))),
            ("route queue", str(shape_health.get("routing_queue_length", ""))),
            ("route action rate", _pct(shape_health.get("route_action_rate"))),
            ("contract success", _pct(shape_health.get("route_contract_success_rate"))),
            ("fragile verify", _pct(shape_health.get("fragile_verify_rate"))),
            ("quiet high-signal", _pct(shape_health.get("high_signal_observe_rate"))),
        ]
    )
    diagnostic_cards = "".join(
        '<article class="output-panel">'
        f'<span>{html.escape(str(item.get("severity") or "diagnostic"))}</span>'
        f'<h3>{html.escape(str(item.get("code") or "shape review").replace("_", " "))}</h3>'
        f'<p>{html.escape(str(item.get("diagnosis") or ""))}</p>'
        '</article>'
        for item in cortex_diagnostics[:4]
    ) or (
        '<article class="output-panel"><span>shape review</span><h3>No cortex review exported</h3>'
        '<p>Run the latest smoke harness to emit alpha_geometry_cortex_review.json.</p></article>'
    )
    work_order_payload = [
        {
            "order_id": item.get("order_id"),
            "owner": item.get("owner"),
            "action": item.get("action"),
            "route_task_id": item.get("route_task_id"),
            "success_gate": item.get("success_gate"),
        }
        for item in cortex_work_orders[:6]
    ]
    policy_patch_payload = {
        "mutation_kind_hint": proposed_policy.get("mutation_kind_hint"),
        "target_metrics": proposed_policy.get("target_metrics"),
        "falsification_gates": proposed_policy.get("falsification_gates"),
        "policy_patch": proposed_policy.get("policy_patch"),
    }
    cortex_mutation_payload = {
        "mutation_kind": cortex_mutation_path.get("mutation_kind"),
        "mutation_source": cortex_mutation_path.get("mutation_source"),
        "diagnostic_codes": cortex_mutation_path.get("diagnostic_codes"),
        "policy_patch": cortex_mutation_path.get("policy_patch"),
        "proof": cortex_mutation_path.get("proof"),
    }
    production_control_payload = {
        "schema_version": evolution_control.get("schema_version"),
        "source": evolution_control.get("source"),
        "shape_can_direct_next": control_proof.get("shape_can_direct_next"),
        "diagnostic_codes": control_proof.get("diagnostic_codes"),
        "lineage_frontier_count": control_proof.get("lineage_frontier_count"),
        "mutation_kind_hint": control_proof.get("mutation_kind_hint"),
        "policy_patch_present": control_proof.get("policy_patch_present"),
    }
    dispatch_payload = {
        "schema_version": (task_dispatch.get("dispatch") or {}).get("schema_version")
        if isinstance(task_dispatch.get("dispatch"), dict) else None,
        "status": (task_dispatch.get("dispatch") or {}).get("status")
        if isinstance(task_dispatch.get("dispatch"), dict) else None,
        "posted_count": (task_dispatch.get("dispatch") or {}).get("posted_count")
        if isinstance(task_dispatch.get("dispatch"), dict) else None,
        "existing_count": (task_dispatch.get("dispatch") or {}).get("existing_count")
        if isinstance(task_dispatch.get("dispatch"), dict) else None,
        "tasks": [
            {
                "id": row.get("id"),
                "topic": row.get("topic"),
                "title": row.get("title"),
                "allowed_tools": row.get("allowed_tools"),
                "success_gate": (row.get("promotion_criteria") or {}).get("success_gate")
                if isinstance(row.get("promotion_criteria"), dict) else None,
                "stop_condition": (row.get("kill_criteria") or {}).get("stop_condition")
                if isinstance(row.get("kill_criteria"), dict) else None,
            }
            for row in (task_dispatch.get("tasks") or [])[:6]
            if isinstance(row, dict)
        ],
    }
    worker_execution = (
        task_execution.get("execution")
        if isinstance(task_execution.get("execution"), dict)
        else {}
    )
    worker_proof = task_execution.get("proof") if isinstance(task_execution.get("proof"), dict) else {}
    worker_tasks = [
        row for row in (task_execution.get("tasks") or [])
        if isinstance(row, dict)
    ]
    worker_events = [
        row for row in (task_execution.get("events") or [])
        if isinstance(row, dict)
    ]
    worker_payload = {
        "schema_version": worker_execution.get("schema_version"),
        "task_count": worker_execution.get("task_count"),
        "claimed_count": worker_execution.get("claimed_count"),
        "completed_count": worker_execution.get("completed_count"),
        "failed_count": worker_execution.get("failed_count"),
        "execute_followup_tools": worker_execution.get("execute_followup_tools"),
        "bounded_followup_tools_enabled": worker_proof.get("bounded_followup_tools_enabled"),
        "followup_observation_count": worker_proof.get("followup_observation_count"),
        "shape_tool_observed": worker_proof.get("shape_tool_observed"),
        "tasks": [
            {
                "id": row.get("id"),
                "topic": row.get("topic"),
                "status": row.get("status"),
                "owner_agent_id": row.get("owner_agent_id"),
                "completed_at": row.get("completed_at"),
            }
            for row in worker_tasks[:6]
        ],
        "event_flow": [
            {
                "event_type": row.get("event_type"),
                "task_id": row.get("task_id"),
                "agent_id": row.get("agent_id"),
            }
            for row in worker_events[:10]
        ],
        "first_observations": [
            {
                "uri": obs.get("uri"),
                "ok": obs.get("ok"),
                "tool_call_log_id": obs.get("tool_call_log_id"),
                "summary": obs.get("summary"),
                "phase": obs.get("phase"),
            }
            for execution in (worker_execution.get("executions") or [])[:3]
            if isinstance(execution, dict)
            for obs in (execution.get("observations") or [])[:6]
            if isinstance(obs, dict)
        ][:10],
        "execution_quality_flags": [
            flag
            for execution in (worker_execution.get("executions") or [])
            if isinstance(execution, dict)
            for flag in (execution.get("quality_flags") or [])
        ][:10],
    }
    feedback_metrics = task_feedback.get("metrics") if isinstance(task_feedback.get("metrics"), dict) else {}
    feedback_payload = {
        "schema_version": task_feedback.get("schema_version"),
        "score": task_feedback.get("score"),
        "proof": task_feedback.get("proof"),
        "metrics": {
            key: feedback_metrics.get(key)
            for key in [
                "cortex_task_count",
                "cortex_task_completion_rate",
                "cortex_task_failure_rate",
                "cortex_task_pending_rate",
                "cortex_shape_observation_rate",
                "cortex_observations_per_task",
                "cortex_deferred_followup_rate",
                "cortex_followup_execution_rate",
                "cortex_followup_observations_per_task",
                "cortex_shape_blocked_followup_rate",
            ]
        },
    }
    proof_target = mutation_proof.get("target_metrics") if isinstance(mutation_proof.get("target_metrics"), dict) else {}
    proof_changed = mutation_proof.get("changed_paths") if isinstance(mutation_proof.get("changed_paths"), list) else []
    proof_gates = mutation_proof.get("falsification_gates") if isinstance(mutation_proof.get("falsification_gates"), list) else []
    proof_payload = {
        "schema_version": mutation_proof.get("schema_version"),
        "mutation_kind": mutation_proof.get("mutation_kind") or mutation_kind,
        "hypothesis": mutation_proof.get("hypothesis"),
        "target_metrics": proof_target,
        "changed_paths": proof_changed[:8],
        "falsification_gates": proof_gates[:8],
        "kill_signal": mutation_proof.get("kill_signal") or criteria.get("kill_signal"),
    }
    hard_cycles = [x for x in (hard_experiment.get("cycles") or []) if isinstance(x, dict)]
    hard_final = (
        hard_cycles[-1].get("result")
        if hard_cycles and isinstance(hard_cycles[-1].get("result"), dict)
        else {}
    )
    hard_gate_payload = {
        "experiment_id": hard_experiment.get("experiment_id") or experiment.get("id"),
        "final_decision": hard_experiment.get("final_decision") or hard_final.get("decision"),
        "final_score_delta": hard_experiment.get("final_score_delta") or hard_final.get("score_delta"),
        "quality_flags": hard_final.get("quality_flags") or [],
        "falsification_gate_results": hard_final.get("falsification_gate_results") or [],
    }
    lineage_nodes = [x for x in (lineage.get("nodes") or []) if isinstance(x, dict)]
    lineage_edges = [x for x in (lineage.get("edges") or []) if isinstance(x, dict)]
    lineage_frontier = [x for x in (lineage.get("frontier") or []) if isinstance(x, dict)]
    candidate_contract_parts = []
    for idx, row in enumerate(mutations[:4], start=1):
        mutation_body = row.get("mutation") if isinstance(row.get("mutation"), dict) else {}
        proof = (
            mutation_body.get("_evolution_proof")
            if isinstance(mutation_body.get("_evolution_proof"), dict)
            else {}
        )
        population_gate = proof.get("population_gate") if isinstance(proof.get("population_gate"), dict) else {}
        gates = proof.get("falsification_gates") if isinstance(proof.get("falsification_gates"), list) else []
        changed = proof.get("changed_paths") if isinstance(proof.get("changed_paths"), list) else []
        target = proof.get("target_metrics") if isinstance(proof.get("target_metrics"), dict) else {}
        stat_html = "".join(
            f'<div><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>'
            for label, value in [
                ("source", str(proof.get("mutation_source") or "metric heuristic").replace("_", " ")),
                ("candidate rank", str(proof.get("source_candidate_rank") if proof.get("source_candidate_rank") is not None else idx - 1)),
                ("population gate", str(population_gate.get("reason") or "unknown")),
                ("open siblings", f"{population_gate.get('open_experiment_count', '?')}/{population_gate.get('max_open_experiments_per_parent', '?')}"),
                ("kill gates", str(len(gates))),
                ("target metrics", str(len(target))),
            ]
        )
        change_html = "".join(
            "<li>"
            f"<strong>{html.escape(str(item.get('path') or 'policy path'))}</strong>"
            f"<span>{html.escape(_compact_value(item.get('before'), limit=80))} -> {html.escape(_compact_value(item.get('after'), limit=80))}</span>"
            "</li>"
            for item in changed[:4]
            if isinstance(item, dict)
        ) or "<li><strong>No explicit changed paths exported</strong><span>The candidate still carries a proof packet if this is a legacy artifact.</span></li>"
        gate_html = "".join(
            "<li>"
            f"<strong>{html.escape(str(gate.get('metric') or 'gate'))} {html.escape(str(gate.get('operator') or ''))} {html.escape(_compact_value(gate.get('threshold'), limit=80))}</strong>"
            f"<span>{html.escape(str(gate.get('decision') or 'reject_or_continue_candidate'))}</span>"
            "</li>"
            for gate in gates[:4]
            if isinstance(gate, dict)
        ) or "<li><strong>No falsification gates exported</strong><span>This candidate should not promote without explicit gates.</span></li>"
        detail_payload = {
            "mutation_id": row.get("mutation_id"),
            "parent_program_id": row.get("parent_program_id"),
            "child_program_id": row.get("child_program_id"),
            "hypothesis": proof.get("hypothesis"),
            "intended_effect": proof.get("intended_effect"),
            "target_metrics": target,
            "promotion_evidence_required": proof.get("promotion_evidence_required") or [],
        }
        candidate_contract_parts.append(
            '<article class="output-panel">'
            f'<span>candidate {idx} / {html.escape(str(row.get("status") or "proposed"))}</span>'
            f'<h3>{html.escape(str(row.get("mutation_kind") or proof.get("mutation_kind") or "policy mutation").replace("_", " "))}</h3>'
            f'<p>{html.escape(str(proof.get("hypothesis") or row.get("rationale") or "Candidate must explain why this research policy should improve."))}</p>'
            f'<div class="contract-grid">{stat_html}</div>'
            '<div class="contract-list"><h4>Would change</h4><ul>'
            f'{change_html}'
            '</ul></div>'
            '<div class="contract-list"><h4>What kills it</h4><ul>'
            f'{gate_html}'
            '</ul></div>'
            f'<pre class="prompt-slice">{html.escape(json.dumps(detail_payload, indent=2, sort_keys=True, ensure_ascii=True, default=str))}</pre>'
            '</article>'
        )
    candidate_contract_cards = "".join(candidate_contract_parts) or (
        '<article class="output-panel"><span>candidate arena</span><h3>No candidate contracts exported</h3>'
        '<p>Run MarketEvolve after a scored cycle so candidate mutations carry proof, kill, and promotion contracts.</p></article>'
    )
    lineage_card_parts = []
    for node in lineage_nodes[:3]:
        node_payload = {
            "program_id": node.get("program_id"),
            "score": node.get("score"),
            "diversity_signature": node.get("diversity_signature"),
            "mutation_source": node.get("mutation_source"),
            "source_candidate_rank": node.get("source_candidate_rank"),
            "mutation_hypothesis": node.get("mutation_hypothesis"),
            "intended_effect": node.get("intended_effect"),
            "kill_signal": node.get("kill_signal"),
            "promotion_evidence_required": node.get("promotion_evidence_required"),
            "falsification_gate_count": node.get("falsification_gate_count"),
            "population_gate": node.get("population_gate"),
            "proof_gate_summary": node.get("proof_gate_summary"),
        }
        lineage_sentence = (
            f"source={node.get('mutation_source') or 'seed'}; "
            f"rank={node.get('source_candidate_rank') if node.get('source_candidate_rank') is not None else '-'}; "
            f"decision={node.get('latest_decision') or 'pending'}; "
            f"kill={_compact_value(node.get('kill_signal') or 'none exported', limit=170)}"
        )
        lineage_card_parts.append(
            '<article class="output-panel">'
            f'<span>{html.escape(str(node.get("status") or "program"))} / gen {html.escape(str(node.get("generation") or 0))}</span>'
            f'<h3>{html.escape(str(node.get("name") or node.get("program_id") or "program"))}</h3>'
            f'<p>{html.escape(lineage_sentence)}</p>'
            f'<pre class="prompt-slice">{html.escape(json.dumps(node_payload, indent=2, sort_keys=True, ensure_ascii=True, default=str))}</pre>'
            '</article>'
        )
    lineage_cards = "".join(lineage_card_parts) or (
        '<article class="output-panel"><span>lineage</span><h3>No lineage graph exported</h3>'
        '<p>Run the hard experiment layer to emit market_evolve_lineage.json.</p></article>'
    )
    frontier_payload = [
        {
            "program_id": row.get("program_id"),
            "status": row.get("status"),
            "frontier_priority_score": row.get("frontier_priority_score"),
            "mutation_kind": row.get("mutation_kind"),
            "mutation_source": row.get("mutation_source"),
            "source_candidate_rank": row.get("source_candidate_rank"),
            "latest_decision": row.get("latest_decision"),
            "latest_score_delta": row.get("latest_score_delta"),
            "next_action": row.get("next_action"),
            "kill_signal": row.get("kill_signal"),
            "falsification_gate_count": row.get("falsification_gate_count"),
            "population_gate": row.get("population_gate"),
            "proof_gate_summary": row.get("proof_gate_summary"),
        }
        for row in lineage_frontier[:6]
    ]
    evolution_cards = "".join(
        '<article class="output-panel">'
        f'<span>{html.escape(label)}</span>'
        f'<h3>{html.escape(title)}</h3>'
        f'<p>{html.escape(body)}</p>'
        '</article>'
        for label, title, body in [
            (
                "Evaluator",
                f"Score {_pct(best.get('score'))}, gates {'passed' if best.get('passed') else 'blocked'}",
                str(best.get("rationale") or "The evaluator scored string yield, source breadth, geometry readiness, fragility, tool activation, and learned-tool usage."),
                ),
                (
                    "First candidate",
                    mutation_kind.replace("_", " "),
                    str(mutation.get("rationale") or "No mutation rationale was exported."),
                ),
            (
                "Hard experiment",
                str(experiment.get("experiment_kind") or "matched policy A/B"),
                (
                    f"Assign {matched.get('unit') or 'seed cells'} by {matched.get('assignment') or 'deterministic hash'}; "
                    f"primary metric: {criteria.get('primary_metric') or 'accepted coverage per dollar'}; "
                    f"min seeds per arm: {matched.get('min_seeds_per_arm') or '?'}."
                ),
            ),
        ]
    )
    return f"""
        <section class="chapter">
          <div class="chapter-head">
            <div class="chapter-label">11 / geometry and evolution</div>
            <h2>The map shape decides what the system should do next.</h2>
            <p>RRGs make rotation visible. This layer does the same for research: each cell is placed by source independence and frontier pressure, lifted by tension, sized by support, colored by fragility, and converted into a route directive. The outcome is not always "trade now"; sometimes the high-signal answer is "widen scouts" or "exploit the learned node tool before spending more."</p>
          </div>
          <div class="score-tape">{score_cards}</div>
          <div class="first-layer" style="margin-top:10px">
            <article class="workbench-panel">
              <h3>Alpha-geometry field</h3>
              <p>Dots higher and farther right have better frontier pressure and source independence. Larger dots have more support. Dimmer dots are more fragile.</p>
              <div class="geometry-plane">
                <div class="geo-axis geo-y">frontier pressure</div>
                <div class="geo-axis geo-x">source independence</div>
                <div class="geo-cross"></div>
                {plane_html}
              </div>
            </article>
            <article class="workbench-panel">
              <h3>Top cell reading</h3>
              <p>{html.escape(top_story)}</p>
              <div class="workbench-grid">{coordinate_cards}</div>
            </article>
          </div>
          <div class="output-grid" style="margin-top:10px">{route_cards}</div>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Cortex geometry controls</h3>
            <p>The evaluator now audits the shape itself: whether the field is routing enough cells, staying too quiet around clean high-signal cells, or sending fragile cells to verifier spend. Those diagnostics can mutate geometry weights and verifier thresholds, so the cortex can improve the map it is reading.</p>
            <div class="workbench-grid">{geometry_control_cards}</div>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Cortex shape review</h3>
            <p>This is the meta layer: the cortex reads the geometry as an object, grades whether the field is mute, brittle, or executable, and emits the policy pressure that MarketEvolve can test.</p>
            <div class="workbench-grid">{shape_health_cards}</div>
          </article>
          <div class="output-grid" style="margin-top:10px">{diagnostic_cards}</div>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Geometry policy patch</h3>
            <p>The review does not silently change the system. It produces a patch, target metrics, and kill gates that have to survive matched experiments before promotion.</p>
            <pre class="prompt-slice">{html.escape(json.dumps(policy_patch_payload, indent=2, sort_keys=True, ensure_ascii=True, default=str))}</pre>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Cortex-driven mutation proof</h3>
            <p>The smoke includes a bad-shape fixture: when route contracts are observed failing, the cortex review is allowed to become a MarketEvolve candidate mutation with its source diagnostics preserved in the proof packet.</p>
            <pre class="prompt-slice">{html.escape(json.dumps(cortex_mutation_payload, indent=2, sort_keys=True, ensure_ascii=True, default=str))}</pre>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Production control plane</h3>
            <p>The monitor now reads the same shared control payload as the swarm manifest. This is the operator-facing proof that the shape, cortex review, and evolution frontier can be inspected from the persisted run instead of only from smoke-only artifacts.</p>
            <pre class="prompt-slice">{html.escape(json.dumps(production_control_payload, indent=2, sort_keys=True, ensure_ascii=True, default=str))}</pre>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Dispatchable cortex tasks</h3>
            <p>The control plane is now writable at the right point in the cycle: route and cortex work orders become task contracts with allowed tools, success gates, stop conditions, budgets, and blackboard events. Re-running the dispatch is idempotent, so the cortex can keep inspecting without flooding the queue.</p>
            <pre class="prompt-slice">{html.escape(json.dumps(dispatch_payload, indent=2, sort_keys=True, ensure_ascii=True, default=str))}</pre>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Cortex worker execution</h3>
            <p>The next layer claims those contracts, starts them, reads the shape through the approved harness, then spends a bounded follow-up budget only after the shape read succeeds. In this run, the worker logged {html.escape(str(worker_proof.get("followup_observation_count") or 0))} follow-up observations and kept every observation tied to task events.</p>
            <pre class="prompt-slice">{html.escape(json.dumps(worker_payload, indent=2, sort_keys=True, ensure_ascii=True, default=str))}</pre>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Evaluator feedback from worker outcomes</h3>
            <p>The worker loop now feeds MarketEvolve directly. Completion rate, task failures, pending work, shape-observation rate, bounded follow-up execution, follow-up observation yield, and shape-blocked follow-up penalties become objective terms, so the system can evolve the harness instead of merely logging that it ran.</p>
            <pre class="prompt-slice">{html.escape(json.dumps(feedback_payload, indent=2, sort_keys=True, ensure_ascii=True, default=str))}</pre>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Cortex work order</h3>
            <p>The shape is converted into ranked tasks with an owner, first tool, follow-up tool sequence, missing edges, success gate, stop condition, and map-update rule. This is the part that lets the cortex look at the field and immediately know where to direct workers next.</p>
            <div class="workbench-grid">
              <div><span>next route task</span><strong>{html.escape(str(cortex_next_step.get("route_task_id") or "none"))}</strong></div>
              <div><span>primary owner</span><strong>{html.escape(str(cortex_next_step.get("primary_owner") or "cortex"))}</strong></div>
              <div><span>primary action</span><strong>{html.escape(str(cortex_next_step.get("primary_action") or "observe").replace("_", " "))}</strong></div>
              <div><span>queue length</span><strong>{len(routing_queue)}</strong></div>
            </div>
          </article>
          <div class="output-grid" style="margin-top:10px">{route_queue_cards}</div>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Shape-supervision work orders</h3>
            <p>These are the orders the cortex would hand to router, verifier, tool builder, or MarketEvolve after reviewing the map shape.</p>
            <pre class="prompt-slice">{html.escape(json.dumps(work_order_payload, indent=2, sort_keys=True, ensure_ascii=True, default=str))}</pre>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Cortex toolkit</h3>
            <p>The first tool is the native shape reader. Other tools are pulled from the action packet so the next worker can inspect, move, or falsify the edge instead of improvising.</p>
            <div class="workbench-grid">{cortex_toolkit_cards}</div>
          </article>
              <div class="chapter-head" style="margin-top:22px">
                <div class="chapter-label">11b / evaluator-guided improvement</div>
                <h2>The system then runs a small evolution arena.</h2>
                <p>MarketEvolve treats the run as evidence about the research program itself. It scores the current policy, proposes a bounded set of diverse candidate mutations, and creates matched A/B experiments so a child has to beat control out of sample before it becomes active.</p>
              </div>
              <div class="output-grid">{evolution_cards}</div>
              <article class="workbench-panel" style="margin-top:10px">
                <h3>Candidate proof contracts</h3>
                <p>Each child is born with a reason, a policy patch, target metrics, a population gate, promotion requirements, and explicit kill signals. This is the difference between "try a prompt" and a real evolving research program.</p>
              </article>
              <div class="output-grid" style="margin-top:10px">{candidate_contract_cards}</div>
              <article class="workbench-panel" style="margin-top:10px">
                <h3>Evolution proof packet</h3>
            <p>Every candidate mutation must name what it saw in the shape, what changed in the cortex policy, which metrics should move, and what evidence would kill the candidate. That is the AlphaEvolve-style loop: propose, test against control, promote only if the evidence survives.</p>
            <pre class="prompt-slice">{html.escape(json.dumps(proof_payload, indent=2, sort_keys=True, ensure_ascii=True, default=str))}</pre>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Hard experiment verdict</h3>
            <p>The proof packet is now executable: falsification gates are evaluated on the candidate-vs-control result and persisted with the experiment outcome. Passing the viewer means more than storing an intent; the candidate had to survive its own kill criteria.</p>
            <pre class="prompt-slice">{html.escape(json.dumps(hard_gate_payload, indent=2, sort_keys=True, ensure_ascii=True, default=str))}</pre>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Evolution lineage</h3>
            <p>The system now exports the ancestry graph: programs, mutation edges, proof-gate verdicts, diversity signatures, and the frontier queue that tells the cortex which policy family should mutate or continue next.</p>
            <div class="workbench-grid">
              <div><span>programs</span><strong>{len(lineage_nodes)}</strong></div>
              <div><span>mutation edges</span><strong>{len(lineage_edges)}</strong></div>
              <div><span>frontier items</span><strong>{len(lineage_frontier)}</strong></div>
              <div><span>active ids</span><strong>{html.escape(str(len(lineage.get("active_program_ids") or [])))}</strong></div>
            </div>
          </article>
          <div class="output-grid" style="margin-top:10px">{lineage_cards}</div>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Evolution frontier</h3>
            <p>This is the ranked next-program surface: it blends current score, generation, candidate decision, proof gates, and diversity signature so the cortex does not keep mutating the same narrow family forever.</p>
            <pre class="prompt-slice">{html.escape(json.dumps(frontier_payload, indent=2, sort_keys=True, ensure_ascii=True, default=str))}</pre>
          </article>
          <article class="workbench-panel" style="margin-top:10px">
            <h3>Global map metrics</h3>
            <pre class="prompt-slice">{html.escape(json.dumps(global_metrics, indent=2, sort_keys=True))}</pre>
          </article>
        </section>
    """


def _geometry_route_sentence(route: str) -> str:
    route = route or "observe"
    if route == "verify_now":
        return "The cell has enough source independence, support, and readiness to earn verifier spend now."
    if route == "widen_scouts":
        return "The cell is interesting but too thin. The map is asking for more independent scouts before it becomes a trade candidate."
    if route == "widen_sources":
        return "The cell has frontier pressure but needs source-family breadth before it can be trusted."
    if route == "repair_sources":
        return "The signal is fragile. The next move is source repair, not more conviction."
    if route == "resolve_tension":
        return "The cell contains disagreement worth attacking directly with a verifier or specialist."
    return "The cell is useful memory, but its shape does not yet justify extra spend."


def _prompt_excerpt(prompt_text: str) -> str:
    if not prompt_text:
        return ""
    keep: list[str] = []
    include = False
    for raw in prompt_text.splitlines():
        line = raw.rstrip()
        if line.startswith("as_of_utc=") or line.startswith("Freshness rule:"):
            keep.append(line)
        if line.startswith("Cell:"):
            include = True
        if include:
            keep.append(line)
        if line.startswith("prior_information_strings:"):
            include = False
        if line.startswith("atlas_policy:"):
            include = True
        if line.startswith("allowed_tool_candidates:"):
            include = True
        if line.startswith("Return one JSON object"):
            keep.append(line)
            break
    return "\n".join(dict.fromkeys(keep))[:2200]


def _render_system_map(layers: list[dict[str, Any]]) -> str:
    layer_status = {str(row.get("name")): str(row.get("status")) for row in layers}
    cards = []
    for idx, part in enumerate(SYSTEM_PARTS, start=1):
        touched = any(layer_status.get(layer) == "pass" for layer in part["layers"])
        state = "active" if touched else ""
        badge = "Touched in this run" if touched else "Broad-system target"
        cards.append(
            f'<article class="system-step {state}">'
            f'<div class="num">{idx:02d} | {html.escape(badge)}</div>'
            f'<h3>{html.escape(part["title"])}</h3>'
            f'<p>{html.escape(part["summary"])}</p>'
            '</article>'
        )
    return "\n".join(cards)


def _render_system_part_details() -> str:
    cards = []
    for part in SYSTEM_PARTS:
        cards.append(
            '<article class="part-card">'
            f'<div class="run-chip">{html.escape(part["title"])}</div>'
            f'<p>{html.escape(part["summary"])}</p>'
            '<div class="part-meta">'
            f'<div><span>Consumes</span><strong>{html.escape(part["input"])}</strong></div>'
            f'<div><span>Emits</span><strong>{html.escape(part["output"])}</strong></div>'
            '</div>'
            f'<p><strong>Why it exists:</strong> {html.escape(part["why"])}</p>'
            '</article>'
        )
    return "\n".join(cards)


def _render_signal_journey(data: dict[str, Any], story: dict[str, Any], summary: dict[str, Any]) -> str:
    evidence = _artifact_list(data, "live_scout_tool_evidence")
    evidence_count = len(evidence)
    strings = str(summary.get("strings", "?"))
    node_blocks = str(summary.get("node_intelligence", "?"))
    event_blocks = str(summary.get("event_intelligence", "?"))
    steps = [
        (
            "A clue arrives",
            "The system starts with something potentially important, not with a trade.",
            "Here: an aHYPE unstake event enters the desk as a market clue.",
        ),
        (
            "Talis asks for receipts",
            "Before reasoning, it gathers a small bounded evidence slice from approved tools.",
            f"Here: {evidence_count} evidence sources were attached, including event, Hydromancer, node, and market-state views.",
        ),
        (
            "A narrow scout reads one cell",
            "The scout is not asked to understand the whole market. It gets one entity, horizon, lens, and prompt variant.",
            f"Here: HYPE, intraday, on-chain, {story['prompt_variant']}.",
        ),
        (
            "The scout emits a causal string",
            "The output must say what changed, why it matters, what would happen next, and what would falsify it.",
            story["thesis"],
        ),
        (
            "The map stores the claim",
            "The system saves the claim with IDs, source refs, time horizon, quality flags, and node/event intelligence.",
            f"Here: {strings} information strings, {event_blocks} event bundle, and {node_blocks} node snapshots are queryable.",
        ),
        (
            "The attention layer decides what deserves more spend",
            "Broad scouts create breadth. The attention layer prevents the desk from chasing every noisy claim.",
            "Here: the run promotes the HYPE route-and-absorption hypothesis for deeper checking.",
        ),
        (
            "Verification knows what to inspect",
            "The next agent does not start cold. It receives the causal chain, evidence refs, and kill signal.",
            story["expected"],
        ),
        (
            "The user sees the story, with the audit trail nearby",
            "The product should feel simple, but every assertion should be traceable back to the raw prompt and output.",
            "Here: the beautiful layer explains the run; the raw JSON remains behind the audit drawer.",
        ),
    ]
    out = []
    for i, (title, explanation, run_note) in enumerate(steps, start=1):
        out.append(
            '<article class="journey-card">'
            f'<div class="mark">{i}</div>'
            '<div>'
            f'<h3>{html.escape(title)}</h3>'
            f'<p>{html.escape(explanation)}</p>'
            f'<em>{html.escape(run_note)}</em>'
            '</div>'
            '</article>'
        )
    return "\n".join(out)


def _render_scout_view(data: dict[str, Any], story: dict[str, Any]) -> str:
    prompt_text = str(((data.get("artifacts") or {}).get("live_scout_user_prompt") or {}).get("text") or "")
    parsed = _parse_user_prompt(prompt_text)
    evidence = _artifact_list(data, "live_scout_tool_evidence")
    cell_rows = [
        ("Entity", parsed.get("entity") or "HYPE"),
        ("Horizon", parsed.get("horizon") or "intraday"),
        ("Lens", parsed.get("lens") or "on_chain"),
        ("Bias", parsed.get("bias_mode") or "frontier"),
        ("Theme", parsed.get("theme") or "validator_unstake"),
        ("Clock", parsed.get("as_of_utc") or "?"),
    ]
    cell_html = "".join(
        f"<div><span>{html.escape(label)}</span><b>{html.escape(str(value))}</b></div>"
        for label, value in cell_rows
    )
    tools = parsed.get("allowed_tools") or []
    tool_html = "".join(f"<code>{html.escape(str(tool))}</code>" for tool in tools)
    evidence_html = "".join(
        '<article class="scout-card">'
        f'<span>{html.escape(str(item.get("tool_call_log_id") or "evidence"))}</span>'
        f'<h3>{html.escape(_source_title(str(item.get("uri") or "")))}</h3>'
        f'<p>{html.escape(_evidence_fact(item))}</p>'
        '</article>'
        for item in evidence[:4]
        if isinstance(item, dict)
    )
    return f"""
      <div class="scout-stack">
        <article class="scout-card">
          <span>Assigned cell</span>
          <h3>One narrow field of view</h3>
          <div class="cell-grid">{cell_html}</div>
        </article>
        <article class="scout-card">
          <span>Allowed tools</span>
          <h3>The scout cannot roam freely</h3>
          <p>It can suggest only these URIs, copied exactly.</p>
          <div class="tool-list">{tool_html}</div>
        </article>
      </div>
      <div class="scout-stack">
        <div class="prompt-window">
          <div class="prompt-top"><b>Exact user prompt sent to scout</b><span>{html.escape(str(len(prompt_text)))} chars</span></div>
          <pre class="prompt-body">{html.escape(prompt_text)}</pre>
        </div>
        <article class="scout-card">
          <span>What the scout must return</span>
          <h3>Not a trade. A falsifiable causal string.</h3>
          <p>It must emit a hypothesis, confidence, rationale, suggested tools, evidence refs, expected outcome, and a kill signal. For this run, the target conclusion was: {html.escape(story["headline"])}</p>
        </article>
      </div>
      <div class="scout-stack">{evidence_html}</div>
    """


def _render_conclusion_logic(data: dict[str, Any], story: dict[str, Any]) -> str:
    evidence = [x for x in _artifact_list(data, "live_scout_tool_evidence") if isinstance(x, dict)]
    facts = _conclusion_facts(evidence)
    logic_rows = [
        (
            "Potential supply",
            facts.get("event", "The event feed showed an unstake-like supply event."),
            "The scout may infer possible supply pressure, but only as a conditional setup.",
        ),
        (
            "Sellability test",
            facts.get("market", "Market-state evidence gives depth, OI, and crowding context."),
            "The event matters only if the supply can reach sellable liquidity before the window expires.",
        ),
        (
            "Actor quality test",
            facts.get("hydro", "Hydromancer evidence identifies whether meaningful wallets are active around the tape."),
            "High-quality actors can either amplify pressure or absorb it; the scout must check who is on the other side.",
        ),
        (
            "Node-state check",
            facts.get("node", "Node-native evidence shows order/reject state from our own source family."),
            "The scout treats our node as privileged evidence for whether flow quality looks healthy or stressed.",
        ),
        (
            "Conclusion",
            story["thesis"],
            story["headline"],
        ),
    ]
    row_html = []
    for left_title, left_body, right_body in logic_rows:
        row_html.append(
            '<div class="logic-row">'
            f'<article class="logic-card"><span>Input</span><h3>{html.escape(left_title)}</h3><p>{html.escape(left_body)}</p></article>'
            '<div class="logic-operator">so</div>'
            f'<article class="logic-card"><span>Allowed inference</span><p>{html.escape(right_body)}</p></article>'
            '</div>'
        )
    score_html = "".join(
        f'<div class="score-box"><strong>{html.escape(value)}</strong><small>{html.escape(label)}</small></div>'
        for label, value in [
            ("model confidence", story["confidence_label"].split(" ")[0]),
            ("conviction", story["conviction"]),
            ("novelty", story["novelty"]),
            ("crowdedness", story["crowdedness"]),
        ]
    )
    row_html.append(
        '<article class="logic-card">'
        '<span>Why it is not final truth</span>'
        '<h3>The claim stays conditional until the kill signal window resolves.</h3>'
        f'<p>{html.escape(story["kill_signal"])}</p>'
        f'<div class="score-row">{score_html}</div>'
        '</article>'
    )
    return "\n".join(row_html)


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


def _conclusion_facts(evidence: list[dict[str, Any]]) -> dict[str, str]:
    facts: dict[str, str] = {}
    for item in evidence:
        uri = str(item.get("uri") or "")
        fact = _evidence_fact(item)
        if "query_events_recent" in uri:
            facts["event"] = fact
        elif "hydromancer" in uri:
            facts["hydro"] = fact
        elif "hl_reject_corpus" in uri:
            facts["node"] = fact
        elif "query_timeseries" in uri:
            facts["market"] = fact
    return facts


def _artifact_json(data: dict[str, Any], key: str) -> dict[str, Any]:
    raw = ((data.get("artifacts") or {}).get(key) or {}).get("json")
    return raw if isinstance(raw, dict) else {}


def _artifact_list(data: dict[str, Any], key: str) -> list[Any]:
    raw = ((data.get("artifacts") or {}).get(key) or {}).get("json")
    return raw if isinstance(raw, list) else []


def _compact_value(value: Any, limit: int = 220) -> str:
    if value is None:
        text = ""
    elif isinstance(value, float):
        text = f"{value:.4f}"
    elif isinstance(value, (list, tuple, set)):
        text = ", ".join(_compact_value(v, limit=80) for v in value if _compact_value(v, limit=80))
    elif isinstance(value, dict):
        try:
            text = json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)
        except Exception:
            text = str(value)
    else:
        text = str(value)
    text = " ".join(text.split())
    if not text:
        return "none"
    if len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "..."
    return text


def _render_metric_cards(
    report: dict[str, Any],
    layers: list[dict[str, Any]],
    summary: dict[str, Any],
    story: dict[str, Any],
) -> str:
    cards = [
        ("Run", str(report.get("status") or "?").upper(), "All ground-up layers completed."),
        ("Layers", f"{sum(1 for row in layers if row.get('status') == 'pass')}/{len(layers)}", "Schema, scouts, synthesis, monitor."),
        ("Strings", str(summary.get("strings", "?")), "Persisted information strings."),
        ("Node Intel", str(summary.get("node_intelligence", "?")), "Hydromancer and node-native snapshots."),
        ("Conviction", story["conviction"], "Strength of the emitted causal string."),
        ("Attention", _pct(summary.get("avg_attention")), "Monitor attention score."),
    ]
    return "\n".join(
        f'<article class="metric"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong><small>{html.escape(note)}</small></article>'
        for label, value, note in cards
    )


def _render_chain(items: list[str]) -> str:
    return "".join(f"<span>{html.escape(str(item))}</span>" for item in items if item)


def _render_depth(layers: Any) -> str:
    if not isinstance(layers, list) or not layers:
        return '<div class="step"><i>1</i><p>No depth ladder emitted.</p></div>'
    out = []
    for i, layer in enumerate(layers[:5], start=1):
        claim = layer.get("claim") if isinstance(layer, dict) else str(layer)
        number = layer.get("layer") if isinstance(layer, dict) else i
        out.append(f'<div class="step"><i>{html.escape(str(number))}</i><p>{html.escape(str(claim or ""))}</p></div>')
    return "".join(out)


def _render_source_cards(data: dict[str, Any]) -> str:
    evidence = _artifact_list(data, "live_scout_tool_evidence")
    if not evidence:
        return '<article class="source-card"><div class="source-type">No evidence</div><h3>No tool evidence captured</h3><p>The raw audit trail may still contain prompt details.</p></article>'
    cards = []
    for item in evidence[:4]:
        if not isinstance(item, dict):
            continue
        uri = str(item.get("uri") or "")
        title = _source_title(uri)
        ref = str(item.get("tool_call_log_id") or "no ref")
        summary = _evidence_fact(item)
        cards.append(
            '<article class="source-card">'
            f'<div class="source-type">{html.escape(ref)}</div>'
            f'<h3>{html.escape(title)}</h3>'
            f'<p>{html.escape(summary)}</p>'
            '</article>'
        )
    return "".join(cards)


def _evidence_fact(item: dict[str, Any]) -> str:
    uri = str(item.get("uri") or "")
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    if "query_events_recent" in uri:
        event = ((result.get("events") or [{}])[0]) if isinstance(result.get("events"), list) else {}
        meta = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        return (
            f"{event.get('headline') or 'Event observed'} "
            f"Event time {event.get('event_time') or 'unknown'}; "
            f"label {meta.get('label') or 'unknown'}; "
            f"depth ratio {meta.get('depth_1pct_ratio') or 'unknown'}."
        )
    if "hydromancer" in uri:
        leader = ((result.get("leaders") or [{}])[0]) if isinstance(result.get("leaders"), list) else {}
        wallet = str(leader.get("wallet") or "wallet")[:12]
        return (
            f"Top observed wallet {wallet}... had realized PnL ${leader.get('realized_pnl_usd') or '?'} "
            f"with win rate {leader.get('win_rate_pct') or '?'}% and volume ${leader.get('volume_usd') or '?'}."
        )
    if "hl_reject_corpus" in uri:
        counts = result.get("status_counts") if isinstance(result.get("status_counts"), dict) else {}
        return (
            f"Our node saw reject rate {result.get('reject_rate_pct') or '?'}%, "
            f"{counts.get('filled') or '?'} filled vs {counts.get('rejected') or '?'} rejected, "
            f"source {result.get('source') or 'unknown'}."
        )
    if "query_timeseries" in uri:
        points = result.get("points") if isinstance(result.get("points"), list) else []
        metrics = []
        for point in points[:3]:
            if isinstance(point, dict):
                metrics.append(f"{point.get('metric')}={point.get('value')}")
        return "Market state snapshot: " + ", ".join(metrics) + "."
    summary = str(item.get("summary") or "")
    return summary[:177] + "..." if len(summary) > 180 else summary


def _source_title(uri: str) -> str:
    if "query_events_recent" in uri:
        return "Event feed"
    if "hydromancer" in uri:
        return "Hydromancer actor quality"
    if "hl_reject_corpus" in uri:
        return "Our HL node rejects"
    if "query_timeseries" in uri:
        return "Market state"
    return uri.rsplit("/", 1)[-1] or "Tool evidence"


def _pct(raw: Any) -> str:
    try:
        value = float(raw)
    except Exception:
        return "?"
    if value <= 1:
        value *= 100
    return f"{round(value)}%"


def _clamp(raw: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, raw))


def _render_panel(panel_id: str, title: str, data: dict[str, Any], key: str, *, active: bool = False) -> str:
    artifact = (data.get("artifacts") or {}).get(key) or {}
    content = artifact.get("text") or ""
    if artifact.get("kind") == "json" and artifact.get("json") is not None:
        content = json.dumps(artifact["json"], indent=2, sort_keys=True)
    return f"""
      <article class="panel {'active' if active else ''}" data-panel="{html.escape(panel_id)}">
        <div class="panel-head">
          <div><h3>{html.escape(title)}</h3><small>{html.escape(str(artifact.get("filename") or ""))} | {html.escape(str(artifact.get("bytes") or 0))} bytes</small></div>
          <button data-copy>Copy</button>
        </div>
        <pre>{html.escape(content)}</pre>
      </article>
    """.strip()


def _render_layer(row: dict[str, Any]) -> str:
    status = str(row.get("status") or "?")
    detail = json.dumps({k: v for k, v in row.items() if k not in {"name", "status"}}, sort_keys=True, default=str)[:260]
    return (
        '<article class="layer">'
        f'<h3>{html.escape(str(row.get("name") or "?"))} <span class="{html.escape(status)}">{html.escape(status)}</span></h3>'
        f"<p>{html.escape(detail)}</p>"
        "</article>"
    )


def _monitor_summary(data: dict[str, Any]) -> dict[str, Any]:
    monitor = ((data.get("artifacts") or {}).get("monitor_payload") or {}).get("json") or {}
    return monitor.get("summary") or {}


if __name__ == "__main__":
    raise SystemExit(main())
