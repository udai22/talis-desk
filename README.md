# talis-desk

**The Talis SOTA autonomous research desk for Hyperliquid traders.**

Layer 2 of the Talis stack. Sits on top of [talis-tic](../docs/research/brief_experiments) (Layer 1 data foundation).

## What this is

A compounding research organism — not a chatbot, not a brief composer. The primary output is a scored trade idea book; the secondary output is the research graph explaining why the book exists and how it improved.

See `wiki/SOTA_DESK_ARCHITECTURE.md` for the locked v2 specification.

## Architecture

```
┌────────────────────────────────────────────────────┐
│  talis-desk  (this repo)                           │
│                                                     │
│  desk/loop/             — canonical research loop  │
│  desk/specialists/      — personas                 │
│  desk/learned_tools/    — agent-built tools        │
│  desk/skills/           — SKILL.md registry        │
│  desk/eval/             — resolver, Brier, alpha   │
│  desk/agents/<id>/fs/   — per-specialist FS        │
│  desk/sandbox/          — Modal integration        │
│  desk.db                — own database             │
│                                                     │
│  Reads from talis-tic via TICStore API (R/O)       │
│  Calls TOOLS registry via function-calling         │
│  Writes to own desk.db (10 SOTA tables)            │
└────────────────────────────────────────────────────┘
                    ▲
                    │ imports talis_tic
                    │ READS ONLY from tic.db
                    │
┌────────────────────────────────────────────────────┐
│  talis-tic  (sibling repo)                         │
│                                                     │
│  tic/store/           — TICStore                   │
│  tic/ingest/          — 90 data sources            │
│  tic/desk/tools/      — 65 agent tools             │
│  tic/features/        — derived features           │
│  tic.db               — claims, events, ts, ...    │
└────────────────────────────────────────────────────┘
```

## Quick start

```bash
# 1. Install (assumes talis-tic is checked out as sibling)
pip install -e ../docs/research/brief_experiments  # talis-tic
pip install -e .                                    # talis-desk

# 2. Initialize the desk database (10 SOTA tables + 5 views + indexes)
python -m talis_desk.schema.init --db desk.db

# 3. Run nanoautomation MVP (one persona, one cycle)
python -m talis_desk.nanoautomation --persona macro_regime --cycle test01

# 4. Run full cycle (when ready)
python -m talis_desk.runner --specialists all
```

## Build state

See `wiki/BUILD_PROGRESS.md`.

## Boundary contract

See `wiki/REPO_BOUNDARY.md`.
