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

## Repair Work Orders

- `lso_json_unparseable_scout_harness` owner=scout_harness priority=P0: json_unparseable_rate == 0 across the next ramp or every parse miss has scout_json_retry_success.
- `lso_invented_tools_tool_harness` owner=tool_harness priority=P0: invented_tool_persist_rate == 0; proposed missing tools appear only in tool_requests/tool_proposals.
- `lso_empty_hypothesis_prompt_policy` owner=prompt_policy priority=P1: empty_hypothesis_with_strings_rate == 0 and low_prompt_quality_rate <= 0.05.
- `lso_missing_information_strings_prompt_policy` owner=prompt_policy priority=P1: missing_information_strings_rate <= 0.01 for scouts with usable evidence or allowed repair tools.
- `lso_missing_evidence_refs_source_router` owner=source_router priority=P1: missing_evidence_refs_rate <= 0.01 and tool_request_followup_rate rises on missing-edge cells.
- `lso_stale_date_directionality_source_router` owner=source_router priority=P1: stale_directionality_flag_rate <= 0.01 and stale evidence produces repair_source/gap strings.
- `lso_adversarial_quarantine_adversarial_reviewer` owner=adversarial_reviewer priority=P1: quarantine_to_repair_route_rate >= 0.95 and quarantined strings are excluded from promoted trade hypotheses.
- `lso_node_not_promoted_node_intelligence` owner=node_intelligence priority=P2: node_promoted_string_rate improves while adversarial_quarantine_rate does not rise.
- `lso_geometry_replication` owner=seed_router priority=P1: Each top cell receives confirmation, contradiction, or kill signal from an independent scout/source family.
- `lso_market_evolve_arena` owner=market_evolve priority=P1: Candidate arm wins hard gates or is rejected with falsification evidence.

## Pre-1000 Gate

- ready_for_authorized_1000: `True`
- must_watch: `accepted_unique_high_quality_coverage_per_dollar_delta, empty_hypothesis_with_strings_rate, invented_tool_persist_rate, json_unparseable_rate, missing_evidence_refs_rate, missing_information_strings_rate, quarantine_to_repair_route_rate, stale_directionality_flag_rate, top_geometry_confirmation_rate`

## Executable Ramp Policy

- policy_id: `lrp_cycle_live100_20260522_live_100_100`
- seed_patch: `{"max_evidence_tools_min": 6, "max_tool_iterations": 2, "preserve_missing_tool_as_proposal": true, "prompt_contract_pressure": "strict", "prompt_min_information_strings": 2, "prompt_require_evidence_refs": true, "prompt_require_kill_signal": true, "prompt_require_mechanism": true, "quarantine_before_verifier_spend": true, "source_family_targets_append": ["event_feed", "hydromancer", "market_timeseries", "our_hl_node", "our_node", "parallel_web", "source_health"], "stale_evidence_becomes_gap_string": true, "suggested_tool_allowlist_only": true, "tool_candidate_limit_min": 12}`
- ramp_policy_artifact: `/tmp/talis-live100-20260522/live_canary/prompt_outputs/live_scout_ramp_policy.json`

## Next Run

- allowed_next_step: `live_1000_scout_ramp`
- operator_note: The 1,000 ramp is allowed by tournament evidence, but scheduled production remains blocked until two independent 1,000-scout shadow runs pass repeatability gates.
