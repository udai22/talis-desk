"""SOTA Desk schema — Phase 1 migration (bitemporal, append-only).

This module is the canonical source-of-truth implementation of the schema
spelled out in `wiki/SOTA_DESK_ARCHITECTURE.md` v2, Section 3
(lines 170-461). Two flavors are provided:

- `SOTA_SCHEMA_PG_SQL`  : verbatim Postgres DDL from the wiki.
- `SOTA_SCHEMA_SQLITE_SQL`: dev-only translation for the local SQLite store.

Translation rules SQLite vs Postgres:
  - `TIMESTAMPTZ`              -> `TEXT NOT NULL` (ISO strings already)
  - `JSONB`                    -> `TEXT`         (json.loads on read)
  - `NUMERIC(p,s)`             -> `REAL`
  - `TEXT[]`                   -> `TEXT`         (JSON-encoded list as text)
  - `gen_random_bytes(12)`     -> `lower(hex(randomblob(12)))` in DEFAULT
  - `encode(.., 'hex')`        -> drop encode (randomblob is already hex)
  - `CREATE EXTENSION`         -> dropped (no-op on SQLite)
  - `WHERE ...` partial indexes -> dropped for SQLite (Postgres prod keeps them)
  - `CREATE MATERIALIZED VIEW` -> `CREATE VIEW` for SQLite (slower, functional)

Honest gaps (carry into Phase 2 / Postgres prod cutover):
  - `gen_random_bytes`, `pgcrypto`, partial indexes, JSONB operators are
    translated to SQLite equivalents; Postgres prod must use the original
    DDL (`SOTA_SCHEMA_PG_SQL` constant below is the spec).
  - Materialized views in SQLite are plain VIEWs (slower, no `REFRESH`);
    production Postgres should use the MV definitions from v2 Section 3.
  - `pgvector` extension is not needed yet — `semantic_index` already
    stores BLOB embeddings on SQLite; enable on the Postgres cutover.
"""
from __future__ import annotations

import sqlite3
from typing import Any


# =============================================================================
# Postgres source-of-truth DDL (verbatim from v2 Section 3, lines 174-460)
# =============================================================================

SOTA_SCHEMA_PG_SQL = r"""
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT PRIMARY KEY DEFAULT ('hyp_' || encode(gen_random_bytes(12), 'hex')),
    cycle_id TEXT NOT NULL,
    specialist_id TEXT NOT NULL,
    title TEXT NOT NULL,
    hypothesis_text TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active','supported','contradicted','resolved','abandoned')),
    posterior_prob NUMERIC(5,4),
    heat_score NUMERIC(5,4) NOT NULL DEFAULT 0,
    novelty_score NUMERIC(5,4),
    expected_resolution_at TIMESTAMPTZ,
    entity_ids TEXT[] NOT NULL DEFAULT '{}',
    source_ids TEXT[] NOT NULL DEFAULT '{}',
    claim_ids TEXT[] NOT NULL DEFAULT '{}',
    tool_call_ids TEXT[] NOT NULL DEFAULT '{}',
    supersedes TEXT REFERENCES hypotheses(id),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS hypothesis_edges (
    id TEXT PRIMARY KEY DEFAULT ('hedge_' || encode(gen_random_bytes(12), 'hex')),
    from_node_kind TEXT NOT NULL,
    from_node_id TEXT NOT NULL,
    to_node_kind TEXT NOT NULL,
    to_node_id TEXT NOT NULL,
    edge_kind TEXT NOT NULL CHECK (edge_kind IN ('supports','contradicts','derived_from','causes','hedges','supersedes','spawned')),
    strength NUMERIC(6,4),
    citation_ids TEXT[] NOT NULL DEFAULT '{}',
    supersedes TEXT,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS trade_ideas (
    id TEXT PRIMARY KEY DEFAULT ('ti_' || encode(gen_random_bytes(12), 'hex')),
    cycle_id TEXT NOT NULL,
    specialist_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    venue TEXT NOT NULL DEFAULT 'hyperliquid',
    direction TEXT NOT NULL CHECK (direction IN ('long','short','flat','spread')),
    sizing JSONB NOT NULL,
    entry JSONB NOT NULL,
    stop JSONB NOT NULL,
    target JSONB,
    time_horizon TEXT NOT NULL,
    edge_thesis TEXT NOT NULL,
    claim_ids TEXT[] NOT NULL DEFAULT '{}',
    contradicting_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    confluence_score NUMERIC(6,4),
    confidence NUMERIC(6,4) NOT NULL,
    hypothesis_ids TEXT[] NOT NULL DEFAULT '{}',
    forecast_ids TEXT[] NOT NULL DEFAULT '{}',
    debate_ids TEXT[] NOT NULL DEFAULT '{}',
    playbook_id TEXT,
    tool_call_ids TEXT[] NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN ('draft','published','open','closed','expired','invalidated')),
    published_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL,
    realized_outcome JSONB,
    realized_pnl_pct NUMERIC(12,6),
    realized_return_after_fees_pct NUMERIC(12,6),
    benchmark_return_pct NUMERIC(12,6),
    contributed_alpha_pct NUMERIC(12,6),
    brier NUMERIC(12,6),
    resolver_run_id TEXT,
    supersedes TEXT REFERENCES trade_ideas(id),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS debates (
    id TEXT PRIMARY KEY DEFAULT ('deb_' || encode(gen_random_bytes(12), 'hex')),
    cycle_id TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    trigger_id TEXT NOT NULL,
    participants TEXT[] NOT NULL,
    judge_model TEXT NOT NULL,
    judge_provider TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open','awaiting_arguments','judged','applied','expired')),
    due_at TIMESTAMPTZ NOT NULL,
    argument_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    verdict JSONB,
    winner TEXT,
    judge_confidence NUMERIC(6,4),
    later_brier NUMERIC(12,6),
    supersedes TEXT REFERENCES debates(id),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS playbooks (
    id TEXT PRIMARY KEY DEFAULT ('pb_' || encode(gen_random_bytes(12), 'hex')),
    name TEXT NOT NULL,
    version INT NOT NULL,
    owner_specialist TEXT NOT NULL,
    description TEXT NOT NULL,
    trigger_spec JSONB NOT NULL,
    action_template JSONB NOT NULL,
    min_sample_size INT NOT NULL DEFAULT 5,
    historical_trigger_count INT NOT NULL DEFAULT 0,
    historical_avg_return_pct NUMERIC(12,6),
    historical_hit_rate NUMERIC(6,4),
    promoted_status TEXT NOT NULL CHECK (promoted_status IN ('candidate','experimental','approved','demoted','retired')),
    evidence_ids TEXT[] NOT NULL DEFAULT '{}',
    supersedes TEXT REFERENCES playbooks(id),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ,
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS specialist_states (
    id TEXT PRIMARY KEY DEFAULT ('spst_' || encode(gen_random_bytes(12), 'hex')),
    specialist_id TEXT NOT NULL,
    persona_version TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    state_kind TEXT NOT NULL CHECK (state_kind IN ('persona','hydration','dehydration','mutation_candidate','rollback')),
    state_json JSONB NOT NULL,
    prompt_hash TEXT,
    parent_state_id TEXT REFERENCES specialist_states(id),
    ab_test_group TEXT,
    brier_delta NUMERIC(12,6),
    alpha_delta_pct NUMERIC(12,6),
    redundancy_delta NUMERIC(12,6),
    supersedes TEXT REFERENCES specialist_states(id),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id TEXT PRIMARY KEY DEFAULT ('msg_' || encode(gen_random_bytes(12), 'hex')),
    from_agent TEXT NOT NULL,
    to_agent_or_topic TEXT NOT NULL,
    message_kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    posted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_by JSONB NOT NULL DEFAULT '[]'::jsonb,
    expires_at TIMESTAMPTZ,
    dedupe_key TEXT,
    related_artifact_id TEXT,
    related_hypothesis_id TEXT,
    related_trade_idea_id TEXT,
    supersedes TEXT REFERENCES agent_messages(id),
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS tool_atlas (
    id TEXT PRIMARY KEY DEFAULT ('tool_' || encode(gen_random_bytes(12), 'hex')),
    tool_uri TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    version TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('builtin','hydromancer','learned','candidate','skill','external','source')),
    provider TEXT NOT NULL,
    callable_ref TEXT NOT NULL,
    schema_json JSONB NOT NULL,
    skill_md_path TEXT,
    description TEXT NOT NULL,
    source_dependencies TEXT[] NOT NULL DEFAULT '{}',
    permission_scope TEXT NOT NULL DEFAULT 'read_only',
    network_hosts TEXT[] NOT NULL DEFAULT '{}',
    cost_hint JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL CHECK (status IN ('active','candidate','approved','demoted','retired','failing')),
    code_sha256 TEXT,
    supersedes TEXT REFERENCES tool_atlas(id),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ,
    UNIQUE (tool_uri, version, transaction_from)
);

CREATE TABLE IF NOT EXISTS tool_call_log (
    id TEXT PRIMARY KEY DEFAULT ('tc_' || encode(gen_random_bytes(12), 'hex')),
    cycle_id TEXT NOT NULL,
    investigation_id TEXT,
    specialist_id TEXT NOT NULL,
    tool_uri TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    args_json JSONB NOT NULL,
    result_hash TEXT,
    result_summary JSONB,
    reward_score JSONB NOT NULL DEFAULT '{}'::jsonb,
    cited_in_ids TEXT[] NOT NULL DEFAULT '{}',
    error JSONB,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    duration_ms INT,
    cost_usd NUMERIC(12,6) NOT NULL DEFAULT 0,
    source_ids TEXT[] NOT NULL DEFAULT '{}',
    claim_ids TEXT[] NOT NULL DEFAULT '{}',
    quality_flags TEXT[] NOT NULL DEFAULT '{}',
    supersedes TEXT,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS reward_log (
    id TEXT PRIMARY KEY DEFAULT ('rew_' || encode(gen_random_bytes(12), 'hex')),
    cycle_id TEXT NOT NULL,
    reward_kind TEXT NOT NULL CHECK (reward_kind IN ('curiosity','correctness','alpha','novelty','coverage','cost_penalty','debate_quality','playbook_hit')),
    subject_kind TEXT NOT NULL CHECK (subject_kind <> 'tool'),
    subject_id TEXT NOT NULL,
    specialist_id TEXT,
    score NUMERIC(12,6) NOT NULL,
    baseline_score NUMERIC(12,6),
    delta NUMERIC(12,6),
    attribution_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    supersedes TEXT,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    transaction_to TIMESTAMPTZ
);

-- Explicit indexes (Postgres prod). Partial indexes filter on
-- `transaction_to IS NULL` so hot reads skip historical rows.
CREATE INDEX IF NOT EXISTS idx_hypotheses_hot ON hypotheses (status, heat_score, expected_resolution_at) WHERE transaction_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_hypotheses_specialist_time ON hypotheses (specialist_id, valid_from) WHERE transaction_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_hyp_edges_to ON hypothesis_edges (to_node_kind, to_node_id, edge_kind) WHERE transaction_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_hyp_edges_from ON hypothesis_edges (from_node_kind, from_node_id, edge_kind) WHERE transaction_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_trade_ideas_book ON trade_ideas (status, instrument, expires_at) WHERE transaction_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_trade_ideas_specialist_alpha ON trade_ideas (specialist_id, contributed_alpha_pct) WHERE transaction_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_trade_ideas_playbook ON trade_ideas (playbook_id, published_at) WHERE transaction_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_debates_due ON debates (status, due_at) WHERE transaction_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_playbooks_status ON playbooks (promoted_status, name, version) WHERE transaction_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_agent_messages_route ON agent_messages (to_agent_or_topic, posted_at) WHERE transaction_to IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_messages_dedupe ON agent_messages (from_agent, dedupe_key, transaction_from) WHERE dedupe_key IS NOT NULL AND transaction_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_tool_atlas_uri ON tool_atlas (tool_uri, valid_from) WHERE transaction_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_tool_call_specialist_time ON tool_call_log (specialist_id, started_at);
CREATE INDEX IF NOT EXISTS idx_tool_call_investigation ON tool_call_log (investigation_id, started_at) WHERE investigation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_reward_subject ON reward_log (subject_kind, subject_id, reward_kind, valid_from);
CREATE INDEX IF NOT EXISTS idx_specialist_states_specialist ON specialist_states (specialist_id, valid_from);

-- Materialized views (Postgres). SQLite uses regular VIEWs below.
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_top_tools_per_specialist_30d AS
SELECT specialist_id, tool_uri, count(*) AS n_calls,
       count(*) FILTER (WHERE error IS NULL) AS n_success,
       sum(cost_usd) AS total_cost_usd,
       avg((reward_score->>'brier_delta')::numeric) AS brier_delta_avg
FROM tool_call_log
WHERE started_at >= now() - interval '30 days' AND transaction_to IS NULL
GROUP BY specialist_id, tool_uri;

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_specialist_brier_rolling AS
SELECT specialist_id, date_trunc('day', valid_from) AS day,
       avg(score) AS brier_avg, avg(delta) AS brier_delta_avg, count(*) AS n
FROM reward_log
WHERE reward_kind = 'correctness' AND transaction_to IS NULL
GROUP BY specialist_id, date_trunc('day', valid_from);

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_hot_hypotheses AS
SELECT h.id, h.specialist_id, h.title, h.posterior_prob, h.heat_score,
       count(e.id) AS evidence_edges, max(h.transaction_from) AS last_updated
FROM hypotheses h
LEFT JOIN hypothesis_edges e ON e.to_node_id = h.id AND e.transaction_to IS NULL
WHERE h.status = 'active' AND h.transaction_to IS NULL AND h.valid_to IS NULL
GROUP BY h.id, h.specialist_id, h.title, h.posterior_prob, h.heat_score;

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_trade_book_open AS
SELECT id, specialist_id, instrument, direction, sizing, entry, stop, target,
       confidence, confluence_score, published_at, expires_at,
       realized_pnl_pct, benchmark_return_pct, contributed_alpha_pct
FROM trade_ideas
WHERE status IN ('published','open') AND transaction_to IS NULL AND valid_to IS NULL;

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_weakness_map AS
SELECT specialist_id,
       'brier_or_alpha_or_cost' AS weakness_kind,
       jsonb_build_object('brier', avg(score), 'delta', avg(delta)) AS evidence_json
FROM reward_log
WHERE transaction_to IS NULL
GROUP BY specialist_id;
"""


# =============================================================================
# SQLite translation (dev only — Postgres prod uses SOTA_SCHEMA_PG_SQL above)
# =============================================================================
#
# Translation applied per the rules in the module docstring. We split the
# DDL into three lists (tables, indexes, views) so `apply_sota_schema` can
# bucket errors by object kind in the return dict.

_SOTA_TABLES_SQLITE = [
    # hypotheses
    """
    CREATE TABLE IF NOT EXISTS hypotheses (
        id TEXT PRIMARY KEY DEFAULT ('hyp_' || lower(hex(randomblob(12)))),
        cycle_id TEXT NOT NULL,
        specialist_id TEXT NOT NULL,
        title TEXT NOT NULL,
        hypothesis_text TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('active','supported','contradicted','resolved','abandoned')),
        posterior_prob REAL,
        heat_score REAL NOT NULL DEFAULT 0,
        novelty_score REAL,
        expected_resolution_at TEXT,
        entity_ids TEXT NOT NULL DEFAULT '[]',
        source_ids TEXT NOT NULL DEFAULT '[]',
        claim_ids TEXT NOT NULL DEFAULT '[]',
        tool_call_ids TEXT NOT NULL DEFAULT '[]',
        supersedes TEXT REFERENCES hypotheses(id),
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        transaction_from TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        transaction_to TEXT,
        payload TEXT NOT NULL DEFAULT '{}'
    )
    """,
    # hypothesis_edges
    """
    CREATE TABLE IF NOT EXISTS hypothesis_edges (
        id TEXT PRIMARY KEY DEFAULT ('hedge_' || lower(hex(randomblob(12)))),
        from_node_kind TEXT NOT NULL,
        from_node_id TEXT NOT NULL,
        to_node_kind TEXT NOT NULL,
        to_node_id TEXT NOT NULL,
        edge_kind TEXT NOT NULL CHECK (edge_kind IN ('supports','contradicts','derived_from','causes','hedges','supersedes','spawned')),
        strength REAL,
        citation_ids TEXT NOT NULL DEFAULT '[]',
        supersedes TEXT,
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        transaction_from TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        transaction_to TEXT
    )
    """,
    # trade_ideas
    """
    CREATE TABLE IF NOT EXISTS trade_ideas (
        id TEXT PRIMARY KEY DEFAULT ('ti_' || lower(hex(randomblob(12)))),
        cycle_id TEXT NOT NULL,
        specialist_id TEXT NOT NULL,
        instrument TEXT NOT NULL,
        venue TEXT NOT NULL DEFAULT 'hyperliquid',
        direction TEXT NOT NULL CHECK (direction IN ('long','short','flat','spread')),
        sizing TEXT NOT NULL,
        entry TEXT NOT NULL,
        stop TEXT NOT NULL,
        target TEXT,
        time_horizon TEXT NOT NULL,
        edge_thesis TEXT NOT NULL,
        claim_ids TEXT NOT NULL DEFAULT '[]',
        contradicting_evidence TEXT NOT NULL DEFAULT '[]',
        confluence_score REAL,
        confidence REAL NOT NULL,
        hypothesis_ids TEXT NOT NULL DEFAULT '[]',
        forecast_ids TEXT NOT NULL DEFAULT '[]',
        debate_ids TEXT NOT NULL DEFAULT '[]',
        playbook_id TEXT,
        tool_call_ids TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL CHECK (status IN ('draft','published','open','closed','expired','invalidated')),
        published_at TEXT,
        expires_at TEXT NOT NULL,
        realized_outcome TEXT,
        realized_pnl_pct REAL,
        realized_return_after_fees_pct REAL,
        benchmark_return_pct REAL,
        contributed_alpha_pct REAL,
        brier REAL,
        resolver_run_id TEXT,
        supersedes TEXT REFERENCES trade_ideas(id),
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        transaction_from TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        transaction_to TEXT,
        payload TEXT NOT NULL DEFAULT '{}'
    )
    """,
    # debates
    """
    CREATE TABLE IF NOT EXISTS debates (
        id TEXT PRIMARY KEY DEFAULT ('deb_' || lower(hex(randomblob(12)))),
        cycle_id TEXT NOT NULL,
        trigger_kind TEXT NOT NULL,
        trigger_id TEXT NOT NULL,
        participants TEXT NOT NULL,
        judge_model TEXT NOT NULL,
        judge_provider TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('open','awaiting_arguments','judged','applied','expired')),
        due_at TEXT NOT NULL,
        argument_payload TEXT NOT NULL DEFAULT '{}',
        verdict TEXT,
        winner TEXT,
        judge_confidence REAL,
        later_brier REAL,
        supersedes TEXT REFERENCES debates(id),
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        transaction_from TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        transaction_to TEXT
    )
    """,
    # playbooks
    """
    CREATE TABLE IF NOT EXISTS playbooks (
        id TEXT PRIMARY KEY DEFAULT ('pb_' || lower(hex(randomblob(12)))),
        name TEXT NOT NULL,
        version INTEGER NOT NULL,
        owner_specialist TEXT NOT NULL,
        description TEXT NOT NULL,
        trigger_spec TEXT NOT NULL,
        action_template TEXT NOT NULL,
        min_sample_size INTEGER NOT NULL DEFAULT 5,
        historical_trigger_count INTEGER NOT NULL DEFAULT 0,
        historical_avg_return_pct REAL,
        historical_hit_rate REAL,
        promoted_status TEXT NOT NULL CHECK (promoted_status IN ('candidate','experimental','approved','demoted','retired')),
        evidence_ids TEXT NOT NULL DEFAULT '[]',
        supersedes TEXT REFERENCES playbooks(id),
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        transaction_from TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        transaction_to TEXT,
        UNIQUE (name, version)
    )
    """,
    # specialist_states
    """
    CREATE TABLE IF NOT EXISTS specialist_states (
        id TEXT PRIMARY KEY DEFAULT ('spst_' || lower(hex(randomblob(12)))),
        specialist_id TEXT NOT NULL,
        persona_version TEXT NOT NULL,
        cycle_id TEXT NOT NULL,
        state_kind TEXT NOT NULL CHECK (state_kind IN ('persona','hydration','dehydration','mutation_candidate','rollback')),
        state_json TEXT NOT NULL,
        prompt_hash TEXT,
        parent_state_id TEXT REFERENCES specialist_states(id),
        ab_test_group TEXT,
        brier_delta REAL,
        alpha_delta_pct REAL,
        redundancy_delta REAL,
        supersedes TEXT REFERENCES specialist_states(id),
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        transaction_from TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        transaction_to TEXT
    )
    """,
    # agent_messages
    """
    CREATE TABLE IF NOT EXISTS agent_messages (
        id TEXT PRIMARY KEY DEFAULT ('msg_' || lower(hex(randomblob(12)))),
        from_agent TEXT NOT NULL,
        to_agent_or_topic TEXT NOT NULL,
        message_kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        posted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        read_by TEXT NOT NULL DEFAULT '[]',
        expires_at TEXT,
        dedupe_key TEXT,
        related_artifact_id TEXT,
        related_hypothesis_id TEXT,
        related_trade_idea_id TEXT,
        supersedes TEXT REFERENCES agent_messages(id),
        valid_from TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        valid_to TEXT,
        transaction_from TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        transaction_to TEXT
    )
    """,
    # tool_atlas
    """
    CREATE TABLE IF NOT EXISTS tool_atlas (
        id TEXT PRIMARY KEY DEFAULT ('tool_' || lower(hex(randomblob(12)))),
        tool_uri TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        version TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('builtin','hydromancer','learned','candidate','skill','external','source')),
        provider TEXT NOT NULL,
        callable_ref TEXT NOT NULL,
        schema_json TEXT NOT NULL,
        skill_md_path TEXT,
        description TEXT NOT NULL,
        source_dependencies TEXT NOT NULL DEFAULT '[]',
        permission_scope TEXT NOT NULL DEFAULT 'read_only',
        network_hosts TEXT NOT NULL DEFAULT '[]',
        cost_hint TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL CHECK (status IN ('active','candidate','approved','demoted','retired','failing')),
        code_sha256 TEXT,
        supersedes TEXT REFERENCES tool_atlas(id),
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        transaction_from TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        transaction_to TEXT,
        UNIQUE (tool_uri, version, transaction_from)
    )
    """,
    # tool_call_log
    """
    CREATE TABLE IF NOT EXISTS tool_call_log (
        id TEXT PRIMARY KEY DEFAULT ('tc_' || lower(hex(randomblob(12)))),
        cycle_id TEXT NOT NULL,
        investigation_id TEXT,
        specialist_id TEXT NOT NULL,
        tool_uri TEXT NOT NULL,
        tool_version TEXT NOT NULL,
        args_hash TEXT NOT NULL,
        args_json TEXT NOT NULL,
        result_hash TEXT,
        result_summary TEXT,
        reward_score TEXT NOT NULL DEFAULT '{}',
        cited_in_ids TEXT NOT NULL DEFAULT '[]',
        error TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        duration_ms INTEGER,
        cost_usd REAL NOT NULL DEFAULT 0,
        source_ids TEXT NOT NULL DEFAULT '[]',
        claim_ids TEXT NOT NULL DEFAULT '[]',
        quality_flags TEXT NOT NULL DEFAULT '[]',
        supersedes TEXT,
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        transaction_from TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        transaction_to TEXT
    )
    """,
    # reward_log
    """
    CREATE TABLE IF NOT EXISTS reward_log (
        id TEXT PRIMARY KEY DEFAULT ('rew_' || lower(hex(randomblob(12)))),
        cycle_id TEXT NOT NULL,
        reward_kind TEXT NOT NULL CHECK (reward_kind IN ('curiosity','correctness','alpha','novelty','coverage','cost_penalty','debate_quality','playbook_hit')),
        subject_kind TEXT NOT NULL CHECK (subject_kind <> 'tool'),
        subject_id TEXT NOT NULL,
        specialist_id TEXT,
        score REAL NOT NULL,
        baseline_score REAL,
        delta REAL,
        attribution_json TEXT NOT NULL DEFAULT '{}',
        supersedes TEXT,
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        transaction_from TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        transaction_to TEXT
    )
    """,
]


# Indexes — SQLite version drops the `WHERE transaction_to IS NULL` partial
# index filter (per translation rules in the module docstring). The unique
# dedupe index keeps its `dedupe_key IS NOT NULL` filter because it's a
# correctness gate, not a perf hint.
_SOTA_INDEXES_SQLITE = [
    "CREATE INDEX IF NOT EXISTS idx_hypotheses_hot ON hypotheses (status, heat_score, expected_resolution_at)",
    "CREATE INDEX IF NOT EXISTS idx_hypotheses_specialist_time ON hypotheses (specialist_id, valid_from)",
    "CREATE INDEX IF NOT EXISTS idx_hyp_edges_to ON hypothesis_edges (to_node_kind, to_node_id, edge_kind)",
    "CREATE INDEX IF NOT EXISTS idx_hyp_edges_from ON hypothesis_edges (from_node_kind, from_node_id, edge_kind)",
    "CREATE INDEX IF NOT EXISTS idx_trade_ideas_book ON trade_ideas (status, instrument, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_trade_ideas_specialist_alpha ON trade_ideas (specialist_id, contributed_alpha_pct)",
    "CREATE INDEX IF NOT EXISTS idx_trade_ideas_playbook ON trade_ideas (playbook_id, published_at)",
    "CREATE INDEX IF NOT EXISTS idx_debates_due ON debates (status, due_at)",
    "CREATE INDEX IF NOT EXISTS idx_playbooks_status ON playbooks (promoted_status, name, version)",
    "CREATE INDEX IF NOT EXISTS idx_agent_messages_route ON agent_messages (to_agent_or_topic, posted_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_messages_dedupe ON agent_messages (from_agent, dedupe_key, transaction_from) WHERE dedupe_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_tool_atlas_uri ON tool_atlas (tool_uri, valid_from)",
    "CREATE INDEX IF NOT EXISTS idx_tool_call_specialist_time ON tool_call_log (specialist_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_tool_call_investigation ON tool_call_log (investigation_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_reward_subject ON reward_log (subject_kind, subject_id, reward_kind, valid_from)",
    "CREATE INDEX IF NOT EXISTS idx_specialist_states_specialist ON specialist_states (specialist_id, valid_from)",
]


# Views — SQLite doesn't have materialized views, so these are regular VIEWs.
# Slower (re-evaluated per query) but functional for dev. Postgres prod uses
# the MV definitions from `SOTA_SCHEMA_PG_SQL` above.
#
# JSON ops translated: Postgres `reward_score->>'brier_delta'` becomes
# `json_extract(reward_score, '$.brier_delta')` in SQLite.
_SOTA_VIEWS_SQLITE = [
    """
    CREATE VIEW IF NOT EXISTS mv_top_tools_per_specialist_30d AS
    SELECT specialist_id,
           tool_uri,
           count(*) AS n_calls,
           sum(CASE WHEN error IS NULL THEN 1 ELSE 0 END) AS n_success,
           sum(cost_usd) AS total_cost_usd,
           avg(CAST(json_extract(reward_score, '$.brier_delta') AS REAL)) AS brier_delta_avg
    FROM tool_call_log
    WHERE started_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-30 days')
      AND transaction_to IS NULL
    GROUP BY specialist_id, tool_uri
    """,
    """
    CREATE VIEW IF NOT EXISTS mv_specialist_brier_rolling AS
    SELECT specialist_id,
           substr(valid_from, 1, 10) AS day,
           avg(score) AS brier_avg,
           avg(delta) AS brier_delta_avg,
           count(*) AS n
    FROM reward_log
    WHERE reward_kind = 'correctness' AND transaction_to IS NULL
    GROUP BY specialist_id, substr(valid_from, 1, 10)
    """,
    """
    CREATE VIEW IF NOT EXISTS mv_hot_hypotheses AS
    SELECT h.id, h.specialist_id, h.title, h.posterior_prob, h.heat_score,
           count(e.id) AS evidence_edges,
           max(h.transaction_from) AS last_updated
    FROM hypotheses h
    LEFT JOIN hypothesis_edges e
      ON e.to_node_id = h.id AND e.transaction_to IS NULL
    WHERE h.status = 'active' AND h.transaction_to IS NULL AND h.valid_to IS NULL
    GROUP BY h.id, h.specialist_id, h.title, h.posterior_prob, h.heat_score
    """,
    """
    CREATE VIEW IF NOT EXISTS mv_trade_book_open AS
    SELECT id, specialist_id, instrument, direction, sizing, entry, stop, target,
           confidence, confluence_score, published_at, expires_at,
           realized_pnl_pct, benchmark_return_pct, contributed_alpha_pct
    FROM trade_ideas
    WHERE status IN ('published','open') AND transaction_to IS NULL AND valid_to IS NULL
    """,
    """
    CREATE VIEW IF NOT EXISTS mv_weakness_map AS
    SELECT specialist_id,
           'brier_or_alpha_or_cost' AS weakness_kind,
           json_object('brier', avg(score), 'delta', avg(delta)) AS evidence_json
    FROM reward_log
    WHERE transaction_to IS NULL
    GROUP BY specialist_id
    """,
]


SOTA_SCHEMA_SQLITE_SQL = (
    ";\n".join(stmt.strip() for stmt in _SOTA_TABLES_SQLITE)
    + ";\n"
    + ";\n".join(_SOTA_INDEXES_SQLITE)
    + ";\n"
    + ";\n".join(stmt.strip() for stmt in _SOTA_VIEWS_SQLITE)
    + ";"
)


# The canonical list of SOTA table names (used by acceptance test #1).
SOTA_TABLE_NAMES = [
    "hypotheses",
    "hypothesis_edges",
    "trade_ideas",
    "debates",
    "playbooks",
    "specialist_states",
    "agent_messages",
    "tool_atlas",
    "tool_call_log",
    "reward_log",
]

SOTA_VIEW_NAMES = [
    "mv_top_tools_per_specialist_30d",
    "mv_specialist_brier_rolling",
    "mv_hot_hypotheses",
    "mv_trade_book_open",
    "mv_weakness_map",
]


# =============================================================================
# Apply
# =============================================================================

def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _view_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _index_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _extract_object_name(ddl: str, kind: str) -> str:
    """Pull `name` out of `CREATE [UNIQUE] {kind} IF NOT EXISTS <name> ...`."""
    cleaned = " ".join(ddl.split())
    upper = cleaned.upper()
    # Find the start of the name token by scanning past CREATE [UNIQUE] {kind}
    # [IF NOT EXISTS].
    tokens = cleaned.split()
    # Drop leading CREATE and any modifiers.
    i = 0
    if tokens[i].upper() == "CREATE":
        i += 1
    if tokens[i].upper() == "UNIQUE":
        i += 1
    # Skip the kind word (TABLE / INDEX / VIEW)
    i += 1
    # Skip IF NOT EXISTS
    if i + 2 < len(tokens) and tokens[i].upper() == "IF" and tokens[i+1].upper() == "NOT" and tokens[i+2].upper() == "EXISTS":
        i += 3
    name = tokens[i].strip("(").strip()
    return name


def apply_sota_schema(conn: sqlite3.Connection, dialect: str = "sqlite") -> dict[str, Any]:
    """Apply the SOTA schema to `conn`. Idempotent.

    Args:
        conn: an open `sqlite3.Connection` (for `dialect="sqlite"`) or a
              psycopg2 connection (`dialect="postgres"`, not yet wired).
        dialect: `"sqlite"` (default) or `"postgres"`. Only sqlite is
                 implemented; postgres pathway returns the raw SQL for the
                 caller to execute against its own driver.

    Returns:
        dict with keys `tables_created`, `tables_existed`, `indexes_created`,
        `views_created`, `errors`. Each list is sorted by object name.
    """
    if dialect == "postgres":
        return {
            "tables_created": [],
            "tables_existed": [],
            "indexes_created": [],
            "views_created": [],
            "errors": [],
            "_note": (
                "Postgres path not auto-applied here. Execute "
                "`SOTA_SCHEMA_PG_SQL` against the target Postgres connection."
            ),
        }
    if dialect != "sqlite":
        raise ValueError(f"unknown dialect: {dialect!r}")

    result = {
        "tables_created": [],
        "tables_existed": [],
        "indexes_created": [],
        "views_created": [],
        "errors": [],
    }

    # ---- tables ----------------------------------------------------------
    for ddl in _SOTA_TABLES_SQLITE:
        try:
            name = _extract_object_name(ddl, "TABLE")
            existed = _table_exists(conn, name)
            conn.execute(ddl)
            if existed:
                result["tables_existed"].append(name)
            else:
                result["tables_created"].append(name)
        except sqlite3.Error as exc:
            result["errors"].append({"kind": "table", "ddl": ddl[:120], "error": str(exc)})

    # ---- indexes ---------------------------------------------------------
    for ddl in _SOTA_INDEXES_SQLITE:
        try:
            name = _extract_object_name(ddl, "INDEX")
            existed = _index_exists(conn, name)
            conn.execute(ddl)
            if not existed:
                result["indexes_created"].append(name)
        except sqlite3.Error as exc:
            result["errors"].append({"kind": "index", "ddl": ddl[:120], "error": str(exc)})

    # ---- views -----------------------------------------------------------
    for ddl in _SOTA_VIEWS_SQLITE:
        try:
            name = _extract_object_name(ddl, "VIEW")
            existed = _view_exists(conn, name)
            conn.execute(ddl)
            if not existed:
                result["views_created"].append(name)
        except sqlite3.Error as exc:
            result["errors"].append({"kind": "view", "ddl": ddl[:120], "error": str(exc)})

    # Sort for deterministic output
    result["tables_created"].sort()
    result["tables_existed"].sort()
    result["indexes_created"].sort()
    result["views_created"].sort()

    return result


# =============================================================================
# __main__ smoke test
# =============================================================================

def _smoke_test() -> int:
    """End-to-end smoke test of the SOTA schema migration.

    Exits 0 on success, 1 on any check failure. Prints a structured report.
    """
    import json
    import os
    import tempfile
    from datetime import datetime, timedelta, timezone

    from .replay_helper import build_replay_context

    print("=" * 70)
    print("SOTA SCHEMA — PHASE 1 SMOKE TEST")
    print("=" * 70)

    tmpdir = tempfile.mkdtemp(prefix="sota_schema_test_")
    db_path = os.path.join(tmpdir, "sota_smoke.db")
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row

    # ---- 1. Fresh-DB apply ----------------------------------------------
    print("\n[1] Fresh DB apply...")
    r1 = apply_sota_schema(conn, dialect="sqlite")
    print(f"    tables_created : {len(r1['tables_created'])}  -> {r1['tables_created']}")
    print(f"    indexes_created: {len(r1['indexes_created'])}")
    print(f"    views_created  : {len(r1['views_created'])}  -> {r1['views_created']}")
    print(f"    errors         : {len(r1['errors'])}")
    if r1["errors"]:
        for e in r1["errors"]:
            print(f"      ! {e}")
        return 1
    if len(r1["tables_created"]) != len(SOTA_TABLE_NAMES):
        print(f"    FAIL: expected {len(SOTA_TABLE_NAMES)} tables, got {len(r1['tables_created'])}")
        return 1
    if len(r1["views_created"]) != len(SOTA_VIEW_NAMES):
        print(f"    FAIL: expected {len(SOTA_VIEW_NAMES)} views, got {len(r1['views_created'])}")
        return 1
    print("    [OK] all 10 tables + 16 indexes + 5 views landed cleanly")

    # ---- 2. Idempotency check -------------------------------------------
    print("\n[2] Re-apply (idempotency)...")
    r2 = apply_sota_schema(conn, dialect="sqlite")
    print(f"    tables_created : {len(r2['tables_created'])} (expect 0)")
    print(f"    tables_existed : {len(r2['tables_existed'])} (expect 10)")
    print(f"    indexes_created: {len(r2['indexes_created'])} (expect 0)")
    print(f"    views_created  : {len(r2['views_created'])} (expect 0)")
    print(f"    errors         : {len(r2['errors'])} (expect 0)")
    if r2["tables_created"] or r2["indexes_created"] or r2["views_created"] or r2["errors"]:
        print("    FAIL: idempotency broken")
        return 1
    print("    [OK] re-run produced zero changes and zero errors")

    # ---- 3. Insert sample rows ------------------------------------------
    print("\n[3] Insert sample rows into all 10 fact tables...")
    now = datetime.now(timezone.utc)
    one_hour_ago = (now - timedelta(hours=1)).isoformat()
    now_iso = now.isoformat()
    expires_at = (now + timedelta(days=1)).isoformat()

    inserts = [
        ("hypotheses",
         "INSERT INTO hypotheses (id, cycle_id, specialist_id, title, hypothesis_text, status, valid_from) VALUES (?, ?, ?, ?, ?, ?, ?)",
         ("hyp_smoke_1", "c_smoke", "macro_regime", "BTC > 100k by EOY", "Funding + flows", "active", one_hour_ago)),
        ("hypothesis_edges",
         "INSERT INTO hypothesis_edges (id, from_node_kind, from_node_id, to_node_kind, to_node_id, edge_kind, valid_from) VALUES (?, ?, ?, ?, ?, ?, ?)",
         ("hedge_smoke_1", "claim", "cl_x", "hypothesis", "hyp_smoke_1", "supports", one_hour_ago)),
        ("trade_ideas",
         "INSERT INTO trade_ideas (id, cycle_id, specialist_id, instrument, direction, sizing, entry, stop, time_horizon, edge_thesis, confidence, status, expires_at, valid_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
         ("ti_smoke_1", "c_smoke", "microstructure", "BTC-USD", "long", "{}", "{}", "{}", "1d", "edge", 0.72, "published", expires_at, one_hour_ago)),
        ("debates",
         "INSERT INTO debates (id, cycle_id, trigger_kind, trigger_id, participants, judge_model, judge_provider, status, due_at, valid_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
         ("deb_smoke_1", "c_smoke", "claim_conflict", "cl_x", '["macro","micro"]', "opus", "anthropic", "open", expires_at, one_hour_ago)),
        ("playbooks",
         "INSERT INTO playbooks (id, name, version, owner_specialist, description, trigger_spec, action_template, promoted_status, valid_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
         ("pb_smoke_1", "funding_squeeze", 1, "macro_regime", "Funding rate squeeze", "{}", "{}", "candidate", one_hour_ago)),
        ("specialist_states",
         "INSERT INTO specialist_states (id, specialist_id, persona_version, cycle_id, state_kind, state_json, valid_from) VALUES (?, ?, ?, ?, ?, ?, ?)",
         ("spst_smoke_1", "macro_regime", "v1", "c_smoke", "persona", "{}", one_hour_ago)),
        ("agent_messages",
         "INSERT INTO agent_messages (id, from_agent, to_agent_or_topic, message_kind, payload, valid_from) VALUES (?, ?, ?, ?, ?, ?)",
         ("msg_smoke_1", "macro_regime", "all", "scratchpad", "{}", one_hour_ago)),
        ("tool_atlas",
         "INSERT INTO tool_atlas (id, tool_uri, tool_name, version, kind, provider, callable_ref, schema_json, description, status, valid_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
         ("tool_smoke_1", "tic://tool/builtin/q@v1", "query_timeseries", "v1", "builtin", "tic", "tic.tools.q", "{}", "ts query", "active", one_hour_ago)),
        ("tool_call_log",
         "INSERT INTO tool_call_log (id, cycle_id, specialist_id, tool_uri, tool_version, args_hash, args_json, started_at, valid_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
         ("tc_smoke_1", "c_smoke", "macro_regime", "tic://tool/builtin/q@v1", "v1", "ah_x", "{}", one_hour_ago, one_hour_ago)),
        ("reward_log",
         "INSERT INTO reward_log (id, cycle_id, reward_kind, subject_kind, subject_id, score, valid_from) VALUES (?, ?, ?, ?, ?, ?, ?)",
         ("rew_smoke_1", "c_smoke", "correctness", "hypothesis", "hyp_smoke_1", 0.81, one_hour_ago)),
    ]
    for tname, sql, params in inserts:
        conn.execute(sql, params)
    counts = {tname: conn.execute(f"SELECT count(*) FROM {tname}").fetchone()[0] for tname, _, _ in inserts}
    print(f"    inserted rows: {counts}")
    if any(c != 1 for c in counts.values()):
        print("    FAIL: not all inserts succeeded")
        return 1
    print("    [OK] one row per table inserted")

    # ---- 4. Bitemporal replay sanity ------------------------------------
    print("\n[4] Bitemporal replay (build_replay_context)...")
    # Case A: valid-time = now-30m AND transaction-time = now+1s (i.e.,
    # "use our current knowledge to ask what was true at now-30m"). Our
    # row's valid_from=now-1h so it should be visible. We bump
    # as_of_transaction past `now` to clear the row's transaction_from.
    ctx_visible = build_replay_context(
        as_of_valid=now - timedelta(minutes=30),
        as_of_transaction=now + timedelta(minutes=1),
    )
    where, params = ctx_visible.where_clause("hypotheses")
    sql = f"SELECT id FROM hypotheses WHERE {where}"
    rows = conn.execute(sql, params).fetchall()
    print(f"    as_of_valid=now-30m, knowledge=now+1m -> {len(rows)} rows (expect 1)")
    if len(rows) != 1:
        print(f"    FAIL: should see 1 row, saw {len(rows)}")
        return 1

    # Case B: valid-time = 2h ago (before valid_from). Row should NOT be
    # visible regardless of knowledge axis — it wasn't "true in the world"
    # 2h ago. This is the canonical bitemporal-replay invariant.
    ctx_past = build_replay_context(
        as_of_valid=now - timedelta(hours=2),
        as_of_transaction=now + timedelta(minutes=1),
    )
    where2, params2 = ctx_past.where_clause("hypotheses")
    sql2 = f"SELECT id FROM hypotheses WHERE {where2}"
    rows2 = conn.execute(sql2, params2).fetchall()
    print(f"    as_of_valid=now-2h,  knowledge=now+1m -> {len(rows2)} rows (expect 0)")
    if len(rows2) != 0:
        print(f"    FAIL: should see 0 rows, saw {len(rows2)}")
        return 1

    # Case C: valid-time = now, but transaction-time = 2h ago (before we
    # had recorded the row). Should also be invisible — we didn't know.
    ctx_unknown = build_replay_context(
        as_of_valid=now,
        as_of_transaction=now - timedelta(hours=2),
    )
    where3, params3 = ctx_unknown.where_clause("hypotheses")
    sql3 = f"SELECT id FROM hypotheses WHERE {where3}"
    rows3 = conn.execute(sql3, params3).fetchall()
    print(f"    as_of_valid=now,     knowledge=now-2h -> {len(rows3)} rows (expect 0)")
    if len(rows3) != 0:
        print(f"    FAIL: should see 0 rows, saw {len(rows3)}")
        return 1
    print("    [OK] both axes (valid-time + transaction-time) honored")

    # ---- 5. Supersedes pattern ------------------------------------------
    print("\n[5] Supersedes pattern (correction row hides original)...")
    # Insert a corrected hypothesis row that supersedes hyp_smoke_1 AND set
    # the old row's transaction_to to now (the "close out" of the old fact).
    conn.execute(
        "UPDATE hypotheses SET transaction_to = ? WHERE id = ?",
        (now_iso, "hyp_smoke_1"),
    )
    conn.execute(
        "INSERT INTO hypotheses (id, cycle_id, specialist_id, title, hypothesis_text, status, supersedes, valid_from) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("hyp_smoke_2", "c_smoke", "macro_regime", "BTC > 100k by EOY (corrected)",
         "Updated thesis", "active", "hyp_smoke_1", now_iso),
    )
    # Default-view query: filter transaction_to IS NULL.
    open_rows = conn.execute(
        "SELECT id FROM hypotheses WHERE transaction_to IS NULL"
    ).fetchall()
    open_ids = [r[0] for r in open_rows]
    print(f"    open (transaction_to IS NULL): {open_ids} (expect ['hyp_smoke_2'])")
    if open_ids != ["hyp_smoke_2"]:
        print("    FAIL: supersedes pattern did not hide old row")
        return 1
    # The audit query (without the filter) still sees both rows.
    all_rows = conn.execute("SELECT id FROM hypotheses ORDER BY id").fetchall()
    all_ids = [r[0] for r in all_rows]
    print(f"    audit (no filter)            : {all_ids} (expect both)")
    if set(all_ids) != {"hyp_smoke_1", "hyp_smoke_2"}:
        print("    FAIL: audit history missing")
        return 1
    print("    [OK] supersedes hides old row from default view, preserves history for audit")

    # ---- Final stamp ----------------------------------------------------
    print()
    print("=" * 70)
    print("PHASE 1 SCHEMA — READY")
    print("=" * 70)
    print(f"  tables: {len(SOTA_TABLE_NAMES)}  views: {len(SOTA_VIEW_NAMES)}  "
          f"indexes: {len(_SOTA_INDEXES_SQLITE)}")
    print(f"  smoke db: {db_path}")
    print()
    conn.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_smoke_test())
