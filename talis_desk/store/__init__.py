"""DeskStore — talis-desk's own database (desk.db), separate from talis-tic.

The desk reads from talis-tic via the TICStore API (claims, events, ts,
source_health, semantic_index, the TOOLS registry). It writes its own
artifacts (hypotheses, trade_ideas, debates, playbooks, specialist_states,
agent_messages, tool_atlas, tool_call_log, reward_log) to desk.db.

Strict boundary: this module imports talis_tic ONLY for read access to the
data layer. talis_tic must never import from talis_desk.

See wiki/REPO_BOUNDARY.md for the full contract.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

from ..schema.sota import apply_sota_schema

DEFAULT_DESK_DB_PATH = Path.home() / ".talis" / "desk.db"


class DeskStore:
    """The desk's own database. 10 SOTA tables + 5 views.

    Separate from TICStore (`tic.db`). Reads from TIC happen through a
    TICStore handle the caller passes in; this class doesn't reach into
    the TIC database directly.
    """

    def __init__(self, db_path: Optional[Path] = None,
                 dialect: str = "sqlite"):
        self.db_path = Path(db_path) if db_path else DEFAULT_DESK_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.dialect = dialect
        self.conn = sqlite3.connect(str(self.db_path), isolation_level=None,
                                     check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.row_factory = sqlite3.Row
        # Apply the SOTA schema idempotently
        apply_sota_schema(self.conn, dialect=self.dialect)

    def close(self) -> None:
        self.conn.close()

    def reset(self) -> None:
        """For tests only. Wipe desk.db + re-apply schema."""
        self.close()
        if self.db_path.exists():
            self.db_path.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.db_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        self.__init__(db_path=self.db_path, dialect=self.dialect)


_STORE: Optional[DeskStore] = None


def get_desk_store(db_path: Optional[Path] = None) -> DeskStore:
    """Singleton DeskStore. Pass db_path on first call to override default."""
    global _STORE
    if _STORE is None:
        _STORE = DeskStore(db_path=db_path)
    return _STORE


# Alias for code ported from talis-tic where `get_store` was used.
# Inside talis-desk, `get_store()` returns the DESK database, not TIC's.
# Reads from TIC go through TICStore() which the caller imports explicitly.
get_store = get_desk_store


def reset_desk_store_for_test(db_path: Optional[Path] = None) -> DeskStore:
    """Force a fresh DeskStore (used in tests)."""
    global _STORE
    if _STORE is not None:
        _STORE.close()
    _STORE = DeskStore(db_path=db_path)
    _STORE.reset()
    return _STORE
