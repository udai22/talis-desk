"""Talis Desk unified storage substrate.

Single namespace (`s3://talis-desk/` in prod, `~/.talis/` in local dev) that
backs all artifacts: desk.db replication, brief markdown, wiki, citation
archive, agent run logs, source library, meta indexes.

Public entry points:
  - `get_storage()`         -> a Storage instance (cached, backend per env)
  - `Storage.read/write`    -> raw blob I/O on the canonical address space
  - `Storage.read_db`       -> stream a SQLite snapshot back into a local path
  - `Storage.write_db_snapshot(prefix, local_path)`
                            -> upload a VACUUMed snapshot
  - `Storage.archive_citation(bytes, metadata) -> s3_url`
                            -> content-hash addressed; idempotent
  - `Storage.fetch_citation(content_hash) -> bytes`
  - `Storage.public_url(prefix, expires_in)`
                            -> presigned (or `file://` for local backend)

Backend selection:
  - `TALIS_STORAGE_BACKEND=s3`    -> boto3 S3 backend (default if AWS creds present)
  - `TALIS_STORAGE_BACKEND=local` -> filesystem under `~/.talis/storage/`
  - default                         -> auto-detect: if boto3 + creds, use s3;
                                       otherwise fall back to local

Bucket layout is defined in `bucket_layout.py`. Litestream daemon lives in
`litestream_runner.py`.
"""
from __future__ import annotations

from .storage import (
    Storage,
    get_storage,
    StorageError,
    CitationMetadata,
)
from .bucket_layout import (
    BUCKET,
    PREFIX_DESK_DB,
    PREFIX_BRIEFS,
    PREFIX_WIKI,
    PREFIX_CITATIONS_ARCHIVE,
    PREFIX_SOURCE_LIBRARY,
    PREFIX_AGENT_RUNS,
    PREFIX_META,
    citation_object_key,
)

__all__ = [
    "Storage",
    "get_storage",
    "StorageError",
    "CitationMetadata",
    "BUCKET",
    "PREFIX_DESK_DB",
    "PREFIX_BRIEFS",
    "PREFIX_WIKI",
    "PREFIX_CITATIONS_ARCHIVE",
    "PREFIX_SOURCE_LIBRARY",
    "PREFIX_AGENT_RUNS",
    "PREFIX_META",
    "citation_object_key",
]
