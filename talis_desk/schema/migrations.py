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
SCHEMA_VERSION = 24


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


def _m11_flow_reaction_ledger(conn: sqlite3.Connection) -> None:
    """v11: FlowSim — calibrated counterparty-reaction ledger.

    Four tables that together implement the closed loop:
      - `flow_reaction_forecasts`: one row per (cycle, brief, thesis)
        FlowSim run. Stores the aggregate forecast + verdict.
      - `flow_archetype_reactions`: per-archetype reaction (15 per
        forecast typically). Lets us Brier-score each archetype.
      - `flow_outcome_resolutions`: actual market reaction recorded when
        the catalyst resolves. The "real outcome" half of the loop.
      - `flow_calibration_weights`: rolling per-(archetype × sector ×
        catalyst × horizon) weights, updated by the Brier scorer.

    All bitemporally versioned (valid_from / transaction_from).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS flow_reaction_forecasts (
            forecast_id TEXT PRIMARY KEY,
            brief_id TEXT,
            cycle_id TEXT NOT NULL,
            entity TEXT NOT NULL,
            thesis_summary TEXT NOT NULL,
            net_buy_pressure REAL NOT NULL,
            net_vol_pressure REAL NOT NULL,
            flow_disagreement REAL NOT NULL,
            timing_concentration REAL NOT NULL,
            publication_recommendation TEXT NOT NULL
                CHECK (publication_recommendation IN
                       ('publish','publish_size_down','revise','kill')),
            simulator_confidence REAL NOT NULL,
            rationale TEXT,
            quality_flags TEXT,
            cost_usd REAL NOT NULL DEFAULT 0,
            elapsed_s REAL,
            payload TEXT,
            as_of TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flow_forecasts_cycle "
        "ON flow_reaction_forecasts(cycle_id, entity)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flow_forecasts_brief "
        "ON flow_reaction_forecasts(brief_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flow_forecasts_open "
        "ON flow_reaction_forecasts(as_of) WHERE transaction_to IS NULL"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS flow_archetype_reactions (
            id TEXT PRIMARY KEY,
            forecast_id TEXT NOT NULL,
            archetype_id TEXT NOT NULL,
            action TEXT NOT NULL,
            action_probability REAL NOT NULL,
            size_pressure REAL NOT NULL,
            confidence REAL NOT NULL,
            timing_hours REAL NOT NULL,
            rationale TEXT,
            model_used TEXT,
            provider TEXT,
            cost_usd REAL NOT NULL DEFAULT 0,
            elapsed_s REAL,
            quality_flags TEXT,
            error TEXT,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT,
            FOREIGN KEY (forecast_id) REFERENCES flow_reaction_forecasts(forecast_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flow_reactions_forecast "
        "ON flow_archetype_reactions(forecast_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flow_reactions_arch "
        "ON flow_archetype_reactions(archetype_id, action)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS flow_outcome_resolutions (
            id TEXT PRIMARY KEY,
            forecast_id TEXT NOT NULL,
            entity TEXT NOT NULL,
            sector TEXT,
            catalyst TEXT,
            horizon_hours REAL NOT NULL,
            actual_return REAL,
            actual_vol_change REAL,
            actual_volume_z REAL,
            actual_sector_relative REAL,
            outcome_payload TEXT,
            resolved_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT,
            FOREIGN KEY (forecast_id) REFERENCES flow_reaction_forecasts(forecast_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flow_outcomes_forecast "
        "ON flow_outcome_resolutions(forecast_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flow_outcomes_resolved "
        "ON flow_outcome_resolutions(resolved_at)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS flow_calibration_weights (
            id TEXT PRIMARY KEY,
            archetype_id TEXT NOT NULL,
            sector TEXT,
            catalyst_kind TEXT,
            horizon_bucket TEXT,
            weight REAL NOT NULL,
            brier_score REAL,
            n_observations INTEGER NOT NULL DEFAULT 0,
            last_outcome_at TEXT,
            payload TEXT,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_flow_weights_open "
        "ON flow_calibration_weights("
        "archetype_id, COALESCE(sector,''), COALESCE(catalyst_kind,''), "
        "COALESCE(horizon_bucket,'')) "
        "WHERE transaction_to IS NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flow_weights_arch "
        "ON flow_calibration_weights(archetype_id)"
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
    Migration(
        version=11,
        name="flow_reaction_ledger",
        forward=_m11_flow_reaction_ledger,
    ),
    Migration(
        version=12,
        name="pm_overlay_and_execution",
        forward=lambda c: _m12_pm_overlay_and_execution(c),
    ),
    Migration(
        version=13,
        name="order_flow_asset_state_and_levels",
        forward=lambda c: _m13_order_flow_asset_state_and_levels(c),
    ),
    Migration(
        version=14,
        name="information_map_strings",
        forward=lambda c: _m14_information_map_strings(c),
    ),
    Migration(
        version=15,
        name="information_string_quality_fields",
        forward=lambda c: _m15_information_string_quality_fields(c),
    ),
    Migration(
        version=16,
        name="information_synthesis_routing_tables",
        forward=lambda c: _m16_information_synthesis_routing_tables(c),
    ),
    Migration(
        version=17,
        name="information_temporal_pyramid",
        forward=lambda c: _m17_information_temporal_pyramid(c),
    ),
    Migration(
        version=18,
        name="market_event_intelligence",
        forward=lambda c: _m18_market_event_intelligence(c),
    ),
    Migration(
        version=19,
        name="node_intelligence",
        forward=lambda c: _m19_node_intelligence(c),
    ),
    Migration(
        version=20,
        name="analysis_tool_proposals",
        forward=lambda c: _m20_analysis_tool_proposals(c),
    ),
    Migration(
        version=21,
        name="information_alpha_geometry",
        forward=lambda c: _m21_information_alpha_geometry(c),
    ),
    Migration(
        version=22,
        name="market_evolve_programs",
        forward=lambda c: _m22_market_evolve_programs(c),
    ),
    Migration(
        version=23,
        name="market_evolve_experiments",
        forward=lambda c: _m23_market_evolve_experiments(c),
    ),
    Migration(
        version=24,
        name="market_evolve_experiment_results",
        forward=lambda c: _m24_market_evolve_experiment_results(c),
    ),
]


def _m13_order_flow_asset_state_and_levels(conn: sqlite3.Connection) -> None:
    """v13: per-asset order-flow state + level catalog.

    Two bitemporal tables:
      - `order_flow_asset_state`: rolling per-asset memory (recent VPOCs,
        naked POCs, persistent HVN/LVN, dominance pivots).
      - `order_flow_levels`: every level called per asset with track-record
        outcome — feeds the self-calibration loop.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_flow_asset_state (
            id TEXT PRIMARY KEY,
            asset TEXT NOT NULL,
            payload TEXT NOT NULL,
            levels_called_total INTEGER NOT NULL DEFAULT 0,
            levels_held_total INTEGER NOT NULL DEFAULT 0,
            last_updated TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_of_asset_state_open "
        "ON order_flow_asset_state(asset) WHERE transaction_to IS NULL"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_flow_levels (
            id TEXT PRIMARY KEY,
            asset TEXT NOT NULL,
            price REAL NOT NULL,
            source TEXT NOT NULL,
            side TEXT NOT NULL,
            strength REAL NOT NULL,
            distance_from_mark_pct REAL,
            age_sessions INTEGER NOT NULL DEFAULT 0,
            hit_count INTEGER NOT NULL DEFAULT 0,
            rationale TEXT,
            tested_outcome TEXT,
            tested_at TEXT,
            called_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_of_levels_asset_recent "
        "ON order_flow_levels(asset, called_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_of_levels_outcome "
        "ON order_flow_levels(tested_outcome, asset)"
    )


def _m14_information_map_strings(conn: sqlite3.Connection) -> None:
    """v14: persistent information strings + graph synthesis.

    This is the compounding layer for the DeepSeek Flash scout swarm:
      - `information_strings`: causal chains produced by scouts.
      - `information_map_nodes`: durable entity/theme/mechanism nodes.
      - `information_map_edges`: weighted causal links between nodes.
      - `information_syntheses`: cross-string confluence/tension summaries
        that promote the best map discoveries back into verification.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS information_strings (
            id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            coverage_cell_key TEXT,
            scout_id TEXT,
            seed_id TEXT,
            entity TEXT,
            theme TEXT,
            horizon TEXT,
            lens TEXT,
            bias_mode TEXT,
            title TEXT,
            thesis TEXT NOT NULL,
            mechanism TEXT,
            expected_outcome TEXT,
            time_horizon TEXT,
            kill_signal TEXT,
            extends_or_contradicts TEXT,
            would_change_decision INTEGER NOT NULL DEFAULT 1,
            expires_at TEXT,
            crowdedness REAL NOT NULL DEFAULT 0.5,
            conviction REAL NOT NULL DEFAULT 0.5,
            novelty_score REAL NOT NULL DEFAULT 0.5,
            attention_score REAL NOT NULL DEFAULT 0.0,
            entities_chain TEXT,
            depth_layers TEXT,
            evidence_refs TEXT,
            prior_thread_refs TEXT,
            source_tool_call_ids TEXT,
            model_used TEXT,
            provider TEXT,
            cost_usd REAL NOT NULL DEFAULT 0.0,
            quality_flags TEXT,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_strings_cycle "
        "ON information_strings(cycle_id, attention_score DESC, conviction DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_strings_cell "
        "ON information_strings(coverage_cell_key, attention_score DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_strings_entity "
        "ON information_strings(entity, horizon, valid_from DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_strings_theme "
        "ON information_strings(theme, valid_from DESC)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS information_map_nodes (
            node_key TEXT PRIMARY KEY,
            node_type TEXT NOT NULL,
            label TEXT NOT NULL,
            entity TEXT,
            theme TEXT,
            first_seen_cycle_id TEXT,
            last_seen_cycle_id TEXT,
            strength REAL NOT NULL DEFAULT 0.0,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            payload TEXT,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_nodes_type_strength "
        "ON information_map_nodes(node_type, strength DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_nodes_seen "
        "ON information_map_nodes(last_seen_cycle_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS information_map_edges (
            id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            string_id TEXT,
            source_node_key TEXT NOT NULL,
            target_node_key TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            mechanism TEXT,
            horizon TEXT,
            strength REAL NOT NULL DEFAULT 0.0,
            evidence_refs TEXT,
            payload TEXT,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_edges_source "
        "ON information_map_edges(source_node_key, strength DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_edges_target "
        "ON information_map_edges(target_node_key, strength DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_edges_cycle "
        "ON information_map_edges(cycle_id, strength DESC)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS information_syntheses (
            id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            summary TEXT,
            confluences_json TEXT,
            tensions_json TEXT,
            promoted_string_ids_json TEXT,
            promoted_hypotheses_json TEXT,
            model_used TEXT,
            provider TEXT,
            cost_usd REAL NOT NULL DEFAULT 0.0,
            quality_flags TEXT,
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_synth_cycle "
        "ON information_syntheses(cycle_id, created_at DESC)"
    )
    _create_information_synthesis_routing_tables(conn)


def _m15_information_string_quality_fields(conn: sqlite3.Connection) -> None:
    """v15: make anti-waste scout-string fields queryable.

    v14 stored the causal strings. v15 adds the quality/slicing fields the
    scout prompt now asks for so downstream selection can rank by decision
    value instead of re-parsing JSON blobs.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(information_strings)")}
    additions = {
        "coverage_cell_key": "TEXT",
        "extends_or_contradicts": "TEXT",
        "would_change_decision": "INTEGER NOT NULL DEFAULT 1",
        "expires_at": "TEXT",
        "attention_score": "REAL NOT NULL DEFAULT 0.0",
    }
    for name, ddl in additions.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE information_strings ADD COLUMN {name} {ddl}")
    conn.execute(
        """
        UPDATE information_strings
        SET attention_score =
            MIN(1.0, MAX(0.0,
                COALESCE(conviction, 0.0) * 0.45
                + COALESCE(novelty_score, 0.0) * 0.30
                + (1.0 - COALESCE(crowdedness, 0.5)) * 0.15
            ))
        WHERE attention_score IS NULL OR attention_score = 0.0
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_strings_cell "
        "ON information_strings(coverage_cell_key, attention_score DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_strings_attention "
        "ON information_strings(cycle_id, attention_score DESC, conviction DESC)"
    )


def _m16_information_synthesis_routing_tables(conn: sqlite3.Connection) -> None:
    """v16: queryable synthesis items, promoted candidates, and artifact edges.

    v14/v15 made scout strings durable. This migration turns synthesis from
    a JSON sidecar into a routing substrate: each confluence/tension and each
    promoted candidate gets its own row, with typed edges back to source
    strings. The JSON columns stay as debug snapshots, not the primary API.
    """
    _create_information_synthesis_routing_tables(conn)


def _create_information_synthesis_routing_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS information_synthesis_items (
            id TEXT PRIMARY KEY,
            synthesis_id TEXT NOT NULL,
            cycle_id TEXT NOT NULL,
            item_type TEXT NOT NULL
              CHECK (item_type IN ('confluence','tension')),
            label TEXT NOT NULL,
            coverage_cell_key TEXT,
            entity TEXT,
            theme TEXT,
            horizon TEXT,
            lens TEXT,
            why_it_matters TEXT,
            string_ids_json TEXT NOT NULL,
            strength REAL NOT NULL DEFAULT 0.0,
            quality_flags TEXT,
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_synth_items_synth "
        "ON information_synthesis_items(synthesis_id, strength DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_synth_items_slice "
        "ON information_synthesis_items(cycle_id, entity, horizon, lens, strength DESC)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS promoted_candidates (
            id TEXT PRIMARY KEY,
            synthesis_id TEXT,
            synthesis_item_id TEXT,
            cycle_id TEXT NOT NULL,
            coverage_cell_key TEXT,
            entity TEXT,
            theme TEXT,
            horizon TEXT,
            lens TEXT,
            bias_mode TEXT,
            hypothesis TEXT NOT NULL,
            rationale_brief TEXT,
            confidence REAL NOT NULL DEFAULT 0.5,
            promotion_score REAL NOT NULL DEFAULT 0.0,
            source_string_ids_json TEXT NOT NULL,
            suggested_tools_json TEXT,
            status TEXT NOT NULL DEFAULT 'queued_verifier',
            quality_flags TEXT,
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_promoted_candidates_synth "
        "ON promoted_candidates(synthesis_id, promotion_score DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_promoted_candidates_slice "
        "ON promoted_candidates(cycle_id, entity, horizon, lens, promotion_score DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_promoted_candidates_status "
        "ON promoted_candidates(status, cycle_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS information_artifact_edges (
            id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            from_kind TEXT NOT NULL,
            from_id TEXT NOT NULL,
            to_kind TEXT NOT NULL,
            to_id TEXT NOT NULL,
            edge_kind TEXT NOT NULL,
            strength REAL NOT NULL DEFAULT 0.0,
            evidence_role TEXT,
            payload TEXT,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_artifact_edges_from "
        "ON information_artifact_edges(from_kind, from_id, edge_kind)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_artifact_edges_to "
        "ON information_artifact_edges(to_kind, to_id, edge_kind)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS information_string_evidence (
            id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            string_id TEXT NOT NULL,
            evidence_ref TEXT NOT NULL,
            evidence_kind TEXT,
            role TEXT,
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_string_evidence_string "
        "ON information_string_evidence(string_id, evidence_kind)"
    )


def _m17_information_temporal_pyramid(conn: sqlite3.Connection) -> None:
    """v17: time-coherent context fields for information strings.

    The scout layer now reasons across a temporal pyramid: tick/nanosecond,
    second, minute, hour, day, week, month, quarter, year, structural. These
    fields prevent a 1-minute observation from being silently mixed with a
    1-month conclusion without an explicit bridge.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(information_strings)")}
    additions = {
        "time_scale": "TEXT",
        "event_time_start": "TEXT",
        "event_time_end": "TEXT",
        "observed_at": "TEXT",
        "ingested_at": "TEXT",
        "source_time_basis": "TEXT",
        "rollup_parent_ids": "TEXT",
        "lower_timeframe_refs": "TEXT",
        "higher_timeframe_context_refs": "TEXT",
        "temporal_confidence": "REAL NOT NULL DEFAULT 0.5",
    }
    for name, ddl in additions.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE information_strings ADD COLUMN {name} {ddl}")
    conn.execute(
        """
        UPDATE information_strings
        SET time_scale = COALESCE(time_scale, time_horizon, horizon),
            ingested_at = COALESCE(ingested_at, transaction_from),
            temporal_confidence = COALESCE(temporal_confidence, conviction, 0.5)
        WHERE time_scale IS NULL OR ingested_at IS NULL
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_strings_time_scale "
        "ON information_strings(time_scale, event_time_start, event_time_end)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_strings_temporal_slice "
        "ON information_strings(entity, time_scale, valid_from DESC)"
    )


def _m18_market_event_intelligence(conn: sqlite3.Connection) -> None:
    """v18: data-complete market event intelligence bundles.

    Hyperview can show the event row. Talis needs the row plus every
    supporting datapoint needed to explain whether it matters: actor identity,
    liquidity context, derivatives context, historical analogs, scenarios, and
    watch triggers.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_event_intelligence (
            id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            entity TEXT NOT NULL,
            asset TEXT,
            protocol TEXT,
            event_time TEXT,
            source_time_basis TEXT,
            actor_label TEXT,
            actor_address TEXT,
            actor_cluster_id TEXT,
            actor_type TEXT,
            amount REAL,
            amount_unit TEXT,
            notional_usd REAL,
            severity_score REAL NOT NULL DEFAULT 0.5,
            intelligence_score REAL NOT NULL DEFAULT 0.5,
            directional_bias TEXT NOT NULL DEFAULT 'neutral',
            summary TEXT,
            base_case TEXT,
            bull_case TEXT,
            bear_case TEXT,
            kill_signal TEXT,
            source_refs_json TEXT,
            quality_flags TEXT,
            raw_event_json TEXT,
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_event_intel_cycle "
        "ON market_event_intelligence(cycle_id, intelligence_score DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_event_intel_entity "
        "ON market_event_intelligence(entity, event_time, intelligence_score DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_event_intel_actor "
        "ON market_event_intelligence(actor_address, actor_cluster_id, event_time)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_event_data_points (
            id TEXT PRIMARY KEY,
            bundle_id TEXT NOT NULL,
            cycle_id TEXT NOT NULL,
            category TEXT NOT NULL CHECK (
                category IN (
                    'event_fact',
                    'actor_profile',
                    'liquidity_context',
                    'derivatives_context',
                    'historical_analog',
                    'scenario',
                    'watch_trigger',
                    'raw_source'
                )
            ),
            label TEXT NOT NULL,
            value_text TEXT,
            numeric_value REAL,
            unit TEXT,
            source_ref TEXT,
            confidence REAL NOT NULL DEFAULT 0.5,
            observed_at TEXT,
            payload_json TEXT,
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_event_dp_bundle "
        "ON market_event_data_points(bundle_id, category, label)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_event_dp_source "
        "ON market_event_data_points(source_ref, observed_at)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_event_watch_triggers (
            id TEXT PRIMARY KEY,
            bundle_id TEXT NOT NULL,
            cycle_id TEXT NOT NULL,
            trigger_kind TEXT NOT NULL,
            description TEXT NOT NULL,
            horizon TEXT,
            direction TEXT,
            severity TEXT NOT NULL DEFAULT 'yellow',
            source_refs_json TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_event_triggers_bundle "
        "ON market_event_watch_triggers(bundle_id, status, severity)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_event_triggers_cycle "
        "ON market_event_watch_triggers(cycle_id, severity, created_at)"
    )


def _m19_node_intelligence(conn: sqlite3.Connection) -> None:
    """v19: Hydromancer/node-native intelligence snapshots.

    This is the substrate for "know everything from our node": one snapshot
    links Hydromancer, HL-node/reject-corpus, event-store, and timeseries
    observations into a scored actor/flow/state view that can be routed into
    information strings, verifier tasks, and monitor panels.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS node_intelligence_snapshots (
            id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            entity TEXT NOT NULL,
            chain TEXT NOT NULL DEFAULT 'hyperliquid',
            protocol TEXT NOT NULL DEFAULT 'hyperliquid',
            as_of TEXT NOT NULL,
            summary TEXT,
            edge_summary TEXT,
            node_score REAL NOT NULL DEFAULT 0.0,
            source_refs_json TEXT NOT NULL DEFAULT '[]',
            source_families_json TEXT NOT NULL DEFAULT '[]',
            coverage_json TEXT NOT NULL DEFAULT '{}',
            actor_summaries_json TEXT NOT NULL DEFAULT '[]',
            quality_flags TEXT NOT NULL DEFAULT '[]',
            raw_payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_node_intel_cycle "
        "ON node_intelligence_snapshots(cycle_id, node_score DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_node_intel_entity "
        "ON node_intelligence_snapshots(entity, as_of DESC, node_score DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS node_intelligence_observations (
            id TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL,
            cycle_id TEXT NOT NULL,
            category TEXT NOT NULL CHECK (
                category IN (
                    'hydromancer_leaderboard',
                    'wallet_quality',
                    'wallet_trade',
                    'wallet_order_quality',
                    'wallet_state',
                    'builder_flow',
                    'onchain_event',
                    'market_state',
                    'node_reject_corpus',
                    'raw_source'
                )
            ),
            label TEXT NOT NULL,
            actor TEXT,
            value_text TEXT,
            numeric_value REAL,
            unit TEXT,
            source_ref TEXT,
            source_family TEXT,
            confidence REAL NOT NULL DEFAULT 0.5,
            observed_at TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_node_obs_snapshot "
        "ON node_intelligence_observations(snapshot_id, category, actor)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_node_obs_actor "
        "ON node_intelligence_observations(actor, source_family, observed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_node_obs_source "
        "ON node_intelligence_observations(source_ref, category, observed_at)"
    )


def _m20_analysis_tool_proposals(conn: sqlite3.Connection) -> None:
    """v20: analysis-wide tool creation and iteration proposals."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS analysis_tool_proposals (
            id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            artifact_kind TEXT NOT NULL,
            artifact_id TEXT NOT NULL,
            entity TEXT,
            horizon TEXT,
            lens TEXT,
            proposal_kind TEXT NOT NULL DEFAULT 'new_tool',
            tool_name TEXT NOT NULL,
            purpose TEXT NOT NULL,
            source_family TEXT,
            trigger TEXT,
            input_shape_json TEXT NOT NULL DEFAULT '{}',
            promotion_gate_json TEXT NOT NULL DEFAULT '{}',
            eval_plan_json TEXT NOT NULL DEFAULT '{}',
            priority TEXT NOT NULL DEFAULT 'medium',
            status TEXT NOT NULL DEFAULT 'proposed',
            parent_proposal_id TEXT,
            iteration INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL DEFAULT 'analysis_tool_discovery',
            quality_flags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_analysis_tool_prop_cycle "
        "ON analysis_tool_proposals(cycle_id, status, priority)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_analysis_tool_prop_artifact "
        "ON analysis_tool_proposals(artifact_kind, artifact_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_analysis_tool_prop_parent "
        "ON analysis_tool_proposals(parent_proposal_id, iteration)"
    )


def _m21_information_alpha_geometry(conn: sqlite3.Connection) -> None:
    """v21: market-specific alpha geometry over information strings.

    This stores the measurable shape of the information map: source entropy,
    scout independence, evidence coverage, fragility, tension, frontier
    pressure, verifier readiness, and the final trade-scream score per
    entity/horizon/lens/theme cell.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS information_geometry_snapshots (
            id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            cell_key TEXT NOT NULL,
            entity TEXT,
            theme TEXT,
            horizon TEXT,
            lens TEXT,
            string_count INTEGER NOT NULL DEFAULT 0,
            scout_count INTEGER NOT NULL DEFAULT 0,
            evidence_ref_count INTEGER NOT NULL DEFAULT 0,
            source_families_json TEXT NOT NULL DEFAULT '[]',
            coordinates_json TEXT NOT NULL DEFAULT '{}',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            route_directive TEXT NOT NULL DEFAULT 'observe',
            trade_scream_score REAL NOT NULL DEFAULT 0.0,
            verifier_readiness REAL NOT NULL DEFAULT 0.0,
            quality_flags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_geometry_cycle "
        "ON information_geometry_snapshots(cycle_id, trade_scream_score DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_geometry_route "
        "ON information_geometry_snapshots(route_directive, cycle_id, verifier_readiness DESC)"
    )


def _m22_market_evolve_programs(conn: sqlite3.Connection) -> None:
    """v22: evaluator-guided MarketEvolve program database.

    AlphaEvolve evolves code against automated evaluators. MarketEvolve evolves
    Talis research policy: prompt selection, evidence routing, tool-request
    gates, and alpha-geometry thresholds. Programs are never trusted by name;
    they must earn promotion through persisted cycle evaluations.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_evolve_programs (
            id TEXT PRIMARY KEY,
            program_kind TEXT NOT NULL,
            name TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0,
            parent_program_ids_json TEXT NOT NULL DEFAULT '[]',
            genome_json TEXT NOT NULL DEFAULT '{}',
            objective_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'candidate',
            created_from_cycle_id TEXT,
            score REAL NOT NULL DEFAULT 0.0,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            quality_flags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_programs_status "
        "ON market_evolve_programs(status, program_kind, score DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_programs_generation "
        "ON market_evolve_programs(program_kind, generation DESC, score DESC)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_evolve_evaluations (
            id TEXT PRIMARY KEY,
            program_id TEXT NOT NULL,
            cycle_id TEXT NOT NULL,
            evaluator_version TEXT NOT NULL,
            score REAL NOT NULL DEFAULT 0.0,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            baseline_metrics_json TEXT NOT NULL DEFAULT '{}',
            passed INTEGER NOT NULL DEFAULT 0,
            rationale TEXT,
            quality_flags TEXT NOT NULL DEFAULT '[]',
            evaluated_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_eval_cycle "
        "ON market_evolve_evaluations(cycle_id, score DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_eval_program "
        "ON market_evolve_evaluations(program_id, evaluated_at DESC)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_evolve_mutations (
            id TEXT PRIMARY KEY,
            parent_program_id TEXT NOT NULL,
            child_program_id TEXT NOT NULL,
            cycle_id TEXT NOT NULL,
            mutation_kind TEXT NOT NULL,
            mutation_json TEXT NOT NULL DEFAULT '{}',
            rationale TEXT,
            status TEXT NOT NULL DEFAULT 'proposed',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_mut_parent "
        "ON market_evolve_mutations(parent_program_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_mut_cycle "
        "ON market_evolve_mutations(cycle_id, status)"
    )


def _m23_market_evolve_experiments(conn: sqlite3.Connection) -> None:
    """v23: close the MarketEvolve loop into live policy application.

    v22 persisted programs, evaluations, and mutations. v23 records which
    policy actually shaped each scout seed, and stores planned hard experiments
    for candidate policies before they can become active.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_evolve_policy_applications (
            id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            program_id TEXT NOT NULL,
            program_name TEXT,
            generation INTEGER NOT NULL DEFAULT 0,
            seed_id TEXT NOT NULL,
            entity TEXT,
            horizon TEXT,
            lens TEXT,
            bias_mode TEXT,
            theme TEXT,
            prompt_variant TEXT,
            tool_candidate_limit INTEGER NOT NULL DEFAULT 0,
            evidence_tool_limit INTEGER NOT NULL DEFAULT 0,
            applied_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_policy_app_cycle "
        "ON market_evolve_policy_applications(cycle_id, program_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_policy_app_seed "
        "ON market_evolve_policy_applications(seed_id, cycle_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_evolve_experiments (
            id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            parent_program_id TEXT NOT NULL,
            candidate_program_id TEXT NOT NULL,
            experiment_kind TEXT NOT NULL,
            matched_slice_json TEXT NOT NULL DEFAULT '{}',
            arms_json TEXT NOT NULL DEFAULT '[]',
            success_criteria_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'planned',
            rationale TEXT,
            quality_flags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_exp_cycle "
        "ON market_evolve_experiments(cycle_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_exp_candidate "
        "ON market_evolve_experiments(candidate_program_id, status)"
    )


def _m24_market_evolve_experiment_results(conn: sqlite3.Connection) -> None:
    """v24: arm-level online experiment results and promotion decisions."""
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(market_evolve_policy_applications)")
    }
    if "experiment_id" not in cols:
        conn.execute("ALTER TABLE market_evolve_policy_applications ADD COLUMN experiment_id TEXT")
    if "experiment_arm" not in cols:
        conn.execute("ALTER TABLE market_evolve_policy_applications ADD COLUMN experiment_arm TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_policy_app_experiment "
        "ON market_evolve_policy_applications(experiment_id, experiment_arm, cycle_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_evolve_experiment_results (
            id TEXT PRIMARY KEY,
            experiment_id TEXT NOT NULL,
            cycle_id TEXT NOT NULL,
            parent_program_id TEXT NOT NULL,
            candidate_program_id TEXT NOT NULL,
            control_metrics_json TEXT NOT NULL DEFAULT '{}',
            candidate_metrics_json TEXT NOT NULL DEFAULT '{}',
            control_score REAL NOT NULL DEFAULT 0.0,
            candidate_score REAL NOT NULL DEFAULT 0.0,
            score_delta REAL NOT NULL DEFAULT 0.0,
            decision TEXT NOT NULL,
            rationale TEXT,
            quality_flags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_exp_result_cycle "
        "ON market_evolve_experiment_results(cycle_id, decision, score_delta)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_evolve_exp_result_experiment "
        "ON market_evolve_experiment_results(experiment_id, created_at DESC)"
    )


def _m12_pm_overlay_and_execution(conn: sqlite3.Connection) -> None:
    """v12: PM overlay + executable expressions + watchtower rulesets.

    Six new bitemporal tables that together make the brief actionable:
      - `pm_overlay_user_state`: per-user (or 'house') risk overlay snapshot
      - `convexity_scores`: deterministic convexity audit per candidate
      - `executable_expressions`: HL-validated trade instructions
      - `watchtower_rulesets`: machine-readable invalidators per thesis
      - `watchtower_triggers`: rule-trigger event log
      - `watchtower_position_state`: per-position tracking state
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pm_overlay_user_state (
            id TEXT PRIMARY KEY,
            user_id TEXT,                          -- NULL = house default
            nav_usd REAL NOT NULL,
            drawdown_pct REAL NOT NULL,
            max_loss_per_thesis_pct REAL NOT NULL,
            max_total_open_risk_pct REAL NOT NULL,
            max_leverage REAL NOT NULL,
            max_book_correlation REAL NOT NULL,
            can_execute_hl INTEGER NOT NULL,
            existing_positions_json TEXT,
            risk_appetite TEXT,
            as_of TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pm_overlay_user "
        "ON pm_overlay_user_state(user_id) WHERE transaction_to IS NULL"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS convexity_scores (
            id TEXT PRIMARY KEY,
            forecast_id TEXT,
            candidate_id TEXT NOT NULL,
            cycle_id TEXT NOT NULL,
            calibrated_edge REAL NOT NULL,
            payoff_asymmetry REAL NOT NULL,
            catalyst_tightness REAL NOT NULL,
            liquidity REAL NOT NULL,
            crowding REAL NOT NULL,
            funding_bleed REAL NOT NULL,
            correlation_to_book REAL NOT NULL,
            stale_data REAL NOT NULL,
            convexity_score REAL NOT NULL,
            rationale TEXT,
            quality_flags TEXT,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_convexity_cycle "
        "ON convexity_scores(cycle_id, convexity_score DESC)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS executable_expressions (
            id TEXT PRIMARY KEY,
            forecast_id TEXT,
            candidate_id TEXT NOT NULL,
            cycle_id TEXT NOT NULL,
            exchange TEXT NOT NULL,
            hl_coin TEXT,
            asset_id INTEGER,
            dex TEXT,
            is_hip3 INTEGER,
            direction TEXT NOT NULL CHECK (direction IN ('long','short','none')),
            leverage REAL NOT NULL,
            target_size_usd REAL NOT NULL,
            target_size_units REAL NOT NULL,
            margin_required_usd REAL NOT NULL,
            tick_size REAL NOT NULL,
            size_decimals INTEGER NOT NULL,
            min_notional_usd REAL NOT NULL,
            mark_price REAL NOT NULL,
            entry_ladder_json TEXT,
            stop_price REAL,
            tp1_price REAL,
            tp2_price REAL,
            liquidation_price_est REAL,
            funding_8h_bps REAL,
            funding_breakeven_hours REAL,
            data_quality TEXT NOT NULL,
            stale_age_ms INTEGER NOT NULL DEFAULT 0,
            protective_order_status TEXT NOT NULL,
            catalog_source TEXT NOT NULL,
            convexity_score REAL,
            survival_pct_nav REAL,
            publication_label TEXT,
            rationale_short TEXT,
            quality_flags TEXT,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executable_cycle "
        "ON executable_expressions(cycle_id, publication_label)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executable_coin_open "
        "ON executable_expressions(hl_coin) WHERE transaction_to IS NULL"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watchtower_rulesets (
            id TEXT PRIMARY KEY,
            forecast_id TEXT,
            candidate_id TEXT NOT NULL,
            executable_id TEXT,
            cycle_id TEXT NOT NULL,
            hl_coin TEXT NOT NULL,
            direction TEXT NOT NULL,
            rules_json TEXT NOT NULL,
            compiled_at TEXT NOT NULL,
            quality_flags TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_watchtower_rulesets_active "
        "ON watchtower_rulesets(hl_coin, status) WHERE transaction_to IS NULL"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watchtower_triggers (
            id TEXT PRIMARY KEY,
            ruleset_id TEXT NOT NULL,
            forecast_id TEXT,
            rule_id TEXT NOT NULL,
            rule_kind TEXT NOT NULL,
            severity TEXT NOT NULL,
            label TEXT,
            threshold REAL,
            current_value REAL,
            triggered_at TEXT NOT NULL,
            snapshot_json TEXT,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_watchtower_triggers_ruleset "
        "ON watchtower_triggers(ruleset_id, triggered_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_watchtower_triggers_severity "
        "ON watchtower_triggers(severity, triggered_at)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watchtower_position_state (
            id TEXT PRIMARY KEY,
            ruleset_id TEXT,
            forecast_id TEXT,
            candidate_id TEXT NOT NULL,
            hl_coin TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            current_size_units REAL NOT NULL,
            intended_size_units REAL NOT NULL,
            mark_price_at_open REAL NOT NULL,
            opened_at TEXT NOT NULL,
            last_evaluated_at TEXT NOT NULL,
            last_mark_price REAL NOT NULL,
            last_funding_8h_bps REAL,
            last_mini_flow_at TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            quality_flags TEXT,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_watchtower_position_open "
        "ON watchtower_position_state(hl_coin, status) WHERE transaction_to IS NULL"
    )


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

    For Postgres, delegate to `apply_sota_schema(conn, dialect='postgres')`
    which applies the baseline DB-API DDL bundle.
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
