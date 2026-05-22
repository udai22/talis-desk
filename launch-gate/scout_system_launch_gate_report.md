# Scout System Launch Gate

- status: `blocked_by_tournament`
- allowed_next_step: `tool_creation_quality_repair_100`
- human_authorization_required: `False`
- reason: The live tournament blocked promotion. Repair the failed gates before increasing scout spend.

## Deterministic 100

- status: `pass`
- ready_for_live_1000: `True`
- failed_gates: `none`

## Live Gate

- mode: `live_provider_cost_capped`
- status: `pass`
- failed_gates: `none`
- first_scout_cell: `kFLOKI / 1d / factor / mean_reversion`
- first_scout_prompt_variant: `flash_temporal_v4`
- first_scout_tool_candidates: `10`
- planned_slice_scouts: `100`
- planned_slice_unique_cells: `80`
- planned_slice_market_evolve_arms: `candidate 50 / control 50`

## Tournament

- decision: `no_promotion`
- ready_for_live_1000: `False`
- ready_for_scheduled_production: `False`
- failed_gates: `tool_creation_quality_pass_rate_ge_0_70, tool_creation_eval_plan_rate_ge_0_85, tool_creation_expected_edge_rate_ge_0_60, tool_creation_would_change_decision_rate_ge_0_60`

## Proof Ladder

- PASS `deterministic_100_scout_system`: 100 scouts traverse seed generation, scout harness, storage, synthesis, geometry, self-healing, and MarketEvolve without provider spend.
- PASS `live_provider_preflight`: Provider import, tool atlas, and market universe are present before spending.
- PASS `explicit_spend_gate`: No paid model calls happen unless --allow-live-spend is present.
- PASS `live_canary_quality`: Live scouts must pass provider, evidence, string-yield, duplicate, synthesis, geometry, and self-healing gates.
- BLOCKED `tournament_promotion`: Tournament blocked scale because agent-created tool proposals were not evaluator-grade.
- BLOCKED `repeatability_before_schedule`: Scheduled shadow production requires repeat 1,000-scout evidence across independent runs.

## Next Command

```bash
PYTHONPATH=. python scripts/run_scout_system_launch_gate.py --allow-live-spend --live-scouts 100 --live-cost-cap-usd 1.00 --live-concurrency 4 --max-tool-iterations 1 --ramp-policy /Users/udaikhattar/talis-desk/docs/launch-gate/raw/live_scout_ramp_policy.json
```

Viewer: `/Users/udaikhattar/talis-desk/docs/scout-system-test/index.html`

Launch cockpit: `/Users/udaikhattar/talis-desk/docs/launch-gate/index.html`
