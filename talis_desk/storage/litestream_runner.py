"""Litestream config generator + daemon launcher.

Litestream (https://litestream.io) replicates SQLite's WAL to S3 in real
time. If the `litestream` binary is on PATH we generate a config and
launch the daemon; otherwise we fall back to a nightly `VACUUM INTO`
snapshot uploader so the production desk still has off-host durability.

Public surface:
  - `is_litestream_available()` -> bool
  - `generate_config(db_path, bucket, prefix) -> str` (YAML)
  - `start_replication(db_path)` -> subprocess.Popen | None
        Starts the litestream daemon. Returns None if binary missing.
  - `snapshot_to_s3(db_path, label='nightly') -> Optional[str]`
        VACUUM INTO + upload via Storage. Returns the canonical URL or
        None on failure.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .bucket_layout import (
    BUCKET,
    PREFIX_DESK_DB_CURRENT,
    desk_db_current_key,
    desk_db_snapshot_key,
)
from .storage import StorageError, get_storage


logger = logging.getLogger(__name__)


# ============================================================================
# Litestream binary detection
# ============================================================================

def is_litestream_available() -> bool:
    """True if the `litestream` binary is on PATH."""
    return shutil.which("litestream") is not None


def generate_config(
    db_path: str | Path,
    bucket: str = BUCKET,
    prefix: str = PREFIX_DESK_DB_CURRENT.rstrip("/"),
    region: str = "us-east-1",
) -> str:
    """Return a Litestream YAML config that replicates `db_path` to S3.

    Caller writes the returned string to a temp file and invokes
    `litestream replicate -config <path>`. The replication target is
    `s3://<bucket>/<prefix>/desk.db` (one WAL stream).
    """
    db_path = str(Path(db_path).expanduser().resolve())
    region = os.environ.get("AWS_REGION", region)
    return (
        "dbs:\n"
        f"  - path: {db_path}\n"
        "    replicas:\n"
        f"      - type: s3\n"
        f"        bucket: {bucket}\n"
        f"        path: {prefix.rstrip('/')}/desk.db\n"
        f"        region: {region}\n"
        "        sync-interval: 1s\n"
        "        retention: 168h\n"  # 7 days
        "        snapshot-interval: 6h\n"
    )


def start_replication(
    db_path: str | Path,
    config_dir: Optional[Path] = None,
) -> Optional[subprocess.Popen]:
    """Start the litestream daemon. Returns the Popen handle or None
    if litestream is not installed. Stdout/err are routed to a log file
    under `~/.talis/logs/litestream.log`.
    """
    if not is_litestream_available():
        logger.info(
            "Litestream binary not on PATH — skipping live WAL replication. "
            "Install via `brew install benbjohnson/litestream/litestream` "
            "(macOS) or follow https://litestream.io/install/."
        )
        return None
    db_path = Path(db_path).expanduser().resolve()
    if not db_path.exists():
        raise StorageError(f"db missing for litestream: {db_path}")
    cfg = generate_config(db_path)
    cfg_dir = config_dir or (Path.home() / ".talis" / "litestream")
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "litestream.yml"
    cfg_path.write_text(cfg)
    log_dir = Path.home() / ".talis" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "litestream.log"
    log_fp = open(log_path, "ab")
    proc = subprocess.Popen(
        ["litestream", "replicate", "-config", str(cfg_path)],
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    logger.info(
        "Litestream replication started: pid=%s log=%s", proc.pid, log_path,
    )
    return proc


# ============================================================================
# Snapshot fallback (no litestream)
# ============================================================================

def snapshot_to_s3(
    db_path: str | Path,
    label: str = "nightly",
) -> Optional[str]:
    """`VACUUM INTO` a temp file + upload via Storage. Returns the
    canonical URL on success, None on failure (logged).
    """
    db_path = Path(db_path).expanduser().resolve()
    if not db_path.exists():
        logger.warning("snapshot_to_s3: source missing: %s", db_path)
        return None
    storage = get_storage()
    date_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = desk_db_snapshot_key(date_utc, label)
    with tempfile.TemporaryDirectory(prefix="desk_snap_") as td:
        snap = Path(td) / "snap.db"
        # VACUUM INTO produces a defragmented copy in one step.
        try:
            conn = sqlite3.connect(str(db_path))
            conn.execute(f"VACUUM INTO '{snap.as_posix()}'")
            conn.close()
        except sqlite3.Error as e:
            logger.warning("snapshot_to_s3: VACUUM INTO failed: %s", e)
            return None
        try:
            storage.write_db_snapshot(key, snap)
        except StorageError as e:
            logger.warning("snapshot_to_s3: upload failed: %s", e)
            return None
    # Also stamp current/ pointer for the most recent nightly.
    try:
        storage.write_db_snapshot(desk_db_current_key(), db_path)
    except StorageError as e:
        logger.warning("snapshot_to_s3: current pointer write failed: %s", e)
    return storage._canonical_url(key)


def maybe_start_or_snapshot(db_path: str | Path) -> dict:
    """Convenience: start litestream if available, otherwise immediately
    snapshot. Returns a status dict for the caller's manifest.
    """
    out: dict = {"db_path": str(db_path), "mode": None, "url": None, "pid": None}
    if is_litestream_available():
        proc = start_replication(db_path)
        if proc is not None:
            out["mode"] = "litestream"
            out["pid"] = proc.pid
            return out
    # Fall through: snapshot-only.
    url = snapshot_to_s3(db_path, label="bootstrap")
    out["mode"] = "snapshot"
    out["url"] = url
    return out
