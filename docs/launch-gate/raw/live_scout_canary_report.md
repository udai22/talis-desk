# Live Scout Canary

- status: `pass`
- mode: `live_provider_cost_capped`
- cycle: `cycle_live_scout_canary_20260522T174212Z`
- scouts: `100`
- cost_cap_usd: `1.0`
- estimated_cost_usd: `0.64944`
- success_rate: `0.93`
- avg_strings_per_scout: `2.91`
- information_strings: `291`

## Gates

- PASS `sample_size_ge_10`
- PASS `cost_below_cap`
- PASS `provider_call_errors_eq_0`
- PASS `scout_success_rate_ge_0_70`
- PASS `avg_strings_ge_1_00`
- PASS `evidence_ok_rate_ge_0_60`
- PASS `duplicate_hypothesis_rate_le_0_35`
- PASS `provider_json_errors_within_stage_budget`
- PASS `scout_errors_within_stage_budget`
- PASS `information_strings_created`
- PASS `synthesis_promoted`
- PASS `geometry_cells_created`
- PASS `self_healing_no_failures`

## Tool Creation Contract Repair

- enabled: `True`
- status: `pass`
- repairs_created: `0`
- frontier_proposals: `202`
- quality_pass_rate: `1.0`
- eval_plan_rate: `1.0`
- expected_edge_rate: `1.0`
- PASS `tool_contract_frontier_quality_ge_0_70`
- PASS `tool_contract_eval_plan_rate_ge_0_85`
- PASS `tool_contract_expected_edge_rate_ge_0_60`
- PASS `tool_contract_would_change_decision_rate_ge_0_60`

## Decision

evaluate_live_1000_ramp_next

Run the live scout tournament evaluator over the 100-scout report. Promote to 1,000 only if the distribution gates pass.
