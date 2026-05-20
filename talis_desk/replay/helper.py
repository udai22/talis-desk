"""Bitemporal replay helper — the canonical way to slice history.

# Why this exists
Bitemporal queries are subtle: a row has a `valid_from`/`valid_to` window
(when the fact was true in the world) AND a `transaction_from`/`transaction_to`
window (when we recorded that fact, including corrections via `supersedes`).
Hand-crafted predicates drift across the codebase. v2 of the SOTA Desk
Architecture flagged this as a top risk; this module is the mitigation.

**Never write hand-crafted bitemporal predicates outside this function.**
If you need a new kind of slice (e.g. "transaction-time as-of"), extend
`ReplayContext` here so all callers get the same semantics.

# API
```python
ctx = build_replay_context(as_of_valid=datetime(2026, 5, 19, 14, 0))
where, params = ctx.where_clause("hypotheses")            # no alias
where, params = ctx.where_clause("hypotheses", alias="h") # qualified
sql = f"SELECT * FROM hypotheses WHERE {where}"
rows = conn.execute(sql, params).fetchall()
```

The returned WHERE clause filters by both axes:
  - `valid_from <= as_of_valid AND (valid_to IS NULL OR valid_to > as_of_valid)`
  - `transaction_from <= as_of_transaction AND (transaction_to IS NULL OR transaction_to > as_of_transaction)`

If `as_of_transaction` is omitted (default), it equals `as_of_valid` —
i.e. "show me the world as we believed it at time t".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


def _iso(dt: datetime) -> str:
    """Render to ISO-8601 with UTC tz (the format used in our SQLite store)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class ReplayContext:
    """Immutable bitemporal slice. Use `where_clause(table_alias)` to get
    a parameterized predicate safe for any SOTA table."""

    as_of_valid: datetime
    as_of_transaction: datetime

    def where_clause(self, table: str, alias: Optional[str] = None) -> tuple[str, list[str]]:
        """Return `(predicate_sql, params)` for the WHERE clause.

        Args:
            table: table name (kept for future per-table customization;
                   currently informational).
            alias: optional column qualifier, e.g. `"h"` -> `h.valid_from`.
                   None means unqualified column refs.

        Returns:
            Tuple of (SQL fragment, param list). Use with positional `?`
            parameters: `conn.execute(f"... WHERE {where}", params)`.
        """
        # Silence linters: `table` is informational for now; subclasses or
        # future custom-axis tables may use it. Keeping the parameter
        # locked in the API.
        _ = table

        prefix = f"{alias}." if alias else ""
        v_iso = _iso(self.as_of_valid)
        t_iso = _iso(self.as_of_transaction)
        predicate = (
            f"{prefix}valid_from <= ? "
            f"AND ({prefix}valid_to IS NULL OR {prefix}valid_to > ?) "
            f"AND {prefix}transaction_from <= ? "
            f"AND ({prefix}transaction_to IS NULL OR {prefix}transaction_to > ?)"
        )
        params = [v_iso, v_iso, t_iso, t_iso]
        return predicate, params


def build_replay_context(
    as_of_valid: datetime,
    as_of_transaction: Optional[datetime] = None,
) -> ReplayContext:
    """Build a bitemporal slice. Centralized per v2 risk mitigation.

    Args:
        as_of_valid: world-time anchor — "show me what was true at this
                     moment in the real world".
        as_of_transaction: knowledge-time anchor — "show me only facts
                           we had recorded by this moment". Defaults to
                           `as_of_valid` (we believed the truth at that
                           time).

    Returns:
        A `ReplayContext` whose `where_clause()` is the only sanctioned
        way to filter the SOTA tables by historical state.
    """
    if as_of_transaction is None:
        as_of_transaction = as_of_valid
    return ReplayContext(
        as_of_valid=as_of_valid,
        as_of_transaction=as_of_transaction,
    )
