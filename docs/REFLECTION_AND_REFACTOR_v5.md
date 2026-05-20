# Talis Desk v5 — Reflection + Refactor Plan
## Grounded in v4 learn-logs, 2025 SOTA research, and 50-year-durable coordination primitives

---

## Executive summary

v4 desk ran for 48 minutes and **never completed cycle 1** for a single specialist. The 6-stage adversarial pipeline within macro_regime hit the $5/cycle budget cap on its own and was killed mid-execution. Sequential per-specialist execution is structurally incompatible with the user's stated goal of producing 70+ research reports per day. **The refactor replaces the sequential pipeline with a 1000+ agent parallel swarm architecture organized around four 50-year-durable coordination primitives: bitemporal blackboard, stigmergic coverage, verifier-prover routing, and market-priced task allocation.**

Do **not** rewrite from a blank repository. The right move is a kernel rewrite: keep the useful tic tools, source-library ingesters, specialist prompts, calendar gate, report pipeline, and desk.db schema; replace the orchestration kernel that decides what gets sampled, verified, escalated, synthesized, persisted, and visualized. A blank rewrite would throw away the only real telemetry we have. A kernel rewrite uses v4's failure data as the spec.

This document is the single source of truth for the v5 refactor agent. It is grounded in real v4 telemetry, 2025 SOTA multi-agent research (Hayes-Roth blackboards, Du/Liang multi-agent debate, ICLR 2025 efficiency critique), and the strategic insights surfaced through this session: market-as-information-pricing-mechanism, cheap-intelligence-as-moat, RRG-generalized information topology, citation provenance for verifiability, and S3-as-unification.

---

## 1. v4 run forensics (what we actually measured)

| Metric | v4 actual | v5 target |
|---|---|---|
| Specialists registered | 15 (osho_astrology_tracer silently failed) | 26 (21 current + 5 in pipeline) |
| Cycles fully completed | **0** (killed after 48 min, no dehydration written) | 15-20/day |
| Tool dispatch success rate | 297/301 = 98.7% | maintain ≥ 98% |
| Tool dispatch errors | 4 (mostly bad_args on edge tools) | <2% |
| Research reports produced | 4 (all macro_regime, 12-14k chars) | 50-100/day desk-wide |
| Trade ideas published | 1 | 5-15/day |
| Debates opened | 10 | scale with hypothesis count |
| Peer messages | 11 | proportional to hypotheses |
| Specialist budget exhaustion | macro_regime hit $5.03 on its own cycle | per-specialist ≤ $1.50 with tiered swarm |
| Brief composition | Defaulted to "Quiet cycle" — wrong cycle_id aggregation | Calendar-gate-validated, narrative-synthesized |
| Phase 6 mutator runs | 0 (no cycles → no Brier outcomes) | 1/day post-cycle |

**Three structural bugs surfaced live:**

1. **Sequential 6-stage pipeline scaling failure.** Each specialist's adversarial cycle is ~$5 + ~50 minutes wall. 16 specialists × 50 min = 13+ hours/cycle. Impossible.
2. **`osho_astrology_tracer` silent registration failure.** Fixed in the current pass: persona validation now accepts the FOUR-FRAME lineage header introduced by the Philosophia Ultima guardrail edit.
3. **Brief composer aggregation mismatch.** Fixed in the current pass: full-desk runs now pass the base cycle plus `{base}__{specialist}` sub-cycles into the composer for best-effort partial briefs.
4. **Knowledge compounding gap.** Fixed at projection level in the current pass: every run now generates `~/.talis/wiki` from desk.db with cycle pages, coverage matrix, topology pages, and human-note-safe annotation files. The next kernel still needs to make this wiki drive scout allocation, not merely describe it.

---

## 2. The four-pillar coordination architecture (the 50-year-durable design)

Each pillar is grounded in 30-250 years of theory and validated in 2025+ SOTA papers.

### Pillar 1 — Bitemporal Blackboard
- **Theory:** Hayes-Roth 1972 (HEARSAY-II), Snodgrass 1986 (bitemporal data), SQL:2011 standardization
- **2025 SOTA:** [arXiv:2507.01701](https://arxiv.org/abs/2507.01701) + [arXiv:2510.01285](https://arxiv.org/abs/2510.01285) — blackboard architecture in LLM multi-agent systems achieves 13-57% improvement over RAG + master-slave baselines
- **Our implementation:** desk.db with explicit topic channels (bb_topic:*) and pull-mode task claiming. All 1000+ agents read + write the same store. Bitemporal supersedes for full audit.

### Pillar 2 — Stigmergic Coverage
- **Theory:** Grassé 1959 (termite coordination), Dorigo 1991 (Ant Colony Optimization)
- **Our implementation:** `coverage_log` (every entity/horizon/lens tuple sampled gets a marker with timestamp + outcome) + `manifold_density_map`. New agents weight their seeds AWAY from saturated cells, TOWARD frontier cells. Self-organizing.

### Pillar 3 — Verifier-Prover Routing
- **Theory:** Goldwasser-Micali-Rackoff 1985 (interactive proofs), Lamport-Shostak-Pease 1982 (Byzantine fault tolerance)
- **2025 SOTA:** Du et al. 2023 + Liang et al. 2023 multi-agent debate; [ICLR 2025 critique](https://d2jud02ci9yv69.cloudfront.net/2025-04-28-mad-159/blog/mad/) on efficiency
- **Our implementation:** any agent can PROPOSE; verification requires 2/3 majority across verifiers from DIFFERENT model families (e.g. Anthropic Haiku + DeepSeek Pro + xAI Grok). Multi-Agent Debate used SURGICALLY only at verification boundaries (Tier 1.5, Tier 3 critic, Tier 4 synthesis).

### Pillar 4 — Market-Priced Task Allocation
- **Theory:** Adam Smith 1776, Vickrey-Clarke-Groves 1961-1973 (auction mechanisms)
- **Our implementation:** Every task posted to blackboard has explicit dollar budget. Agents bid by self-selecting based on persona + Brier reputation. High-Brier specialists get cheaper bids accepted; new agents must prove themselves first. cost_ledger + Phase 6 mutator together form the market.

---

## 3. The complete 9-layer architecture

| # | Layer | Function | Substrate |
|---|---|---|---|
| 0 | Operating thesis | "Market = information pricing mechanism" — every architectural choice serves bandwidth × quality × speed | conceptual |
| 1 | Cost-cliff moat | Cheap intelligence at 10,000x cost advantage. Architecture extracts top quality via 11 quality patterns | LLM model selection + cost_ledger |
| 2 | 1000+ agent swarm | 4 tiers: 1000 Flash scouts → 100 verifiers → 50 Sonnet analysts → 10 Opus critic+synth | Stigmergic Blackboard (Pillar 1+2) |
| 3 | Stratified sampling + alpha frontier | Latin Hypercube over (entity × horizon × lens × bias) with coverage-history penalty + manifold-density routing | `coverage_log` + density map |
| 4 | Citation provenance graph | Every claim → canonical URL + content_hash + anchor; verify_citation re-fetches on demand | `citations` table + S3 archive |
| 5 | Information topology engine | UMAP/t-SNE projections of hypothesis embeddings → 8 RRG-style 2D views (catalyst, narrative, vol, sentiment, flow, specialist-convergence, cohort, source-coverage) | new `topology` module |
| 6 | Knowledge compounding wiki | Auto-generated markdown organized by time/instrument/theme/specialist/citation/outcome/topology/debate. Diff-able cycle-over-cycle | `wiki/` filesystem in S3 |
| 7 | S3 unified storage | Single bucket as canonical address space for desk-db, tic-db, briefs, wiki, citations-archive, source-library, agent-runs, meta. Litestream for SQL replication, Object Lock for immutables | `s3://talis-desk/` |
| 8 | Coordination architecture | Bitemporal blackboard + stigmergy + verifier-prover + market routing (the 4 pillars composed) | the synthesis |

---

## 4. The 11 quality-extraction techniques (squeeze top quality from cheap intelligence)

Each is a published multiplier on cheap-model output quality. Apply selectively at the right pipeline stages.

| # | Technique | Where applied | Cost overhead | Quality gain |
|---|---|---|---|---|
| 1 | Ensemble + voting | Tier 1.5 verifier council | 3-5x cheap | Catches hallucinations |
| 2 | Self-consistency with seeds | Tier 1 scouts (different temps) | 3-5x | Reduces tail-event noise |
| 3 | Best-of-N + grader | Tier 2 analyst drafts | ~10x cheap < 1x Opus | Often matches Opus |
| 4 | Hierarchical verification | Tier 1 → 1.5 → 2 → 3 cascade | escalate-on-failure only | Eliminates cite-fabrication |
| 5 | Tool-augmented drafting | All tiers | 0 overhead | Direct quality lift |
| 6 | Task decomposition | Tier 0 seed gen + Tier 2 sub-investigations | coordination cost | Avoids context-window degradation |
| 7 | Verification cascade | Tier 1.5 (verify), Tier 3 (deep critique) | cheap by default | Expensive only on uncertainty |
| 8 | Targeted critic at boundaries | Tier 3 ONLY (per ICLR 2025) | Opus is rare and high-value | Inverts cost curve |
| 9 | Diversity-first at scout layer | Tier 0 stratified seed generation | same total cost | Alpha probability scales with diversity |
| 10 | Panel consensus pattern | Tier 1.5 (5+ independent agreement = signal) | free signal | Selection IS the alpha |
| 11 | Knowledge distillation feedback | Phase 6 mutator + tracked corrections | compounding | Compounds Flash quality over time |

---

## 5. v4 → v5 priority refactor list

Ordered by **highest leverage × lowest risk**:

### Priority 0 — Foundation (must land before anything else)
- **F1.** `talis_storage/` module exposing `s3://talis-desk/` unified namespace. Litestream desk.db replication. Citation archive content-hash addressed.
- **F2.** Persistent desk.db (kill tempdir-per-run bug). Default to `~/.talis/desk.db` mirrored to S3. **Local default fixed; S3 mirror pending.**
- **F3.** Bitemporal blackboard topic channels in `agent_messages` + pull-mode task claiming.

### Priority 1 — Architecture (the swarm)
- **A1.** Tier-0 stratified seed generator. Latin Hypercube over (entity × horizon × lens × bias) + coverage-history penalty + theme injection.
- **A2.** Tier-1 1000-scout swarm runner with DeepSeek Flash backend.
- **A3.** Tier-1.5 verifier council (3-family 2/3 majority).
- **A4.** Tier-2 analyst tier (50 Sonnet/DeepSeek-Pro agents on verified hypotheses).
- **A5.** Tier-3 adversarial critic (existing 6-stage pipeline, scoped to top-20 analyst outputs only).
- **A6.** Tier-4 brief synthesis (Opus, 1-2 agents).

### Priority 2 — Quality + Trust
- **Q1.** Citation provenance graph (`citations` table + 3 new tools: `resolve_citation`, `fetch_source`, `verify_citation`).
- **Q2.** Calendar gate wired into brief composer. **Fixed locally; NVDA-style critical catalysts are forced into the headline/brief section.**
- **Q3.** Brief composer cycle_id aggregation fix (best-effort partial briefs). **Fixed locally in `run_full_desk.py`.**
- **Q4.** Fix osho_astrology_tracer silent registration error. **Fixed locally; 23/23 specialist registration smoke passed.**

### Priority 3 — Coverage + Organization
- **O1.** Information topology engine (UMAP/t-SNE + 8 RRG-style projections + frontier identification).
- **O2.** Knowledge compounding wiki generator. **Initial projection shipped as `talis_desk/wiki/generator.py`; next step is routing future scouts from its coverage gaps.**
- **O3.** Research Director runs FIRST each cycle (currently runs nowhere).

### Priority 4 — Visualization deepening
- **V1.** Pipeline Inspector (Layer 2 of visualizer) — per-report stage-by-stage forensics with diff viewer, critic transcripts, evidence-chain drill-downs.

### Priority 5 — Devens-gap ingesters
- **D1.** HL vehicle ETF volume tracker (BHYP/THYP wrapper ETFs, separate from HL spot)
- **D2.** DAT (Digital Asset Treasury) tracker (Hyperliquid Strategies + future DATs)
- **D3.** Stablecoin revenue model per L1 (Circle/Coinbase filings → per-chain revenue projection)
- **D4.** TradeXYZ pre-IPO open interest scraper
- **D5.** RWA OI per chain (fix Hydromancer auth)
- **D6.** Holder-cohort wallet taxonomy (Deployers/AF/DATs/stakers/whales/retail/MMs)
- **D7.** Tokenized-stocks regulatory tracker

---

## 6. Acceptance criteria for v5

| Criterion | v4 actual | v5 target |
|---|---|---|
| Cycle wall time | 48 min for 1 specialist (never finished) | ≤ 10 min for full desk |
| Cost per cycle | $5.03/specialist × N specialists impossible | ≤ $5/cycle full desk |
| Cycles per day | 0 (impossible at current rate) | 15-20 |
| Hypotheses generated per cycle | 6 | 1000 (Tier 1) → 100-300 verified |
| Research reports per day | 4 (single specialist only) | 50-100 desk-wide |
| Citation verification | none | 2/3 majority cross-family on every cited claim |
| Brief completeness | "Quiet cycle" default | Always has headline, top reports, calendar gate compliance |
| Calendar gate compliance | NVDA earnings MISSED | 100% — critical catalyst always in brief headline |
| Phase 6 mutator runs | 0 | 1/day after cycle |
| Source library citations in reports | minimal | every framework/method cite a `[lib:slug:p#]` reference |
| Topology coverage map | none | rendered each cycle to `wiki/topology/` |
| Visualizer | overview only | overview + Pipeline Inspector drill-down |

---

## 7. Build sequence for the refactor agent

The refactor agent executes in 3 waves, each gated by smoke tests:

**Wave 1 — Foundation (F1-F3 + Q3 + Q4)** — 2-3 hours
- Get persistent desk.db + S3 replication working
- Fix the brief composer aggregation + osho_astrology_tracer registration
- Smoke: run a single-specialist cycle end-to-end, verify desk.db persists to S3
- Current local status: persistent default, aggregation, calendar gate, osho registration, and wiki projection are done. S3 replication, blackboard claiming, and end-to-end swarm smoke are not done.

**Wave 2 — Swarm (A1-A6)** — 4-6 hours  
- Build the 4-tier swarm runner
- Replace `run_full_desk.py` with `run_swarm.py`
- Smoke: 1 cycle generates 1000 scouts → ≥100 verified → ≥10 published reports

**Wave 3 — Quality + Coverage (Q1-Q2 + O1-O3 + V1)** — 4-6 hours
- Citation provenance graph + 3 tools
- Wire calendar gate into composer
- Topology engine + wiki generator
- Research Director runs first
- Pipeline Inspector v2

Wave 4 (Devens-gap ingesters D1-D7) and any deferred items handled by separate scoped agents AFTER v5 ships and the refactor stabilizes.

---

## 8. What the refactor agent does NOT touch

- The existing 21 specialist personas (already debugged, already tested). They run inside the new swarm architecture unchanged — Tier 2 analysts ARE these specialists with constitutional prompts. The swarm wraps them; it doesn't replace them.
- The adversarial pipeline modules (`talis_desk/reports/{pipeline,dossier,comparables,polish,grade}.py`). These become Tier 3 — invoked surgically on top-20 analyst outputs, not on every hypothesis.
- The 65+ tic.db tools and ingesters. The swarm calls them; doesn't replace them.
- The source library + ingestion infrastructure (already shipped via Agent P).

---

## 9. The 50-year test

If someone reads this architecture document in 2076:

- **Pillar 1 (Bitemporal Blackboard)** — Hayes-Roth's HEARSAY-II from 1972 → still SOTA in 2026 → still valid in 2076. ✓
- **Pillar 2 (Stigmergy)** — Grassé 1959 → ACO 1991 → still how nature coordinates billion-agent systems → still valid. ✓
- **Pillar 3 (Verifier-Prover)** — Goldwasser-Micali-Rackoff 1985 → zero-knowledge proofs, TLS, blockchain → computational asymmetry is forever. ✓
- **Pillar 4 (Market mechanisms)** — Smith 1776 → VCG 1961 → compute markets between AI agents are inevitable → still valid. ✓
- **Citation provenance** — every claim cryptographically traceable → forever required once trust matters. ✓
- **Bitemporal audit** — append-only history → SQL:2011, Datomic, blockchain → permanent primitive. ✓

Architectural decisions that won't age well (warning signs to AVOID in v5):
- Hard-coded agent counts — make all counts elastic + budget-determined
- Static role assignments — agents self-select tasks via market
- Sequential execution defaults — parallel by default, sequential only when ordering is semantically required
- Centralized orchestrator — coordinator dies → desk dies. Make it work without one.
- Hand-tuned prompts — Phase 6 mutator learns them
- Implicit coordination — explicit via blackboard topics + market bids

---

## 10. Sources

- v4 desk run telemetry: `/var/folders/.../full_desk_zcmrsdda/desk.db` (snapshotted)
- v4 brief: `/tmp/talis_full_desk_brief_20260520_193958.md`
- v3 brief (last successful full cycle): `/tmp/talis_full_desk_brief_20260520_190339.md`
- Codex prod review: `docs/CODEX_PROD_REVIEW_2026-05-20.md`
- arXiv:2507.01701 — Blackboard Architecture for LLM Multi-Agent Systems
- arXiv:2510.01285 — LLM-Based Multi-Agent Blackboard for Information Discovery
- arXiv:2510.12697 — Multi-Agent Debate for LLM Judges
- ICLR 2025 — MAD Performance/Efficiency/Scaling critique
- arXiv:2602.08009 — Adaptive Scalable Robust Coordination of LLM Agents
- arXiv:2602.17046 — Dynamic System Instructions and Tool Exposure (+32% tool routing accuracy)
- Hayes-Roth 1972 (HEARSAY-II), Grassé 1959, Goldwasser-Micali-Rackoff 1985, Lamport-Shostak-Pease 1982, Smith 1776, VCG 1961-73

---

*Generated 2026-05-20 18:25 UTC. The refactor agent works from this single source of truth.*

---

## 11. Post-document additions (alpha-pattern detectors triggered by user screenshots)

Adding these as Wave 3 (or Wave 4 if Wave 3 budget exhausted) line items based on signal screenshots reviewed during the design session:

| Pattern | Detector tool to build | Trigger |
|---|---|---|
| **One-way flow** (zero outflows for N consecutive days) | `detect_one_way_flow(symbol, lookback_days, outflow_pct_floor)` | @therollupco HYPE "ZERO OUTFLOWS" screenshot — net inflow with sub-5% outflow rate for ≥3 consecutive days is a structural accumulation signal |
| **Vehicle-vs-AF order-of-magnitude breach** | Agent T's `compare_vehicle_flow_to_af_baseline` extended to alert when multiple > 5x (instead of just reporting) | @Evan_ss6 BHYP/THYP "order of magnitude > AF buys" screenshot |
| **Named-investor disclosure catalyst** | Agent V's `named_investor_watchlist` + auto-pull SEC filings for ~50 famous investors | @Autopilot Aschenbrenner → T1 Energy $400M cap move |
| **Catalyst + small-cap mid-day move** | Agent T's `synthesize_confluence_window` extended with intraday-trigger mode | @APLD 1GW threshold + +7.92% intra-day move |
| **Supply-side cohort decomposition** | New tool: `analyze_holder_cohorts(symbol)` — break holders into Deployers/AF/DATs/stakers/whales/retail/MMs | @shaundadevens HYPE supply-side analysis ("how much is left to sell above $50?") |

Each of these is a *routine probe* assigned to Tier-1 scouts. When 5+ scouts converge on the same signal (panel consensus pattern), it escalates to Tier-2 for deep investigation. The selection step IS the alpha — not any single agent's IQ.

---

## 12. SOTA coordination critique and amendments

The four-pillar architecture is directionally right, but the first draft overstates a few analogies and underspecifies the engineering contract. These amendments keep the 50-year primitives while making the system buildable.

### What the prior draft got right

- **Blackboard/shared-state coordination is the right substrate.** Recent LLM-MAS work is converging back toward blackboard systems: shared state, dynamic agent selection from current board contents, and repeated selection/execution until convergence.
- **Dynamic tool narrowing is mandatory.** The desk should not expose an 80-tool blob every step. Per-step instruction/tool retrieval is now an explicit SOTA pattern: retrieve only the prompt fragments and tools needed for the current step, with confidence-gated fallbacks.
- **Mass cheap sampling is real but only at the proposal layer.** Agent Forest / sample-and-vote style results support more independent attempts for harder tasks, but this does not justify expensive multi-agent dialogue everywhere.
- **Naive multi-agent debate is not enough.** 2025 MAD evaluations show debate often fails to beat simpler baselines when cost-normalized. Debate belongs at contested verification boundaries, not in every scout loop.
- **Failure attribution and self-evolution are first-class.** Recent surveys argue that multi-agent systems fail at stage boundaries: collaboration without attribution does not become learning.

### What the prior draft got wrong or too loose

- **"2/3 Byzantine consensus" is an analogy, not a proof.** Three LLM verifiers are not a Byzantine-fault-tolerant distributed system. Treat 2/3 cross-family agreement as a practical gate, then measure calibration and false-pass/false-kill rates.
- **"Verifier-prover" is not cryptographic proof.** It is evidence-checking plus source re-fetch plus citation hashing. Do not imply zero-knowledge-level guarantees unless the underlying evidence is actually machine-verifiable.
- **Hard-coded 1000 agents is a target envelope, not an invariant.** The control variable is expected value per dollar and coverage frontier pressure. Agent count should be elastic under the cost ledger.
- **Market pricing needs a measurable payout.** Brier score alone is too slow and sparse for intraday research. Add intermediate rewards: citation pass rate, novelty, non-duplication, realized catalyst detection, and downstream analyst adoption.
- **Shared state without contracts becomes a trash heap.** Every task, claim, citation, vote, and report needs a schema, status lifecycle, owner, TTL, and termination condition.

### Updated coordination design

The durable design is:

**Event-sourced bitemporal blackboard + dynamic task graph + stigmergic coverage + verifier gates + reputation pricing.**

That means:

1. **Event-sourced blackboard.** Agents do not mutate opaque shared text. They append typed events: `task.posted`, `task.claimed`, `claim.proposed`, `citation.resolved`, `verification.vote`, `analysis.promoted`, `brief.used`, `outcome.resolved`.
2. **Dynamic task graph.** Each event can create dependent tasks. The scheduler executes independent tasks in parallel and blocks only on true dependencies. This replaces the current sequential specialist loop.
3. **Task contracts.** Every task declares input schema, allowed tools, evidence requirements, budget, TTL, promotion criteria, and kill criteria.
4. **Instruction/tool retrieval.** At every step, retrieve only relevant persona fragments and tools. Escalate to the broader atlas only when confidence is low.
5. **Diversity allocator.** Seed generation samples the market across `(entity, horizon, lens, source, bias_mode)` with penalties for duplicate cells and stale dense regions.
6. **Verifier gates, not endless debate.** Cheap scouts propose. Independent verifiers re-fetch citations and vote. Analysts only receive verified survivors. Senior debate is reserved for contested/high-impact claims.
7. **Failure attribution loop.** Every killed thesis records why it died: bad source, stale data, duplicate, weak catalyst, wrong math, overfit, too obvious, missing calendar event, or failed outcome. The next cycle's allocator uses this.
8. **Elastic model routing.** DeepSeek/Flash-class models scout; mid-tier models verify/analyze; frontier models synthesize and adjudicate. Counts float with budget, queue pressure, and historical ROI.

### Implementation consequences for Talis

- Add `blackboard_events`, `task_contracts`, `claim_votes`, `coverage_cells`, and `failure_attributions` tables before building `run_swarm.py`.
- Make `coverage_cells` the anti-duplication source of truth. The visualizer and scout allocator both read it.
- Promote only claims that pass citation verification plus novelty/non-dup gates.
- Keep specialist personas as analyst/critic constitutions, not as the top-level scheduler.
- Treat `~/.talis/wiki` as the human-readable projection; `desk.db` remains canonical.
- The visualizer must expose the task graph, coverage heatmap, claim genealogy, citation chain, verifier votes, and failure attributions. A pretty dashboard without these is cosmetic.

### Sources informing this amendment

- Blackboard LLM-MAS: https://arxiv.org/abs/2507.01701
- Data-discovery blackboard system: https://arxiv.org/abs/2510.01285
- Instruction/tool retrieval: https://arxiv.org/abs/2602.17046
- Adaptive pub/sub coordination: https://arxiv.org/abs/2602.08009
- Failure attribution and self-evolution survey: https://arxiv.org/abs/2605.14892
- MAD efficiency critique: https://iclr-blogposts.github.io/2025/blog/mad/
- Agent Forest / sampling-voting: https://arxiv.org/abs/2402.05120
- Dynamic task graphs: https://doi.org/10.1609/icaps.v35i1.36130
