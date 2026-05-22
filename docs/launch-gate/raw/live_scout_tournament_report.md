# Live Scout Tournament

- decision: `no_promotion`
- ready_for_live_100: `False`
- ready_for_live_1000: `False`
- ready_for_scheduled_production: `False`
- reason: The top candidate `flash_temporal_v4_deepseek_v4_flash_n100_t45_0_c4_iter1_live_100` still failed: tool_creation_quality_pass_rate_ge_0_70, tool_creation_eval_plan_rate_ge_0_85, tool_creation_expected_edge_rate_ge_0_60, tool_creation_would_change_decision_rate_ge_0_60. Do not promote the 100-scout stage yet.
- repeatability_ready: `False`
- repeatability_runs: `0/2`

## Candidates

### flash_temporal_v4_deepseek_v4_flash_n100_t45_0_c4_iter1_live_100

- score: `0.932`
- promotion_eligible: `False`
- scouts: `93/100`
- success_rate: `0.93`
- provider_error_rate: `0.0`
- tool_error_rate: `0.0`
- strings_per_scout: `2.92`
- duplicate_rate: `0.06`
- avg_latency_s: `16.662`
- market_evolve_pairs: `20`
- market_evolve_decision: `reject_candidate`
- market_evolve_proof: `policy=True arms=True falsified=True`
- ramp_policy_rehearsal: `required=True observed=True status=pass decision=policy_can_gate_live_spend`
- ramp_policy_metrics: `tool_refresh=1.0 source_coverage=1.0 over_limit=0`
- tool_creation: `required=True proposals=189 quality_pass=0.4127 eval_plan=0.4127 expected_edge=0.0212`
- failed_gates: `tool_creation_quality_pass_rate_ge_0_70, tool_creation_eval_plan_rate_ge_0_85, tool_creation_expected_edge_rate_ge_0_60, tool_creation_would_change_decision_rate_ge_0_60`

Blocked from scale because agent-created tool proposals are not yet evaluator-grade.

## Next Experiment Plan

- `tool_creation_quality_repair_100`: Repair the agent-created tool surface before buying a larger scout ramp. Scouts may request and create tools, but every proposed tool needs an expected market-map edge, decision-change claim, and deterministic eval plan.
  - command: `PYTHONPATH=. python scripts/run_scout_system_launch_gate.py --allow-live-spend --live-scouts 100 --live-cost-cap-usd 1.00 --live-concurrency 4 --max-tool-iterations 1 --ramp-policy /Users/udaikhattar/talis-desk/docs/launch-gate/raw/live_scout_ramp_policy.json`
  - promotion_rule: Do not promote to 1,000 until analysis_tool_proposals pass quality >= 70%, eval-plan attachment >= 85%, expected-edge attachment >= 60%, decision-change attachment >= 60%, and eval/runtime backlog stays bounded.
