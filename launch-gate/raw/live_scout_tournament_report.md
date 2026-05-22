# Live Scout Tournament

- decision: `promote_to_1000_scout_ramp`
- ready_for_live_100: `True`
- ready_for_live_1000: `True`
- ready_for_scheduled_production: `False`
- reason: The 100+ scout live distribution passed provider reliability, string yield, duplicate, prompt-quality, temporal-structure, geometry, self-healing, and MarketEvolve proof gates. A 1,000-scout ramp is allowed under a hard cap; it is still a ramp, not an always-on production schedule.
- repeatability_ready: `False`
- repeatability_runs: `0/2`

## Candidates

### flash_temporal_v4_deepseek_v4_flash_n100_t45_0_c4_iter1_live_100

- score: `0.9625`
- promotion_eligible: `True`
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
- failed_gates: `none`

This 100-scout distribution is clean enough for a capped 1,000-scout ramp, but not scheduled production.

## Next Experiment Plan

- `live_1000_ramp`: Validate the winning policy at broad market-sensing scale under a hard cap.
  - command: `PYTHONPATH=.:talis_tic python scripts/run_live_scout_canary.py --n-scouts 1000 --concurrency 8 --cost-cap-usd 5.00 --provider-timeout-s 45 --prompt-variant flash_temporal_v4 --max-tool-iterations 1 --allow-live-spend`
  - promotion_rule: Promote to a repeat 1,000-scout shadow trial only if the 1,000-scout run keeps provider errors <= 0.02, success >= 0.9, duplicate rate <= 0.2, structural misses <= 0.1, and produces usable geometry/coverage deltas. Scheduled production remains blocked until an independent repeat 1,000 run passes.
