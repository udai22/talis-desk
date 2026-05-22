"""Monitor must never mix DB/manifest/brief artifacts across cycles.

The old `/api/state` would happily glue:
  desk_db   from cycle A (pinned via TALIS_DESK_DB)
  manifest  from cycle A (matched on desk_db)
  brief     from cycle B (newest in /tmp at glob time)

That ships a UI showing cycle-A specialist rows under a cycle-B headline,
which is worse than rendering nothing. These tests pin the contract:

  * a pinned DB only pairs with a manifest that names it
  * a brief only comes from `manifest.brief_path`
  * `/api/state` exposes an `artifact_coherence` block so callers can
    distinguish "ok" from "missing_brief" from "missing_manifest"
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest


def _make_cycle(
    tmp_path: Path, cycle_id: str, *, with_brief: bool = True
) -> tuple[Path, Path, Path | None]:
    """Create db + manifest (+ brief) for a single cycle. Brief is optional."""
    db = tmp_path / f"desk_{cycle_id}.db"
    # Create a valid (empty) SQLite file so _open_ro doesn't crash if the
    # end-to-end tests exercise the panel-building path. The DB has no
    # tables, but every panel query is wrapped in _safe and returns a
    # default.
    conn = sqlite3.connect(str(db))
    conn.close()

    brief: Path | None = None
    if with_brief:
        brief = tmp_path / f"brief_{cycle_id}.md"
        brief.write_text(f"# brief {cycle_id}\n")

    manifest = tmp_path / f"manifest_{cycle_id}.json"
    manifest.write_text(
        json.dumps(
            {
                "cycle_id": cycle_id,
                "desk_db": str(db),
                # brief_path may point at a file that does NOT exist —
                # that is exactly the missing_brief case.
                "brief_path": str(brief) if brief else str(tmp_path / "missing.md"),
            }
        )
    )
    return db, manifest, brief


def _install_fake_glob(
    monkeypatch: pytest.MonkeyPatch,
    *,
    manifest_paths: list[Path],
    brief_paths: list[Path],
) -> None:
    """Force the monitor's glob() to see only our temp artifacts."""
    from talis_desk.monitor import server as srv

    manifest_strs = [str(p) for p in manifest_paths]
    brief_strs = [str(p) for p in brief_paths]

    def fake_glob(pattern: str) -> list[str]:
        if "manifest" in pattern:
            return list(manifest_strs)
        if "brief" in pattern:
            return list(brief_strs)
        # desk DB discovery globs — we always pin via TALIS_DESK_DB
        return []

    monkeypatch.setattr(srv.glob, "glob", fake_glob)


def _bump_mtime(path: Path, delta_seconds: float) -> None:
    """Push a file's mtime forward so it looks newer than its siblings."""
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + delta_seconds))


# ---------------------------------------------------------------------------
# _find_latest_manifest — must require DB match when DB is pinned
# ---------------------------------------------------------------------------

def test_manifest_must_match_pinned_db_even_when_older(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A newer unrelated manifest must NOT win over an older matching one."""
    from talis_desk.monitor import server as srv

    db_a, m_a, _ = _make_cycle(tmp_path, "a")
    db_b, m_b, _ = _make_cycle(tmp_path, "b")
    _bump_mtime(m_b, 600)  # manifest B is much newer

    _install_fake_glob(monkeypatch, manifest_paths=[m_a, m_b], brief_paths=[])

    chosen = srv._find_latest_manifest(db_a)
    assert chosen == m_a, "must select the manifest that names db_a, not the newest"


def test_manifest_none_when_no_match_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If no manifest names the pinned DB, return None (do not fall back)."""
    from talis_desk.monitor import server as srv

    db_a = tmp_path / "desk_a.db"
    db_a.write_bytes(b"")
    _, m_b, _ = _make_cycle(tmp_path, "b")  # only B exists, names db_b

    _install_fake_glob(monkeypatch, manifest_paths=[m_b], brief_paths=[])

    assert srv._find_latest_manifest(db_a) is None


# ---------------------------------------------------------------------------
# _find_latest_brief — must come from the manifest, never from a newer glob
# ---------------------------------------------------------------------------

def test_brief_comes_from_manifest_not_from_newest_glob(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The historical bug: newest /tmp brief was winning over manifest brief."""
    from talis_desk.monitor import server as srv

    _, _, brief_a = _make_cycle(tmp_path, "a")
    _, _, brief_b = _make_cycle(tmp_path, "b")
    assert brief_a is not None and brief_b is not None
    _bump_mtime(brief_b, 600)  # brief B is much newer in /tmp

    # Both briefs are visible to glob, but the manifest names brief_a.
    _install_fake_glob(
        monkeypatch, manifest_paths=[], brief_paths=[brief_a, brief_b]
    )

    manifest = {"cycle_id": "a", "brief_path": str(brief_a)}
    chosen = srv._find_latest_brief(manifest)
    assert chosen == brief_a, (
        "brief must follow the manifest pointer, "
        "never get hijacked by a newer file in /tmp"
    )


def test_brief_none_when_manifest_brief_missing_on_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """manifest.brief_path that doesn't exist must NOT fall back to a glob."""
    from talis_desk.monitor import server as srv

    _, _, brief_other = _make_cycle(tmp_path, "other")
    assert brief_other is not None  # still on disk, but for a different cycle

    _install_fake_glob(monkeypatch, manifest_paths=[], brief_paths=[brief_other])

    manifest = {
        "cycle_id": "a",
        "brief_path": str(tmp_path / "missing_brief.md"),  # never created
    }
    assert srv._find_latest_brief(manifest) is None


def test_brief_none_when_no_manifest_at_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without a manifest there is no cycle to pair against — return None."""
    from talis_desk.monitor import server as srv

    _, _, brief_a = _make_cycle(tmp_path, "a")
    assert brief_a is not None
    _install_fake_glob(monkeypatch, manifest_paths=[], brief_paths=[brief_a])

    assert srv._find_latest_brief(None) is None
    assert srv._find_latest_brief({}) is None


# ---------------------------------------------------------------------------
# _compute_artifact_coherence diagnostic
# ---------------------------------------------------------------------------

def test_coherence_ok_when_all_three_match(tmp_path: Path) -> None:
    from talis_desk.monitor import server as srv

    db, manifest_path, brief = _make_cycle(tmp_path, "ok")
    manifest = json.loads(manifest_path.read_text())
    coh = srv._compute_artifact_coherence(db, manifest_path, manifest, brief)
    assert coh["status"] == "ok"
    assert coh["cycle_id"] == "ok"
    assert coh["desk_db"] == str(db)
    assert coh["manifest_path"] == str(manifest_path)
    assert coh["brief_path"] == str(brief)
    assert coh["reason"] is None


def test_coherence_no_db_when_db_path_missing(tmp_path: Path) -> None:
    from talis_desk.monitor import server as srv

    coh = srv._compute_artifact_coherence(None, None, None, None)
    assert coh["status"] == "no_db"
    assert coh["cycle_id"] is None
    assert coh["desk_db"] is None


def test_coherence_missing_manifest_when_db_unmatched(tmp_path: Path) -> None:
    from talis_desk.monitor import server as srv

    db = tmp_path / "desk_pinned.db"
    db.write_bytes(b"")
    coh = srv._compute_artifact_coherence(db, None, {}, None)
    assert coh["status"] == "missing_manifest"
    assert coh["desk_db"] == str(db)
    assert coh["manifest_path"] is None
    assert coh["brief_path"] is None
    assert "no manifest matches" in (coh["reason"] or "")


def test_coherence_missing_brief_when_manifest_brief_absent(
    tmp_path: Path,
) -> None:
    from talis_desk.monitor import server as srv

    db, manifest_path, _ = _make_cycle(tmp_path, "nobrief", with_brief=False)
    manifest = json.loads(manifest_path.read_text())
    coh = srv._compute_artifact_coherence(db, manifest_path, manifest, None)
    assert coh["status"] == "missing_brief"
    assert coh["cycle_id"] == "nobrief"
    assert coh["manifest_path"] == str(manifest_path)
    assert coh["brief_path"] is None
    assert "not substituting" in (coh["reason"] or "")


# ---------------------------------------------------------------------------
# End-to-end: get_state surfaces coherence and refuses to mix cycles
# ---------------------------------------------------------------------------

def test_get_state_refuses_to_pair_old_db_with_newer_unrelated_brief(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exact production failure mode — reproduced and pinned shut.

    - TALIS_DESK_DB pins cycle A's db.
    - Cycle A's manifest + brief both exist.
    - A newer cycle B brief sits in /tmp.

    Before the fix, /api/state would attach cycle B's brief to cycle A's
    DB. After the fix, brief_path matches cycle A's manifest exactly.
    """
    from talis_desk.monitor import server as srv

    db_a, m_a, brief_a = _make_cycle(tmp_path, "a")
    _, _, brief_b = _make_cycle(tmp_path, "b")
    assert brief_a is not None and brief_b is not None
    _bump_mtime(brief_b, 600)  # B looks newer to any "pick newest" code path

    _install_fake_glob(
        monkeypatch,
        manifest_paths=[m_a],
        brief_paths=[brief_a, brief_b],
    )
    monkeypatch.setenv("TALIS_DESK_DB", str(db_a))

    state = srv.get_state()

    # We do NOT assert status == "ok" because the temp DB is an empty file
    # (panel queries will short-circuit via _safe). What we DO pin is the
    # artifact resolution.
    assert state["desk_db"] == str(db_a)
    assert state["manifest_path"] == str(m_a)
    assert state["brief_path"] == str(brief_a), (
        "brief must follow manifest pointer, not get hijacked by a newer "
        "unrelated brief"
    )
    assert state["cycle_id"] == "a"
    coh = state["artifact_coherence"]
    assert coh["status"] in {"ok", "missing_brief"}  # ok if brief_a survives
    assert coh["status"] == "ok"
    assert coh["cycle_id"] == "a"


def test_get_state_reports_missing_manifest_instead_of_mixing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pinned DB with no matching manifest → status reflects that, brief is None."""
    from talis_desk.monitor import server as srv

    db_a = tmp_path / "desk_a.db"
    db_a.write_bytes(b"")
    _, m_b, brief_b = _make_cycle(tmp_path, "b")  # only cycle B's artifacts exist
    assert brief_b is not None

    _install_fake_glob(
        monkeypatch,
        manifest_paths=[m_b],
        brief_paths=[brief_b],
    )
    monkeypatch.setenv("TALIS_DESK_DB", str(db_a))

    state = srv.get_state()
    assert state["desk_db"] == str(db_a)
    assert state["manifest_path"] is None
    assert state["brief_path"] is None
    coh = state["artifact_coherence"]
    assert coh["status"] == "missing_manifest"
    assert coh["cycle_id"] is None
    assert "no manifest matches" in (coh["reason"] or "")


def test_get_state_no_active_run_includes_coherence_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When no DB is discoverable, the no_active_run payload still exposes coherence."""
    from talis_desk.monitor import server as srv

    monkeypatch.delenv("TALIS_DESK_DB", raising=False)
    # Bypass discovery directly — avoids stubbing pathlib.Path globally
    # and is robust to the developer machine actually having ~/.talis/desk.db.
    monkeypatch.setattr(srv, "_find_latest_desk_db", lambda: None)
    _install_fake_glob(monkeypatch, manifest_paths=[], brief_paths=[])

    state = srv.get_state()
    assert state["status"] == "no_active_run"
    coh = state["artifact_coherence"]
    assert coh["status"] == "no_db"
    assert state["cycle_id"] is None
