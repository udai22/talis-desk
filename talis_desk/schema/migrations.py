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
SCHEMA_VERSION = 10


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


def _m6_blackboard_topic(conn: sqlite3.Connection) -> None:
    """v6: Bitemporal blackboard topic channels.

    Adds a `topic` column to `agent_messages` so swarm tasks can be
    routed via topic patterns (e.g. `bb_topic:scout_output`,
    `bb_topic:verified:macro:*`) instead of overloading
    `to_agent_or_topic`. Backfills `topic` from `to_agent_or_topic` for
    existing rows. Adds an index for pull-mode topic claim queries.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_messages)")}
    if "topic" not in cols:
        conn.execute("ALTER TABLE agent_messages ADD COLUMN topic TEXT")
    # Backfill: legacy rows used `to_agent_or_topic` for both routing and
    # topic; copy that value into `topic` where it looks topic-shaped
    # (starts with `#` or `bb_topic:` prefix). All other rows get
    # `topic = NULL` (direct messages).
    conn.execute(
        "UPDATE agent_messages SET topic = to_agent_or_topic "
        "WHERE topic IS NULL "
        "AND (to_agent_or_topic LIKE '#%' "
        "     OR to_agent_or_topic LIKE 'bb_topic:%' "
        "     OR to_agent_or_topic LIKE 'topic:%')"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_messages_topic "
        "ON agent_messages(topic, transaction_to)"
    )


def _m7_citations(conn: sqlite3.Connection) -> None:
    """v7: Citation provenance graph.

    Every claim that surfaces into a hypothesis / report / brief is
    rooted in a citation row. The row points at (a) the canonical URL,
    (b) the content_hash of the archived snapshot in S3
    (`citations-archive/<hash>`), (c) an anchor (DOM path / page+line /
    PDF page), and (d) the original fetched_at + fetched_via tool URI.

    `coverage_log` is the stigmergic substrate Tier 0 reads when
    deciding which (entity x horizon x lens) cells to sample —
    saturated cells get penalized, frontier cells get boosted.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS citations (
            id TEXT PRIMARY KEY,
            canonical_url TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            content_type TEXT,
            anchor TEXT,
            quote_excerpt TEXT,
            fetched_at TEXT NOT NULL,
            fetched_via_tool_uri TEXT,
            fetched_via_tool_call_id TEXT,
            archive_s3_url TEXT,
            verified_at TEXT,
            verifier_agent_id TEXT,
            still_valid INTEGER,
            hash_changed INTEGER,
            quality_flags TEXT,
            payload TEXT,
            valid_from TEXT NOT NULL,
            valid_to TEXT,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_citations_hash "
        "ON citations(content_hash)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_citations_url "
        "ON citations(canonical_url)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_citations_fetched "
        "ON citations(fetched_at)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coverage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity TEXT,
            horizon TEXT,
            lens TEXT,
            theme TEXT,
            cycle_id TEXT,
            scout_id TEXT,
            scout_count INTEGER NOT NULL DEFAULT 1,
            downstream_published_at TEXT,
            last_covered_at TEXT NOT NULL,
            outcome TEXT,
            payload TEXT,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_coverage_cell "
        "ON coverage_log(entity, horizon, lens)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_coverage_recency "
        "ON coverage_log(last_covered_at)"
    )


def _m8_topology_density(conn: sqlite3.Connection) -> None:
    """v8: Information topology density map.

    Tier 4 (topology engine) writes a snapshot per cycle: a list of
    (region_id, projection_view, density, centroid_x, centroid_y,
    member_hypothesis_ids[]) rows. Tier 0 reads the most recent
    snapshot when deciding which manifold regions to over-sample
    (frontier = low density, novel) vs avoid (consensus = high density,
    already-covered).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS topology_density_map (
            id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            projection_view TEXT NOT NULL,
            region_id TEXT NOT NULL,
            density REAL NOT NULL,
            centroid_x REAL,
            centroid_y REAL,
            radius REAL,
            n_members INTEGER NOT NULL DEFAULT 0,
            member_hypothesis_ids TEXT,
            label TEXT,
            is_frontier INTEGER DEFAULT 0,
            payload TEXT,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_topology_cycle "
        "ON topology_density_map(cycle_id, projection_view)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_topology_frontier "
        "ON topology_density_map(is_frontier, density)"
    )


def _m9_market_bids(conn: sqlite3.Connection) -> None:
    """v9: Market-priced task allocation ledger.

    Every task posted to the blackboard with a `posted_budget` gets a
    bid round. Each candidate agent posts a row (bid_amount + persona
    brier_reputation snapshot). The lowest qualifying bid wins
    (subject to Brier-track-record gating; see
    `talis_desk/swarm/market.py`). Phase 6 mutator updates posteriors
    weekly.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_bids (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            cycle_id TEXT,
            topic TEXT,
            agent_id TEXT NOT NULL,
            specialist_id TEXT,
            bid_amount_usd REAL NOT NULL,
            brier_reputation REAL,
            posted_budget_usd REAL,
            awarded_at TEXT,
            outcome TEXT,
            payload TEXT,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_bids_task "
        "ON market_bids(task_id, awarded_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_bids_agent "
        "ON market_bids(agent_id, awarded_at)"
    )


def _m10_coordination_contracts(conn: sqlite3.Connection) -> None:
    """v10: SOTA coordination kernel primitives.

    The v5 refactor needs typed events and task contracts, not loose
    agent-to-agent prose. These tables make the bitemporal blackboard
    auditable and schedulable:

      - `blackboard_events`: append-only event stream
      - `task_contracts`: typed work items with budget/TTL/promotion gates
      - `claim_votes`: verifier-gate votes on proposed claims
      - `coverage_cells`: anti-duplication source of truth
      - `failure_attributions`: structured reasons killed theses died
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS blackboard_events (
            id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            cycle_id TEXT,
            topic TEXT,
            task_id TEXT,
            claim_id TEXT,
            parent_event_id TEXT,
            agent_id TEXT,
            specialist_id TEXT,
            payload TEXT NOT NULL DEFAULT '{}',
            occurred_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_to TEXT,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bb_events_cycle "
        "ON blackboard_events(cycle_id, occurred_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bb_events_topic "
        "ON blackboard_events(topic, occurred_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bb_events_task "
        "ON blackboard_events(task_id, event_type, occurred_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bb_events_type "
        "ON blackboard_events(event_type, occurred_at)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_contracts (
            id TEXT PRIMARY KEY,
            cycle_id TEXT,
            topic TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL
              CHECK (status IN (
                'posted','claimed','running','completed','failed',
                'expired','killed','promoted'
              )),
            priority REAL NOT NULL DEFAULT 0,
            budget_usd REAL NOT NULL DEFAULT 0,
            ttl_seconds INTEGER,
            owner_agent_id TEXT,
            owner_specialist_id TEXT,
            input_schema_json TEXT NOT NULL DEFAULT '{}',
            allowed_tools_json TEXT NOT NULL DEFAULT '[]',
            evidence_requirements_json TEXT NOT NULL DEFAULT '[]',
            promotion_criteria_json TEXT NOT NULL DEFAULT '{}',
            kill_criteria_json TEXT NOT NULL DEFAULT '{}',
            coverage_cell_key TEXT,
            parent_task_id TEXT,
            posted_at TEXT NOT NULL,
            claimed_at TEXT,
            completed_at TEXT,
            payload TEXT NOT NULL DEFAULT '{}',
            valid_from TEXT NOT NULL,
            valid_to TEXT,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_contracts_status "
        "ON task_contracts(status, priority, posted_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_contracts_topic "
        "ON task_contracts(topic, status, posted_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_contracts_cycle "
        "ON task_contracts(cycle_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_contracts_cell "
        "ON task_contracts(coverage_cell_key, status)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS claim_votes (
            id TEXT PRIMARY KEY,
            claim_id TEXT NOT NULL,
            task_id TEXT,
            cycle_id TEXT,
            verifier_agent_id TEXT NOT NULL,
            verifier_specialist_id TEXT,
            model_family TEXT,
            vote TEXT NOT NULL
              CHECK (vote IN ('pass','fail','abstain','needs_review')),
            confidence REAL,
            rationale TEXT,
            citation_ids TEXT NOT NULL DEFAULT '[]',
            evidence_hash TEXT,
            voted_at TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            valid_from TEXT NOT NULL,
            valid_to TEXT,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_claim_votes_claim "
        "ON claim_votes(claim_id, voted_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_claim_votes_task "
        "ON claim_votes(task_id, voted_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_claim_votes_cycle "
        "ON claim_votes(cycle_id, vote)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coverage_cells (
            cell_key TEXT PRIMARY KEY,
            entity TEXT,
            horizon TEXT,
            lens TEXT,
            source TEXT,
            bias_mode TEXT,
            theme TEXT,
            n_samples INTEGER NOT NULL DEFAULT 0,
            n_promoted INTEGER NOT NULL DEFAULT 0,
            n_killed INTEGER NOT NULL DEFAULT 0,
            novelty_score REAL,
            density_score REAL,
            expected_value_usd REAL,
            last_sampled_at TEXT,
            last_promoted_at TEXT,
            next_sample_after TEXT,
            payload TEXT NOT NULL DEFAULT '{}',
            valid_from TEXT NOT NULL,
            valid_to TEXT,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_coverage_cells_frontier "
        "ON coverage_cells(density_score, novelty_score, expected_value_usd)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_coverage_cells_entity "
        "ON coverage_cells(entity, horizon, lens)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_coverage_cells_recency "
        "ON coverage_cells(last_sampled_at)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS failure_attributions (
            id TEXT PRIMARY KEY,
            artifact_kind TEXT NOT NULL,
            artifact_id TEXT NOT NULL,
            cycle_id TEXT,
            task_id TEXT,
            specialist_id TEXT,
            failure_kind TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'yellow'
              CHECK (severity IN ('green','yellow','red')),
            rationale TEXT,
            source_event_id TEXT,
            payload TEXT NOT NULL DEFAULT '{}',
            attributed_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_to TEXT,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_failure_artifact "
        "ON failure_attributions(artifact_kind, artifact_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_failure_cycle "
        "ON failure_attributions(cycle_id, severity)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_failure_kind "
        "ON failure_attributions(failure_kind, attributed_at)"
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
    Migration(version=6, name="blackboard_topic", forward=_m6_blackboard_topic),
    Migration(version=7, name="citations", forward=_m7_citations),
    Migration(version=8, name="topology_density_map", forward=_m8_topology_density),
    Migration(version=9, name="market_bids", forward=_m9_market_bids),
    Migration(
        version=10,
        name="coordination_contracts",
        forward=_m10_coordination_contracts,
    ),
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
