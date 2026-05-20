# Schema version — v1.0 (frozen 2026-05-20)

Single source of truth for the TIC schema state at Layer-1 lock-in. **All changes are additive forever** per `WIKI_GROWTH_PRINCIPLES.md`. Never DROP / RENAME / UPDATE-in-place — corrections come as new rows with `supersedes` pointing at the row they replace.

## Migrations applied (chronological)

Each migration is idempotent — `TICStore.__init__` runs them on every startup, wrapped in `try/except sqlite3.OperationalError` so re-running is a no-op.

| # | Migration | Applied | Notes |
|---|---|---|---|
| 1 | `claims` table created | v0 (initial) | id / kind / entity_id / value(JSON) / source_ref / confidence / as_of / provenance_version |
| 2 | `events` table created | v0 | id / event_type / entity_ids(JSON) / event_time / headline / body / impact_score / source / metadata(JSON) / created_at |
| 3 | `timeseries` table created | v0 | (entity_id, metric, ts) PK; value float + value_jsonb |
| 4 | `forecasts` table created | v0 | + supersedes_by (forecast-only at first) |
| 5 | `artifacts` table created | v0 | desk_run_id + claim_ids + forecast_ids + watch_levels + what_we_dont_have + validator_report |
| 6 | `briefs` table created | v0 | tenant_id + wallet + local_date + brief_run_id + idempotency_key UNIQUE |
| 7 | `ALTER TABLE claims ADD COLUMN quality_flags TEXT DEFAULT '[]'` | 2026-05-19 (v0.9) | list-of-strings JSON array on every Claim |
| 8 | `CREATE TABLE source_health` | 2026-05-19 (v0.9) | id / source_slug / attempted_at / succeeded / rows_added / duration_s / error / metadata. Append-only. |
| 9 | `ALTER TABLE claims ADD COLUMN supersedes TEXT` | 2026-05-20 (v1.0) | id of the claim this corrects; NULL when original. Default `get_claims_for_entity` excludes superseded rows unless `include_superseded=True`. |
| 10 | `CREATE INDEX idx_claims_supersedes ON claims(supersedes)` | 2026-05-20 (v1.0) | speeds up the NOT IN subquery for superseded-exclusion |
| 11 | `CREATE TABLE semantic_index` + FTS5 mirror | 2026-05-20 (v1.0) | object_kind / object_id / text_for_embedding / embedding(BLOB) / embedded_at / model_id / **context_blurb** (SOTA upgrade) |
| 12 | `CREATE TABLE scratchpads` | 2026-05-20 (v1.0) | cycle_id / posted_by_specialist / posted_by_model / note_kind / target_audience / content / related_entity_ids / posted_at / read_by / dedupe_key |

## Enum snapshots at v1.0

### `ClaimKind` (62 values)
```
# Market data
PRICE_AT, FUNDING_RATE, OI_USD, VOLUME_24H_USD, PRICE_CHANGE_PCT, BASIS_PCT,
STABLECOIN_SUPPLY_USD, ORACLE_DRIFT_BPS

# L4 microstructure (client-side, public HL Info API)
OFI_5L, KYLE_LAMBDA, VPIN, SPREAD_BPS, SWEEP_COUNT_24H

# Position state
POSITION_SIZE, POSITION_SIDE, LEVERAGE, LIQUIDATION_DISTANCE_PCT,
UNREALIZED_PNL_USD

# Fundamentals
REVENUE_USD_M, REVENUE_YOY_PCT, GROSS_MARGIN_PCT, OP_MARGIN_PCT,
NET_INCOME_USD_M, EPS (new v1.0), EBITDA_USD_M (new v1.0), EBIT_USD_M (new v1.0),
SEGMENT_REVENUE_USD_M (new v1.0), NON_GAAP_METRIC (new v1.0)

# Macro
MACRO_LEVEL, MACRO_CHANGE_PCT

# Event
NEWS_HEADLINE, EARNINGS_DATE, WHALE_MOVE_USD

# Analyst estimates
ANALYST_PRICE_TARGET_USD, ANALYST_EPS_ESTIMATE,
ANALYST_REVENUE_ESTIMATE_USD_M, ANALYST_COUNT

# Corporate-action / equity-meta surface
SHARES_OUTSTANDING_M, FLOAT_SHARES_M, INSIDER_OWN_PCT,
INSTITUTIONAL_OWN_PCT, SHORT_RATIO, BETA, FORWARD_PE, TRAILING_EPS,
MARKET_CAP_USD, FIFTY_TWO_WEEK_HIGH, FIFTY_TWO_WEEK_LOW, FIFTY_DAY_AVG,
TWO_HUNDRED_DAY_AVG, DIVIDEND_YIELD_PCT, RECOMMENDATION_CONSENSUS

# Derived / interpretive (higher confidence threshold)
CAUSAL_LINK, LEVEL_TOUCHED, REGIME_LABEL
```

**Adding a new kind**: append to the enum in `tic/schema/__init__.py`. NEVER remove or rename existing values — old rows reference them by string.

### `Confidence` (7 levels, hierarchical)
```
HIGH         # directly observed in primary source
MEDIUM       # derived from primary source with minor assumption
LOW          # weak signal or heuristic
CAVEAT       # legacy hedge — treat as MEDIUM in new code
STALE        # source last refreshed beyond its freshness budget
INFERRED     # synthesized from other claims, not observed
EXTRAPOLATED # forward-projected from observed history
```

### `EventType` (8 values)
```
NEWS, EARNINGS, FILING, WHALE_MOVE, REGIME_SHIFT, UNLOCK, MACRO_RELEASE,
MARKET_ANOMALY
```

### `EntityType` (5 values)
```
ASSET, COMPANY, WALLET, SECTOR, MACRO_SERIES
```

## Pydantic models at v1.0

```python
class Claim(BaseModel):
    id: str
    kind: ClaimKind
    entity_id: Optional[str]
    value: Any                                          # typed per kind
    source_ref: str                                     # canonical pointer
    confidence: Confidence = Confidence.HIGH
    quality_flags: list[str] = []                       # added v0.9
    supersedes: Optional[str] = None                    # added v1.0
    as_of: datetime
    provenance_version: int = 1

class Event(BaseModel):
    id: str
    event_type: EventType
    entity_ids: list[str] = []
    event_time: datetime
    headline: str
    body: str = ""
    impact_score: Optional[float] = None
    source: str
    metadata: dict[str, Any] = {}
    # quality_flags lives in metadata for events (not a first-class column)
    created_at: datetime
```

## Source-ref canonical format

```
<system>:<table>/<id>[?<qualifier>]
```

Examples:
- `hl:perp_ctx/BTC?t=2026-05-20T02:00:00Z`
- `hl:fundingHistory/HIMS?start=...&end=...`
- `sec:0001045810-26-000123/IS/Revenue`
- `finmodel:NVDA/Q4_FY26/gross_margin`
- `fred:DGS10?t=...`
- `tic:event/<uuid>`
- `tic:artifact/<uuid>#claim_<id>`
- `asksurf:fear_greed?t=...`

## Adding new sources

1. New ingester in `tic/ingest/<name>.py` following the `predicted_fundings.py` pattern
2. `_safe_call("<slug>", "tic.ingest.<name>", "<fn_name>")` in `run_layer1_foundation.py`
3. `_wrap("<slug>", lambda: ...)` in `tic.desk.tools.data_tools._dispatch_table()` so agents can call via `fetch_live("<slug>")`
4. If the ingester emits a new metric prefix, document the convention here for the next hand

## Time-machine guarantees

Every read tool (`query_claims_by_entity`, `query_events_recent`, `query_timeseries`, `semantic_search`, `time_machine_snapshot`) accepts `as_of: Optional[str]`. When supplied, the tool returns the world as it appeared at that moment — no future data leaks in. Per-claim envelope carries `age_s` so agents can hedge staleness without a follow-up call.

## Health invariant

After every `_stage(...)` in the orchestrator (and after every `health_wrap(...)` for opportunistic calls), exactly one row is appended to `source_health` with `(source_slug, attempted_at, succeeded, rows_added, duration_s, error, metadata)`. Agents query this via `query_source_health(source_slug=None, lookback_hours=72)` before citing any claim from that source.
