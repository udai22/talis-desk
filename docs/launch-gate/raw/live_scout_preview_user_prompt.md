as_of_utc=2026-05-22T15:42:45.191166+00:00
Freshness rule: for intraday/1d/1w horizons, do not use old historical dates as live catalyst evidence unless explicitly labeled as regime context.

Cell:
  entity=kFLOKI
  horizon=1d
  lens=factor
  bias_mode=mean_reversion
  theme=hyperliquid_node_intelligence

atlas_policy:
  Scouts may use any read-only approved atlas tool/source before the LLM call, under the evidence budget. The list below is the relevant subset already exposed for this cell; suggested_tools must copy from it exactly.


allowed_tool_candidates:
  tic://tool/builtin/query_timeseries@v1
  tic://tool/builtin/query_source_health@v1
  tic://tool/agent_native/find_similar_setups@v1
  tic://tool/builtin/compute_float_absorption_rate@v1
  tic://tool/builtin/compute_wallet_stats@v1
  tic://tool/builtin/solar_iv_regression@v1
  tic://tool/builtin/query_claims_by_entity@v1
  tic://tool/agent_native/find_confluence@v1
  tic://tool/agent_native/replay_artifact_reasoning@v1
  tic://tool/agent_native/semantic_search@v1

Return one JSON object only.