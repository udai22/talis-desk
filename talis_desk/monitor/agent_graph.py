"""Live scout agent/information graph payloads for the monitor.

The monitor has two modes:
* local live mode, where FastAPI polls the newest run directory while scouts are
  still finishing;
* static snapshot mode, where GitHub Pages serves the same normalized state from
  ``agent_graph_state.json`` plus copied raw artifacts.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..cadence import default_intelligence_cadence_policy


RAW_ARTIFACTS = [
    "live_scout_canary_outputs.json",
    "live_scout_canary_report.json",
    "live_scout_canary_report.md",
    "live_scout_learning_report.json",
    "live_scout_learning_report.md",
    "live_scout_preview_system_prompt.md",
    "live_scout_preview_user_prompt.md",
    "live_scout_prompt_preview.json",
    "live_scout_ramp_policy.json",
    "live_scout_ramp_policy_rehearsal.json",
    "live_scout_slice_preview.json",
    "live_scout_tournament_report.json",
    "live_scout_tournament_report.md",
    "live_scout_transcript.json",
    "live_scout_canary_transcript_progress.json",
    "market_evolve_hard_experiment.json",
    "tool_creation_contract_repair.json",
]


def discover_latest_agent_graph_run() -> Path | None:
    """Find the freshest launch-gate style run directory."""
    env = os.environ.get("TALIS_AGENT_GRAPH_RUN_DIR") or os.environ.get("TALIS_LAUNCH_GATE_DIR")
    if env and Path(env).exists():
        return _coerce_run_root(Path(env))
    candidates: list[Path] = []
    for pattern in (
        "/var/folders/*/*/T/talis-scout-system-launch-*",
        "/var/folders/*/*/talis-scout-system-launch-*",
        "/tmp/talis-scout-system-launch-*",
    ):
        candidates.extend(Path(p) for p in glob.glob(pattern))
    candidates = [p for p in candidates if p.exists()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: _mtime_score(p))


def build_agent_graph_state(
    run_dir: str | Path | None = None,
    *,
    artifact_href_prefix: str = "/api/agent-graph/artifact/",
    max_agents: int = 240,
) -> dict[str, Any]:
    root = _coerce_run_root(Path(run_dir)) if run_dir else discover_latest_agent_graph_run()
    if root is None:
        return {
            "schema_version": "talis_agent_graph_state_v1",
            "status": "waiting",
            "generated_at": _now(),
            "run_dir": None,
            "summary": {},
            "cadence_policy": default_intelligence_cadence_policy(),
            "agents": [],
            "nodes": [],
            "edges": [],
            "reports": [],
            "timeline": [],
        }

    raw_dir = _raw_dir(root)
    prompt_dir = _prompt_dir(root)
    outputs = _read_json(raw_dir / "live_scout_canary_outputs.json", [])
    if not outputs:
        outputs = _read_json(prompt_dir / "live_scout_canary_outputs.json", [])
    transcript = _read_calls(raw_dir) or _read_calls(prompt_dir)
    slice_preview = _read_json(raw_dir / "live_scout_slice_preview.json", {}) or _read_json(prompt_dir / "live_scout_slice_preview.json", {})
    canary_report = _read_json(raw_dir / "live_scout_canary_report.json", {}) or _read_json(prompt_dir / "live_scout_canary_report.json", {})
    tournament_report = _read_json(raw_dir / "live_scout_tournament_report.json", {}) or _read_json(root / "tournament" / "live_scout_tournament_report.json", {})
    learning_report = _read_json(raw_dir / "live_scout_learning_report.json", {}) or _read_json(prompt_dir / "live_scout_learning_report.json", {})
    repair_report = _read_json(raw_dir / "tool_creation_contract_repair.json", {}) or _read_json(prompt_dir / "tool_creation_contract_repair.json", {})
    launch_report = _read_json(root / "launch-gate" / "launch_gate_report.json", {}) or _read_json(root / "scout_system_launch_gate_report.json", {})

    agents = _agent_rows(
        outputs=outputs[:max_agents] if isinstance(outputs, list) else [],
        transcript=transcript,
        slice_preview=slice_preview,
        canary_report=canary_report,
        max_agents=max_agents,
    )
    situational_agents = _situational_awareness_agents(
        agents,
        canary_report=canary_report,
        tournament_report=tournament_report,
        learning_report=learning_report,
        repair_report=repair_report,
    )
    nodes, edges = _graph_from_agents(
        agents,
        situational_agents=situational_agents,
        tournament_report=tournament_report,
        learning_report=learning_report,
        repair_report=repair_report,
        canary_report=canary_report,
    )
    reports = _report_links(root=root, raw_dir=raw_dir, artifact_href_prefix=artifact_href_prefix)
    summary = _summary(
        root=root,
        agents=agents,
        situational_agents=situational_agents,
        nodes=nodes,
        edges=edges,
        canary_report=canary_report,
        tournament_report=tournament_report,
        learning_report=learning_report,
        repair_report=repair_report,
        launch_report=launch_report,
        slice_preview=slice_preview,
    )
    return {
        "schema_version": "talis_agent_graph_state_v1",
        "status": "ok",
        "generated_at": _now(),
        "run_dir": str(root),
        "raw_dir": str(raw_dir) if raw_dir.exists() else str(prompt_dir),
        "summary": summary,
        "cadence_policy": default_intelligence_cadence_policy(),
        "agents": agents,
        "situational_agents": situational_agents,
        "nodes": nodes,
        "edges": edges,
        "reports": reports,
        "timeline": _timeline(summary, canary_report, tournament_report, learning_report, repair_report),
    }


def export_agent_graph_viewer(
    *,
    output_dir: str | Path,
    html_source: str | Path,
    run_dir: str | Path | None = None,
) -> dict[str, str]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    raw_out = out / "raw"
    if raw_out.exists():
        shutil.rmtree(raw_out)
    raw_out.mkdir(parents=True, exist_ok=True)

    root = _coerce_run_root(Path(run_dir)) if run_dir else discover_latest_agent_graph_run()
    copied: list[str] = []
    if root:
        raw_dir = _raw_dir(root)
        prompt_dir = _prompt_dir(root)
        for name in RAW_ARTIFACTS:
            src = raw_dir / name
            if not src.exists():
                src = prompt_dir / name
            if src.exists():
                shutil.copy2(src, raw_out / name)
                copied.append(name)
        for name in ("launch_gate_report.json", "launch_gate_report.md"):
            src = root / "launch-gate" / name
            if not src.exists():
                src = root / name.replace("launch_gate_", "scout_system_launch_gate_")
            if src.exists():
                shutil.copy2(src, out / name)
                copied.append(name)

    state = build_agent_graph_state(root, artifact_href_prefix="raw/") if root else build_agent_graph_state(None, artifact_href_prefix="raw/")
    (out / "agent_graph_state.json").write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    shutil.copy2(html_source, out / "index.html")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    return {
        "index": str(out / "index.html"),
        "state": str(out / "agent_graph_state.json"),
        "raw_dir": str(raw_out),
        "run_dir": str(root) if root else "",
        "copied": ",".join(copied),
    }


def artifact_path(name: str, run_dir: str | Path | None = None) -> Path | None:
    if "/" in name or name.startswith("."):
        return None
    root = _coerce_run_root(Path(run_dir)) if run_dir else discover_latest_agent_graph_run()
    if root is None:
        return None
    for base in (_raw_dir(root), _prompt_dir(root), root / "launch-gate", root / "tournament", root):
        candidate = base / name
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _agent_rows(
    *,
    outputs: list[dict[str, Any]],
    transcript: list[dict[str, Any]],
    slice_preview: dict[str, Any],
    canary_report: dict[str, Any],
    max_agents: int,
) -> list[dict[str, Any]]:
    seed_rows = slice_preview.get("seed_rows") if isinstance(slice_preview.get("seed_rows"), list) else []
    outputs_by_seed = {
        str(o.get("seed_id")): o
        for o in outputs
        if isinstance(o, dict) and o.get("seed_id")
    }
    outputs_by_index = {
        i: o
        for i, o in enumerate(outputs)
        if isinstance(o, dict)
    }
    transcript_by_index = {
        int(t.get("index")): t
        for t in transcript
        if isinstance(t, dict) and isinstance(t.get("index"), int)
    }
    n_requested = int(
        canary_report.get("n_scouts_requested")
        or slice_preview.get("n_scouts")
        or max(len(seed_rows), len(outputs), len(transcript))
        or 0
    )
    completed = len(transcript_by_index) if transcript_by_index else len(outputs)
    concurrency = int(canary_report.get("concurrency") or 4)
    rows: list[dict[str, Any]] = []
    for i in range(min(max_agents, max(n_requested, len(seed_rows), len(outputs), len(transcript)))):
        seed = seed_rows[i] if i < len(seed_rows) and isinstance(seed_rows[i], dict) else {}
        out = outputs_by_seed.get(str(seed.get("seed_id"))) or outputs_by_index.get(i) or {}
        call = transcript_by_index.get(i, {})
        status = _agent_status(i, out, call, completed=completed, concurrency=concurrency)
        strings = out.get("information_strings") if isinstance(out.get("information_strings"), list) else []
        evidence = out.get("tool_evidence") if isinstance(out.get("tool_evidence"), list) else []
        proposals = out.get("tool_proposal_ids") if isinstance(out.get("tool_proposal_ids"), list) else []
        quality_flags = [str(x) for x in (out.get("quality_flags") or []) if x is not None]
        if status == "complete" and any("missing_hypothesis" in f or "empty_hypothesis" in f for f in quality_flags):
            status = "weak"
        response_envelope = call.get("response_envelope") if isinstance(call.get("response_envelope"), dict) else {}
        prompt_text = str(call.get("text") or "")
        normalized_strings = [_normalize_string(s) for s in strings[:5] if isinstance(s, dict)]
        source_families = _str_list(seed.get("source_families"))
        source_targets = _str_list(seed.get("source_family_targets"))
        early_score = _early_signal_score(
            confidence=_num(out.get("confidence")),
            strings=normalized_strings,
            source_families=source_families,
            source_targets=source_targets,
            quality_flags=quality_flags,
            tool_evidence=evidence,
            market_evolve=seed.get("market_evolve") if isinstance(seed.get("market_evolve"), dict) else {},
        )
        directional_pressure = _directional_pressure(
            hypothesis=str(out.get("hypothesis_text") or ""),
            rationale=str(out.get("rationale_brief") or ""),
            strings=normalized_strings,
            early_score=early_score,
            confidence=_num(out.get("confidence")),
            source_families=source_families,
            source_targets=source_targets,
        )
        rows.append({
            "id": str(out.get("scout_id") or seed.get("seed_id") or f"agent_{i:04d}"),
            "index": i,
            "status": status,
            "seed_id": str(out.get("seed_id") or seed.get("seed_id") or ""),
            "entity": str(out.get("entity") or seed.get("entity") or "UNKNOWN"),
            "asset_class": str(seed.get("asset_class") or "unknown"),
            "horizon": str(out.get("horizon") or seed.get("horizon") or ""),
            "lens": str(out.get("lens") or seed.get("lens") or ""),
            "bias_mode": str(out.get("bias_mode") or seed.get("bias_mode") or ""),
            "theme": str(seed.get("theme") or ""),
            "cell_key": str(seed.get("cell_key") or ""),
            "hypothesis": str(out.get("hypothesis_text") or ""),
            "rationale": str(out.get("rationale_brief") or ""),
            "confidence": _num(out.get("confidence")),
            "elapsed_s": _num(out.get("elapsed_s") or call.get("elapsed_s")),
            "cost_usd": _num(out.get("cost_usd")),
            "model_used": str(out.get("model_used") or response_envelope.get("model_used") or call.get("model") or ""),
            "provider": str(out.get("provider") or response_envelope.get("provider") or ""),
            "prompt_variant": str(out.get("prompt_variant") or seed.get("prompt_variant") or ""),
            "market_evolve": seed.get("market_evolve") if isinstance(seed.get("market_evolve"), dict) else {},
            "source_families": source_families,
            "source_family_targets": source_targets,
            "allowed_tools": _str_list(seed.get("allowed_tool_candidates_head")),
            "suggested_tools": _str_list(out.get("suggested_tools")),
            "tool_evidence": evidence[:10],
            "tool_proposal_ids": _str_list(proposals),
            "information_string_ids": _str_list(out.get("information_string_ids")),
            "information_strings": normalized_strings,
            "early_signal_score": early_score,
            "directional_pressure": directional_pressure,
            "quality_flags": quality_flags[:18],
            "error": out.get("error") or response_envelope.get("error"),
            "prompt": {
                "system": str(call.get("system_prompt") or ""),
                "user": str(call.get("user_prompt") or ""),
                "model_text": prompt_text,
            },
        })
    return rows


def _graph_from_agents(
    agents: list[dict[str, Any]],
    *,
    situational_agents: list[dict[str, Any]],
    tournament_report: dict[str, Any],
    learning_report: dict[str, Any],
    repair_report: dict[str, Any],
    canary_report: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    def node(node_id: str, kind: str, label: str, **extra: Any) -> None:
        if not node_id:
            return
        current = nodes.get(node_id)
        if current:
            current["weight"] = current.get("weight", 1) + extra.pop("weight", 1)
            current.update({k: v for k, v in extra.items() if v not in (None, "", [])})
            return
        nodes[node_id] = {"id": node_id, "kind": kind, "label": label[:90], "weight": extra.pop("weight", 1), **extra}

    def edge(source: str, target: str, kind: str, **extra: Any) -> None:
        if source and target and source != target:
            edges.append({"source": source, "target": target, "kind": kind, **extra})

    node("run", "run", "Scout run", status=canary_report.get("mode") or "live")
    for agent in agents:
        aid = f"agent:{agent['id']}"
        cell_id = f"cell:{agent.get('cell_key') or '|'.join([agent.get('entity',''), agent.get('horizon',''), agent.get('lens',''), agent.get('bias_mode','')])}"
        node(cell_id, "cell", f"{agent.get('entity')} / {agent.get('horizon')} / {agent.get('lens')}", entity=agent.get("entity"), horizon=agent.get("horizon"), lens=agent.get("lens"), bias=agent.get("bias_mode"))
        node(
            aid,
            "agent",
            f"Scout {agent['index']:03d}",
            status=agent.get("status"),
            entity=agent.get("entity"),
            confidence=agent.get("confidence"),
            elapsed_s=agent.get("elapsed_s"),
            early_signal_score=agent.get("early_signal_score"),
            direction=(agent.get("directional_pressure") or {}).get("direction"),
            upward_pressure_score=(agent.get("directional_pressure") or {}).get("score"),
        )
        edge("run", aid, "spawned")
        edge(cell_id, aid, "routed")
        for family in agent.get("source_families") or []:
            sid = f"source:{family}"
            node(sid, "source", family.replace("_", " "), weight=2)
            edge(sid, aid, "fed")
        for tool in (agent.get("suggested_tools") or [])[:5]:
            tid = f"tool:{tool}"
            node(tid, "tool", _tool_label(tool))
            edge(aid, tid, "requested")
        for ev in (agent.get("tool_evidence") or [])[:5]:
            uri = str(ev.get("tool_uri") or ev.get("tool") or "")
            if uri:
                tid = f"tool:{uri}"
                node(tid, "tool", _tool_label(uri))
                edge(tid, aid, "evidence")
        for s in agent.get("information_strings") or []:
            sid = f"string:{s.get('id') or agent['id'] + ':' + s.get('title','')[:24]}"
            node(sid, "string", s.get("title") or s.get("thesis") or "information string", conviction=s.get("conviction"), novelty=s.get("novelty_score"), crowdedness=s.get("crowdedness"))
            edge(aid, sid, "emitted")
            edge(sid, cell_id, "maps_to")
        for prop in agent.get("tool_proposal_ids") or []:
            pid = f"proposal:{prop}"
            node(pid, "proposal", prop, weight=0.6)
            edge(aid, pid, "created_tool_proposal")

    for watcher in situational_agents:
        wid = f"situational:{watcher['id']}"
        node(
            wid,
            "situational",
            watcher.get("label") or watcher["id"],
            status=watcher.get("status"),
            score=watcher.get("score"),
            directive=watcher.get("directive"),
        )
        edge("run", wid, "observed_by")
        for agent_id in watcher.get("related_agent_ids") or []:
            edge(wid, f"agent:{agent_id}", "directs_attention")

    for name, report in (
        ("Canary report", canary_report),
        ("Tournament", tournament_report),
        ("Learning policy", learning_report),
        ("Tool repair", repair_report),
    ):
        if report:
            rid = "report:" + name.lower().replace(" ", "_")
            node(rid, "report", name, status=report.get("status") or (report.get("promotion_decision") or {}).get("decision"))
            edge("run", rid, "summarized_by")

    return list(nodes.values()), edges[:2500]


def _summary(
    *,
    root: Path,
    agents: list[dict[str, Any]],
    situational_agents: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    canary_report: dict[str, Any],
    tournament_report: dict[str, Any],
    learning_report: dict[str, Any],
    repair_report: dict[str, Any],
    launch_report: dict[str, Any],
    slice_preview: dict[str, Any],
) -> dict[str, Any]:
    metrics = canary_report.get("metrics") if isinstance(canary_report.get("metrics"), dict) else {}
    scout_metrics = metrics.get("scouts") if isinstance(metrics.get("scouts"), dict) else {}
    promotion = tournament_report.get("promotion_decision") if isinstance(tournament_report.get("promotion_decision"), dict) else {}
    decision = launch_report.get("decision") if isinstance(launch_report.get("decision"), dict) else {}
    statuses = Counter(a.get("status") for a in agents)
    by_lens = Counter(a.get("lens") for a in agents if a.get("lens"))
    by_source = Counter(f for a in agents for f in (a.get("source_families") or []))
    string_count = sum(len(a.get("information_strings") or []) for a in agents)
    tool_count = len({t for a in agents for t in ((a.get("suggested_tools") or []) + [str(e.get("tool_uri") or "") for e in (a.get("tool_evidence") or [])]) if t})
    confs = [a.get("confidence") for a in agents if isinstance(a.get("confidence"), (int, float))]
    elapsed = [a.get("elapsed_s") for a in agents if isinstance(a.get("elapsed_s"), (int, float))]
    weak_flags = Counter(f for a in agents for f in (a.get("quality_flags") or []) if any(k in f for k in ("missing", "empty", "invented", "not_promoted", "stale")))
    launch_live = launch_report.get("live") if isinstance(launch_report.get("live"), dict) else {}
    cycle_id = canary_report.get("cycle_id") or launch_live.get("cycle_id") or ""
    next_policy = learning_report.get("next_ramp_policy") if isinstance(learning_report.get("next_ramp_policy"), dict) else {}
    allowed_next_step = decision.get("allowed_next_step") or next_policy.get("allowed_next_step") or ""
    return {
        "run_name": root.name,
        "cycle_id": cycle_id,
        "decision": decision.get("status") or promotion.get("decision") or canary_report.get("mode") or "running",
        "allowed_next_step": allowed_next_step,
        "agents_requested": int(canary_report.get("n_scouts_requested") or slice_preview.get("n_scouts") or len(agents)),
        "agents_seen": len(agents),
        "agents_complete": int(statuses.get("complete", 0) + statuses.get("weak", 0)),
        "agents_weak": int(statuses.get("weak", 0)),
        "agents_error": int(statuses.get("error", 0)),
        "agents_running": int(statuses.get("running", 0)),
        "strings": string_count or int((scout_metrics.get("avg_information_strings_per_scout") or 0) * (scout_metrics.get("completed") or 0)),
        "avg_strings_per_scout": scout_metrics.get("avg_information_strings_per_scout"),
        "success_rate": scout_metrics.get("success_rate"),
        "duplicate_hypothesis_rate": scout_metrics.get("duplicate_hypothesis_rate"),
        "avg_confidence": round(sum(confs) / len(confs), 3) if confs else None,
        "cost_usd": scout_metrics.get("total_cost_usd_estimate") or sum(float(a.get("cost_usd") or 0) for a in agents),
        "elapsed_s": canary_report.get("elapsed_s"),
        "median_elapsed_s": _median(elapsed),
        "p95_elapsed_s": _percentile(elapsed, 0.95),
        "nodes": len(nodes),
        "edges": len(edges),
        "source_families": dict(by_source.most_common(12)),
        "lenses": dict(by_lens.most_common(12)),
        "tool_proposals": len({p for a in agents for p in (a.get("tool_proposal_ids") or [])}),
        "tool_contract_status": repair_report.get("status"),
        "tool_contract_frontier": ((repair_report.get("after") or {}).get("frontier_proposal_count") if isinstance(repair_report.get("after"), dict) else None),
        "tool_count": tool_count,
        "top_quality_flags": dict(weak_flags.most_common(8)),
        "early_signal_target": "surface before consensus acceleration",
        "early_signal_candidates": _top_early_signal_candidates(agents),
        "early_signal_count": sum(1 for a in agents if float(a.get("early_signal_score") or 0) >= 0.88),
        "max_early_signal_score": max((float(a.get("early_signal_score") or 0) for a in agents), default=0.0),
        "upward_pressure_target": "identify repricing-up pressure before price consensus",
        "upward_pressure_candidates": _top_directional_pressure_candidates(agents, direction="up"),
        "upward_pressure_count": sum(
            1
            for a in agents
            if (a.get("directional_pressure") or {}).get("direction") == "up"
            and float((a.get("directional_pressure") or {}).get("score") or 0) >= 0.75
        ),
        "max_upward_pressure_score": max(
            (
                float((a.get("directional_pressure") or {}).get("score") or 0)
                for a in agents
                if (a.get("directional_pressure") or {}).get("direction") == "up"
            ),
            default=0.0,
        ),
        "situational_awareness_agents": situational_agents,
        "max_situational_score": max((float(a.get("score") or 0) for a in situational_agents), default=0.0),
        "promotion_reason": promotion.get("reason") or decision.get("reason") or "",
        "coverage": (canary_report.get("metrics") or {}).get("coverage") if isinstance(canary_report.get("metrics"), dict) else {},
    }


def _timeline(summary: dict[str, Any], canary: dict[str, Any], tournament: dict[str, Any], learning: dict[str, Any], repair: dict[str, Any]) -> list[dict[str, Any]]:
    promotion = tournament.get("promotion_decision") if isinstance(tournament.get("promotion_decision"), dict) else {}
    return [
        {"id": "cadence", "label": "Cadence", "status": "pass", "detail": "2 full runs/day + always-on Flash sentinel"},
        {"id": "slice", "label": "Slice", "status": "pass", "detail": f"{summary.get('agents_requested', 0)} cells routed"},
        {"id": "scouts", "label": "Scouts", "status": "pass" if summary.get("success_rate", 0) >= 0.9 else "watch", "detail": f"{summary.get('agents_complete', 0)} returned"},
        {"id": "strings", "label": "Strings", "status": "pass" if summary.get("strings", 0) else "watch", "detail": f"{summary.get('strings', 0)} strings"},
        {"id": "pressure", "label": "Pressure", "status": "pass" if summary.get("upward_pressure_count", 0) else "watch", "detail": f"{summary.get('upward_pressure_count', 0)} upward candidates"},
        {"id": "graph", "label": "Graph", "status": "pass" if summary.get("nodes", 0) else "watch", "detail": f"{summary.get('nodes', 0)} nodes"},
        {"id": "cortex", "label": "Cortex", "status": "pass" if summary.get("situational_awareness_agents") else "watch", "detail": f"{len(summary.get('situational_awareness_agents') or [])} overseers"},
        {"id": "repair", "label": "Repair", "status": repair.get("status") or "pending", "detail": f"{repair.get('repairs_created', 0)} repairs"},
        {"id": "tournament", "label": "Tournament", "status": "pass" if promotion.get("ready_for_live_1000") else "watch", "detail": promotion.get("decision") or "pending"},
        {"id": "next", "label": "Next", "status": "locked", "detail": summary.get("allowed_next_step") or "human gate"},
    ]


def _report_links(*, root: Path, raw_dir: Path, artifact_href_prefix: str) -> list[dict[str, Any]]:
    names = [
        ("live_scout_canary_report.json", "Canary report", "Run metrics, gates, coverage, scale decision"),
        ("live_scout_canary_outputs.json", "Scout outputs", "Every returned scout object and strings"),
        ("live_scout_transcript.json", "Prompt transcript", "System/user prompts and raw model text"),
        ("live_scout_slice_preview.json", "Slice preview", "Seed cells, source families, tool menus"),
        ("live_scout_tournament_report.json", "Tournament report", "Promotion authority for 100/1000"),
        ("live_scout_learning_report.json", "Learning policy", "Repair work orders and next ramp policy"),
        ("live_scout_ramp_policy.json", "Ramp policy", "Executable policy patch for next run"),
        ("live_scout_ramp_policy_rehearsal.json", "Policy rehearsal", "No-spend proof that policy changed seeds"),
        ("tool_creation_contract_repair.json", "Tool repair", "Native tool-proposal quality gate"),
        ("market_evolve_hard_experiment.json", "MarketEvolve", "Control/candidate evolution proof"),
    ]
    out: list[dict[str, Any]] = []
    for filename, title, desc in names:
        if (raw_dir / filename).exists() or (_prompt_dir(root) / filename).exists():
            out.append({"filename": filename, "title": title, "description": desc, "href": artifact_href_prefix + filename})
    if (root / "launch-gate" / "launch_gate_report.json").exists() or (root / "scout_system_launch_gate_report.json").exists():
        out.insert(0, {"filename": "launch_gate_report.json", "title": "Launch gate", "description": "Operator decision and next command", "href": "../launch-gate/launch_gate_report.json" if artifact_href_prefix == "raw/" else artifact_href_prefix + "launch_gate_report.json"})
    return out


def _agent_status(index: int, out: dict[str, Any], call: dict[str, Any], *, completed: int, concurrency: int) -> str:
    if out:
        if out.get("error"):
            return "error"
        return "complete"
    envelope = call.get("response_envelope") if isinstance(call.get("response_envelope"), dict) else {}
    if call:
        if envelope.get("error"):
            return "error"
        return "complete"
    if index < completed + concurrency:
        return "running"
    return "queued"


def _early_signal_score(
    *,
    confidence: float | None,
    strings: list[dict[str, Any]],
    source_families: list[str],
    source_targets: list[str],
    quality_flags: list[str],
    tool_evidence: list[dict[str, Any]],
    market_evolve: dict[str, Any],
) -> float:
    """Heuristic for "surface it before the move is obvious."

    This is not a trade score. It is a routing/attention score: low-crowded,
    high-novelty, node-rich, evidence-incomplete shapes deserve more scout or
    verifier budget before they become consensus.
    """
    novelty = max((float(s.get("novelty_score") or 0) for s in strings), default=0.0)
    uncrowded = max((1.0 - float(s.get("crowdedness") or 0.5) for s in strings), default=0.0)
    conviction = max((float(s.get("conviction") or 0) for s in strings), default=0.0)
    families = set(source_families) | set(source_targets)
    node_bonus = 0.0
    for needle in ("our_node", "our_hl_node", "hydromancer", "parallel_web", "mempool", "grok_x", "x_search", "twitter"):
        if any(needle in f for f in families):
            node_bonus += 0.06
    candidate_bonus = 0.08 if market_evolve.get("experiment_arm") == "candidate" else 0.0
    gap_bonus = 0.0
    for flag in quality_flags:
        if any(k in flag for k in ("source_health", "not_promoted", "missing", "empty_hypothesis", "stale")):
            gap_bonus += 0.025
    evidence_bonus = min(0.12, 0.015 * len(tool_evidence))
    confidence_term = min(0.16, max(0.0, float(confidence or 0.0)) * 0.16)
    score = (
        novelty * 0.25
        + uncrowded * 0.18
        + conviction * 0.16
        + min(0.22, node_bonus)
        + candidate_bonus
        + min(0.12, gap_bonus)
        + evidence_bonus
        + confidence_term
    )
    return round(min(1.0, score), 3)


def _directional_pressure(
    *,
    hypothesis: str,
    rationale: str,
    strings: list[dict[str, Any]],
    early_score: float,
    confidence: float | None,
    source_families: list[str],
    source_targets: list[str],
) -> dict[str, Any]:
    """Estimate whether information flow points toward upward/downward repricing.

    This is intentionally interpretable. It is the "VVV should show up at 2-3,
    not after 5" lens: look for uncrowded, fresh, source-diverse causal language
    that implies upward price pressure before consensus has fully moved.
    """
    chunks = [hypothesis, rationale]
    for s in strings:
        chunks.extend([
            str(s.get("title") or ""),
            str(s.get("thesis") or ""),
            str(s.get("mechanism") or ""),
            str(s.get("expected_outcome") or ""),
        ])
    text = " ".join(chunks).lower()
    up_terms = (
        "reprices higher", "priced upwards", "upward", "upside", "long", "bid",
        "accumulation", "absorbed", "absorption", "squeeze", "breakout",
        "forced buying", "demand", "inflow", "tailwind", "bullish", "scarcity",
        "higher", "rerate", "re-rate", "positive convexity",
    )
    down_terms = (
        "reprices lower", "downward", "downside", "short", "sell pressure",
        "supply overhang", "unlock", "unstake", "outflow", "bearish", "headwind",
        "liquidation", "forced selling", "lower", "de-risk", "reject cluster",
    )
    up_hits = [t for t in up_terms if t in text]
    down_hits = [t for t in down_terms if t in text]
    if len(up_hits) > len(down_hits):
        direction = "up"
        direction_strength = min(1.0, (len(up_hits) - len(down_hits)) / 4)
    elif len(down_hits) > len(up_hits):
        direction = "down"
        direction_strength = min(1.0, (len(down_hits) - len(up_hits)) / 4)
    elif up_hits or down_hits:
        direction = "mixed"
        direction_strength = 0.35
    else:
        direction = "unknown"
        direction_strength = 0.0

    novelty = max((float(s.get("novelty_score") or 0) for s in strings), default=0.0)
    uncrowded = max((1.0 - float(s.get("crowdedness") or 0.5) for s in strings), default=0.0)
    conviction = max((float(s.get("conviction") or 0) for s in strings), default=0.0)
    families = set(source_families) | set(source_targets)
    source_diversity = min(1.0, len(families) / 7)
    node_presence = 1.0 if any(
        any(needle in f for f in families)
        for needle in ("our_node", "our_hl_node", "hydromancer", "parallel_web", "mempool", "grok_x", "x_search", "twitter")
    ) else 0.0
    score = (
        early_score * 0.34
        + direction_strength * 0.22
        + novelty * 0.14
        + uncrowded * 0.10
        + conviction * 0.08
        + source_diversity * 0.06
        + node_presence * 0.06
        + min(0.06, float(confidence or 0.0) * 0.06)
    )
    reason_bits = []
    if up_hits:
        reason_bits.append("up: " + ", ".join(up_hits[:3]))
    if down_hits:
        reason_bits.append("down: " + ", ".join(down_hits[:3]))
    if novelty:
        reason_bits.append(f"novelty {novelty:.0%}")
    reason_bits.append(f"uncrowded {uncrowded:.0%}")
    if node_presence:
        reason_bits.append("node/hydromancer/web/X footprint")
    return {
        "direction": direction,
        "score": round(min(1.0, score), 3),
        "direction_strength": round(direction_strength, 3),
        "up_hits": up_hits[:6],
        "down_hits": down_hits[:6],
        "why": "; ".join(reason_bits[:5]),
    }


def _top_directional_pressure_candidates(
    agents: list[dict[str, Any]],
    *,
    direction: str,
    limit: int = 8,
) -> list[dict[str, Any]]:
    ranked = sorted(
        [
            a for a in agents
            if (a.get("directional_pressure") or {}).get("direction") == direction
        ],
        key=lambda a: (
            float((a.get("directional_pressure") or {}).get("score") or 0),
            float(a.get("early_signal_score") or 0),
        ),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    for a in ranked[:limit]:
        pressure = a.get("directional_pressure") or {}
        out.append({
            "agent_id": a.get("id"),
            "index": a.get("index"),
            "entity": a.get("entity"),
            "horizon": a.get("horizon"),
            "lens": a.get("lens"),
            "score": pressure.get("score"),
            "early_signal_score": a.get("early_signal_score"),
            "why": pressure.get("why") or _early_signal_why(a),
        })
    return out


def _situational_awareness_agents(
    agents: list[dict[str, Any]],
    *,
    canary_report: dict[str, Any],
    tournament_report: dict[str, Any],
    learning_report: dict[str, Any],
    repair_report: dict[str, Any],
) -> list[dict[str, Any]]:
    """Deterministic situational-awareness overlays.

    These represent the Leopold-style "know the whole situation" agents:
    not another narrow scout, but monitors that reason across market cells,
    data sources, tool creation, budget gates, and early repricing pressure.
    """
    up = _top_directional_pressure_candidates(agents, direction="up", limit=6)
    early = _top_early_signal_candidates(agents, limit=6)
    flags = Counter(f for a in agents for f in (a.get("quality_flags") or []))
    node_gap_count = sum(
        1 for a in agents
        if any("node_intelligence_not_promoted" in f for f in (a.get("quality_flags") or []))
    )
    source_gap_count = sum(
        1 for a in agents
        if any(k in " ".join(a.get("quality_flags") or []) for k in ("stale", "source_health", "missing_evidence"))
    )
    promotion = tournament_report.get("promotion_decision") if isinstance(tournament_report.get("promotion_decision"), dict) else {}
    repair_status = str(repair_report.get("status") or "missing")
    success = (((canary_report.get("metrics") or {}).get("scouts") or {}).get("success_rate") or 0)
    return [
        {
            "id": "situational_overwatch",
            "label": "Situational Overwatch",
            "status": "active",
            "score": round(float(success or 0), 3),
            "directive": "read the whole run state before allowing more spend",
            "summary": f"Success {float(success or 0):.0%}; tournament {promotion.get('decision') or 'pending'}.",
            "related_agent_ids": [str(x.get("agent_id")) for x in early[:4] if x.get("agent_id")],
        },
        {
            "id": "early_upward_pressure",
            "label": "Early Upward Pressure",
            "status": "active" if up else "watch",
            "score": round(max((float(x.get("score") or 0) for x in up), default=0.0), 3),
            "directive": "surface VVV-like upward repricing before consensus price catches up",
            "summary": f"{len(up)} upward-pressure candidates; top: {up[0].get('entity') if up else 'none'}.",
            "related_agent_ids": [str(x.get("agent_id")) for x in up[:6] if x.get("agent_id")],
        },
        {
            "id": "source_blindspot_watch",
            "label": "Source Blindspot Watch",
            "status": "watch" if source_gap_count else "clear",
            "score": round(min(1.0, source_gap_count / max(1, len(agents))), 3),
            "directive": "detect when a signal is real but the data layer is too stale or incomplete to act",
            "summary": f"{source_gap_count} scouts carried source freshness/evidence gap pressure.",
            "related_agent_ids": [
                str(a.get("id")) for a in agents
                if any(k in " ".join(a.get("quality_flags") or []) for k in ("stale", "source_health", "missing_evidence"))
            ][:6],
        },
        {
            "id": "node_hydromancer_watch",
            "label": "Node + Hydromancer Watch",
            "status": "watch" if node_gap_count else "clear",
            "score": round(min(1.0, node_gap_count / max(1, len(agents))), 3),
            "directive": "ensure node intelligence is promoted into trade geometry, not stranded as side evidence",
            "summary": f"{node_gap_count} scouts flagged node intelligence not yet promoted.",
            "related_agent_ids": [
                str(a.get("id")) for a in agents
                if any("node_intelligence_not_promoted" in f for f in (a.get("quality_flags") or []))
            ][:6],
        },
        {
            "id": "tool_budget_governor",
            "label": "Tool + Budget Governor",
            "status": "clear" if repair_status == "pass" else "watch",
            "score": 1.0 if repair_status == "pass" else 0.0,
            "directive": "only buy more scout width when tool creation and repair gates are clean",
            "summary": f"Tool repair {repair_status}; common flags: {', '.join(k for k, _ in flags.most_common(3))}.",
            "related_agent_ids": [],
        },
    ]


def _top_early_signal_candidates(agents: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    ranked = sorted(
        agents,
        key=lambda a: (float(a.get("early_signal_score") or 0), len(a.get("information_strings") or [])),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    for a in ranked[:limit]:
        out.append({
            "agent_id": a.get("id"),
            "index": a.get("index"),
            "entity": a.get("entity"),
            "horizon": a.get("horizon"),
            "lens": a.get("lens"),
            "score": a.get("early_signal_score"),
            "status": a.get("status"),
            "why": _early_signal_why(a),
        })
    return out


def _early_signal_why(agent: dict[str, Any]) -> str:
    bits: list[str] = []
    strings = agent.get("information_strings") or []
    if strings:
        top = max(strings, key=lambda s: float(s.get("novelty_score") or 0))
        bits.append(f"novelty {float(top.get('novelty_score') or 0):.0%}")
        bits.append(f"crowded {float(top.get('crowdedness') or 0.5):.0%}")
    fam = [f for f in (agent.get("source_families") or []) if f in {"our_node", "hydromancer", "parallel_web", "grok_x_alpha"}]
    if fam:
        bits.append("+".join(fam[:3]))
    if agent.get("quality_flags"):
        bits.append("has unresolved repair/gap signal")
    return ", ".join(bits[:4]) or "low-crowded emerging shape"


def _normalize_string(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(raw.get("id") or raw.get("string_id") or ""),
        "title": str(raw.get("title") or "")[:160],
        "thesis": str(raw.get("thesis") or "")[:800],
        "mechanism": str(raw.get("mechanism") or "")[:600],
        "expected_outcome": str(raw.get("expected_outcome") or "")[:600],
        "kill_signal": str(raw.get("kill_signal") or "")[:500],
        "time_horizon": str(raw.get("time_horizon") or raw.get("horizon") or ""),
        "conviction": _num(raw.get("conviction")),
        "crowdedness": _num(raw.get("crowdedness")),
        "novelty_score": _num(raw.get("novelty_score")),
        "entities_chain": _str_list(raw.get("entities_chain")),
    }


def _read_calls(raw_dir: Path) -> list[dict[str, Any]]:
    for name in ("live_scout_canary_transcript_progress.json", "live_scout_transcript.json"):
        payload = _read_json(raw_dir / name, {})
        if isinstance(payload, dict) and isinstance(payload.get("calls"), list):
            return [x for x in payload["calls"] if isinstance(x, dict)]
    return []


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def _coerce_run_root(path: Path) -> Path:
    p = path.expanduser()
    if p.name == "raw":
        p = p.parent
    if p.name == "prompt_outputs":
        p = p.parent.parent
    if p.name in {"launch-gate", "live_canary", "tournament"}:
        p = p.parent
    return p


def _raw_dir(root: Path) -> Path:
    return root / "launch-gate" / "raw"


def _prompt_dir(root: Path) -> Path:
    return root / "live_canary" / "prompt_outputs"


def _mtime_score(path: Path) -> float:
    try:
        return max((p.stat().st_mtime for p in path.rglob("*") if p.is_file()), default=path.stat().st_mtime)
    except Exception:
        return 0.0


def _num(raw: Any) -> float | None:
    try:
        if raw is None or raw == "":
            return None
        return float(raw)
    except Exception:
        return None


def _median(values: list[float | None]) -> float | None:
    vals = sorted(float(v) for v in values if isinstance(v, (int, float)))
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return round(vals[mid], 3)
    return round((vals[mid - 1] + vals[mid]) / 2, 3)


def _percentile(values: list[float | None], q: float) -> float | None:
    vals = sorted(float(v) for v in values if isinstance(v, (int, float)))
    if not vals:
        return None
    idx = min(len(vals) - 1, max(0, int(round((len(vals) - 1) * q))))
    return round(vals[idx], 3)


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if x is not None and str(x)]


def _tool_label(uri: str) -> str:
    s = uri.split("/")[-1].split("@")[0]
    return s.replace("_", " ")[:80] or uri[:80]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
