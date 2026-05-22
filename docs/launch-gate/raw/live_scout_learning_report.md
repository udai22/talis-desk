# Live Scout Learning Report

The live run earned `promote_to_1000_scout_ramp` with average prompt quality 0.862 and 15/100 scouts carrying at least one repair signal. The system should treat the next scale step as an evaluator-gated ramp, not production; the main repair pockets are node_not_promoted, empty_hypothesis, missing_evidence_refs.

## Scorecard

- requested: `100`
- provider_calls: `100`
- completed: `93`
- success_rate: `0.93`
- estimated_cost_usd: `0.35944`
- strings: `292`
- geometry_cells: `79`
- avg_prompt_quality: `0.8622`
- weak_scout_count: `15`

## Tournament

- decision: `promote_to_1000_scout_ramp`
- ready_for_live_1000: `True`
- ready_for_scheduled_production: `False`

## Failure Modes

- `node_not_promoted` count=52: Node payloads are captured but need stronger actor/source coverage before promotion into trade strings.
- `empty_hypothesis` count=19: Keep strict pressure: if strings exist, hypothesis must summarize the best valid string.
- `missing_evidence_refs` count=4: Strings without provided refs should become tool_requests or be rejected by prompt quality.
- `adversarial_quarantine` count=4: Quarantined strings should route to source repair or independent scout replication before verifier spend.
- `missing_information_strings` count=3: Stale/thin data should become a low-conviction gap string with evidence refs, not an empty packet.
- `invented_tools` count=2: Suggested tools are filtered against allowed_tool_candidates before persistence.
- `stale_date_directionality` count=2: Stale evidence should map source-health gaps instead of directional trade claims.
- `json_unparseable` count=1: One fallback-model JSON repair is now attempted before a non-calendar scout is dropped.

## Evolution Arms

- `harness_repair_arm`: Reduce preventable formatting/tool-contract waste before the 1,000 ramp. Gate: json_unparseable_rate == 0 and invented_tool_persist_rate == 0
- `source_freshness_gap_arm`: Convert stale/thin evidence into auditable gap strings and follow-up tool requests. Gate: low_prompt_quality_rate <= 0.05 and stale_directionality_flags <= 0.01
- `node_intelligence_promotion_arm`: Make Hydromancer/HL-node observations first-class graph edges instead of sidecar context. Gate: node_promoted_string_rate increases without adversarial quarantine rising
- `geometry_replication_arm`: Let the map shape choose independent follow-up scouts for high tension/frontier cells. Gate: top geometry cells receive independent scout confirmation, contradiction, or kill signal
- `market_evolve_policy_arena`: Continue matched control/candidate policy testing instead of hand-picking a prompt by taste. Gate: candidate improves accepted unique high-quality coverage per dollar without worsening low-EV tool rate

## Next Run

- allowed_next_step: `live_1000_scout_ramp`
- operator_note: The 1,000 ramp is allowed by tournament evidence, but scheduled production remains blocked until two independent 1,000-scout shadow runs pass repeatability gates.
