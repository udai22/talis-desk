"""Watchlist setups + blocked ideas — Codex finding #7.

The trade-idea pipeline currently emits only when a hypothesis hits
`status == 'supported'` (posterior >= 0.7). That binary gate turns the desk
into a sparse classifier: 95% of cycles produce no artifact even when
hypotheses are sitting in the 0.55-0.70 confidence band or were rejected
by validate_trade_idea's hard gates.

This module adds two siblings to TradeIdea:

* `WatchlistSetup` — a hypothesis that hasn't crossed the supported
  threshold but is interesting (0.55-0.70 posterior OR high heat). Carries
  the trigger condition the desk is waiting on. Useful in the daily brief
  as "we're watching X for Y."

* `BlockedIdea` — a hypothesis that *did* try to become a trade idea but
  hit a validation gate (e.g. missing entries, sizing out of band, no
  contradiction at high confidence). Carries the gate's rejection reason
  + what would unblock it. Surfaces in the brief as "we wanted to publish
  X but couldn't because Y."

Both are bitemporal append-only writes (valid_from + transaction_from on
every insert), exactly like trade_ideas. Schema migration lives in
`schema/sota.py::apply_sota_schema`; this module owns the Pydantic models
+ emit helpers.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ============================================================================
# Shared helpers
# ============================================================================

def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _conn_from_context(context: Any) -> sqlite3.Connection:
    """Mirror trade_ideas.model._conn_from_context — accept either an
    AgentContext-like object with `.conn`/`.desk_store` or fall back to
    the singleton DeskStore.
    """
    if context is not None and getattr(context, "conn", None) is not None:
        return context.conn
    if context is not None and getattr(context, "desk_store", None) is not None:
        return context.desk_store.conn
    from ..store import get_desk_store
    return get_desk_store().conn


# ============================================================================
# WatchlistSetup
# ============================================================================

class WatchlistSetup(BaseModel):
    """A hypothesis the desk is monitoring — posterior in the watch band
    [0.55, 0.70) or high-heat-but-not-yet-supported.

    The `watch_condition` is the explicit trigger the desk is waiting on
    (e.g. "BTC closes above 76k on volume", "funding flips negative on ETH
    perp", "Twitter sentiment crosses +0.3 on the 4h"). The brief renders
    this so the user knows what would promote the setup to a trade idea.
    """

    id: str = Field(default_factory=lambda: f"wls_{uuid.uuid4().hex[:12]}")
    specialist_id: str
    hypothesis_id: str
    instrument: str
    direction: Literal["long", "short", "flat", "spread"]
    watch_condition: str = Field(
        ...,
        description=(
            "Plain-text trigger that would promote this setup to a live "
            "trade idea (e.g. 'price closes above 76k on >2x ADV')."
        ),
    )
    expected_horizon: str = Field(
        ...,
        description="Time horizon token (intraday / 12h / 1d / 3d / 7d / 14d / 30d).",
    )
    current_posterior: float
    citation_claim_ids: list[str] = Field(default_factory=list)
    # Bitemporal stamps — every write goes through these (append-only).
    valid_from: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    transaction_from: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    cycle_id: Optional[str] = None
    payload: dict = Field(default_factory=dict)

    @field_validator("current_posterior")
    @classmethod
    def _posterior_in_unit(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"current_posterior must be in [0,1], got {v}")
        return v


# ============================================================================
# BlockedIdea
# ============================================================================

class BlockedIdea(BaseModel):
    """A hypothesis that tried to become a trade idea but hit a
    `validate_trade_idea` gate.

    `block_reason` is the gate's exact error message (e.g.
    `gate3_contradiction_required_at_confidence_0.85`) so the brief
    surfaces what kept it off the book. `what_would_unblock` is a short
    human-facing description of the missing input (e.g.
    "specialist must surface >=1 contradicting claim").
    """

    id: str = Field(default_factory=lambda: f"blk_{uuid.uuid4().hex[:12]}")
    specialist_id: str
    hypothesis_id: str
    instrument: str
    direction: Literal["long", "short", "flat", "spread"]
    block_reason: str = Field(
        ...,
        description="Exact gate message from validate_trade_idea or upstream guard.",
    )
    what_would_unblock: str = Field(
        ...,
        description="Short human description of the missing input.",
    )
    current_posterior: float
    citation_claim_ids: list[str] = Field(default_factory=list)
    valid_from: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    transaction_from: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    cycle_id: Optional[str] = None
    payload: dict = Field(default_factory=dict)

    @field_validator("current_posterior")
    @classmethod
    def _posterior_in_unit(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"current_posterior must be in [0,1], got {v}")
        return v


# ============================================================================
# Emit helpers — append-only INSERTs into the new tables.
# ============================================================================

def emit_watchlist_setup(
    setup: WatchlistSetup,
    context: Any = None,
) -> WatchlistSetup:
    """Persist a WatchlistSetup row. Bitemporal append-only.

    Idempotent on (specialist_id, hypothesis_id, transaction_from) — caller
    is expected to supply a fresh transaction_from per cycle.
    """
    conn = _conn_from_context(context)
    cycle_id = setup.cycle_id or getattr(context, "cycle_id", None)
    conn.execute(
        "INSERT INTO watchlist_setups ("
        "id, specialist_id, hypothesis_id, cycle_id, instrument, direction, "
        "watch_condition, expected_horizon, current_posterior, "
        "citation_claim_ids, payload, valid_from, transaction_from"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            setup.id,
            setup.specialist_id,
            setup.hypothesis_id,
            cycle_id,
            setup.instrument,
            setup.direction,
            setup.watch_condition,
            setup.expected_horizon,
            float(setup.current_posterior),
            _canonical_json(list(setup.citation_claim_ids)),
            _canonical_json(setup.payload or {}),
            _iso(setup.valid_from),
            _iso(setup.transaction_from),
        ),
    )
    return setup


def emit_blocked_idea(
    blocked: BlockedIdea,
    context: Any = None,
) -> BlockedIdea:
    """Persist a BlockedIdea row. Bitemporal append-only."""
    conn = _conn_from_context(context)
    cycle_id = blocked.cycle_id or getattr(context, "cycle_id", None)
    conn.execute(
        "INSERT INTO blocked_ideas ("
        "id, specialist_id, hypothesis_id, cycle_id, instrument, direction, "
        "block_reason, what_would_unblock, current_posterior, "
        "citation_claim_ids, payload, valid_from, transaction_from"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            blocked.id,
            blocked.specialist_id,
            blocked.hypothesis_id,
            cycle_id,
            blocked.instrument,
            blocked.direction,
            blocked.block_reason,
            blocked.what_would_unblock,
            float(blocked.current_posterior),
            _canonical_json(list(blocked.citation_claim_ids)),
            _canonical_json(blocked.payload or {}),
            _iso(blocked.valid_from),
            _iso(blocked.transaction_from),
        ),
    )
    return blocked


# ============================================================================
# Read helpers for the brief composer
# ============================================================================

def fetch_watchlist_setups_for_cycle(
    cycle_id: str, conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    """Return all WatchlistSetup rows for a cycle as dicts (newest first)."""
    if conn is None:
        from ..store import get_desk_store
        conn = get_desk_store().conn
    rows = conn.execute(
        "SELECT * FROM watchlist_setups WHERE cycle_id = ? "
        "ORDER BY transaction_from DESC",
        (cycle_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_blocked_ideas_for_cycle(
    cycle_id: str, conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    """Return all BlockedIdea rows for a cycle as dicts (newest first)."""
    if conn is None:
        from ..store import get_desk_store
        conn = get_desk_store().conn
    rows = conn.execute(
        "SELECT * FROM blocked_ideas WHERE cycle_id = ? "
        "ORDER BY transaction_from DESC",
        (cycle_id,),
    ).fetchall()
    return [dict(r) for r in rows]


__all__ = [
    "WatchlistSetup",
    "BlockedIdea",
    "emit_watchlist_setup",
    "emit_blocked_idea",
    "fetch_watchlist_setups_for_cycle",
    "fetch_blocked_ideas_for_cycle",
]
