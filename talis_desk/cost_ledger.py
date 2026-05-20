"""Desk-wide daily LLM-spend ledger with a $100/day hard cap.

Codex review finding #15: today the desk's only spend governor is
`LoopConfig.max_cost_usd` (per-cycle, default $5, extension to $10). That
caps any *single* cycle but does nothing to stop a runaway day — if 40
cycles fire (4 specialists * 10 retries on a flaky data source, say) we
could spend $200 before anyone notices. This module adds the missing
daily ceiling: a persistent UTC-day-keyed accumulator with a hard cap
defaulting to $100/day.

# Contract

  * `record(...)` is the post-call write — call it *after* an LLM call
    completes with the real cost. Always persists to `cost_ledger`.
  * `reserve(...)` is advisory in this PR (records the same way `record`
    does). The intent is that a future PR can wire it as a pre-call
    pessimistic reserve, but for now we just want truth-on-write to fuel
    the kill switch.
  * `hard_cap_breached()` returns True once today's total >= the hard
    cap. The loop runner checks this at the TOP of every cycle and
    raises `DailyCostCapExceededError` if tripped.

# Configuration

  * Hard cap defaults to $100/day; override via the
    `TALIS_DESK_DAILY_COST_CAP_USD` env var (parsed as float, must be > 0).
  * Stage tags are loose strings — the canonical set is
    {plan, explore_evidence, synthesize_idea, reflect, debate,
     brief_headline, headline, persona_mutation}.

# Persistence

  * Backed by the `cost_ledger` table (created by schema migration v3).
  * One row per `record()`. Aggregations use the `date_utc` index.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional


#: Hard cap defaults. Override via `TALIS_DESK_DAILY_COST_CAP_USD`.
DEFAULT_HARD_CAP_USD = 100.0


#: Stage tag canonical set — strings, not an Enum, so callers can grow
#: the list without bumping a schema migration. Documentation only;
#: not enforced at write time.
CANONICAL_STAGES = (
    "plan",
    "explore_evidence",
    "synthesize_idea",
    "reflect",
    "debate",
    "brief_headline",
    "headline",
    "persona_mutation",
)


class DailyCostCapExceededError(RuntimeError):
    """Raised by the loop runner when today's accumulated LLM spend has
    crossed the daily hard cap. The caller (cycle dispatcher) should
    skip running new cycles until the next UTC day."""


def _utc_day(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolved_hard_cap() -> float:
    raw = os.environ.get("TALIS_DESK_DAILY_COST_CAP_USD")
    if not raw:
        return DEFAULT_HARD_CAP_USD
    try:
        v = float(raw)
    except ValueError:
        return DEFAULT_HARD_CAP_USD
    if v <= 0:
        return DEFAULT_HARD_CAP_USD
    return v


class CostLedger:
    """Persistent UTC-day-keyed cost accumulator with a hard cap.

    All instances share the desk's singleton `DeskStore` connection by
    default. Pass a `conn` for tests that want an isolated DB.
    """

    DEFAULT_HARD_CAP_USD = DEFAULT_HARD_CAP_USD

    def __init__(
        self,
        conn: Optional[sqlite3.Connection] = None,
        *,
        hard_cap_usd: Optional[float] = None,
    ) -> None:
        if conn is None:
            # Lazy import to dodge a circular import at module load.
            from .store import get_desk_store
            conn = get_desk_store().conn
        self.conn = conn
        self.hard_cap_usd = (
            hard_cap_usd if hard_cap_usd is not None else _resolved_hard_cap()
        )
        # Defensive: ensure the table exists. Normally the migrations
        # module created it on DeskStore init; the guard here keeps
        # `CostLedger(conn=…)` usable in tests that point at a hand-made
        # DB.
        self._ensure_table()

    def _ensure_table(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cost_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date_utc TEXT NOT NULL,
                stage TEXT NOT NULL,
                specialist_id TEXT,
                cycle_id TEXT,
                amount_usd REAL NOT NULL,
                recorded_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cost_ledger_day "
            "ON cost_ledger(date_utc)"
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        amount_usd: float,
        stage: str,
        specialist_id: Optional[str],
        cycle_id: str,
    ) -> None:
        """Persist a real cost. Idempotency / dedup is the caller's
        problem — we just append.

        Negative or zero amounts are silently dropped (don't pollute
        the ledger with no-op LLM calls)."""
        amt = float(amount_usd or 0.0)
        if amt <= 0.0:
            return
        self.conn.execute(
            "INSERT INTO cost_ledger "
            "(date_utc, stage, specialist_id, cycle_id, amount_usd, "
            "recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                _utc_day(),
                str(stage),
                str(specialist_id) if specialist_id is not None else None,
                str(cycle_id),
                amt,
                _utc_now_iso(),
            ),
        )

    def reserve(
        self,
        *,
        amount_usd: float,
        stage: str,
        specialist_id: Optional[str],
        cycle_id: str,
    ) -> bool:
        """Advisory pre-call write. Returns False (and does NOT write)
        when the projected total would breach the hard cap; otherwise
        writes the row and returns True.

        Wired sparingly today — only the cycle dispatcher uses this. The
        canonical write path is `record(...)` which always persists.
        """
        if self.today_total() + float(amount_usd or 0.0) > self.hard_cap_usd:
            return False
        self.record(
            amount_usd=amount_usd,
            stage=stage,
            specialist_id=specialist_id,
            cycle_id=cycle_id,
        )
        return True

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def today_total(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0.0) AS total "
            "FROM cost_ledger WHERE date_utc = ?",
            (_utc_day(),),
        ).fetchone()
        try:
            return float(row["total"])
        except (TypeError, IndexError, KeyError):
            return float(row[0])

    def remaining(self) -> float:
        """USD remaining under the daily cap. Floored at zero."""
        return max(0.0, self.hard_cap_usd - self.today_total())

    def hard_cap_breached(self) -> bool:
        """True once today's spend is at or above the hard cap."""
        return self.today_total() >= self.hard_cap_usd


# ============================================================================
# Module-level convenience accessor — singleton wired against the desk store.
# ============================================================================

_LEDGER: Optional[CostLedger] = None


def get_cost_ledger() -> CostLedger:
    """Singleton CostLedger bound to the desk store's connection."""
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = CostLedger()
    return _LEDGER


def reset_cost_ledger_for_test() -> None:
    """Drop the cached ledger so the next `get_cost_ledger()` rebinds.

    Used by tests that swap the desk store mid-test.
    """
    global _LEDGER
    _LEDGER = None
