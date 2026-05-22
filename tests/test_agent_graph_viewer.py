from __future__ import annotations

import json
from pathlib import Path

from talis_desk.monitor.agent_graph import build_agent_graph_state, export_agent_graph_viewer


def test_agent_graph_state_normalizes_live_scout_artifacts(tmp_path: Path) -> None:
    run = _write_run(tmp_path)

    state = build_agent_graph_state(run, artifact_href_prefix="raw/")

    assert state["status"] == "ok"
    assert state["summary"]["agents_requested"] == 2
    assert state["summary"]["agents_seen"] == 2
    assert state["summary"]["strings"] == 1
    assert state["summary"]["decision"] == "ready_for_live_1000_ramp"
    assert state["summary"]["allowed_next_step"] == "live_1000_scout_ramp"
    assert any(n["kind"] == "agent" for n in state["nodes"])
    assert any(n["kind"] == "string" for n in state["nodes"])
    assert any(n["kind"] == "situational" for n in state["nodes"])
    assert any(e["kind"] == "emitted" for e in state["edges"])
    assert any(e["kind"] == "directs_attention" for e in state["edges"])
    assert state["agents"][0]["source_families"] == ["our_node", "hydromancer"]
    assert state["agents"][0]["directional_pressure"]["direction"] == "up"
    assert state["summary"]["max_upward_pressure_score"] > 0
    assert state["summary"]["upward_pressure_candidates"]
    assert state["summary"]["situational_awareness_agents"]
    assert state["cadence_policy"]["full_pipeline"]["cadence"] == "twice_daily"
    assert state["cadence_policy"]["always_on_flash"]["mode"] == "continuous_sentinel"
    assert any(trigger["id"] == "fresh_social_alpha" for trigger in state["cadence_policy"]["sentinel_triggers"])
    assert any(step["id"] == "cadence" for step in state["timeline"])
    assert any(step["id"] == "cortex" for step in state["timeline"])
    assert state["reports"][0]["title"] == "Launch gate"


def test_agent_graph_static_export_copies_raw_artifacts_and_state(tmp_path: Path) -> None:
    run = _write_run(tmp_path)
    html = tmp_path / "agent_graph.html"
    html.write_text("<!doctype html><title>Agent Graph</title>", encoding="utf-8")
    out = tmp_path / "site"

    result = export_agent_graph_viewer(output_dir=out, html_source=html, run_dir=run)

    assert Path(result["index"]).exists()
    assert Path(result["state"]).exists()
    assert (out / "raw" / "live_scout_canary_outputs.json").exists()
    state = json.loads((out / "agent_graph_state.json").read_text())
    assert state["summary"]["agents_complete"] == 2
    assert state["reports"][1]["href"] == "raw/live_scout_canary_report.json"


def test_agent_graph_handles_blocked_sentinel_preflight(tmp_path: Path) -> None:
    run = tmp_path / "sentinel"
    prompt = run / "live_canary" / "prompt_outputs"
    prompt.mkdir(parents=True)
    (prompt / "live_scout_canary_outputs.json").write_text("[]", encoding="utf-8")
    (prompt / "live_scout_slice_preview.json").write_text(json.dumps({
        "n_scouts": 8,
        "seed_rows": [{"seed_id": "s1", "entity": "VVV", "horizon": "intraday", "lens": "sentiment"}],
    }), encoding="utf-8")
    (prompt / "live_scout_canary_report.json").write_text(json.dumps({
        "cycle_id": "cadence_blocked",
        "mode": "preflight_no_live_spend",
        "n_scouts_requested": 8,
        "status": "blocked",
    }), encoding="utf-8")

    state = build_agent_graph_state(run, artifact_href_prefix="raw/")

    assert state["status"] == "ok"
    assert state["summary"]["agents_requested"] == 8
    assert any(step["id"] == "scouts" and step["status"] == "watch" for step in state["timeline"])
    assert state["cadence_policy"]["always_on_flash"]["mode"] == "continuous_sentinel"


def _write_run(tmp_path: Path) -> Path:
    run = tmp_path / "talis-scout-system-launch-test"
    raw = run / "launch-gate" / "raw"
    raw.mkdir(parents=True)
    (run / "launch-gate").mkdir(exist_ok=True)

    outputs = [
        {
            "scout_id": "scout_a",
            "seed_id": "seed_a",
            "entity": "HYPE",
            "horizon": "intraday",
            "lens": "on_chain",
            "bias_mode": "frontier",
            "hypothesis_text": "HYPE reprices higher if node absorption confirms before sellable liquidity appears.",
            "rationale_brief": "Node route absorption creates demand, scarcity, and upside pressure before consensus catches up.",
            "confidence": 0.72,
            "provider": "deepseek",
            "model_used": "deepseek:v4-flash",
            "prompt_variant": "flash_temporal_v4",
            "information_strings": [
                {
                    "id": "istr_a",
                    "title": "HYPE node absorption upside",
                    "thesis": "HYPE supply gets absorbed by high-quality nodes before sellable liquidity, creating upward repricing pressure.",
                    "mechanism": "Node absorption converts possible supply overhang into scarcity and demand confirmation.",
                    "expected_outcome": "Bids strengthen while sellable liquidity stays thin.",
                    "conviction": 0.8,
                    "novelty_score": 0.7,
                    "crowdedness": 0.4,
                }
            ],
            "tool_evidence": [{"tool_uri": "tic://tool/builtin/query_node@v1", "cost_usd": 0.001}],
            "suggested_tools": ["tic://tool/builtin/query_node@v1"],
            "tool_proposal_ids": ["atp_a"],
        },
        {
            "scout_id": "scout_b",
            "seed_id": "seed_b",
            "entity": "PURR",
            "horizon": "1d",
            "lens": "microstructure",
            "bias_mode": "mean_reversion",
            "hypothesis_text": "",
            "rationale_brief": "No fresh edge.",
            "confidence": 0.0,
            "quality_flags": ["prompt_missing_hypothesis"],
        },
    ]
    transcript = {
        "calls": [
            {"index": 0, "elapsed_s": 2.5, "system_prompt": "system", "user_prompt": "user", "text": "{\"ok\": true}"},
            {"index": 1, "elapsed_s": 3.5, "system_prompt": "system", "user_prompt": "user", "text": "{}"},
        ]
    }
    slice_preview = {
        "n_scouts": 2,
        "seed_rows": [
            {
                "seed_id": "seed_a",
                "cell_key": "HYPE|intraday|on_chain|frontier",
                "entity": "HYPE",
                "horizon": "intraday",
                "lens": "on_chain",
                "bias_mode": "frontier",
                "theme": "node_intelligence",
                "source_families": ["our_node", "hydromancer"],
                "allowed_tool_candidates_head": ["tic://tool/builtin/query_node@v1"],
                "market_evolve": {"experiment_arm": "candidate"},
            },
            {
                "seed_id": "seed_b",
                "cell_key": "PURR|1d|microstructure|mean_reversion",
                "entity": "PURR",
                "horizon": "1d",
                "lens": "microstructure",
                "bias_mode": "mean_reversion",
                "source_families": ["market_timeseries"],
                "market_evolve": {"experiment_arm": "control"},
            },
        ],
    }
    canary = {
        "cycle_id": "cycle_test",
        "mode": "live_provider_cost_capped",
        "n_scouts_requested": 2,
        "concurrency": 1,
        "metrics": {
            "scouts": {
                "completed": 2,
                "success_rate": 1.0,
                "duplicate_hypothesis_rate": 0.0,
                "avg_information_strings_per_scout": 0.5,
                "total_cost_usd_estimate": 0.02,
            }
        },
    }
    launch = {
        "decision": {
            "status": "ready_for_live_1000_ramp",
            "allowed_next_step": "live_1000_scout_ramp",
        }
    }
    tournament = {
        "promotion_decision": {
            "decision": "promote_to_1000_scout_ramp",
            "ready_for_live_1000": True,
            "reason": "passed",
        }
    }
    repair = {"status": "pass", "after": {"frontier_proposal_count": 1}, "repairs_created": 0}

    files = {
        "live_scout_canary_outputs.json": outputs,
        "live_scout_transcript.json": transcript,
        "live_scout_slice_preview.json": slice_preview,
        "live_scout_canary_report.json": canary,
        "live_scout_tournament_report.json": tournament,
        "tool_creation_contract_repair.json": repair,
        "live_scout_learning_report.json": {"next_ramp_policy": {"allowed_next_step": "live_1000_scout_ramp"}},
    }
    for name, payload in files.items():
        (raw / name).write_text(json.dumps(payload), encoding="utf-8")
    (run / "launch-gate" / "launch_gate_report.json").write_text(json.dumps(launch), encoding="utf-8")
    return run
