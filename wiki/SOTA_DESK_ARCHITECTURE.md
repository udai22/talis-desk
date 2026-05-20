# SOTA Desk Architecture v2

Status: Elon simplify pass  
Date: 2026-05-20  
Scope: autonomous Hyperliquid research desk built on `AGENT_INFRA_PLAN.md`.

Hyper.space is the OS benchmark, not the product benchmark. Copy the commodity primitives: URI tool addressing, `SKILL.md`, per-agent filesystem, sandboxed execution, and durable messages. Win where Talis can be structurally different: HL-native data depth, a trade-idea book graded on PnL, bitemporal replay, and a compounding self-improvement flywheel.

## 1. North Star

S-tier means the desk compounds into measurable trading edge, not impressive briefs. The primary output is a scored trade idea book; the secondary output is the research graph explaining why the book exists and how it improved.

Rolling 30-day scorecard:

| Metric | Threshold | Measurement |
|---|---:|---|
| Weekly alpha vs HL benchmark | > 50 bps after fees | Equal-risk trade book return minus passive HL benchmark, net fees/slippage |
| Trade idea hit rate | > 55% | Closed ideas with positive return after costs or favorable target/stop outcome |
| Trade book Sharpe | > 1.5 | Daily idea-book returns, volatility scaled, after fees |
| Novel-and-correct claim share | > 30% | Predictive claims later validated and not present in external research at emit time |
| Coverage breadth | > 80% tools touched weekly | Distinct active tools called / approved tools in frozen `tool_atlas` |
| Investigation depth | avg >= 30 calls per hot investigation; stretch 300+ | Calls attached to investigations with `heat_score >= 0.7` |
| Reflection lift | Brier improvement > 0 in 70% of reflection updates | Next-cycle forecasts citing reflection vs ablated replay |
| Playbook hit rate | > 50% historical avg when triggered | Triggered idea return vs playbook version history |
| Debate resolution rate | > 90% within 1 cycle | Structured verdict before next cycle close |

Declare S-tier only when 7 of 9 are green and no critical risk is red: unreplayable trade idea, unbounded cost, missing source health, disabled PnL gate, or kill switch active. If alpha is green but replay/audit is broken, the desk is profitable but not trustworthy; if Brier is green but PnL is flat, it is a calibrated forecaster, not a research desk. Alpha must also exceed the all-in desk cost budget.

Novelty is computed, not hand-waved:

```python
def score_novelty(claim: ClaimLike, as_of: datetime) -> NoveltyScore:
    """Return cosine_to_nearest_in_corpus, present_in_external_research, is_novel."""
```

`score_novelty` searches the internal `semantic_index` plus indexed external research available at `as_of`: arXiv quant-fin claims, `news.py`/GDELT, and asksurf mindshare. `is_novel = cosine_to_nearest_in_corpus < 0.65 and not present_in_external_research`. Store novelty in `reward_log` with `reward_kind='novelty'`.

## 2. Five-Layer Architecture

One canonical loop uses these five capabilities. Specialists differ by priors, subscriptions, tool affinity, risk limits, and playbook access, not by custom runtime.

### Layer 1: Tool Omniscience

Every callable surface is discoverable, addressable, permissioned, scored, and replayable:

```text
tic://tool/builtin/query_timeseries@v1
tic://tool/hydromancer/fetch_live@v3?source=hl_l4_ofi
tic://tool/learned/usdt_mint_flow_to_btc_correlation@v2
tic://skill/microstructure_sweep_detection@v1
tic://source/hl/info/metaAndAssetCtxs
tic://artifact/trade_idea/ti_...
```

Core APIs:

```python
def regenerate_tool_atlas(as_of: datetime, include_candidates: bool = False) -> ToolAtlasSnapshot: ...
def resolve_tool_uri(uri: str, as_of: datetime | None = None) -> ToolContract: ...
def dispatch_uri(uri: str, args: dict, context: AgentContext) -> ToolResult: ...
def load_skill_registry(as_of: datetime, specialist_id: str | None = None) -> list[SkillManifest]: ...
```

`tool_atlas` regenerates nightly at 00:00 UTC. During a cycle the atlas snapshot is frozen; newly promoted tools land in the next day's atlas, never mid-cycle. This preserves replay determinism.

#### SKILL.md Contract

Each skill lives at:

```text
skills/<slug>/
  SKILL.md
  tool.py                  # executable entrypoint
  tests/<*.json>           # input/expected fixtures
```

Required `SKILL.md` sections: `name`, `when_to_use`, `inputs`, `outputs`, `example_invocations` with at least 3 examples, `cost_hint`, `last_brier_30d`, `owner_specialist`, and `supersedes_skill_id`.

Discovery has two paths. Fast path: each specialist hydrates with a curated subset in its tool atlas. Full path: registry search uses `semantic_search` over `SKILL.md` content where `text_for_embedding = skill_name || when_to_use || examples`. Cap the registry at 200 skills; when capped, demote least-recently-used skills and the Brier-worst skills first.

### Layer 2: Adversarial Exploration

Hot investigations do breadth-first exploration and contradiction search in one loop. A hot signal spawns confirmation branches plus a parallel devil's-advocate sub-investigation.

```python
def start_investigation(seed: HypothesisSeed, heat_score: float, budget: InvestigationBudget) -> Investigation: ...
def explore_adversarial(investigation_id: str, frontier: list[QuestionNode], max_calls: int = 300) -> ExplorationTrace: ...
def score_hot_signal(result: ToolResult, hypothesis_id: str) -> HotSignalScore: ...
def maybe_trigger_debate(claim: ClaimLike, context: AgentContext) -> DebateTriggerDecision: ...
```

In-cycle learning is immediate. If a tool result creates `abs(posterior_delta) > 0.2`, the specialist opens a hot-investigation branch and posts a scratchpad message to relevant peers without waiting for reflection. Debate triggers include high confidence (`posterior_prob > 0.75`), short horizon, position-size implication, trade idea confidence >= 0.7, source conflict, or cross-specialist contradiction.

Thrash controls: max 10 debates per cycle, max 3 debates per specialist pair, max 1 per instrument/horizon per 6 hours, and debate budget <= 20% of daily LLM spend. Any cycle that triggers > 10 debates activates the kill switch.

### Layer 3: Playbook Consolidation

Repeated profitable patterns become named, versioned, triggerable playbooks with out-of-sample scoring.

```python
def propose_playbook(name: str, trigger_sql: str, evidence_ids: list[str], owner: str) -> PlaybookCandidate: ...
def evaluate_playbook(playbook_id: str, as_of: datetime, lookback_days: int = 365) -> PlaybookBacktest: ...
def detect_playbook_triggers(as_of: datetime) -> list[PlaybookTrigger]: ...
def instantiate_playbook_trade(playbook_id: str, trigger_id: str, context: AgentContext) -> TradeIdea: ...
```

Promotion requires at least five historical triggers or explicit `frontier_experimental` human marking. New trigger logic creates a new version. `mv_playbook_trigger_today` is intentionally not a materialized view; use a SQL function so evaluation happens on demand against bitemporal state.

### Layer 4: Trade Idea Pipeline

Trade ideas are the final artifact. Briefs explain the book; they are not the product.

```python
class TradeIdea(BaseModel):
    instrument: str
    venue: str = "hyperliquid"
    direction: Literal["long", "short", "flat", "spread"]
    sizing: dict                 # risk pct, notional cap, Kelly fraction, leverage cap
    entry: dict                  # trigger, limit/market assumptions, invalidation
    stop: dict
    target: dict | None = None   # one target; dynamic trailing handles scaling
    time_horizon: str
    edge_thesis: str             # cites claim_ids/hypothesis_ids/tool_call_ids
    contradicting_evidence: list[dict]
    confluence_score: float
    confidence: float
```

Validation gates:

- Must include instrument, direction, entry, stop, target/invalidation, sizing, time horizon, edge thesis citations, and contradicting evidence.
- Sizing includes max loss if stop hits. Default risk cap: 25-50 bps of model book per idea until S-tier; Kelly capped at quarter-Kelly and leverage capped at 2x unless human raises it.
- Entry/stop/target must be resolvable against HL mark/orderbook. Slippage assumptions must distinguish liquid vs thin markets.
- Resolver runs daily and at expiry, filling realized PnL, benchmark return, Brier, alpha, and attribution.

Post-hoc grading starts simple: realized PnL, risk-adjusted return, benchmark delta, and weekly ablation replay for the top 20% PnL-impact ideas.

### Layer 5: Self-Improvement Loop

Persona mutation, tool affinity, weakness mapping, and curriculum allocation are one loop.

Cross-cycle learning happens at `REFLECT` and `DEHYDRATE`: update priors, write state object hashes, close branches, and score outputs. Reflection is capped at 5% of the cycle's LLM spend. Persona evolution runs nightly, not inside every branch.

Nightly auto-mutation:

1. Meta-agent reads `reward_log`, `mv_specialist_brier_rolling`, and `mv_weakness_map` for each specialist.
2. It proposes a small prompt diff: add/remove one bullet or change one threshold.
3. The diff is appended as `specialist_states.state_kind='mutation_candidate'`.
4. A/B runs base vs candidate on identical bitemporal snapshots and frozen tool atlas.
5. Human veto window is 24h. After 24h with no veto and positive metrics, auto-promote.

Promotion uses one 15-day window. A monthly stable league table reports durable winners. Drop 7-day early warning and 90-day promotion logic; those windows add branching without enough HL signal value.

Per-agent filesystem mounted into the Modal sandbox at the same path:

```text
/agents/<specialist_id>/
  priors.json              # current beliefs, durable
  hypotheses_open.jsonl    # active hypotheses (graph node pointers)
  hypotheses_resolved.jsonl
  tool_affinity.json       # rolling Brier-weighted scores per tool URI
  scratch/                 # per-cycle ephemeral; cleared each run
  notebooks/<date>/        # exported run_code Jupyter-style outputs (replayable)
  cache/                   # cached extracts, hashed by source_ref
  proposals/               # learned-tool proposals in flight
```

The filesystem survives cycles through object-hash references in `specialist_states.state_json`; paths are conveniences, not the audit source of truth.

## 3. Schema DDL

Rules: append-only, bitemporal, and graph-native. Corrections insert new rows with `supersedes`. `hypotheses.parent_hypothesis_id` is dropped; parent-child is represented only by `hypothesis_edges.edge_kind='spawned'`. Tool-level rewards are stored in `tool_call_log.reward_score`; no `reward_log` rows with `subject_kind='tool'`.

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT PRIMARY KEY DEFAULT ('hyp_' || encode(gen_random_bytes(12), 'hex')),
    cycle_id TEXT NOT NULL,
    specialist_id TEXT NOT NULL,
    title TEXT NOT NULL,
    hypothesis_text TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active','supported','contradicted','resolved','abandoned')),
    posterior_prob NUMERIC(5,4),
    heat_score NUMERIC(5,4) NOT NULL DEFAULT 0,
    novelty_score NUMERIC(5,4),
    expected_resolution_at TIMESTAMPTZ,
    entity_ids TEXT[] NOT NULL DEFAULT '{}',
    source_ids TEXT[] NOT NULL DEFAULT '{}',
    claim_ids TEXT[] NOT NULL DEFAULT '{}',
    tool_call_ids TEXT[] NOT NULL DEFAULT '{}',
    supersedes TEXT REFERENCES hypotheses(id),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS hypothesis_edges (
    id TEXT PRIMARY KEY DEFAULT ('hedge_' || encode(gen_random_bytes(12), 'hex')),
    from_node_kind TEXT NOT NULL,
    from_node_id TEXT NOT NULL,
    to_node_kind TEXT NOT NULL,
    to_node_id TEXT NOT NULL,
    edge_kind TEXT NOT NULL CHECK (edge_kind IN ('supports','contradicts','derived_from','causes','hedges','supersedes','spawned')),
    strength NUMERIC(6,4),
    citation_ids TEXT[] NOT NULL DEFAULT '{}',
    supersedes TEXT,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS trade_ideas (
    id TEXT PRIMARY KEY DEFAULT ('ti_' || encode(gen_random_bytes(12), 'hex')),
    cycle_id TEXT NOT NULL,
    specialist_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    venue TEXT NOT NULL DEFAULT 'hyperliquid',
    direction TEXT NOT NULL CHECK (direction IN ('long','short','flat','spread')),
    sizing JSONB NOT NULL,
    entry JSONB NOT NULL,
    stop JSONB NOT NULL,
    target JSONB,
    time_horizon TEXT NOT NULL,
    edge_thesis TEXT NOT NULL,
    claim_ids TEXT[] NOT NULL DEFAULT '{}',
    contradicting_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    confluence_score NUMERIC(6,4),
    confidence NUMERIC(6,4) NOT NULL,
    hypothesis_ids TEXT[] NOT NULL DEFAULT '{}',
    forecast_ids TEXT[] NOT NULL DEFAULT '{}',
    debate_ids TEXT[] NOT NULL DEFAULT '{}',
    playbook_id TEXT,
    tool_call_ids TEXT[] NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN ('draft','published','open','closed','expired','invalidated')),
    published_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL,
    realized_outcome JSONB,
    realized_pnl_pct NUMERIC(12,6),
    realized_return_after_fees_pct NUMERIC(12,6),
    benchmark_return_pct NUMERIC(12,6),
    contributed_alpha_pct NUMERIC(12,6),
    brier NUMERIC(12,6),
    resolver_run_id TEXT,
    supersedes TEXT REFERENCES trade_ideas(id),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS debates (
    id TEXT PRIMARY KEY DEFAULT ('deb_' || encode(gen_random_bytes(12), 'hex')),
    cycle_id TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    trigger_id TEXT NOT NULL,
    participants TEXT[] NOT NULL,
    judge_model TEXT NOT NULL,
    judge_provider TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open','awaiting_arguments','judged','applied','expired')),
    due_at TIMESTAMPTZ NOT NULL,
    argument_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    verdict JSONB,
    winner TEXT,
    judge_confidence NUMERIC(6,4),
    later_brier NUMERIC(12,6),
    supersedes TEXT REFERENCES debates(id),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS playbooks (
    id TEXT PRIMARY KEY DEFAULT ('pb_' || encode(gen_random_bytes(12), 'hex')),
    name TEXT NOT NULL,
    version INT NOT NULL,
    owner_specialist TEXT NOT NULL,
    description TEXT NOT NULL,
    trigger_spec JSONB NOT NULL,
    action_template JSONB NOT NULL,
    min_sample_size INT NOT NULL DEFAULT 5,
    historical_trigger_count INT NOT NULL DEFAULT 0,
    historical_avg_return_pct NUMERIC(12,6),
    historical_hit_rate NUMERIC(6,4),
    promoted_status TEXT NOT NULL CHECK (promoted_status IN ('candidate','experimental','approved','demoted','retired')),
    evidence_ids TEXT[] NOT NULL DEFAULT '{}',
    supersedes TEXT REFERENCES playbooks(id),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ,
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS specialist_states (
    id TEXT PRIMARY KEY DEFAULT ('spst_' || encode(gen_random_bytes(12), 'hex')),
    specialist_id TEXT NOT NULL,
    persona_version TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    state_kind TEXT NOT NULL CHECK (state_kind IN ('persona','hydration','dehydration','mutation_candidate','rollback')),
    state_json JSONB NOT NULL,
    prompt_hash TEXT,
    parent_state_id TEXT REFERENCES specialist_states(id),
    ab_test_group TEXT,
    brier_delta NUMERIC(12,6),
    alpha_delta_pct NUMERIC(12,6),
    redundancy_delta NUMERIC(12,6),
    supersedes TEXT REFERENCES specialist_states(id),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id TEXT PRIMARY KEY DEFAULT ('msg_' || encode(gen_random_bytes(12), 'hex')),
    from_agent TEXT NOT NULL,
    to_agent_or_topic TEXT NOT NULL,
    message_kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    posted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_by JSONB NOT NULL DEFAULT '[]'::jsonb,
    expires_at TIMESTAMPTZ,
    dedupe_key TEXT,
    related_artifact_id TEXT,
    related_hypothesis_id TEXT,
    related_trade_idea_id TEXT,
    supersedes TEXT REFERENCES agent_messages(id),
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS tool_atlas (
    id TEXT PRIMARY KEY DEFAULT ('tool_' || encode(gen_random_bytes(12), 'hex')),
    tool_uri TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    version TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('builtin','hydromancer','learned','candidate','skill','external','source')),
    provider TEXT NOT NULL,
    callable_ref TEXT NOT NULL,
    schema_json JSONB NOT NULL,
    skill_md_path TEXT,
    description TEXT NOT NULL,
    source_dependencies TEXT[] NOT NULL DEFAULT '{}',
    permission_scope TEXT NOT NULL DEFAULT 'read_only',
    network_hosts TEXT[] NOT NULL DEFAULT '{}',
    cost_hint JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL CHECK (status IN ('active','candidate','approved','demoted','retired','failing')),
    code_sha256 TEXT,
    supersedes TEXT REFERENCES tool_atlas(id),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ,
    UNIQUE (tool_uri, version, transaction_from)
);

CREATE TABLE IF NOT EXISTS tool_call_log (
    id TEXT PRIMARY KEY DEFAULT ('tc_' || encode(gen_random_bytes(12), 'hex')),
    cycle_id TEXT NOT NULL,
    investigation_id TEXT,
    specialist_id TEXT NOT NULL,
    tool_uri TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    args_json JSONB NOT NULL,
    result_hash TEXT,
    result_summary JSONB,
    reward_score JSONB NOT NULL DEFAULT '{}'::jsonb,
    cited_in_ids TEXT[] NOT NULL DEFAULT '{}',
    error JSONB,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    duration_ms INT,
    cost_usd NUMERIC(12,6) NOT NULL DEFAULT 0,
    source_ids TEXT[] NOT NULL DEFAULT '{}',
    claim_ids TEXT[] NOT NULL DEFAULT '{}',
    quality_flags TEXT[] NOT NULL DEFAULT '{}',
    supersedes TEXT,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS reward_log (
    id TEXT PRIMARY KEY DEFAULT ('rew_' || encode(gen_random_bytes(12), 'hex')),
    cycle_id TEXT NOT NULL,
    reward_kind TEXT NOT NULL CHECK (reward_kind IN ('curiosity','correctness','alpha','novelty','coverage','cost_penalty','debate_quality','playbook_hit')),
    subject_kind TEXT NOT NULL CHECK (subject_kind <> 'tool'),
    subject_id TEXT NOT NULL,
    specialist_id TEXT,
    score NUMERIC(12,6) NOT NULL,
    baseline_score NUMERIC(12,6),
    delta NUMERIC(12,6),
    attribution_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    supersedes TEXT,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ
);
```

Keep only high-value materialized views:

```sql
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_top_tools_per_specialist_30d AS
SELECT specialist_id, tool_uri, count(*) AS n_calls,
       count(*) FILTER (WHERE error IS NULL) AS n_success,
       sum(cost_usd) AS total_cost_usd,
       avg((reward_score->>'brier_delta')::numeric) AS brier_delta_avg
FROM tool_call_log
WHERE started_at >= now() - interval '30 days' AND transaction_to IS NULL
GROUP BY specialist_id, tool_uri;

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_specialist_brier_rolling AS
SELECT specialist_id, date_trunc('day', valid_from) AS day,
       avg(score) AS brier_avg, avg(delta) AS brier_delta_avg, count(*) AS n
FROM reward_log
WHERE reward_kind = 'correctness' AND transaction_to IS NULL
GROUP BY specialist_id, date_trunc('day', valid_from);

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_hot_hypotheses AS
SELECT h.id, h.specialist_id, h.title, h.posterior_prob, h.heat_score,
       count(e.id) AS evidence_edges, max(h.transaction_from) AS last_updated
FROM hypotheses h
LEFT JOIN hypothesis_edges e ON e.to_node_id = h.id AND e.transaction_to IS NULL
WHERE h.status = 'active' AND h.transaction_to IS NULL AND h.valid_to IS NULL
GROUP BY h.id, h.specialist_id, h.title, h.posterior_prob, h.heat_score;

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_trade_book_open AS
SELECT id, specialist_id, instrument, direction, sizing, entry, stop, target,
       confidence, confluence_score, published_at, expires_at,
       realized_pnl_pct, benchmark_return_pct, contributed_alpha_pct
FROM trade_ideas
WHERE status IN ('published','open') AND transaction_to IS NULL AND valid_to IS NULL;
```

`mv_weakness_map` replaces the `weakness_map` table and refreshes daily:

```sql
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_weakness_map AS
SELECT specialist_id,
       'brier_or_alpha_or_cost' AS weakness_kind,
       jsonb_build_object('brier', avg(score), 'delta', avg(delta)) AS evidence_json
FROM reward_log
WHERE transaction_to IS NULL
GROUP BY specialist_id;
```

Refresh `mv_trade_book_open` every 1-5 minutes during market hours; refresh the other three after each cycle; refresh `mv_weakness_map` daily after the nightly score rollup.

## 4. Agent Loop

```text
HYDRATE   -> state + unread messages + Brier outcomes + frozen tool atlas
PLAN      -> propose 3+ hypotheses, pick tools per hypothesis
EXPLORE   -> adversarial BFS; hot branches; debates when required
SYNTHESIZE-> update graph; emit forecasts + trade ideas; post peer messages
REFLECT   -> update priors/tool affinity within 5% LLM spend cap
DEHYDRATE -> write object-hash state refs; close cycle
```

Karpathy bitter lesson stance: use one loop. Different loops per specialist multiply bugs, replay semantics, cost policy, and eval surfaces. If a specialist needs an extra move, it proposes a skill or learned tool; it does not fork the runtime. The Research Director uses the same loop but emits assignments unless explicitly authorized to emit trade ideas.

Daily total cost cap: $100/day across all specialists, debates, reflections, Modal runs, and Research Director. Hard kill if exceeded; low rolling-Brier specialists lose budget first. Cost overrun > $200/day for two consecutive days activates `paper_only`.

## 5. Manual Eval Dashboard

Human review should take <30 minutes weekly.

Panels:

- Top strip: 9 S-tier metrics, 30-day trend, and "why red".
- Trade book: open/closed ideas, PnL waterfall, benchmark delta, stop/target discipline, worst loss, best idea, biggest ignored contradiction.
- Debate: unresolved, judged, overturned by outcome, judge reliability.
- Hypothesis graph sampler: random graph with evidence, contradictions, and source health.
- Persona diff: prompt/prior/tool-affinity diff with 15-day Brier/alpha before-after.
- Weakness map: top 5 weaknesses, owner, budget spent, expected lift, actual lift.
- Tool atlas: coverage, cost, latency, errors, affinity by specialist.

Reviewer flow: start at red metrics, inspect closed losing ideas, open one related graph, sample debates, approve/veto persona diffs, sign weekly eval.

## 6. Build Sequence

### 6.0 Prerequisite: `AGENT_INFRA_PLAN.md`

Ship these first: Phase 1 Modal `run_code_modal`, Phase 2 `learned_tools/` directory, Phase 3 Brier-gated promotion, Phase 4 durable messages, and Phase 5 specialist state. That is about 12 days of infra work. Then run the six-week SOTA desk build below. Total elapsed from today to "S-tier candidate" is about eight weeks.

Each week has a MANUAL GATE. Do not start the next phase until that gate is signed.

### Week 1: Schema + Tool Atlas

Create `tic/agent_native/schema_pg.sql`, `tic/agent_native/tool_atlas.py`, `skills/README.md`, and URI wrappers around current tools. DDL applies; `regenerate_tool_atlas` lists built-ins, Hydromancer sources, learned tools, and skills; three URI calls replay under the same `as_of`. Manual gate: human resolves five tool URIs to schema, source health, result summary, and replay slice.

### Week 2: Trade Ideas + Resolver

Create `tic/agent_native/trade_ideas.py` and `tic/eval/trade_resolver.py`; add `emit_trade_idea`. Ideas validate required fields and contradiction evidence; resolver fills PnL, benchmark return, Brier, and alpha. Manual gate: human reviews five generated ideas before publication mode.

### Week 3: Hypothesis Graph + Adversarial Exploration

Create `tic/agent_native/hypotheses.py` and `tic/agent_native/exploration.py`. Specialists propose 3+ hypotheses, attach evidence, spawn contradiction branches, and link trade ideas to graphs. Manual gate: reviewer approves two traces as non-trivial and contradiction-aware.

### Week 4: Playbooks + Debate

Create `tic/agent_native/playbooks.py`, `tic/agent_native/debate.py`, and message wiring. Playbooks can be proposed/backtested/triggered; debates fire for high-confidence, high-stakes, or contradictory claims. Manual gate: sign off first debate protocol and first approved playbook.

### Week 5: Self-Improvement + Rewards

Create `tic/agent_native/persona_evolution.py` and `tic/agent_native/rewards.py`. Mutation candidates are append-only, A/B uses identical snapshots, rollback/promote uses 15-day metrics and 24h veto. Manual gate: approve one harmless mutation.

### Week 6: Research Director + Dashboard

Create `tic/agent_native/research_director.py` and dashboard panels. `mv_weakness_map` identifies top issues from Brier, alpha, novelty, coverage, cost, and debate metrics; curriculum assigns investigations and budgets through messages. Manual gate: complete a dry-run eval and mark "S-tier candidate", not S-tier.

### Minimum Viable Agent

If a solo dev needs the smallest compounding version, ship one persona with tool atlas, trade ideas, resolver, reflection, and nightly meta-prompt mutation. Skip debate, playbooks, and multi-specialist curriculum until the first idea book is resolving. This is about 1.5 weeks and gives the flywheel: call tools, emit ideas, score outcomes, mutate the prompt, repeat.

## 7. Kill Switch

Automatic shutdown puts the desk in `paper_only` until a human resumes. Triggers:

- Trade book drawdown > 5% from peak in any 24h.
- Source-health failures > 30% of cited sources in last cycle.
- Cost overrun > $200/day for 2 consecutive days.
- 3 consecutive cycles with no novel-and-correct claims.
- Any cycle that triggers > 10 debates.
- Daily total cost cap of $100 exceeded.

`paper_only` still allows research, replay, resolver, and manual eval; it blocks live publication/execution.

## 8. What Is Not The Moat

Do not spend originality budget here:

- URI tool addressing: copy hyper.space's scheme as closely as practical.
- `SKILL.md` format: copy hyper.space-style manifests.
- Sandbox: adopt Modal primitives; do not build a sandbox.
- Matrix Profile, Contextual Retrieval, and RRF: common SOTA, already shipped.
- Reservoir/Hydromancer historical data: their data, our analytics.

The moat is HL-native data depth, trade ideas graded on PnL, bitemporal replay, and the compounding flywheel.

## 9. Risks

Trade ideas can lose money. Keep max loss, portfolio heat cap, no martingale sizing, quarter-Kelly, liquidity gates, and paper-only until 30-day S-tier.

Playbooks can overfit. Require sample warnings, bitemporal no-leak backtests, out-of-sample checks, and demotion if live return falls below 50% of historical average for five triggers.

Debate can become theater. Require citations, provider diversity, judge Brier tracking, and cost caps.

Persona evolution can overfit. Keep the 24h veto, monthly league table, generalist baseline, and black-swan caution prior that cannot be mutated away without human sign-off.

Replay can break under bitemporal complexity. Centralize slicing in `build_replay_context(as_of_valid, as_of_transaction)` and forbid hand-written temporal predicates in feature code without fixtures.

## 10. Four-Sentence Example

The desk sees BTC funding at a 2.4 sigma positive extreme while L5 OFI flips from toxic sell to passive bid refill, so `structure_v3` emits a long scalp with 35 bps risk. `macro_regime_v2` objects because DXY and Treasury auction stress are risk-off; the judge keeps the idea but cuts sizing by 40% and sets a 12h expiry. The resolver closes it +82 bps after fees, credits OFI/sweep tools and the funding-normalization playbook, and improves the judge's reliability. Next week the prompt mutation adds: "when macro contradicts but L4 evidence is fresh, size down rather than veto."
