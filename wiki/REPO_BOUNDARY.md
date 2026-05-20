# Repo Boundary Contract — talis-desk ↔ talis-tic

**Two repos. One direction of dependency. Strict boundary.**

## Direction

```
talis-desk  ──depends on──>  talis-tic
talis-tic   ──knows nothing about──>  talis-desk
```

`talis-tic` must remain ignorant of the desk. If you find yourself adding `from talis_desk import ...` inside the talis-tic codebase, **stop** — the dependency arrow goes the other way.

## What talis-desk reads from talis-tic

**Read-only:**
- `TICStore` — claims, events, timeseries, forecasts (v2 type), source_health, semantic_index, scratchpads, entities
- The 65 tools in `tic.desk.tools.TOOLS` (function-calling primitives)
- The 90 data sources in `tic.desk.tools.data_tools.fetch_live(source)` dispatch table
- Pydantic models in `tic.schema` (Claim, Event, Forecast, ClaimKind, Confidence, etc.)

**Write paths (talis-desk emits CLAIMS that get cited by the desk):**
- Trade ideas, hypotheses, debates, etc. live in **desk.db** (own DB)
- When the desk wants to publish a finding into the TIC store as a typed `Claim`, it does so via `TICStore.insert_claim(...)` — only this one mutation, and only with `source_ref="talis_desk:..."`. All other writes stay in `desk.db`.

## What talis-desk owns

**Own database** (`desk.db` SQLite for dev; Postgres schema for prod):

The 10 SOTA tables (from `SOTA_DESK_ARCHITECTURE.md v2` Section 3):
- `hypotheses` + `hypothesis_edges` (graph)
- `trade_ideas` (primary artifact)
- `debates`
- `playbooks`
- `specialist_states`
- `agent_messages`
- `tool_atlas`
- `tool_call_log`
- `reward_log`

Plus the 5 materialized views.

## Boundary at the import level

Allowed imports in talis-desk code:
```python
from talis_tic.store import TICStore               # OK — read access
from talis_tic.desk.tools import TOOLS, dispatch   # OK — tool surface
from talis_tic.schema import Claim, ClaimKind       # OK — type contracts
```

Disallowed:
```python
# in talis-tic — FORBIDDEN:
from talis_desk.something import ...                # NO — wrong direction
```

A linter rule + CI check enforces this.

## Service boundary (future)

For prod, talis-desk becomes a deployable service that:
1. Pulls a snapshot of needed TIC data via the read-only API at cycle start (hydration)
2. Runs the research loop in isolated Modal sandboxes
3. Writes outputs to `desk.db`
4. Publishes a small set of resolved findings back to `tic.db` as `Claim`s
5. Surfaces the trade book via its own HTTP API (or gRPC) to the iOS app

talis-tic stays as the batch ingest pipeline (cron every 30 min) populating the data foundation.

## Versioning

talis-desk pins a specific talis-tic version in `pyproject.toml`. A replay at any `(as_of_valid, as_of_transaction)` uses:
- talis-tic schema snapshot in effect at the time
- talis-desk schema snapshot in effect at the time
- The atlas snapshot and persona version in effect at the time

This is the bitemporal correctness rule from v2.

## Dev workflow

```bash
# Sibling checkouts:
~/jarvis-ios/docs/research/brief_experiments/   # talis-tic
~/jarvis-ios/talis-desk/                          # talis-desk

# Editable installs:
cd ~/jarvis-ios/docs/research/brief_experiments && pip install -e .
cd ~/jarvis-ios/talis-desk && pip install -e .

# Run:
python -m talis_desk.nanoautomation       # MVP
python -m talis_desk.runner               # full cycle
```

## When to break the boundary

Never. If you think you need to, you don't — refactor instead. Examples of "I need to break the boundary" that are actually refactors:

| Smell | Real fix |
|---|---|
| "I need to write a claim from inside the agent loop" | Use `TICStore.insert_claim(source_ref='talis_desk:...')` — that's the one allowed mutation |
| "I need a TIC ingester to emit data the desk consumes" | Add to talis-tic; the desk reads it via TOOLS/store like everything else |
| "The desk needs to know if a source is stale" | Read `source_health` table via TICStore — already exposed |
| "I want a hypothesis to be a TIC entity" | Don't. Hypotheses live in desk.db. Use a TEXT pointer (`hypothesis_id`) if you need to reference from a claim |
