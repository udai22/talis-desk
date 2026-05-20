"""Citation API — read/write the provenance graph.

All four entry points (resolve / fetch / verify / archive_and_record)
operate on the `citations` table introduced in migration 7. The archive
itself lives in the Storage substrate
(`talis_desk.storage.archive_citation`) so we have one canonical
namespace across local + S3.

NO STUBS. If the source is unreachable on verify_citation, we mark
`still_valid=False` and surface `quality_flag=['source_unreachable']` —
we never fabricate a verification.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from ..storage import CitationMetadata, get_storage
from ..store import get_desk_store


logger = logging.getLogger(__name__)


# ============================================================================
# Types
# ============================================================================

@dataclass
class CitationRecord:
    id: str
    canonical_url: str
    content_hash: str
    content_type: Optional[str]
    anchor: Optional[str]
    quote_excerpt: Optional[str]
    fetched_at: str
    fetched_via_tool_uri: Optional[str]
    fetched_via_tool_call_id: Optional[str]
    archive_s3_url: Optional[str]
    verified_at: Optional[str]
    verifier_agent_id: Optional[str]
    still_valid: Optional[bool]
    hash_changed: Optional[bool]
    quality_flags: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)


# ============================================================================
# Helpers
# ============================================================================

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_record(row: sqlite3.Row) -> CitationRecord:
    d = dict(row)
    try:
        flags = json.loads(d["quality_flags"]) if d.get("quality_flags") else []
        if not isinstance(flags, list):
            flags = []
    except Exception:
        flags = []
    try:
        payload = json.loads(d["payload"]) if d.get("payload") else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    return CitationRecord(
        id=d["id"],
        canonical_url=d["canonical_url"],
        content_hash=d["content_hash"],
        content_type=d.get("content_type"),
        anchor=d.get("anchor"),
        quote_excerpt=d.get("quote_excerpt"),
        fetched_at=d["fetched_at"],
        fetched_via_tool_uri=d.get("fetched_via_tool_uri"),
        fetched_via_tool_call_id=d.get("fetched_via_tool_call_id"),
        archive_s3_url=d.get("archive_s3_url"),
        verified_at=d.get("verified_at"),
        verifier_agent_id=d.get("verifier_agent_id"),
        still_valid=(
            bool(d["still_valid"]) if d.get("still_valid") is not None else None
        ),
        hash_changed=(
            bool(d["hash_changed"]) if d.get("hash_changed") is not None else None
        ),
        quality_flags=flags,
        payload=payload,
    )


# ============================================================================
# Public API
# ============================================================================

def resolve_citation(citation_id: str) -> Optional[CitationRecord]:
    """Return the `CitationRecord` for `citation_id`, or None if missing."""
    if not citation_id:
        return None
    conn = get_desk_store().conn
    row = conn.execute(
        "SELECT * FROM citations WHERE id = ? AND transaction_to IS NULL",
        (citation_id,),
    ).fetchone()
    return _row_to_record(row) if row else None


def fetch_source(
    citation_id: str, depth: str = "excerpt",
) -> dict[str, Any]:
    """Return the source document at the requested depth.

    `depth='excerpt'` -> quote_excerpt field only (cheapest path)
    `depth='section'` -> the surrounding paragraph from the S3 archive
    `depth='full'`    -> the full archived body bytes (decoded as text if
                          content_type starts with text/)

    Returns a dict with keys: citation_id, depth, content, content_type,
    canonical_url, content_hash, archive_s3_url.
    """
    rec = resolve_citation(citation_id)
    if rec is None:
        return {
            "citation_id": citation_id,
            "depth": depth,
            "error": "citation_not_found",
        }
    if depth == "excerpt":
        return {
            "citation_id": citation_id,
            "depth": "excerpt",
            "content": rec.quote_excerpt or "",
            "content_type": rec.content_type,
            "canonical_url": rec.canonical_url,
            "content_hash": rec.content_hash,
            "archive_s3_url": rec.archive_s3_url,
        }
    # section / full: pull from archive
    try:
        body = get_storage().fetch_citation(
            rec.content_hash, ext=_ext_for_content_type(rec.content_type),
        )
    except Exception as e:
        return {
            "citation_id": citation_id,
            "depth": depth,
            "error": f"archive_fetch_failed: {e}",
            "canonical_url": rec.canonical_url,
            "content_hash": rec.content_hash,
        }
    content_str: str
    if rec.content_type and rec.content_type.startswith(("text/", "application/json")):
        try:
            content_str = body.decode("utf-8", errors="replace")
        except Exception:
            content_str = body.decode("latin-1", errors="replace")
    else:
        content_str = f"<binary {len(body)} bytes content_type={rec.content_type}>"
    if depth == "section":
        # Best-effort: return the paragraph containing the quote excerpt.
        if rec.quote_excerpt:
            idx = content_str.find(rec.quote_excerpt)
            if idx >= 0:
                # ~1000 chars surrounding window.
                start = max(0, idx - 400)
                end = min(len(content_str), idx + len(rec.quote_excerpt) + 400)
                content_str = content_str[start:end]
    return {
        "citation_id": citation_id,
        "depth": depth,
        "content": content_str,
        "content_type": rec.content_type,
        "canonical_url": rec.canonical_url,
        "content_hash": rec.content_hash,
        "archive_s3_url": rec.archive_s3_url,
    }


def verify_citation(
    citation_id: str,
    force_refetch: bool = False,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    """Walk the citation back to its canonical URL and confirm validity.

    Steps:
      1. Resolve the citation row
      2. (If `force_refetch` or never verified) refetch the URL
      3. Recompute content_hash; flag `hash_changed=True` if it differs
      4. If quote_excerpt set, verify the quote still appears verbatim
         in the refetched body. If not -> `still_valid=False`,
         quality_flag=['quote_no_longer_appears'].
      5. Stamp `verified_at`. Update the row (bitemporal supersede).

    Returns a dict: still_valid, hash_changed, new_content_hash,
    quality_flags, error.
    """
    rec = resolve_citation(citation_id)
    if rec is None:
        return {
            "citation_id": citation_id,
            "still_valid": False,
            "error": "citation_not_found",
            "quality_flags": ["citation_not_found"],
        }
    # If we already verified recently and not forcing, return cached result.
    if rec.verified_at and not force_refetch:
        return {
            "citation_id": citation_id,
            "still_valid": rec.still_valid,
            "hash_changed": rec.hash_changed,
            "new_content_hash": rec.content_hash,
            "quality_flags": rec.quality_flags,
            "cached": True,
            "verified_at": rec.verified_at,
        }

    flags: list[str] = []
    body: Optional[bytes] = None
    err: Optional[str] = None
    try:
        req = urllib.request.Request(
            rec.canonical_url,
            headers={"User-Agent": "TalisDeskVerifier/5.0 (research)"},
        )
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        err = f"http_{e.code}"
        flags.append("source_unreachable")
    except urllib.error.URLError as e:
        err = f"url_error: {e.reason}"
        flags.append("source_unreachable")
    except Exception as e:  # network / TLS / timeout
        err = f"{type(e).__name__}: {e}"
        flags.append("source_unreachable")

    if body is None:
        _update_verification(
            citation_id, still_valid=False, hash_changed=None,
            new_hash=rec.content_hash, flags=flags,
        )
        return {
            "citation_id": citation_id,
            "still_valid": False,
            "hash_changed": None,
            "new_content_hash": rec.content_hash,
            "quality_flags": flags,
            "error": err,
        }

    new_hash = hashlib.sha256(body).hexdigest()
    hash_changed = new_hash != rec.content_hash
    if hash_changed:
        flags.append("source_hash_changed")
    still_valid = True
    if rec.quote_excerpt:
        try:
            text_body = body.decode("utf-8", errors="replace")
        except Exception:
            text_body = body.decode("latin-1", errors="replace")
        if rec.quote_excerpt not in text_body:
            still_valid = False
            flags.append("quote_no_longer_appears")
    _update_verification(
        citation_id,
        still_valid=still_valid,
        hash_changed=hash_changed,
        new_hash=new_hash,
        flags=flags,
    )
    return {
        "citation_id": citation_id,
        "still_valid": still_valid,
        "hash_changed": hash_changed,
        "new_content_hash": new_hash,
        "quality_flags": flags,
        "verified_at": _utc_now_iso(),
        "cached": False,
    }


def archive_and_record(
    canonical_url: str,
    content_bytes: bytes,
    quote_excerpt: Optional[str] = None,
    anchor: Optional[str] = None,
    content_type: str = "text/html",
    fetched_via_tool_uri: Optional[str] = None,
    fetched_via_tool_call_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> CitationRecord:
    """Archive `content_bytes` to S3 + insert a citations row.

    This is the canonical entry point ingesters use. Returns the new
    `CitationRecord` (or the existing one if the content_hash + anchor +
    URL already match an active row — idempotent).
    """
    if not canonical_url:
        raise ValueError("archive_and_record: canonical_url required")
    if not isinstance(content_bytes, (bytes, bytearray)):
        raise ValueError("archive_and_record: content_bytes must be bytes")
    storage = get_storage()
    ext = _ext_for_content_type(content_type)
    meta = CitationMetadata(
        canonical_url=canonical_url,
        content_type=content_type,
        ext=ext,
        fetched_at=_utc_now_iso(),
        anchor=anchor or "",
    )
    content_hash, archive_url = storage.archive_citation(bytes(content_bytes), meta)

    conn = get_desk_store().conn
    # Idempotency: same URL + hash + anchor + tx_to NULL -> reuse.
    existing = conn.execute(
        "SELECT * FROM citations WHERE canonical_url = ? AND content_hash = ? "
        "AND COALESCE(anchor, '') = COALESCE(?, '') AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (canonical_url, content_hash, anchor),
    ).fetchone()
    if existing is not None:
        return _row_to_record(existing)

    cid = f"ct_{uuid4().hex[:12]}"
    now = _utc_now_iso()
    conn.execute(
        "INSERT INTO citations "
        "(id, canonical_url, content_hash, content_type, anchor, "
        " quote_excerpt, fetched_at, fetched_via_tool_uri, "
        " fetched_via_tool_call_id, archive_s3_url, "
        " quality_flags, payload, valid_from, transaction_from) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            cid,
            canonical_url,
            content_hash,
            content_type,
            anchor,
            quote_excerpt,
            now,
            fetched_via_tool_uri,
            fetched_via_tool_call_id,
            archive_url,
            json.dumps([]),
            json.dumps(payload or {}),
            now,
            now,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM citations WHERE id = ?", (cid,),
    ).fetchone()
    return _row_to_record(row)


# ============================================================================
# Internal helpers
# ============================================================================

def _update_verification(
    citation_id: str,
    still_valid: bool,
    hash_changed: Optional[bool],
    new_hash: str,
    flags: list[str],
) -> None:
    conn = get_desk_store().conn
    conn.execute(
        "UPDATE citations SET verified_at = ?, still_valid = ?, "
        "hash_changed = ?, quality_flags = ? WHERE id = ?",
        (
            _utc_now_iso(),
            1 if still_valid else 0,
            None if hash_changed is None else (1 if hash_changed else 0),
            json.dumps(sorted(set(flags))),
            citation_id,
        ),
    )
    conn.commit()


def _ext_for_content_type(content_type: Optional[str]) -> str:
    if not content_type:
        return "bin"
    ct = content_type.lower().split(";", 1)[0].strip()
    if ct == "text/html":
        return "html"
    if ct in ("application/json", "text/json"):
        return "json"
    if ct == "application/pdf":
        return "pdf"
    if ct.startswith("text/"):
        return "txt"
    if ct.startswith("application/xml") or ct == "text/xml":
        return "xml"
    return "bin"
