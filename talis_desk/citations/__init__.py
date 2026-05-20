"""Citation provenance graph — Pillar 4 of the v5 desk.

Every claim that surfaces in a hypothesis, report, or brief must root in a
`citations` row. The row points at:
  - `canonical_url` — the source URL the data came from
  - `content_hash` — sha256 of the archived bytes in S3
                       (`citations-archive/<hash>`)
  - `anchor` — DOM path / page+line / PDF page / paragraph hash
  - `quote_excerpt` — verbatim quote (for verifier ground truth)
  - `fetched_at` + `fetched_via_tool_uri` + `fetched_via_tool_call_id`

The Tier 1.5 verifier council uses `verify_citation` to walk the chain
back to the original source, re-fetch (or read from S3 archive), and
confirm the quote still appears.

Public API (also registered as tic.desk.tools):
  - `resolve_citation(citation_id) -> CitationRecord`
  - `fetch_source(citation_id, depth='excerpt'|'section'|'full') -> dict`
  - `verify_citation(citation_id, force_refetch=False) -> dict`
  - `archive_and_record(...)` — helper for ingesters
"""
from .api import (
    CitationRecord,
    archive_and_record,
    fetch_source,
    resolve_citation,
    verify_citation,
)
from .registry import register_citation_tools

__all__ = [
    "CitationRecord",
    "archive_and_record",
    "fetch_source",
    "resolve_citation",
    "verify_citation",
    "register_citation_tools",
]
