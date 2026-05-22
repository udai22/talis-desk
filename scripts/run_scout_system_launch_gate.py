#!/usr/bin/env python
"""Run the first-layer launch gate from deterministic proof to live preflight.

This is the operator-facing wrapper around the scout/evolution stack. It keeps
the paid live path explicit while making the proof ladder repeatable:

1. deterministic 100-scout readiness slice,
2. phone-viewable readiness viewer export,
3. live-provider preflight or live canary under a hard cap,
4. tournament evaluation when live calls were actually made.

By default this script does not spend on model calls. Add ``--allow-live-spend``
only after the preflight report says the system is ready for the next paid gate.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class StageResult:
    name: str
    command: list[str]
    returncode: int
    elapsed_s: float
    stdout: str = ""
    stderr: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def main() -> int:
    args = _parse_args()
    repo = Path(__file__).resolve().parents[1]
    artifact_dir = (
        Path(args.artifact_dir).expanduser().resolve()
        if args.artifact_dir
        else _artifact_dir()
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    deterministic_dir = artifact_dir / "deterministic_100" / "prompt_outputs"
    live_dir = artifact_dir / "live_canary" / "prompt_outputs"
    tournament_dir = artifact_dir / "tournament"
    viewer_dir = (
        Path(args.viewer_output_dir).expanduser().resolve()
        if args.viewer_output_dir
        else artifact_dir / "scout-system-test"
    )
    launch_viewer_dir = (
        Path(args.launch_viewer_output_dir).expanduser().resolve()
        if args.launch_viewer_output_dir
        else artifact_dir / "launch-gate"
    )

    stages: list[StageResult] = []
    deterministic_cmd = [
        sys.executable,
        "scripts/run_100_scout_readiness_slice.py",
        "--n-scouts",
        str(args.deterministic_scouts),
        "--concurrency",
        str(args.deterministic_concurrency),
        "--cost-cap-usd",
        str(args.deterministic_cost_cap_usd),
        "--artifact-dir",
        str(artifact_dir / "deterministic_100"),
        "--prompt-output-dir",
        str(deterministic_dir),
    ]
    if args.cycle_prefix:
        deterministic_cmd.extend(["--cycle-id", f"{args.cycle_prefix}_deterministic_100"])
    deterministic = _run_stage("deterministic_100", deterministic_cmd, repo=repo)
    deterministic.artifacts.update(_parse_stdout_paths(deterministic.stdout))
    deterministic_report_path = _path_from_stage(deterministic, "SCOUT100_REPORT_JSON")
    deterministic.report = _read_json(deterministic_report_path)
    stages.append(deterministic)

    viewer_stage: StageResult | None = None
    deterministic_prompt_dir = _path_from_stage(deterministic, "SCOUT100_PROMPT_OUTPUT_DIR")
    if deterministic_report_path and deterministic_prompt_dir and deterministic.report:
        viewer_cmd = [
            sys.executable,
            "scripts/export_100_scout_system_viewer.py",
            str(deterministic_prompt_dir),
            "--report-json",
            str(deterministic_report_path),
            "--output-dir",
            str(viewer_dir),
        ]
        viewer_stage = _run_stage("viewer_export", viewer_cmd, repo=repo)
        viewer_stage.artifacts.update(_parse_stdout_paths(viewer_stage.stdout))
        stages.append(viewer_stage)

    live_cmd = [
        sys.executable,
        "scripts/run_live_scout_canary.py",
        "--n-scouts",
        str(args.live_scouts),
        "--concurrency",
        str(args.live_concurrency),
        "--cost-cap-usd",
        str(args.live_cost_cap_usd),
        "--provider-timeout-s",
        str(args.provider_timeout_s),
        "--max-tool-iterations",
        str(args.max_tool_iterations),
        "--artifact-dir",
        str(artifact_dir / "live_canary"),
        "--prompt-output-dir",
        str(live_dir),
    ]
    if args.prompt_variant:
        live_cmd.extend(["--prompt-variant", args.prompt_variant])
    if args.cycle_prefix:
        live_cmd.extend(["--cycle-id", f"{args.cycle_prefix}_live_{args.live_scouts}"])
    if args.allow_live_spend:
        live_cmd.append("--allow-live-spend")
    live = _run_stage("live_canary_or_preflight", live_cmd, repo=repo)
    live.artifacts.update(_parse_stdout_paths(live.stdout))
    live_report_path = _path_from_stage(live, "LIVE_CANARY_REPORT_JSON")
    live.report = _read_json(live_report_path)
    stages.append(live)

    tournament: StageResult | None = None
    if args.allow_live_spend and live_report_path and live.report.get("mode") != "preflight_no_live_spend":
        tournament_dir.mkdir(parents=True, exist_ok=True)
        tournament_cmd = [
            sys.executable,
            "scripts/evaluate_live_scout_tournament.py",
            str(live_report_path),
            "--output-dir",
            str(tournament_dir),
        ]
        tournament = _run_stage("live_tournament", tournament_cmd, repo=repo)
        tournament.artifacts.update(_parse_stdout_paths(tournament.stdout))
        tournament_report = tournament_dir / "live_scout_tournament_report.json"
        tournament.artifacts["LIVE_SCOUT_TOURNAMENT_REPORT_JSON"] = str(tournament_report)
        tournament.report = _read_json(tournament_report)
        stages.append(tournament)

    report = build_launch_gate_report(
        deterministic_report=deterministic.report,
        live_report=live.report,
        tournament_report=tournament.report if tournament else {},
        stages=stages,
        viewer_index=str(viewer_dir / "index.html") if viewer_dir.exists() else "",
        artifact_dir=str(artifact_dir),
        allow_live_spend=args.allow_live_spend,
        next_live_scouts=args.next_live_scouts,
    )
    report["launch_viewer_index"] = str(launch_viewer_dir / "index.html")
    report_path = artifact_dir / "scout_system_launch_gate_report.json"
    md_path = artifact_dir / "scout_system_launch_gate_report.md"
    _write_json(report_path, report)
    _write_text(md_path, render_launch_gate_markdown(report))
    export_launch_gate_viewer(
        report,
        output_dir=launch_viewer_dir,
        report_json=report_path,
        report_md=md_path,
    )
    print(f"SCOUT_SYSTEM_LAUNCH_DECISION={report['decision']['status']}")
    print(f"SCOUT_SYSTEM_LAUNCH_ALLOWED_NEXT={report['decision']['allowed_next_step']}")
    print(f"SCOUT_SYSTEM_LAUNCH_REPORT_JSON={report_path}")
    print(f"SCOUT_SYSTEM_LAUNCH_REPORT_MD={md_path}")
    print(f"SCOUT_SYSTEM_LAUNCH_VIEWER_INDEX={launch_viewer_dir / 'index.html'}")
    if report.get("viewer_index"):
        print(f"SCOUT_SYSTEM_VIEWER_INDEX={report['viewer_index']}")
    return 0 if report["decision"]["exit_ok"] else 1


def build_launch_gate_report(
    *,
    deterministic_report: dict[str, Any],
    live_report: dict[str, Any],
    tournament_report: dict[str, Any] | None = None,
    stages: list[StageResult] | None = None,
    viewer_index: str = "",
    artifact_dir: str = "",
    allow_live_spend: bool = False,
    next_live_scouts: int = 10,
) -> dict[str, Any]:
    tournament_report = tournament_report if isinstance(tournament_report, dict) else {}
    stages = stages or []
    deterministic_readiness = (
        deterministic_report.get("readiness")
        if isinstance(deterministic_report.get("readiness"), dict)
        else {}
    )
    deterministic_ready = (
        deterministic_readiness.get("status") == "pass"
        and bool(deterministic_readiness.get("ready_for_live_1000"))
    )
    live_verdict = live_report.get("verdict") if isinstance(live_report.get("verdict"), dict) else {}
    preflight = live_report.get("preflight") if isinstance(live_report.get("preflight"), dict) else {}
    preflight_ok = all(bool(preflight.get(key)) for key in (
        "tic_root_ok",
        "provider_import_ok",
        "tool_atlas_ok",
        "market_universe_ok",
    ))
    tournament_decision = (
        tournament_report.get("promotion_decision")
        if isinstance(tournament_report.get("promotion_decision"), dict)
        else {}
    )
    proof_ladder = _proof_ladder(
        deterministic_ready=deterministic_ready,
        live_report=live_report,
        tournament_decision=tournament_decision,
        preflight_ok=preflight_ok,
    )
    decision = _launch_decision(
        deterministic_ready=deterministic_ready,
        live_report=live_report,
        live_verdict=live_verdict,
        preflight_ok=preflight_ok,
        tournament_decision=tournament_decision,
        allow_live_spend=allow_live_spend,
        next_live_scouts=next_live_scouts,
    )
    return {
        "schema_version": "talis_scout_system_launch_gate_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifact_dir": artifact_dir,
        "viewer_index": viewer_index,
        "allow_live_spend": allow_live_spend,
        "launch_viewer_index": "",
        "decision": decision,
        "proof_ladder": proof_ladder,
        "deterministic": {
            "cycle_id": deterministic_report.get("cycle_id"),
            "status": deterministic_readiness.get("status"),
            "ready_for_live_1000": bool(deterministic_readiness.get("ready_for_live_1000")),
            "failed_gates": deterministic_readiness.get("failed_gates") or [],
            "metrics": _deterministic_summary(deterministic_report),
        },
        "live": {
            "cycle_id": live_report.get("cycle_id"),
            "mode": live_report.get("mode"),
            "status": live_verdict.get("status"),
            "failed_gates": live_verdict.get("failed_gates") or [],
            "preflight": {
                "tic_root_ok": bool(preflight.get("tic_root_ok")),
                "provider_import_ok": bool(preflight.get("provider_import_ok")),
                "tool_atlas_ok": bool(preflight.get("tool_atlas_ok")),
                "market_universe_ok": bool(preflight.get("market_universe_ok")),
                "tool_atlas": preflight.get("tool_atlas") or {},
                "market_universe": preflight.get("market_universe") or {},
            },
            "scale_decision": live_report.get("scale_decision") or {},
            "metrics": _live_summary(live_report),
        },
        "tournament": {
            "decision": tournament_decision.get("decision"),
            "ready_for_live_100": bool(tournament_decision.get("ready_for_live_100")),
            "ready_for_live_1000": bool(tournament_decision.get("ready_for_live_1000")),
            "ready_for_scheduled_production": bool(tournament_decision.get("ready_for_scheduled_production")),
            "reason": tournament_decision.get("reason") or "",
        },
        "stages": [asdict(stage) for stage in stages],
    }


def render_launch_gate_markdown(report: dict[str, Any]) -> str:
    decision = report.get("decision") if isinstance(report.get("decision"), dict) else {}
    deterministic = report.get("deterministic") if isinstance(report.get("deterministic"), dict) else {}
    live = report.get("live") if isinstance(report.get("live"), dict) else {}
    tournament = report.get("tournament") if isinstance(report.get("tournament"), dict) else {}
    lines = [
        "# Scout System Launch Gate",
        "",
        f"- status: `{decision.get('status')}`",
        f"- allowed_next_step: `{decision.get('allowed_next_step')}`",
        f"- human_authorization_required: `{decision.get('human_authorization_required')}`",
        f"- reason: {decision.get('reason')}",
        "",
        "## Deterministic 100",
        "",
        f"- status: `{deterministic.get('status')}`",
        f"- ready_for_live_1000: `{deterministic.get('ready_for_live_1000')}`",
        f"- failed_gates: `{', '.join(deterministic.get('failed_gates') or []) or 'none'}`",
        "",
        "## Live Gate",
        "",
        f"- mode: `{live.get('mode')}`",
        f"- status: `{live.get('status')}`",
        f"- failed_gates: `{', '.join(live.get('failed_gates') or []) or 'none'}`",
        "",
        "## Tournament",
        "",
        f"- decision: `{tournament.get('decision')}`",
        f"- ready_for_live_1000: `{tournament.get('ready_for_live_1000')}`",
        f"- ready_for_scheduled_production: `{tournament.get('ready_for_scheduled_production')}`",
        "",
        "## Proof Ladder",
        "",
    ]
    for step in report.get("proof_ladder") or []:
        lines.append(f"- {'PASS' if step.get('passed') else 'BLOCKED'} `{step.get('id')}`: {step.get('summary')}")
    lines.extend([
        "",
        "## Next Command",
        "",
        "```bash",
        str(decision.get("next_command") or ""),
        "```",
    ])
    if report.get("viewer_index"):
        lines.extend(["", f"Viewer: `{report.get('viewer_index')}`"])
    if report.get("launch_viewer_index"):
        lines.extend(["", f"Launch cockpit: `{report.get('launch_viewer_index')}`"])
    return "\n".join(lines) + "\n"


def export_launch_gate_viewer(
    report: dict[str, Any],
    *,
    output_dir: Path,
    report_json: Path,
    report_md: Path,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_text(output_dir / ".nojekyll", "")
    _write_json(output_dir / "launch_gate_report.json", report)
    _write_text(output_dir / "launch_gate_report.md", render_launch_gate_markdown(report))
    _write_text(output_dir / "index.html", render_launch_gate_html(report))
    return output_dir / "index.html"


def render_launch_gate_html(report: dict[str, Any]) -> str:
    decision = report.get("decision") if isinstance(report.get("decision"), dict) else {}
    deterministic = report.get("deterministic") if isinstance(report.get("deterministic"), dict) else {}
    live = report.get("live") if isinstance(report.get("live"), dict) else {}
    tournament = report.get("tournament") if isinstance(report.get("tournament"), dict) else {}
    det_metrics = deterministic.get("metrics") if isinstance(deterministic.get("metrics"), dict) else {}
    live_preflight = live.get("preflight") if isinstance(live.get("preflight"), dict) else {}
    tool_atlas = live_preflight.get("tool_atlas") if isinstance(live_preflight.get("tool_atlas"), dict) else {}
    universe = live_preflight.get("market_universe") if isinstance(live_preflight.get("market_universe"), dict) else {}
    status = str(decision.get("status") or "unknown")
    hero = _launch_hero_copy(status)
    proof_rows = "".join(_proof_card(step) for step in report.get("proof_ladder") or [])
    stage_rows = "".join(_stage_card(stage) for stage in report.get("stages") or [])
    next_command = str(decision.get("next_command") or "")
    scout_viewer = str(report.get("viewer_index") or "")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <link rel="icon" href="data:," />
  <title>Talis Scout Launch Gate</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #07080a;
      --ink: #f7f8f4;
      --muted: rgba(247,248,244,.68);
      --line: rgba(255,255,255,.13);
      --panel: rgba(255,255,255,.074);
      --green: #72f0ac;
      --cyan: #7bdcff;
      --amber: #f6ca71;
      --red: #ff8b9a;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background:
        radial-gradient(circle at 82% 0%, rgba(123,220,255,.16), transparent 30%),
        linear-gradient(180deg, #101719 0%, var(--bg) 58%);
      color: var(--ink);
      font: 15px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", sans-serif;
      letter-spacing: 0;
      overflow-x: hidden;
    }}
    main {{ width: min(1180px, 100%); margin: 0 auto; padding: 22px 16px 48px; }}
    .hero {{ min-height: 76vh; display: grid; align-content: center; gap: 24px; }}
    .eyebrow {{ color: var(--green); text-transform: uppercase; font-size: 12px; font-weight: 800; letter-spacing: .08em; }}
    h1 {{ font-size: clamp(50px, 10vw, 112px); line-height: .9; margin: 10px 0 14px; max-width: 940px; }}
    h2 {{ font-size: clamp(32px, 6vw, 62px); line-height: .96; margin: 0 0 10px; }}
    h3 {{ font-size: 21px; line-height: 1.06; margin: 0 0 9px; }}
    p {{ margin: 0; color: var(--muted); font-size: 18px; max-width: 790px; }}
    section {{ margin-top: 34px; }}
    .grid {{ display: grid; gap: 10px; }}
    .hero-grid {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .metrics {{ grid-template-columns: repeat(6, minmax(0, 1fr)); }}
    .two {{ grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); align-items: start; }}
    .proof {{ grid-template-columns: repeat(6, minmax(210px, 1fr)); overflow-x: auto; padding-bottom: 12px; scroll-snap-type: x mandatory; }}
    .card, .metric, .panel, .stage {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      backdrop-filter: blur(18px);
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    .card, .panel, .stage {{ padding: 16px; }}
    .metric {{ min-height: 95px; padding: 13px; }}
    .metric span, .card span, .stage span {{ display: block; color: var(--muted); font-size: 12px; font-weight: 760; text-transform: uppercase; }}
    .metric strong {{ display: block; margin-top: 8px; font-size: 28px; line-height: 1; }}
    .proof .card {{ scroll-snap-align: start; min-height: 218px; }}
    .pass {{ color: var(--green); }}
    .blocked {{ color: var(--amber); }}
    .fail {{ color: var(--red); }}
    .command {{ position: relative; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; color: #dce8e6; background: rgba(0,0,0,.3); border: 1px solid var(--line); border-radius: 8px; padding: 13px; margin: 12px 0 0; }}
    a {{ color: var(--cyan); text-decoration: none; }}
    .list {{ list-style: none; margin: 12px 0 0; padding: 0; display: grid; gap: 8px; }}
    .list li {{ display: flex; justify-content: space-between; gap: 14px; border-top: 1px solid var(--line); padding-top: 9px; color: var(--muted); }}
    .list b {{ color: var(--ink); text-align: right; }}
    @media (max-width: 860px) {{
      main {{ padding: 18px 12px 40px; }}
      .hero {{ min-height: 84vh; }}
      .hero-grid, .metrics, .two {{ grid-template-columns: 1fr; }}
      .proof {{ display: flex; }}
      .proof .card {{ flex: 0 0 84%; }}
      p {{ font-size: 17px; }}
    }}
  </style>
</head>
<body>
<main>
  <section class="hero">
    <div>
      <div class="eyebrow">Scout system launch gate / {html.escape(status.replace('_', ' '))}</div>
      <h1>{html.escape(hero["title"])}</h1>
      <p>{html.escape(hero["body"])}</p>
    </div>
    <div class="grid hero-grid">
      <div class="card"><span>Decision</span><h3 class="{_status_class(status)}">{html.escape(status.replace('_', ' '))}</h3><p>{html.escape(str(decision.get("reason") or ""))}</p></div>
      <div class="card"><span>Spend posture</span><h3>{'Locked' if decision.get('human_authorization_required') else 'Automatic'}</h3><p>Paid calls remain behind the explicit live-spend flag.</p></div>
      <div class="card"><span>Allowed next</span><h3>{html.escape(str(decision.get("allowed_next_step") or "none").replace('_', ' '))}</h3><p>The gate never jumps straight to production.</p></div>
      <div class="card"><span>Schedule</span><h3>{'Blocked' if not tournament.get('ready_for_scheduled_production') else 'Candidate'}</h3><p>Scheduled shadow requires repeat 1,000-scout proof.</p></div>
    </div>
  </section>

  <section>
    <h2>Proof Ladder</h2>
    <p>This is the control system. Each rung must become true in order: offline mechanics, live preflight, explicit spend, live quality, tournament promotion, then repeatability.</p>
    <div class="grid proof">{proof_rows}</div>
  </section>

  <section>
    <h2>Current Evidence</h2>
    <div class="grid metrics">
      <div class="metric"><span>Det scouts</span><strong>{html.escape(str(det_metrics.get("scouts_completed") or 0))}</strong></div>
      <div class="metric"><span>Strings</span><strong>{html.escape(str(det_metrics.get("strings") or 0))}</strong></div>
      <div class="metric"><span>Geometry cells</span><strong>{html.escape(str(det_metrics.get("geometry_cells") or 0))}</strong></div>
      <div class="metric"><span>MarketEvolve pairs</span><strong>{html.escape(str(det_metrics.get("market_evolve_pairs") or 0))}</strong></div>
      <div class="metric"><span>Tools</span><strong>{html.escape(str(tool_atlas.get("tools") or 0))}</strong></div>
      <div class="metric"><span>Market entities</span><strong>{html.escape(str(universe.get("entity_count") or 0))}</strong></div>
    </div>
  </section>

  <section class="grid two">
    <div class="panel">
      <h2>What Is Proven</h2>
      <ul class="list">
        <li><span>Deterministic system readiness</span><b>{html.escape(str(deterministic.get("status") or "unknown"))}</b></li>
        <li><span>Effective unique cell ratio</span><b>{html.escape(str(det_metrics.get("effective_unique_cell_ratio") or 0))}</b></li>
        <li><span>MarketEvolve decision</span><b>{html.escape(str(det_metrics.get("market_evolve_decision") or "none").replace('_', ' '))}</b></li>
        <li><span>Live provider import</span><b>{html.escape(str(live_preflight.get("provider_import_ok")))}</b></li>
        <li><span>Tool/source atlas</span><b>{html.escape(str(tool_atlas.get("tools") or 0))} / {html.escape(str(tool_atlas.get("sources") or 0))}</b></li>
      </ul>
    </div>
    <div class="panel command">
      <h2>Next Command</h2>
      <p>This is the exact next move. It spends only if a human deliberately runs it with the live-spend flag.</p>
      <pre>{html.escape(next_command)}</pre>
      <ul class="list">
        <li><span>Launch report</span><b><a href="launch_gate_report.json">open</a></b></li>
        <li><span>Launch markdown</span><b><a href="launch_gate_report.md">open</a></b></li>
        <li><span>100-scout viewer</span><b>{_viewer_link(scout_viewer)}</b></li>
      </ul>
    </div>
  </section>

  <section>
    <h2>Run Stages</h2>
    <div class="grid hero-grid">{stage_rows}</div>
  </section>
</main>
</body>
</html>
"""


def _launch_hero_copy(status: str) -> dict[str, str]:
    if status == "ready_for_authorized_live_canary":
        return {
            "title": "Ready, but the spend gate is locked.",
            "body": "Talis proved the first layer offline and verified that the live provider, tool atlas, and market universe are reachable. The next step is a deliberately authorized 10-scout live canary, not a blind scale-up.",
        }
    if status.startswith("ready_for_live_1000"):
        return {
            "title": "The tournament opened the 1,000-scout ramp.",
            "body": "A live distribution earned the next spend gate. Scheduled production is still blocked until repeatability proves itself across independent 1,000-scout runs.",
        }
    if status.startswith("blocked"):
        return {
            "title": "The gate is doing its job.",
            "body": "Something important is not proven yet. The system is stopping before scale, preserving capital and attention until the failed rung is repaired.",
        }
    return {
        "title": "Scout launch evidence, assembled.",
        "body": "This cockpit shows what the first layer has proven, what remains locked, and which command is allowed next.",
    }


def _proof_card(step: dict[str, Any]) -> str:
    passed = bool(step.get("passed"))
    klass = "pass" if passed else "blocked"
    label = "passed" if passed else "blocked"
    return (
        f'<div class="card"><span>{html.escape(label)}</span>'
        f'<h3 class="{klass}">{html.escape(str(step.get("id") or "").replace("_", " "))}</h3>'
        f'<p>{html.escape(str(step.get("summary") or ""))}</p></div>'
    )


def _stage_card(stage: dict[str, Any]) -> str:
    ok = int(stage.get("returncode") or 0) == 0
    name = str(stage.get("name") or "stage").replace("_", " ")
    return (
        f'<div class="stage"><span>{"ok" if ok else "failed"}</span>'
        f'<h3 class="{"pass" if ok else "fail"}">{html.escape(name)}</h3>'
        f'<p>{html.escape(str(stage.get("elapsed_s") or 0))}s</p></div>'
    )


def _status_class(status: str) -> str:
    if status.startswith("ready"):
        return "pass"
    if status.startswith("blocked"):
        return "blocked"
    return ""


def _viewer_link(path: str) -> str:
    if not path:
        return "none"
    if "scout-system-test" in path:
        return '<a href="../scout-system-test/">open</a>'
    return f'<a href="{html.escape(path)}">open</a>'


def _launch_decision(
    *,
    deterministic_ready: bool,
    live_report: dict[str, Any],
    live_verdict: dict[str, Any],
    preflight_ok: bool,
    tournament_decision: dict[str, Any],
    allow_live_spend: bool,
    next_live_scouts: int,
) -> dict[str, Any]:
    if not deterministic_ready:
        return {
            "status": "blocked_deterministic_readiness",
            "allowed_next_step": "repeat_deterministic_100",
            "human_authorization_required": False,
            "exit_ok": False,
            "reason": "The 100-scout deterministic layer has not proven clean orchestration, storage, geometry, self-healing, and MarketEvolve gates.",
            "next_command": "PYTHONPATH=. python scripts/run_scout_system_launch_gate.py",
        }
    if not preflight_ok:
        return {
            "status": "blocked_live_preflight",
            "allowed_next_step": "repair_provider_tool_or_universe_preflight",
            "human_authorization_required": False,
            "exit_ok": False,
            "reason": "The live-provider preflight did not prove provider import, tool atlas, and market universe readiness.",
            "next_command": "PYTHONPATH=. python scripts/run_live_scout_canary.py --n-scouts 10 --cost-cap-usd 0.10",
        }
    if live_report.get("mode") == "preflight_no_live_spend":
        return {
            "status": "ready_for_authorized_live_canary",
            "allowed_next_step": f"live_{next_live_scouts}_scout_canary",
            "human_authorization_required": True,
            "exit_ok": True,
            "reason": "The deterministic layer and live preflight are clean. The next step requires explicit approval to spend on a tiny live canary.",
            "next_command": (
                "PYTHONPATH=. python scripts/run_scout_system_launch_gate.py "
                f"--allow-live-spend --live-scouts {next_live_scouts} "
                "--live-cost-cap-usd 0.10 --live-concurrency 1"
            ),
        }
    if not tournament_decision:
        return {
            "status": "live_canary_done_tournament_missing",
            "allowed_next_step": "evaluate_live_tournament",
            "human_authorization_required": False,
            "exit_ok": False,
            "reason": "Live calls ran, but no tournament decision was captured.",
            "next_command": "PYTHONPATH=. python scripts/evaluate_live_scout_tournament.py PROMPT_OUTPUT_DIR/live_scout_canary_report.json",
        }
    if tournament_decision.get("ready_for_scheduled_production"):
        return {
            "status": "ready_for_guarded_scheduled_shadow",
            "allowed_next_step": "schedule_shadow_production_candidate",
            "human_authorization_required": True,
            "exit_ok": True,
            "reason": "The tournament has repeat 1,000-scout evidence. Scheduled shadow remains guarded and non-trading.",
            "next_command": "PYTHONPATH=. python scripts/run_guarded_shadow_production.py --tournament-report ARTIFACT_DIR/tournament/live_scout_tournament_report.json",
        }
    if tournament_decision.get("ready_for_live_1000"):
        return {
            "status": "ready_for_live_1000_ramp",
            "allowed_next_step": "live_1000_scout_ramp",
            "human_authorization_required": True,
            "exit_ok": True,
            "reason": "The live tournament passed distribution gates. A capped 1,000-scout ramp is allowed, not scheduled production.",
            "next_command": (
                "PYTHONPATH=. python scripts/run_scout_system_launch_gate.py "
                "--allow-live-spend --live-scouts 1000 --live-cost-cap-usd 5.00 --live-concurrency 8"
            ),
        }
    if tournament_decision.get("ready_for_live_100"):
        return {
            "status": "ready_for_live_100_ramp",
            "allowed_next_step": "live_100_scout_ramp",
            "human_authorization_required": True,
            "exit_ok": True,
            "reason": "The live canary passed tournament gates. A capped 100-scout ramp is allowed.",
            "next_command": (
                "PYTHONPATH=. python scripts/run_scout_system_launch_gate.py "
                "--allow-live-spend --live-scouts 100 --live-cost-cap-usd 1.00 --live-concurrency 4"
            ),
        }
    return {
        "status": "blocked_by_tournament",
        "allowed_next_step": "repair_prompt_tool_or_market_evolve_policy",
        "human_authorization_required": False,
        "exit_ok": False,
        "reason": "The live tournament blocked promotion. Repair the failed gates before increasing scout spend.",
        "next_command": str((live_report.get("scale_decision") or {}).get("next_step") or "Inspect live_scout_tournament_report.json"),
    }


def _proof_ladder(
    *,
    deterministic_ready: bool,
    live_report: dict[str, Any],
    tournament_decision: dict[str, Any],
    preflight_ok: bool,
) -> list[dict[str, Any]]:
    live_mode = str(live_report.get("mode") or "")
    live_verdict = live_report.get("verdict") if isinstance(live_report.get("verdict"), dict) else {}
    return [
        {
            "id": "deterministic_100_scout_system",
            "passed": deterministic_ready,
            "summary": "100 scouts traverse seed generation, scout harness, storage, synthesis, geometry, self-healing, and MarketEvolve without provider spend.",
        },
        {
            "id": "live_provider_preflight",
            "passed": preflight_ok,
            "summary": "Provider import, tool atlas, and market universe are present before spending.",
        },
        {
            "id": "explicit_spend_gate",
            "passed": live_mode != "preflight_no_live_spend",
            "summary": "No paid model calls happen unless --allow-live-spend is present.",
        },
        {
            "id": "live_canary_quality",
            "passed": live_verdict.get("status") == "pass",
            "summary": "Live scouts must pass provider, evidence, string-yield, duplicate, synthesis, geometry, and self-healing gates.",
        },
        {
            "id": "tournament_promotion",
            "passed": bool(tournament_decision.get("ready_for_live_100") or tournament_decision.get("ready_for_live_1000")),
            "summary": "The tournament is the only authority for 100/1,000-scout spend promotion.",
        },
        {
            "id": "repeatability_before_schedule",
            "passed": bool(tournament_decision.get("ready_for_scheduled_production")),
            "summary": "Scheduled shadow production requires repeat 1,000-scout evidence across independent runs.",
        },
    ]


def _deterministic_summary(report: dict[str, Any]) -> dict[str, Any]:
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    scouts = metrics.get("scouts") if isinstance(metrics.get("scouts"), dict) else {}
    seeds = metrics.get("seeds") if isinstance(metrics.get("seeds"), dict) else {}
    info = metrics.get("information_map") if isinstance(metrics.get("information_map"), dict) else {}
    geometry = metrics.get("geometry") if isinstance(metrics.get("geometry"), dict) else {}
    evolve = metrics.get("market_evolve") if isinstance(metrics.get("market_evolve"), dict) else {}
    return {
        "scouts_completed": scouts.get("completed"),
        "success_rate": scouts.get("success_rate"),
        "strings": info.get("string_count"),
        "strings_per_scout": scouts.get("avg_information_strings_per_scout"),
        "effective_unique_cell_ratio": seeds.get("effective_unique_cell_ratio"),
        "geometry_cells": geometry.get("cell_count"),
        "routing_tasks": geometry.get("routing_queue_count"),
        "market_evolve_pairs": evolve.get("paired_seed_slices"),
        "market_evolve_decision": evolve.get("latest_experiment_decision"),
    }


def _live_summary(report: dict[str, Any]) -> dict[str, Any]:
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    scouts = metrics.get("scouts") if isinstance(metrics.get("scouts"), dict) else {}
    info = metrics.get("information_map") if isinstance(metrics.get("information_map"), dict) else {}
    return {
        "scouts_completed": scouts.get("completed", 0),
        "success_rate": scouts.get("success_rate", 0),
        "strings": info.get("string_count", 0),
        "strings_per_scout": scouts.get("avg_information_strings_per_scout", 0),
        "estimated_cost_usd": scouts.get("total_cost_usd_estimate", 0),
    }


def _run_stage(name: str, command: list[str], *, repo: Path) -> StageResult:
    env = os.environ.copy()
    env["PYTHONPATH"] = _prepend_pythonpath(env.get("PYTHONPATH", ""), [str(repo), str(repo / "talis_tic")])
    t0 = time.perf_counter()
    proc = subprocess.run(
        command,
        cwd=str(repo),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return StageResult(
        name=name,
        command=command,
        returncode=proc.returncode,
        elapsed_s=round(time.perf_counter() - t0, 3),
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def _parse_stdout_paths(stdout: str) -> dict[str, str]:
    paths: dict[str, str] = {}
    for line in (stdout or "").splitlines():
        match = re.match(r"^([A-Z0-9_]+)=(.+)$", line.strip())
        if match:
            paths[match.group(1)] = match.group(2)
    return paths


def _path_from_stage(stage: StageResult, key: str) -> Path | None:
    raw = stage.artifacts.get(key)
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _read_json(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _prepend_pythonpath(existing: str, entries: list[str]) -> str:
    parts = [entry for entry in entries if entry]
    if existing:
        parts.append(existing)
    return os.pathsep.join(parts)


def _artifact_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path(tempfile.gettempdir()) / f"talis-scout-system-launch-{stamp}"
    root.mkdir(parents=True, exist_ok=False)
    return root


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", default="")
    parser.add_argument("--viewer-output-dir", default="")
    parser.add_argument("--launch-viewer-output-dir", default="")
    parser.add_argument("--cycle-prefix", default="")
    parser.add_argument("--deterministic-scouts", type=int, default=100)
    parser.add_argument("--deterministic-concurrency", type=int, default=20)
    parser.add_argument("--deterministic-cost-cap-usd", type=float, default=1.0)
    parser.add_argument("--live-scouts", type=int, default=10)
    parser.add_argument("--next-live-scouts", type=int, default=10)
    parser.add_argument("--live-concurrency", type=int, default=1)
    parser.add_argument("--live-cost-cap-usd", type=float, default=0.10)
    parser.add_argument("--provider-timeout-s", type=float, default=45.0)
    parser.add_argument("--max-tool-iterations", type=int, default=0)
    parser.add_argument("--prompt-variant", default="flash_temporal_v4")
    parser.add_argument("--allow-live-spend", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
