"""Storage backend implementation — S3 (boto3) + local-filesystem fallback.

The public surface is a single `Storage` class with these methods:
  - `read(prefix) -> bytes`
  - `write(prefix, content)`
  - `read_db(prefix, local_path)`
  - `write_db_snapshot(prefix, local_path)`
  - `archive_citation(content_bytes, metadata) -> str`
        (returns canonical URL: `s3://...` or `file://...`)
  - `fetch_citation(content_hash) -> bytes`
  - `public_url(prefix, expires_in)`

Backend selection (deterministic):
  - env `TALIS_STORAGE_BACKEND=s3`     -> force S3 (raises if boto3 missing)
  - env `TALIS_STORAGE_BACKEND=local`  -> force local fs (no AWS contact)
  - unset                                -> try S3 if boto3 import + creds OK,
                                            else fall back to local quietly

Local mode mirrors the S3 prefix discipline under `$TALIS_STORAGE_ROOT`
(default `~/.talis/storage/`). All callers see the same logical address
space — only the canonical URL prefix differs (`s3://` vs `file://`).
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .bucket_layout import (
    BUCKET,
    citation_object_key,
)


logger = logging.getLogger(__name__)


class StorageError(RuntimeError):
    """Raised on any storage backend failure (auth, network, missing key)."""


@dataclass
class CitationMetadata:
    """Optional metadata attached to a citation archive object.

    The content_hash is computed by `archive_citation` from the bytes; the
    caller doesn't pre-compute it. canonical_url is the original source URL
    (e.g. https://sec.gov/...); ext picks the object suffix.
    """
    canonical_url: str = ""
    content_type: str = "text/html"
    ext: str = "html"
    fetched_at: str = ""
    anchor: str = ""
    notes: str = ""
    extra: dict[str, str] = field(default_factory=dict)


# ============================================================================
# Local backend
# ============================================================================

class _LocalBackend:
    """Filesystem mirror of the S3 prefix discipline.

    Root: `$TALIS_STORAGE_ROOT` or `~/.talis/storage/`. Object keys map 1:1
    to relative paths under the root. Citation archive remains
    content-addressed.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        if root is None:
            env_root = os.environ.get("TALIS_STORAGE_ROOT")
            root = Path(env_root) if env_root else (Path.home() / ".talis" / "storage")
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _abs(self, prefix: str) -> Path:
        # Strip any leading slash so prefixes act as relative keys.
        rel = prefix.lstrip("/")
        p = (self.root / rel).resolve()
        # Path traversal guard: refuse anything that escapes the root.
        if not str(p).startswith(str(self.root)):
            raise StorageError(f"refusing path-traversal write: {prefix!r}")
        return p

    def read(self, prefix: str) -> bytes:
        p = self._abs(prefix)
        if not p.exists():
            raise StorageError(f"key not found: {prefix!r}")
        return p.read_bytes()

    def write(self, prefix: str, content: bytes) -> None:
        p = self._abs(prefix)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write via .tmp + rename.
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(content)
        tmp.replace(p)

    def exists(self, prefix: str) -> bool:
        return self._abs(prefix).exists()

    def url(self, prefix: str) -> str:
        return f"file://{self._abs(prefix)}"


# ============================================================================
# S3 backend (boto3)
# ============================================================================

class _S3Backend:
    def __init__(self, bucket: str) -> None:
        try:
            import boto3  # type: ignore
            from botocore.exceptions import (  # type: ignore  # noqa: F401
                BotoCoreError, ClientError,
            )
        except ImportError as e:
            raise StorageError(f"boto3 not available: {e}") from e
        self._boto3 = boto3
        self._ClientError = ClientError
        self._BotoCoreError = BotoCoreError
        self.bucket = bucket
        try:
            self.client = boto3.client("s3")
        except Exception as e:
            raise StorageError(f"failed to construct S3 client: {e}") from e

    def read(self, prefix: str) -> bytes:
        key = prefix.lstrip("/")
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
        except self._ClientError as e:
            raise StorageError(f"S3 get {key!r} failed: {e}") from e
        return resp["Body"].read()

    def write(self, prefix: str, content: bytes) -> None:
        key = prefix.lstrip("/")
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=content)
        except (self._ClientError, self._BotoCoreError) as e:
            raise StorageError(f"S3 put {key!r} failed: {e}") from e

    def exists(self, prefix: str) -> bool:
        key = prefix.lstrip("/")
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except self._ClientError:
            return False

    def url(self, prefix: str, expires_in: int = 3600) -> str:
        key = prefix.lstrip("/")
        try:
            return self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except (self._ClientError, self._BotoCoreError) as e:
            raise StorageError(f"S3 presign {key!r} failed: {e}") from e

    def canonical_url(self, prefix: str) -> str:
        key = prefix.lstrip("/")
        return f"s3://{self.bucket}/{key}"


# ============================================================================
# Storage facade
# ============================================================================

class Storage:
    def __init__(self, backend: Any, bucket: Optional[str] = None) -> None:
        self._backend = backend
        self.bucket = bucket
        self.is_s3 = isinstance(backend, _S3Backend)
        self.is_local = isinstance(backend, _LocalBackend)

    # ---- raw I/O ----------------------------------------------------------
    def read(self, prefix: str) -> bytes:
        return self._backend.read(prefix)

    def write(self, prefix: str, content: bytes | str) -> None:
        if isinstance(content, str):
            content = content.encode("utf-8")
        self._backend.write(prefix, content)

    def exists(self, prefix: str) -> bool:
        return self._backend.exists(prefix)

    # ---- DB snapshots -----------------------------------------------------
    def read_db(self, prefix: str, local_path: str | Path) -> Path:
        """Download a SQLite snapshot from `prefix` into `local_path`."""
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        data = self._backend.read(prefix)
        local_path.write_bytes(data)
        return local_path

    def write_db_snapshot(self, prefix: str, local_path: str | Path) -> None:
        """Upload a SQLite snapshot from `local_path` to `prefix`."""
        local_path = Path(local_path)
        if not local_path.exists():
            raise StorageError(f"snapshot path missing: {local_path}")
        self._backend.write(prefix, local_path.read_bytes())

    # ---- Citation archive (content-addressed, idempotent) -----------------
    def archive_citation(
        self,
        content_bytes: bytes,
        metadata: Optional[CitationMetadata] = None,
    ) -> tuple[str, str]:
        """Write a citation body to the archive. Returns (content_hash, canonical_url).

        Content is hashed with sha256. If an object with the same hash
        already exists, we skip the write (idempotent). The canonical URL
        is `s3://talis-desk/...` for S3 or `file://...` for local.
        """
        if not isinstance(content_bytes, (bytes, bytearray)):
            raise StorageError(
                f"archive_citation requires bytes, got {type(content_bytes)}"
            )
        meta = metadata or CitationMetadata()
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        key = citation_object_key(content_hash, ext=meta.ext or "html")
        if not self._backend.exists(key):
            self._backend.write(key, bytes(content_bytes))
            # Sidecar metadata (small JSON) sits next to the blob.
            try:
                import json
                sidecar = {
                    "content_hash": content_hash,
                    "canonical_url": meta.canonical_url,
                    "content_type": meta.content_type,
                    "fetched_at": meta.fetched_at,
                    "anchor": meta.anchor,
                    "notes": meta.notes,
                    **(meta.extra or {}),
                }
                self._backend.write(key + ".meta.json", json.dumps(sidecar).encode("utf-8"))
            except Exception as e:
                # Sidecar is best-effort; don't fail the archive call.
                logger.warning("archive_citation: sidecar write failed: %s", e)
        canonical = self._canonical_url(key)
        return content_hash, canonical

    def fetch_citation(self, content_hash: str, ext: str = "html") -> bytes:
        """Fetch a citation body by its content_hash."""
        key = citation_object_key(content_hash, ext=ext)
        return self._backend.read(key)

    # ---- URL helpers ------------------------------------------------------
    def public_url(self, prefix: str, expires_in: int = 3600) -> str:
        if self.is_s3:
            return self._backend.url(prefix, expires_in=expires_in)
        return self._backend.url(prefix)

    def _canonical_url(self, prefix: str) -> str:
        if self.is_s3:
            return self._backend.canonical_url(prefix)
        return self._backend.url(prefix)


# ============================================================================
# Module-level singleton
# ============================================================================

_LOCK = threading.Lock()
_STORAGE: Optional[Storage] = None


def get_storage() -> Storage:
    """Return a process-cached Storage instance, chosen per env."""
    global _STORAGE
    if _STORAGE is not None:
        return _STORAGE
    with _LOCK:
        if _STORAGE is not None:
            return _STORAGE
        backend_pref = (os.environ.get("TALIS_STORAGE_BACKEND") or "").lower().strip()
        if backend_pref == "local":
            _STORAGE = Storage(_LocalBackend(), bucket=None)
            return _STORAGE
        if backend_pref == "s3":
            backend = _S3Backend(BUCKET)
            _STORAGE = Storage(backend, bucket=BUCKET)
            return _STORAGE
        # Auto: try S3 first; fall back to local quietly.
        try:
            backend = _S3Backend(BUCKET)
            # Probe credentials with a cheap call.
            backend.client.list_buckets()
            _STORAGE = Storage(backend, bucket=BUCKET)
            logger.info("Storage: using S3 backend (bucket=%s)", BUCKET)
        except Exception as e:
            logger.info(
                "Storage: S3 backend unavailable (%s) — falling back to local",
                type(e).__name__,
            )
            _STORAGE = Storage(_LocalBackend(), bucket=None)
        return _STORAGE


def reset_storage_for_tests() -> None:
    """Test hook: clear the cached singleton."""
    global _STORAGE
    with _LOCK:
        _STORAGE = None
