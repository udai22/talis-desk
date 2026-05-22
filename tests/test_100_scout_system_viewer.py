import json

from scripts.export_100_scout_system_viewer import build_packet, render_html
from scripts.run_100_scout_readiness_slice import (
    _attach_effective_unique_seed_metrics,
    _readiness_verdict,
)


def test_readiness_counts_matched_experiment_pairs_as_intentional_coverage():
    metrics = {
        "seeds": {
            "count": 100,
            "unique_cell_count": 80,
            "unique_cell_ratio": 0.80,
        },
        "scouts": {
            "success_rate": 1.0,
            "avg_information_strings_per_scout": 3.0,
            "evidence_ok_rate": 1.0,
            "duplicate_hypothesis_rate": 0.20,
        },
        "information_map": {"promoted_hypotheses": 8},
        "geometry": {"cell_count": 80, "routing_queue_count": 24},
        "self_healing": {"completed_tasks": 6, "failed_tasks": 0},
        "market_evolve": {
            "policy_application_count": 100,
            "planning_experiment_count": 2,
            "paired_seed_slices": 20,
            "arm_counts": {"control": 50, "candidate": 50},
            "experiment_result_count": 1,
        },
    }

    _attach_effective_unique_seed_metrics(metrics)
    verdict = _readiness_verdict(metrics)

    assert metrics["seeds"]["effective_unique_cell_ratio"] == 1.0
    assert verdict["status"] == "pass"
    assert verdict["ready_for_live_1000"] is True
    assert verdict["gates"]["unique_or_experiment_cell_ratio_ge_0_92"] is True


def test_100_scout_system_viewer_renders_evolution_and_geometry_story(tmp_path):
    prompt_dir = tmp_path / "prompt_outputs"
    prompt_dir.mkdir()
    report_path = prompt_dir / "100_scout_readiness_report.json"
    _write_json(
        report_path,
        {
            "cycle_id": "cycle_test_100",
            "metrics": {
                "seeds": {"count": 100, "effective_unique_cell_ratio": 1.0},
                "scouts": {
                    "completed": 100,
                    "errored": 0,
                    "duplicate_hypothesis_rate": 0.2,
                    "evidence_ok_rate": 1.0,
                    "total_cost_usd_estimate": 0.06864,
                    "prompt_variants": {"temporal_pyramid_v1": 13},
                },
                "information_map": {"string_count": 337, "promoted_hypotheses": 8},
                "geometry": {"cell_count": 80, "routing_queue_count": 24},
                "self_healing": {"completed_tasks": 6, "failed_tasks": 0},
                "market_evolve": {
                    "paired_seed_slices": 20,
                    "arm_counts": {"control": 50, "candidate": 50},
                    "policy_application_count": 100,
                },
            },
            "readiness": {
                "status": "pass",
                "ready_for_live_1000": True,
                "failed_gates": [],
                "gates": {
                    "seed_count_100": True,
                    "market_evolve_control_candidate_arms": True,
                },
            },
        },
    )
    _write_json(
        prompt_dir / "alpha_geometry.json",
        {
            "global_metrics": {"avg_trade_scream_score": 0.5},
            "cells": [
                {
                    "cell_key": "HYPE|intraday|on_chain|test",
                    "entity": "HYPE",
                    "horizon": "intraday",
                    "lens": "on_chain",
                    "theme": "test",
                    "route_directive": "verify_now",
                    "trade_scream_score": 0.72,
                    "verifier_readiness": 0.81,
                    "metrics": {"source_independence": 0.67, "fragility": 0.2},
                }
            ],
            "action_plan": {
                "actions": [
                    {
                        "action": "verify_cell",
                        "owner": "verifier",
                        "cell_key": "HYPE|intraday|on_chain|test",
                        "priority_score": 0.91,
                        "success_gate": "independent source confirms edge",
                        "missing_edges": ["wallet_quality"],
                    }
                ]
            },
        },
    )
    _write_json(
        prompt_dir / "alpha_geometry_cortex_review.json",
        {"status": "ready", "shape_can_direct_next": True, "cortex_work_orders": []},
    )
    _write_json(
        prompt_dir / "market_evolve_hard_experiment.json",
        {
            "status": "evaluated",
            "final_decision": "reject_candidate",
            "final_score_delta": -0.0003,
            "proof": {
                "policy_stamped_on_seeds": True,
                "candidate_arm_present": True,
                "control_arm_present": True,
                "falsification_gates_evaluated": True,
            },
        },
    )
    _write_json(
        prompt_dir / "market_evolve_step.json",
        {"best_evaluation": {"score": 0.5777, "passed": True}},
    )
    _write_json(
        prompt_dir / "market_evolve_lineage.json",
        {
            "nodes": [{"program_id": "mprog_active"}],
            "edges": [{"from_program_id": "mprog_active", "to_program_id": "mprog_child"}],
            "frontier": [{"name": "rosewood_research_policy_v1", "next_action": "mutate_active_policy"}],
        },
    )

    packet = build_packet(prompt_dir=prompt_dir, report_path=report_path)
    page = render_html(packet)

    assert packet["ready_for_live_1000"] is True
    assert packet["summary"]["strings"] == 337
    assert packet["market_evolve"]["experiment_decision"] == "reject_candidate"
    assert packet["geometry"]["top_cells"][0]["route_directive"] == "verify_now"
    assert "The first layer is ready for a guarded live scale test." in page
    assert "MarketEvolve" in page
    assert "reject candidate" in page
    assert "337" in page


def _write_json(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
