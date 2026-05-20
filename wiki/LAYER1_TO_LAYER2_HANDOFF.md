# Layer 1 → Layer 2 handoff · v1.0 (frozen 2026-05-20)

**State**: Layer 1 (the data foundation) is **frozen at v1.0**. Layer 2 (LLM specialists composing the daily research desk) consumes the surface contracted below. See `CHANGELOG.md` for what shipped in v1.0; see `SCHEMA_VERSION.md` for migrations and enum snapshots.

## What an agent gets

| | |
|---|---|
| Function-calling tools | **47** in `tic.desk.tools.TOOLS` (all serialize to Anthropic + OpenAI function-calling) |
| Live data sources callable mid-draft | **86** via `fetch_live(source, **params)` |
| Historical claims / events / TS | unlimited via `query_*` (per-claim envelope: `kind, value, as_of, age_s, source_ref, confidence, quality_flags`) |
| Time-machine replay | every read tool accepts `as_of` — honest backtests, no future-leak |
| Hot-path stats | `compute_stat(series, op, params)` — 20 ops, ~0.2ms warm |
| Long-tail bespoke | `run_code(code, timeout_s)` — sandboxed Python with numpy/pandas/scipy/statsmodels + `get_ts/get_claims/get_events/list_entities` helpers |
| Semantic retrieval | `semantic_search` — **contextual retrieval** (Anthropic Sept '24, 49% lift) + **hybrid BM25+dense+RRF** (Cormack k=60) |
| Historical analogs | `find_similar_setups(mode="shape")` — **Matrix Profile** via stumpy SCRIMP++, returns matches with realized forward returns |
| Cross-source confluence | `find_confluence(entity)` — 8-family BULL/BEAR vote tally |
| Cross-cycle communication | `scratchpad_post` / `scratchpad_read` |
| Source trust signals | `query_source_health` · `list_data_sources(include_health=True)` |
| Schema introspection per entity | `describe_entity(symbol)` |
| Multi-provider LLM ensemble | 6 providers: Anthropic, OpenAI, xAI, Moonshot, Perplexity, DeepSeek (v4 Pro + Flash) |

## Calling convention

The TOOLS dict is the authoritative registry. For an Anthropic Messages call:

```python
from tic.desk.tools import TOOLS, dispatch
import anthropic

client = anthropic.Anthropic()
fc_tools = [
    {"name": n, "description": s["description"], "input_schema": s["input_schema"]}
    for n, s in TOOLS.items()
]

resp = client.messages.create(
    model="claude-opus-4-7",
    max_tokens=4096,
    tools=fc_tools,
    messages=[{"role": "user", "content": "Compose a BTC daily brief…"}],
)

# Loop on tool_use blocks, dispatch each:
for block in resp.content:
    if block.type == "tool_use":
        result = dispatch(block.name, block.input)
```

Every tool returns a dict. Errors come back as `{"error": "...", ...}`, never as exceptions.

## Trust + freshness — the agent's first reflex

Before citing any claim, an agent should sanity-check the source. Two cheap calls cover it:

1. **`list_data_sources(include_health=True)`** — directory + live status. Every source has a `status` ∈ `{ok, stale, failing, never_succeeded, unknown}` and a `freshness_s` (seconds since last successful run).
2. **`query_source_health(source_slug=…)`** — per-source detail with last error.

Then every read tool's response carries a per-row envelope:

```json
{
  "kind": "funding_rate",
  "value": 0.0034,
  "as_of": "2026-05-19T22:14:03Z",
  "age_s": 187.2,                   // how stale right now
  "source_ref": "hl:predicted_funding",
  "confidence": "high|medium|low|stale|inferred|extrapolated|caveat",
  "quality_flags": ["cap_artifact"]  // ingester-specific hints
}
```

## Time-machine

`as_of` is supported on:
- `query_claims_by_entity`
- `query_events_recent`
- `query_timeseries`
- `semantic_search`
- `time_machine_snapshot` (full point-in-time replay)

Use it for honest backtests: "What did the desk know at 2026-04-22 14:00?" returns claims that were inserted at-or-before that wall clock — nothing leaked from the future.

## Agent-native operations (the moat)

| Tool | What it does | When to use |
|---|---|---|
| `semantic_search(query, k=20)` | OpenAI-embedding cosine over claims + events + artifacts | "Find anything about Fed liquidity tightening" |
| `find_similar_setups(entity_symbol)` | 10-dim feature-vector analog matching + realized forward returns | "When did BTC look like this and what happened next?" |
| `find_confluence(entity_symbol)` | BULL/BEAR/NEUTRAL vote tally across 8 source families | "Does the whole stack agree this is bullish?" |
| `scratchpad_post(cycle_id, …)` | Drop a note for OTHER specialists in this run | macro_regime → semis ("alt-funding spiking, reframe NVDA") |
| `scratchpad_read(cycle_id)` | Read notes other specialists posted in this cycle | Pick up hand-offs |
| `time_machine_snapshot(point_in_time)` | Reconstruct the entire data store as it was at t | Replay a past brief; debug a forecast |
| `replay_artifact_reasoning(artifact_id)` | Re-walk the tool calls that built an artifact | Audit a bad past brief |

## What's NOT yet wired (honest)

- **Quality_flags propagation** — the column exists and persists, but most ingesters still insert with `Confidence.HIGH` and `[]` flags. Wire ingester-by-ingester next.
- **Form 4 same-day netting** — insider trades emit exercise-and-sale legs separately. A specialist looking at NVDA Form 4s will see "CEO sold $4M" when the net is closer to zero. Mitigation: agents should always check for paired Buy+Sell on the same day from the same insider before citing.
- **`whales.py` notional uses entry price** — wallets that bought BTC at $40k still report $40k notional at $80k mark. Hedge mid-cycle whale-move impact scores.
- **`finmodel_full.py` ClaimKind misassignments** — EBITDA written as `NET_INCOME_USD_M`, EBIT as `OP_MARGIN_PCT`. Agents querying financial-statement kinds should disambiguate via `value` JSON `concept` field for now.
- **3 federal sources stubbed**: BEA, Congress.gov, NASA FIRMS — keys arriving via email; flagged with `quality_flags=["needs_refresh","missing_api_key"]` until live.
- **L4 microstructure pack** — VPIN, OFI, Kyle λ. Tokyo node has the raw data; ingester pending.

## Live numbers (latest pipeline run, 2026-05-19)

```
entities      4,819
claims        23,695     (top kinds: funding_rate, price_at, volume_24h_usd, OI, basis_pct)
events        2,022      (top types: news, filing, macro_release, market_anomaly)
timeseries    38,547
anomalies     318        (157 cross-venue funding-arb, 152 funding-arb candidates,
                          5 cross-DEX collisions, 4 RRG regime shifts,
                          5 unit-mismatch ARTIFACTS properly tagged)
```

## How to invoke Layer 2

The repo already has `tic/desk/runner_v3.py` (or equivalent) wired to run a sequence of specialists. A minimal "smoke run" that proves Layer 2 reads Layer 1 cleanly:

```python
from tic.desk.runner_v3 import run_desk_for_scope
result = run_desk_for_scope(
    scope="market", as_of="2026-05-19T22:00:00Z",
    specialists=["macro_regime", "structure", "smart_money"],
)
```

Each specialist gets the TOOLS dict, a system prompt, and a fixed deadline; they emit `Artifact` objects + zero-or-more `Forecast` objects, which the renderer composes into the daily brief.

## What Layer 2 still needs from us (Layer 1)

When Layer 2 runs hit specific gaps, file them as additive ingester requests:
- A new source: add an ingester to `tic/ingest/`, wire `_safe_call` in `run_layer1_foundation.py`, wire `_wrap` in `tic/desk/tools/data_tools.py`.
- A new derived feature: add a `tic/features/<name>.py` and call it after ingesters in the orchestrator.
- A new ClaimKind: append to the `ClaimKind` enum (never replace).

Layer 2 should never silently extend the schema — every new kind/event_type/metric goes through Layer 1's append-only path. This keeps the wiki growing additively forever.

---

**Sign-off**: Data layer is ready. Spine fixes (`source_health`, `Confidence` extension, `quality_flags`, `as_of`) are persisted, smoke-tested, and consumable by an LLM via standard Anthropic function-calling. The 5 outstanding ingester semantic bugs (Form 4 netting, whales notional, finmodel kinds, ClaimKind audit, L4) are listed honestly above — Layer 2 specialists can compose around them today, but those are the highest-leverage 50 hours of remaining Layer-1 work.
