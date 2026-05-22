# Scout System Launch Gate

- status: `ready_for_live_1000_ramp`
- allowed_next_step: `live_1000_scout_ramp`
- human_authorization_required: `True`
- reason: The live tournament passed distribution gates. A capped 1,000-scout ramp is allowed, not scheduled production.

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

- decision: `promote_to_1000_scout_ramp`
- ready_for_live_1000: `True`
- ready_for_scheduled_production: `False`

## Proof Ladder

- PASS `deterministic_100_scout_system`: 100 scouts traverse seed generation, scout harness, storage, synthesis, geometry, self-healing, and MarketEvolve without provider spend.
- PASS `live_provider_preflight`: Provider import, tool atlas, and market universe are present before spending.
- PASS `explicit_spend_gate`: No paid model calls happen unless --allow-live-spend is present.
- PASS `live_canary_quality`: Live scouts must pass provider, evidence, string-yield, duplicate, synthesis, geometry, and self-healing gates.
- PASS `tournament_promotion`: The tournament is the only authority for 100/1,000-scout spend promotion.
- BLOCKED `repeatability_before_schedule`: Scheduled shadow production requires repeat 1,000-scout evidence across independent runs.

## Next Command

```bash
PYTHONPATH=. python scripts/run_scout_system_launch_gate.py --allow-live-spend --live-scouts 1000 --live-cost-cap-usd 5.00 --live-concurrency 8 --max-tool-iterations 1
```

Viewer: `/Users/udaikhattar/talis-desk/docs/scout-system-test/index.html`

Launch cockpit: `/Users/udaikhattar/talis-desk/docs/launch-gate/index.html`
