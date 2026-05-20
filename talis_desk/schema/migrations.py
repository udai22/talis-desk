"""SQLite schema migrations + version tracking for `desk.db`.

Codex review finding #17: today `apply_sota_schema()` runs idempotent
`CREATE TABLE IF NOT EXISTS` on every desk store init. That works for
greenfield development, but it leaves us with no version pointer, no
migration story, and no way to evolve a column without inventing a
sentinel test (e.g. `PRAGMA table_info(...)` introspection). This
module installs the standard pattern:

  - A single-row `schema_version` table records the current version.
  - Each migration is a small named record (`Migration`) that knows
    how to advance the schema from version N -> N+1.
  - `apply_migrations()` runs every migration whose `version > current`,
    in order, inside a transaction, then bumps the recorded version.

Adding a new migration:
  1. Append a new `Migration` to `_MIGRATIONS` with `version =
     SCHEMA_VERSION + 1`.
  2. Bump `SCHEMA_VERSION` to that value.
  3. Implement `forward(conn)` — pure SQL, no third-party deps.
  4. Add a one-line note in the docstring above the migration.

Back-compat: `talis_desk.schema.apply_sota_schema` continues to work as
before; `apply_migrations()` now wraps it so existing call sites pick up
versioning automatically.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


#: Current schema version. Bump when adding a Migration below.
SCHEMA_VERSION = 5


# ============================================================================
# schema_version housekeeping
# ============================================================================

_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL
)
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA_VERSION_DDL)


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Return the current schema version of `conn`. Returns 0 when the
    `schema_version` table is absent or empty (greenfield DB)."""
    _ensure_version_table(conn)
    row = conn.execute(
        "SELECT version FROM schema_version WHERE id = 1"
    ).fetchone()
    if row is None:
        return 0
    # row may be sqlite3.Row or plain tuple depending on row_factory.
    try:
        return int(row["version"])
    except (TypeError, IndexError, KeyError):
        return int(row[0])


def set_schema_version(conn: sqlite3.Connection, v: int) -> None:
    """Stamp the schema_version row to `v`."""
    _ensure_version_table(conn)
    conn.execute(
        "INSERT INTO schema_version (id, version, applied_at) "
        "VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET version = excluded.version, "
        "applied_at = excluded.applied_at",
        (int(v), _utc_now_iso()),
    )


# ============================================================================
# Migration records
# ============================================================================

@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    forward: Callable[[sqlite3.Connection], None]


def _m1_sota_baseline(conn: sqlite3.Connection) -> None:
    """v1: Apply the full SOTA schema (tables + indexes + views) idempotently.

    This mirrors what `apply_sota_schema()` has been doing on every init —
    we just stamp it as version 1 so future migrations can build on a
    known baseline.
    """
    # Local import to avoid a circular import at module load time
    # (`schema/__init__.py` re-exports `apply_sota_schema`).
    from .sota import apply_sota_schema as _baseline
    _baseline(conn, dialect="sqlite")


def _m2_candidate_artifacts(conn: sqlite3.Connection) -> None:
    """v2: Ensure Codex-finding-#7 candidate artifact tables exist.

    `watchlist_setups` and `blocked_ideas` were folded into the SOTA
    baseline shortly after the initial migration; this step exists so a
    DB that was created on an older snapshot picks them up explicitly.
    Idempotent — the underlying DDL uses `IF NOT EXISTS`.
    """
    from .sota import apply_sota_schema as _baseline
    _baseline(conn, dialect="sqlite")


def _m3_cost_ledger(conn: sqlite3.Connection) -> None:
    """v3: Add the daily cost ledger table (codex finding #15).

    Stage tag enumerates the desk's LLM-cost emission points:
    `plan`, `explore_evidence`, `synthesize_idea`, `reflect`, `debate`,
    `brief_headline`, `headline`, `persona_mutation`.
    """
    conn.execute(
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cost_ledger_day "
        "ON cost_ledger(date_utc)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cost_ledger_day_stage "
        "ON cost_ledger(date_utc, stage)"
    )


def _m4_schema_version_seed(conn: sqlite3.Connection) -> None:
    """v4: No-op marker — present so SCHEMA_VERSION matches the highest
    migration version. Future migrations should append below and bump
    SCHEMA_VERSION accordingly."""
    return None


def _m5_research_reports(conn: sqlite3.Connection) -> None:
    """v5: research_reports table — adversarial-pipeline output artifacts.

    Each surviving hypothesis (supported / candidate-promoted / watchlist /
    blocked) gets a 3-stage LLM report (researcher -> adversarial critic ->
    revision). The daily brief composes from these rows instead of from raw
    hypotheses + ideas. Bitemporal append-only contract per the rest of the
    desk store.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_reports (
            id TEXT PRIMARY KEY,
            specialist_id TEXT NOT NULL,
            cycle_id TEXT NOT NULL,
            hypothesis_id TEXT,
            instrument TEXT,
            report_kind TEXT NOT NULL,
            title TEXT NOT NULL,
            abstract TEXT,
            body_md TEXT NOT NULL,
            edge_thesis TEXT,
            contradicting_evidence TEXT,
            citation_claim_ids TEXT,
            citation_tool_call_ids TEXT,
            primary_artifact_id TEXT,
            confidence REAL,
            novelty_score REAL,
            quality_flags TEXT,
            reviewer_turns TEXT,
            adversarial_severity TEXT
              CHECK (adversarial_severity IN ('green','yellow','red')),
            revised_at TEXT NOT NULL,
            cost_usd REAL DEFAULT 0,
            payload TEXT,
            valid_from TEXT NOT NULL,
            valid_to TEXT,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reports_cycle "
        "ON research_reports(cycle_id, transaction_to)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reports_specialist "
        "ON research_reports(specialist_id, transaction_to)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reports_severity "
        "ON research_reports(adversarial_severity, confidence)"
    )


_MIGRATIONS: list[Migration] = [
    Migration(version=1, name="sota_baseline", forward=_m1_sota_baseline),
    Migration(
        version=2,
        name="candidate_artifacts",
        forward=_m2_candidate_artifacts,
    ),
    Migration(version=3, name="cost_ledger", forward=_m3_cost_ledger),
    Migration(
        version=4,
        name="schema_version_seed",
        forward=_m4_schema_version_seed,
    ),
    Migration(version=5, name="research_reports", forward=_m5_research_reports),
]


# ============================================================================
# Public entry point
# ============================================================================

def apply_migrations(
    conn: sqlite3.Connection, dialect: str = "sqlite"
) -> int:
    """Run every migration whose version > current, in ascending order.

    Returns the final schema version stamped into `schema_version`. On a
    fresh DB this advances 0 -> SCHEMA_VERSION; on a healthy DB this is
    a no-op (no migrations fire, version unchanged).

    Postgres dispatching is not yet implemented — for now we delegate to
    `apply_sota_schema(conn, dialect='postgres')` which returns the
    spec SQL without auto-applying it.
    """
    if dialect == "postgres":
        from .sota import apply_sota_schema as _baseline
        _baseline(conn, dialect="postgres")
        return 0
    if dialect != "sqlite":
        raise ValueError(f"unknown dialect: {dialect!r}")

    _ensure_version_table(conn)
    current = get_schema_version(conn)
    if current >= SCHEMA_VERSION:
        return current

    # Apply in ascending order. Each migration runs in its own
    # transaction so a mid-flight crash leaves the DB at the last
    # successful version. We use BEGIN/COMMIT instead of `with conn:`
    # because the DeskStore opens connections with
    # `isolation_level=None` (autocommit) which disables Python's
    # implicit transactions.
    ordered = sorted(_MIGRATIONS, key=lambda m: m.version)
    for m in ordered:
        if m.version <= current:
            continue
        conn.execute("BEGIN")
        try:
            m.forward(conn)
            set_schema_version(conn, m.version)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return get_schema_version(conn)
