(query: 'str', kinds: 'Optional[list[str]]' = None, k: 'int' = 20, since_hours: 'Optional[int]' = None, retrieval_mode: 'str' = 'hybrid') -> 'list[dict[str, Any]]'

codex
# Talis Daily Research Desk Prod-Readiness Review

Blunt version: this run did not prove an autonomous Hyperliquid research desk. It proved that the shell around the desk can register specialists, ask an LLM for hypotheses, loop 300 times per specialist, and render a markdown brief. The actual research path was mostly dead: the tool atlas was empty, persona tool URIs were accepted anyway, 1,200 dispatches mostly failed as `tool_uri_not_in_atlas`, debates were only “trigger decisions” and never opened/judged, and the one published trade idea was a synthetic flat/no-entry placeholder from a housekeeping hypothesis.

## Root Cause

Only 1 trade idea because the pipeline currently emits trade ideas only from `supported` hypotheses, `supported` means final posterior `>= 0.7`, real evidence does not actually move posterior unless a tool result contains synthetic `_posterior_delta`, and in the latest run the atlas was empty so all persona tools failed at dispatch. The only idea that slipped through was microstructure’s “oracle drift is within tolerance” hypothesis, turned into a `flat` paper idea with confidence ratcheted down to `0.69`.

Zero peer messages because the only peer-message paths are thresholded on posterior movement or hot branches, and the failed tool calls produced no scored hot signals. Separately, `maybe_trigger_debate()` only returns decisions; the loop never calls `open_debate()` or `run_full_debate_cycle()`, so “24 debates triggered” really means “24 booleans collected, 0 debates created, 0 judged.”

## Severity 1

### 1. Tool atlas is never regenerated before the full desk run

**What:** `run_full_desk.py` freezes an empty `tool_atlas` and then runs all specialists against it.

**Why:** This is the root cause of the fake-looking output. In the observed run, `tool_atlas` had 0 rows, yet each specialist still ran 300 calls. The DB shows nearly every call failed with `resolve_error: 'tool_uri_not_in_atlas: ...'`. That means the 36k-claim TIC layer was not actually being queried through the desk.

**Fix:** In `run_full_desk.py:152-156`, immediately after pinning `_store_mod._STORE`, call `regenerate_tool_atlas()` and fail fast if it returns no rows:
```python
from talis_desk.tool_atlas.atlas import regenerate_tool_atlas
atlas = regenerate_tool_atlas()
if not atlas.rows:
    raise RuntimeError("tool atlas empty; refusing to run desk cycle")
```
Also add a preflight print/assert for `n_tools`, `n_sources`, `n_skills`.

### 2. `_parse_plan_payload` accepts persona fallback URIs that are not in the atlas

**What:** `loop/runner.py:734-740` drops invalid LLM-selected URIs, then falls back to `persona_uris[:2]` without checking whether those persona URIs exist in the frozen atlas.

**Why:** This converts an atlas/config failure into 1,200 fake “tool calls.” The LLM may have chosen bad URIs, but the bigger bug is that the runner repaired the plan into known-unresolvable calls instead of failing the cycle.

**Fix:** In `loop/runner.py:736-740`, filter fallback persona URIs against `atlas_uris`; if none remain, raise a structured `PlanToolResolutionError`. Do not silently use `atlas.rows[0]` either unless the tool schema fits the hypothesis.

### 3. Tool-call failures are treated as research evidence

**What:** `exploration/bfs.py:770-805` scores and updates posterior even when `ToolResult.ok` is false.

**Why:** A failed resolver call should not become a support/contradict edge or count toward investigation quality. Right now failed calls still create `tool_call_log_id`s, consume budget, and can be linked as evidence. This contaminates hypotheses and hides source-health failures.

**Fix:** In `exploration/bfs.py:770-789`, branch on `if not result.ok`: append an error step, increment failure metrics, do not call `score_hot_signal`, `link_evidence`, or `update_posterior`, and consider aborting an investigation if failure share exceeds a threshold.

### 4. Posterior scoring is synthetic, not calibrated to real TIC outputs

**What:** `score_hot_signal()` only reads `_posterior_delta`, `_surprise`, and `_novelty` from result payloads; real tool outputs default to zero movement (`exploration/bfs.py:571-586`, `589-629`).

**Why:** Even after the atlas is fixed, most real tools will not produce `_posterior_delta`, so the desk will keep doing hundreds of calls with little posterior movement. Trade ideas are gated on posterior thresholds, so idea volume stays structurally low.

**Fix:** Add a real evidence evaluator between dispatch and posterior update: `talis_desk/exploration/evidence_scorer.py`. It should take hypothesis text, question, tool result summary, source health, and prior posterior, then return signed delta, citations, contradiction flag, and confidence. Call it from `exploration/bfs.py:775` before `score_hot_signal`.

### 5. “Debates triggered” are never opened or judged

**What:** `synthesize()` calls `maybe_trigger_debate()` (`loop/runner.py:1236-1249`, `1276-1289`) but never calls `debate.runner.open_debate()` or `run_full_debate_cycle()`.

**Why:** The run reported 24 debate triggers but the DB has 0 rows in `debates` and 0 judged debates. This removes one of the system’s main SOTA claims: adversarial specialist interaction.

**Fix:** In `loop/runner.py:synthesize`, after a `decision.should_trigger`, enqueue/open an actual debate with two distinct participants. Add a desk-level debate coordinator that pairs the triggering specialist with the most relevant opponent, then calls `open_debate()` from `debate/runner.py:262`.

## Severity 2

### 6. Trade idea emission is a minimal placeholder, not specialist synthesis

**What:** `_build_minimal_trade_idea_draft()` fabricates a generic draft from a supported hypothesis using placeholder prices (`100`, `99`, `101`) and often `flat` direction (`loop/runner.py:1024-1154`).

**Why:** This is why the one idea feels embarrassing: it is not a specialist-authored HL setup with real entry, stop, target, and market context. It is a safety stub that happens to pass validation.

**Fix:** Replace `_build_minimal_trade_idea_draft()` with an LLM synthesis stage that receives resolved hypotheses, top tool results, source health, and market snapshots, and asks for 0-N `TradeIdeaDraft` JSON objects. Validate them with `validate_trade_idea`, but do not fabricate prices.

### 7. Idea volume is gated too narrowly on `status == "supported"`

**What:** `loop/runner.py:1252-1273` emits only when a hypothesis resolves as `supported`; `_resolve_hypothesis_status()` requires posterior `>=0.7` (`loop/runner.py:1010-1021`).

**Why:** Daily briefs need a richer artifact set: actionable trades, watchlist setups, “no trade because X”, invalidation alerts, and contradiction-led fades. A single posterior threshold turns the desk into a sparse binary classifier.

**Fix:** Add a candidate generation layer before final validation:
- `supported >= 0.7` → trade idea candidate
- `0.55-0.7` with high heat/confluence → watchlist setup
- `<=0.3` contradiction on crowded thesis → fade candidate
- failed quality gates → “blocked idea” with reason for brief

### 8. Brief cycle ID does not match specialist cycle IDs

**What:** `run_full_desk.py:86-89` runs cycles as `prod_...__specialist`, but `compose_brief()` is called with the base `cycle_id` at `run_full_desk.py:209-213`.

**Why:** The rendered brief’s Cycle Stats showed 0 tool calls, 0 hypotheses, 0 trade ideas, and 0 debates even though the manifest showed 1,200 calls and 24 hypotheses. That makes prod observability misleading.

**Fix:** Introduce a `desk_run_id` separate from per-specialist `cycle_id`, or make all tables carry both. Update `compose_brief()` to accept `cycle_ids: list[str]` and aggregate over all specialist cycle IDs.

### 9. Source-health kill switch misses failed tool calls

**What:** `_check_kill_switches()` counts failed calls only when `tool_call_log_id is None` (`loop/runner.py:1666-1675`).

**Why:** Failed dispatches still get a `tool_call_log_id`, so the observed 1,200 failed tool calls did not trigger a source-health quality flag or kill switch.

**Fix:** Add `ok`/`error` to `ExplorationStep`, or fetch `tool_call_log.error` during kill-switch evaluation. Trip if failed calls exceed 30%, and fail hard if `tool_uri_not_in_atlas` appears.

## Severity 3

### 10. Planner prompt does not force valid schema-aware tool arguments

**What:** `_build_plan_user_prompt()` lists URI and description only (`loop/runner.py:650-652`); `_default_args_for_uri()` later guesses `{"entity_symbol": entities[0]}` for every tool (`loop/runner.py:991-1002`).

**Why:** Even with a fixed atlas, many TIC tools require `coin`, `metric_prefix`, `query`, or `wallet_address`, not `entity_symbol`. The planner picks tools, but never specifies arguments, so execution will still produce bad-args failures.

**Fix:** Extend `HypothesisDraftPlan` with `tool_calls: [{uri, args, purpose}]`. Include each tool’s `input_schema` in the plan prompt. Delete `_default_args_for_uri()` for production paths.

### 11. Scratchpad posting is too passive and thresholded

**What:** Peer messages are only posted on cumulative posterior delta `>0.2` in synthesize (`loop/runner.py:1189-1213`) or hot branch spawn in BFS (`exploration/bfs.py:808-839`).

**Why:** Specialists cannot cross-pollinate during the cycle unless the posterior engine already works. Since it does not, peer messages stay at zero. Also, persona prompts ask specialists to post 0-5 messages, but the runtime never lets the LLM choose messages in SYNTHESIZE.

**Fix:** Add a `peer_messages` JSON field to the LLM synthesis output and call `post_message()` for each validated message. For sequential mode, run lightweight synthesis/posting after each specialist so later specialists actually hydrate prior messages.

### 12. Director exists but is not in the cycle path

**What:** `director/research_director.py` defines curriculum allocation, but `run_full_desk.py` registers specialists and runs them directly without a director pass.

**Why:** The system has no desk-level agenda, no coverage balancing, and no explicit “we need 5-10 user-worthy ideas today” objective. The four agents independently generate hypotheses and the composer renders whatever happens.

**Fix:** In `run_full_desk.py`, run `propose_curriculum()` before specialists, write assignments to `agent_messages`, then hydrate specialists. Add a minimum brief artifact target: e.g. 3 published ideas, 5 watchlist setups, or explicit “blocked because...” sections.

## Severity 4

### 13. Brief composer mostly renders sections instead of weaving a desk narrative

**What:** `compose_brief()` calls the LLM only for a headline (`brief/composer.py:309-325`) and then renders tables/sections (`brief/composer.py:365-388`).

**Why:** With rich data, this still feels thin because it does not reconcile specialists, explain disagreements, or turn failed/blocked candidates into useful user-facing intelligence.

**Fix:** Add `compose_narrative()` after data fetch: feed open ideas, blocked candidates, hot hypotheses, failed sources, and specialist summaries into one LLM call that outputs a structured daily brief narrative. Keep tables as appendices.

### 14. Specialist isolation is catch-and-continue, but not operationally visible enough

**What:** `run_one_cycle()` catches failures and returns an error dict (`run_full_desk.py:85-113`), but there are no structured logs, alert hooks, retries, or partial-output health policy.

**Why:** For 30 days unattended, silent degradation is the main enemy. A specialist can produce zero useful artifacts and the run still composes a brief.

**Fix:** Add structured JSON logs for every stage, per-specialist status, fatal vs degraded classification, and alert thresholds: zero atlas rows, >20% tool failures, zero usable ideas for 2 cycles, cost anomaly, source-health degradation.

### 15. Desk-wide cost cap is not implemented

**What:** Per-specialist caps exist in `LoopConfig` (`loop/runner.py:192-215`), and kill switch checks per-cycle overrun (`loop/runner.py:1641-1664`), but there is no actual $100/day desk-wide accumulator.

**Why:** Four specialists at today’s cost are cheap, but once real LLM synthesis, debate, search, and parallel work are wired, runaway spend becomes possible.

**Fix:** Add `cost_ledger` table keyed by UTC date and stage. Before every LLM/tool/debate call, reserve budget. Hard fail into paper-only/no-LLM mode at `$100/day`.

## Severity 5

### 16. Hardcoded local paths and `sys.path` injection are not production-safe

**What:** Hardcoded `/Users/udaikhattar/.../brief_experiments` appears in `loop/runner.py:122`, `run_full_desk.py:30`, `run_daily_brief.py:22`, and `debate/judge.py:272-276`.

**Why:** This will break on any server, worker, container, or teammate machine. It is also a security smell because arbitrary local path precedence can shadow imports.

**Fix:** Package `talis-tic` as an installable dependency, or configure `TALIS_TIC_ROOT` / `TALIS_TIC_DB_PATH` via env with validation. Remove direct `sys.path.insert()` from runtime modules.

### 17. SQLite prod story lacks migration and backup discipline

**What:** `DeskStore` auto-applies schema to `~/.talis/desk.db` (`store/__init__.py:21-42`) with no migration versioning, backup job, retention policy, or corruption recovery.

**Why:** Thirty unattended days means the artifact DB is the product memory. Losing it loses hypotheses, trade ideas, debates, and evaluation lineage.

**Fix:** Add migrations with schema version checks, daily `VACUUM INTO` backup, WAL checkpointing, restore test, and explicit TIC/desk DB paths in deploy config.

## S-tier Target Tweaks

1. Run `regenerate_tool_atlas()` and schema/tool preflight first; fail closed on empty atlas or URI mismatch.

2. Make PLAN produce schema-valid tool calls, not just URIs.

3. Add a real evidence scorer so TIC outputs move posterior.

4. Add LLM SYNTHESIZE that emits multiple candidates: trade ideas, watchlist setups, blocked ideas, and peer messages.

5. Open real debates when `maybe_trigger_debate()` fires; require judged debates before high-confidence publication.

6. Run Director before specialists, then specialists sequentially or in two waves so scratchpad messages matter.

7. Compose the brief with a narrative LLM pass that reconciles the whole desk, then render tables.

8. Add desk-wide cost ledger, structured metrics, alerts, DB backups, and remove local path injection.

Ship call: not prod-ready. The architecture is promising, but today’s E2E is mostly scaffolding. The first production gate should be: non-empty atlas, <5% tool resolution failures, real posterior movement from real tool payloads, at least one actual debate row when triggers fire, and a brief whose cycle stats match the run manifest.
tokens used
168,958
# Talis Daily Research Desk Prod-Readiness Review

Blunt version: this run did not prove an autonomous Hyperliquid research desk. It proved that the shell around the desk can register specialists, ask an LLM for hypotheses, loop 300 times per specialist, and render a markdown brief. The actual research path was mostly dead: the tool atlas was empty, persona tool URIs were accepted anyway, 1,200 dispatches mostly failed as `tool_uri_not_in_atlas`, debates were only “trigger decisions” and never opened/judged, and the one published trade idea was a synthetic flat/no-entry placeholder from a housekeeping hypothesis.

## Root Cause

Only 1 trade idea because the pipeline currently emits trade ideas only from `supported` hypotheses, `supported` means final posterior `>= 0.7`, real evidence does not actually move posterior unless a tool result contains synthetic `_posterior_delta`, and in the latest run the atlas was empty so all persona tools failed at dispatch. The only idea that slipped through was microstructure’s “oracle drift is within tolerance” hypothesis, turned into a `flat` paper idea with confidence ratcheted down to `0.69`.

Zero peer messages because the only peer-message paths are thresholded on posterior movement or hot branches, and the failed tool calls produced no scored hot signals. Separately, `maybe_trigger_debate()` only returns decisions; the loop never calls `open_debate()` or `run_full_debate_cycle()`, so “24 debates triggered” really means “24 booleans collected, 0 debates created, 0 judged.”

## Severity 1

### 1. Tool atlas is never regenerated before the full desk run

**What:** `run_full_desk.py` freezes an empty `tool_atlas` and then runs all specialists against it.

**Why:** This is the root cause of the fake-looking output. In the observed run, `tool_atlas` had 0 rows, yet each specialist still ran 300 calls. The DB shows nearly every call failed with `resolve_error: 'tool_uri_not_in_atlas: ...'`. That means the 36k-claim TIC layer was not actually being queried through the desk.

**Fix:** In `run_full_desk.py:152-156`, immediately after pinning `_store_mod._STORE`, call `regenerate_tool_atlas()` and fail fast if it returns no rows:
```python
from talis_desk.tool_atlas.atlas import regenerate_tool_atlas
atlas = regenerate_tool_atlas()
if not atlas.rows:
    raise RuntimeError("tool atlas empty; refusing to run desk cycle")
```
Also add a preflight print/assert for `n_tools`, `n_sources`, `n_skills`.

### 2. `_parse_plan_payload` accepts persona fallback URIs that are not in the atlas

**What:** `loop/runner.py:734-740` drops invalid LLM-selected URIs, then falls back to `persona_uris[:2]` without checking whether those persona URIs exist in the frozen atlas.

**Why:** This converts an atlas/config failure into 1,200 fake “tool calls.” The LLM may have chosen bad URIs, but the bigger bug is that the runner repaired the plan into known-unresolvable calls instead of failing the cycle.

**Fix:** In `loop/runner.py:736-740`, filter fallback persona URIs against `atlas_uris`; if none remain, raise a structured `PlanToolResolutionError`. Do not silently use `atlas.rows[0]` either unless the tool schema fits the hypothesis.

### 3. Tool-call failures are treated as research evidence

**What:** `exploration/bfs.py:770-805` scores and updates posterior even when `ToolResult.ok` is false.

**Why:** A failed resolver call should not become a support/contradict edge or count toward investigation quality. Right now failed calls still create `tool_call_log_id`s, consume budget, and can be linked as evidence. This contaminates hypotheses and hides source-health failures.

**Fix:** In `exploration/bfs.py:770-789`, branch on `if not result.ok`: append an error step, increment failure metrics, do not call `score_hot_signal`, `link_evidence`, or `update_posterior`, and consider aborting an investigation if failure share exceeds a threshold.

### 4. Posterior scoring is synthetic, not calibrated to real TIC outputs

**What:** `score_hot_signal()` only reads `_posterior_delta`, `_surprise`, and `_novelty` from result payloads; real tool outputs default to zero movement (`exploration/bfs.py:571-586`, `589-629`).

**Why:** Even after the atlas is fixed, most real tools will not produce `_posterior_delta`, so the desk will keep doing hundreds of calls with little posterior movement. Trade ideas are gated on posterior thresholds, so idea volume stays structurally low.

**Fix:** Add a real evidence evaluator between dispatch and posterior update: `talis_desk/exploration/evidence_scorer.py`. It should take hypothesis text, question, tool result summary, source health, and prior posterior, then return signed delta, citations, contradiction flag, and confidence. Call it from `exploration/bfs.py:775` before `score_hot_signal`.

### 5. “Debates triggered” are never opened or judged

**What:** `synthesize()` calls `maybe_trigger_debate()` (`loop/runner.py:1236-1249`, `1276-1289`) but never calls `debate.runner.open_debate()` or `run_full_debate_cycle()`.

**Why:** The run reported 24 debate triggers but the DB has 0 rows in `debates` and 0 judged debates. This removes one of the system’s main SOTA claims: adversarial specialist interaction.

**Fix:** In `loop/runner.py:synthesize`, after a `decision.should_trigger`, enqueue/open an actual debate with two distinct participants. Add a desk-level debate coordinator that pairs the triggering specialist with the most relevant opponent, then calls `open_debate()` from `debate/runner.py:262`.

## Severity 2

### 6. Trade idea emission is a minimal placeholder, not specialist synthesis

**What:** `_build_minimal_trade_idea_draft()` fabricates a generic draft from a supported hypothesis using placeholder prices (`100`, `99`, `101`) and often `flat` direction (`loop/runner.py:1024-1154`).

**Why:** This is why the one idea feels embarrassing: it is not a specialist-authored HL setup with real entry, stop, target, and market context. It is a safety stub that happens to pass validation.

**Fix:** Replace `_build_minimal_trade_idea_draft()` with an LLM synthesis stage that receives resolved hypotheses, top tool results, source health, and market snapshots, and asks for 0-N `TradeIdeaDraft` JSON objects. Validate them with `validate_trade_idea`, but do not fabricate prices.

### 7. Idea volume is gated too narrowly on `status == "supported"`

**What:** `loop/runner.py:1252-1273` emits only when a hypothesis resolves as `supported`; `_resolve_hypothesis_status()` requires posterior `>=0.7` (`loop/runner.py:1010-1021`).

**Why:** Daily briefs need a richer artifact set: actionable trades, watchlist setups, “no trade because X”, invalidation alerts, and contradiction-led fades. A single posterior threshold turns the desk into a sparse binary classifier.

**Fix:** Add a candidate generation layer before final validation:
- `supported >= 0.7` → trade idea candidate
- `0.55-0.7` with high heat/confluence → watchlist setup
- `<=0.3` contradiction on crowded thesis → fade candidate
- failed quality gates → “blocked idea” with reason for brief

### 8. Brief cycle ID does not match specialist cycle IDs

**What:** `run_full_desk.py:86-89` runs cycles as `prod_...__specialist`, but `compose_brief()` is called with the base `cycle_id` at `run_full_desk.py:209-213`.

**Why:** The rendered brief’s Cycle Stats showed 0 tool calls, 0 hypotheses, 0 trade ideas, and 0 debates even though the manifest showed 1,200 calls and 24 hypotheses. That makes prod observability misleading.

**Fix:** Introduce a `desk_run_id` separate from per-specialist `cycle_id`, or make all tables carry both. Update `compose_brief()` to accept `cycle_ids: list[str]` and aggregate over all specialist cycle IDs.

### 9. Source-health kill switch misses failed tool calls

**What:** `_check_kill_switches()` counts failed calls only when `tool_call_log_id is None` (`loop/runner.py:1666-1675`).

**Why:** Failed dispatches still get a `tool_call_log_id`, so the observed 1,200 failed tool calls did not trigger a source-health quality flag or kill switch.

**Fix:** Add `ok`/`error` to `ExplorationStep`, or fetch `tool_call_log.error` during kill-switch evaluation. Trip if failed calls exceed 30%, and fail hard if `tool_uri_not_in_atlas` appears.

## Severity 3

### 10. Planner prompt does not force valid schema-aware tool arguments

**What:** `_build_plan_user_prompt()` lists URI and description only (`loop/runner.py:650-652`); `_default_args_for_uri()` later guesses `{"entity_symbol": entities[0]}` for every tool (`loop/runner.py:991-1002`).

**Why:** Even with a fixed atlas, many TIC tools require `coin`, `metric_prefix`, `query`, or `wallet_address`, not `entity_symbol`. The planner picks tools, but never specifies arguments, so execution will still produce bad-args failures.

**Fix:** Extend `HypothesisDraftPlan` with `tool_calls: [{uri, args, purpose}]`. Include each tool’s `input_schema` in the plan prompt. Delete `_default_args_for_uri()` for production paths.

### 11. Scratchpad posting is too passive and thresholded

**What:** Peer messages are only posted on cumulative posterior delta `>0.2` in synthesize (`loop/runner.py:1189-1213`) or hot branch spawn in BFS (`exploration/bfs.py:808-839`).

**Why:** Specialists cannot cross-pollinate during the cycle unless the posterior engine already works. Since it does not, peer messages stay at zero. Also, persona prompts ask specialists to post 0-5 messages, but the runtime never lets the LLM choose messages in SYNTHESIZE.

**Fix:** Add a `peer_messages` JSON field to the LLM synthesis output and call `post_message()` for each validated message. For sequential mode, run lightweight synthesis/posting after each specialist so later specialists actually hydrate prior messages.

### 12. Director exists but is not in the cycle path

**What:** `director/research_director.py` defines curriculum allocation, but `run_full_desk.py` registers specialists and runs them directly without a director pass.

**Why:** The system has no desk-level agenda, no coverage balancing, and no explicit “we need 5-10 user-worthy ideas today” objective. The four agents independently generate hypotheses and the composer renders whatever happens.

**Fix:** In `run_full_desk.py`, run `propose_curriculum()` before specialists, write assignments to `agent_messages`, then hydrate specialists. Add a minimum brief artifact target: e.g. 3 published ideas, 5 watchlist setups, or explicit “blocked because...” sections.

## Severity 4

### 13. Brief composer mostly renders sections instead of weaving a desk narrative

**What:** `compose_brief()` calls the LLM only for a headline (`brief/composer.py:309-325`) and then renders tables/sections (`brief/composer.py:365-388`).

**Why:** With rich data, this still feels thin because it does not reconcile specialists, explain disagreements, or turn failed/blocked candidates into useful user-facing intelligence.

**Fix:** Add `compose_narrative()` after data fetch: feed open ideas, blocked candidates, hot hypotheses, failed sources, and specialist summaries into one LLM call that outputs a structured daily brief narrative. Keep tables as appendices.

### 14. Specialist isolation is catch-and-continue, but not operationally visible enough

**What:** `run_one_cycle()` catches failures and returns an error dict (`run_full_desk.py:85-113`), but there are no structured logs, alert hooks, retries, or partial-output health policy.

**Why:** For 30 days unattended, silent degradation is the main enemy. A specialist can produce zero useful artifacts and the run still composes a brief.

**Fix:** Add structured JSON logs for every stage, per-specialist status, fatal vs degraded classification, and alert thresholds: zero atlas rows, >20% tool failures, zero usable ideas for 2 cycles, cost anomaly, source-health degradation.

### 15. Desk-wide cost cap is not implemented

**What:** Per-specialist caps exist in `LoopConfig` (`loop/runner.py:192-215`), and kill switch checks per-cycle overrun (`loop/runner.py:1641-1664`), but there is no actual $100/day desk-wide accumulator.

**Why:** Four specialists at today’s cost are cheap, but once real LLM synthesis, debate, search, and parallel work are wired, runaway spend becomes possible.

**Fix:** Add `cost_ledger` table keyed by UTC date and stage. Before every LLM/tool/debate call, reserve budget. Hard fail into paper-only/no-LLM mode at `$100/day`.

## Severity 5

### 16. Hardcoded local paths and `sys.path` injection are not production-safe

**What:** Hardcoded `/Users/udaikhattar/.../brief_experiments` appears in `loop/runner.py:122`, `run_full_desk.py:30`, `run_daily_brief.py:22`, and `debate/judge.py:272-276`.

**Why:** This will break on any server, worker, container, or teammate machine. It is also a security smell because arbitrary local path precedence can shadow imports.

**Fix:** Package `talis-tic` as an installable dependency, or configure `TALIS_TIC_ROOT` / `TALIS_TIC_DB_PATH` via env with validation. Remove direct `sys.path.insert()` from runtime modules.

### 17. SQLite prod story lacks migration and backup discipline

**What:** `DeskStore` auto-applies schema to `~/.talis/desk.db` (`store/__init__.py:21-42`) with no migration versioning, backup job, retention policy, or corruption recovery.

**Why:** Thirty unattended days means the artifact DB is the product memory. Losing it loses hypotheses, trade ideas, debates, and evaluation lineage.

**Fix:** Add migrations with schema version checks, daily `VACUUM INTO` backup, WAL checkpointing, restore test, and explicit TIC/desk DB paths in deploy config.

## S-tier Target Tweaks

1. Run `regenerate_tool_atlas()` and schema/tool preflight first; fail closed on empty atlas or URI mismatch.

2. Make PLAN produce schema-valid tool calls, not just URIs.

3. Add a real evidence scorer so TIC outputs move posterior.

4. Add LLM SYNTHESIZE that emits multiple candidates: trade ideas, watchlist setups, blocked ideas, and peer messages.

5. Open real debates when `maybe_trigger_debate()` fires; require judged debates before high-confidence publication.

6. Run Director before specialists, then specialists sequentially or in two waves so scratchpad messages matter.

7. Compose the brief with a narrative LLM pass that reconciles the whole desk, then render tables.

8. Add desk-wide cost ledger, structured metrics, alerts, DB backups, and remove local path injection.

Ship call: not prod-ready. The architecture is promising, but today’s E2E is mostly scaffolding. The first production gate should be: non-empty atlas, <5% tool resolution failures, real posterior movement from real tool payloads, at least one actual debate row when triggers fire, and a brief whose cycle stats match the run manifest.
