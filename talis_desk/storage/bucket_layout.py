"""Canonical S3 prefix discipline for `s3://talis-desk/`.

This module is the single source of truth for keying objects so callers
can't accidentally collide namespaces. Per REFLECTION_AND_REFACTOR_v5.md §3:

  s3://talis-desk/
    desk-db/                 -> Litestream WAL + nightly VACUUM snapshots
    briefs/                  -> daily brief markdown + manifest JSON
    wiki/                    -> auto-organized markdown wiki (per-cycle rewrite)
    citations-archive/       -> content-hash addressed citation bodies
    source-library/          -> curated framework / paper PDFs (Agent P)
    agent-runs/              -> per-cycle agent run logs (one tarball per cycle)
    meta/                    -> indexes, theme rollups, coverage maps,
                                topology renders, mutator outputs

Object naming conventions:
  - desk-db/current/desk.db                 (live WAL replication target)
  - desk-db/snapshots/<date_utc>/desk.db    (nightly VACUUM INTO)
  - briefs/<YYYY-MM-DD>/<cycle_id>.md
  - wiki/<YYYY-MM-DD>/<cycle_id>/<path>.md
  - citations-archive/<sha256_hex[:2]>/<sha256_hex>.<ext>
                                            (content-addressed; idempotent)
  - source-library/<slug>/<filename>
  - agent-runs/<YYYY-MM-DD>/<cycle_id>/<tier>/<agent_id>.jsonl
  - meta/themes/<YYYY-MM-DD>.json
  - meta/coverage/<YYYY-MM-DD>.json
  - meta/topology/<YYYY-MM-DD>/<view>.{svg,json}
"""
from __future__ import annotations

import os
from typing import Optional


# Canonical bucket name. Override via env for staging / shared dev buckets.
BUCKET: str = os.environ.get("TALIS_S3_BUCKET", "talis-desk")

# Top-level prefixes. Add a trailing slash for clarity.
PREFIX_DESK_DB = "desk-db/"
PREFIX_BRIEFS = "briefs/"
PREFIX_WIKI = "wiki/"
PREFIX_CITATIONS_ARCHIVE = "citations-archive/"
PREFIX_SOURCE_LIBRARY = "source-library/"
PREFIX_AGENT_RUNS = "agent-runs/"
PREFIX_META = "meta/"

# Sub-prefixes within desk-db/
PREFIX_DESK_DB_CURRENT = PREFIX_DESK_DB + "current/"
PREFIX_DESK_DB_SNAPSHOTS = PREFIX_DESK_DB + "snapshots/"


def citation_object_key(
    content_hash: str,
    ext: str = "html",
) -> str:
    """Content-addressed key for a citation archive object.

    Splits on the first two hex chars so we don't end up with 100k+ objects
    under a single prefix (S3 prefix listing perf cliff).
    """
    if not content_hash:
        raise ValueError("content_hash required")
    content_hash = content_hash.lower().strip()
    if len(content_hash) < 4:
        raise ValueError(f"content_hash too short: {content_hash!r}")
    if ext and not ext.startswith("."):
        ext = "." + ext
    return f"{PREFIX_CITATIONS_ARCHIVE}{content_hash[:2]}/{content_hash}{ext}"


def brief_object_key(date_utc: str, cycle_id: str) -> str:
    """Daily brief markdown key — `briefs/YYYY-MM-DD/<cycle_id>.md`."""
    if not date_utc or not cycle_id:
        raise ValueError("date_utc + cycle_id required")
    safe_cycle = cycle_id.replace("/", "_")
    return f"{PREFIX_BRIEFS}{date_utc}/{safe_cycle}.md"


def wiki_object_key(date_utc: str, cycle_id: str, path: str) -> str:
    """Wiki page key — `wiki/YYYY-MM-DD/<cycle_id>/<path>`."""
    if not date_utc or not cycle_id or not path:
        raise ValueError("date_utc + cycle_id + path required")
    safe_cycle = cycle_id.replace("/", "_")
    safe_path = path.lstrip("/")
    return f"{PREFIX_WIKI}{date_utc}/{safe_cycle}/{safe_path}"


def agent_run_object_key(
    date_utc: str, cycle_id: str, tier: str, agent_id: str,
) -> str:
    """Agent-run log key — `agent-runs/YYYY-MM-DD/<cycle>/<tier>/<agent>.jsonl`."""
    if not all([date_utc, cycle_id, tier, agent_id]):
        raise ValueError("date_utc + cycle_id + tier + agent_id required")
    safe_cycle = cycle_id.replace("/", "_")
    safe_agent = agent_id.replace("/", "_")
    return (
        f"{PREFIX_AGENT_RUNS}{date_utc}/{safe_cycle}/{tier}/{safe_agent}.jsonl"
    )


def meta_object_key(category: str, date_utc: str, filename: str) -> str:
    """Meta index key — `meta/<category>/<YYYY-MM-DD>/<filename>`."""
    if not category or not date_utc or not filename:
        raise ValueError("category + date_utc + filename required")
    return f"{PREFIX_META}{category}/{date_utc}/{filename}"


def desk_db_snapshot_key(date_utc: str, label: Optional[str] = None) -> str:
    """Nightly snapshot key — `desk-db/snapshots/YYYY-MM-DD/desk.db[.label]`."""
    if not date_utc:
        raise ValueError("date_utc required")
    suffix = f".{label}" if label else ""
    return f"{PREFIX_DESK_DB_SNAPSHOTS}{date_utc}/desk{suffix}.db"


def desk_db_current_key() -> str:
    """Live Litestream replication target key."""
    return f"{PREFIX_DESK_DB_CURRENT}desk.db"
