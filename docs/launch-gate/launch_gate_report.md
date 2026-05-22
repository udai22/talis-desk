# Scout System Launch Gate

- status: `ready_for_authorized_live_canary`
- allowed_next_step: `live_10_scout_canary`
- human_authorization_required: `True`
- reason: The deterministic layer and live preflight are clean. The next step requires explicit approval to spend on a tiny live canary.

## Deterministic 100

- status: `pass`
- ready_for_live_1000: `True`
- failed_gates: `none`

## Live Gate

- mode: `preflight_no_live_spend`
- status: `blocked`
- failed_gates: `explicit_live_spend_allowed`

## Tournament

- decision: `None`
- ready_for_live_1000: `False`
- ready_for_scheduled_production: `False`

## Proof Ladder

- PASS `deterministic_100_scout_system`: 100 scouts traverse seed generation, scout harness, storage, synthesis, geometry, self-healing, and MarketEvolve without provider spend.
- PASS `live_provider_preflight`: Provider import, tool atlas, and market universe are present before spending.
- BLOCKED `explicit_spend_gate`: No paid model calls happen unless --allow-live-spend is present.
- BLOCKED `live_canary_quality`: Live scouts must pass provider, evidence, string-yield, duplicate, synthesis, geometry, and self-healing gates.
- BLOCKED `tournament_promotion`: The tournament is the only authority for 100/1,000-scout spend promotion.
- BLOCKED `repeatability_before_schedule`: Scheduled shadow production requires repeat 1,000-scout evidence across independent runs.

## Next Command

```bash
PYTHONPATH=. python scripts/run_scout_system_launch_gate.py --allow-live-spend --live-scouts 10 --live-cost-cap-usd 0.10 --live-concurrency 1
```

Viewer: `/Users/udaikhattar/talis-desk/docs/scout-system-test/index.html`

Launch cockpit: `/Users/udaikhattar/talis-desk/docs/launch-gate/index.html`
