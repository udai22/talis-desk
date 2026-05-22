"""Configuration helper for locating the talis-tic data substrate.

Codex review finding #16: scattered hardcoded `/Users/udaikhattar/...`
paths inserted into `sys.path` make the desk unrunnable on any host
other than the original developer's machine. This module consolidates
the resolution behind a single helper backed by the `TALIS_TIC_ROOT`
environment variable.

Resolution order:
  1. `TALIS_TIC_ROOT` env var (preferred, prod path).
  2. Standalone repo-local `talis_tic/` directory.
  3. Raise `RuntimeError` if neither resolves.

Every consumer that needs the `tic` package importable must call
`ensure_tic_on_path()` (or use `get_tic_root()` directly). The repo-local
fallback is intentionally narrow and is gated on the expected `tic/tic.db`
file existing inside it; we never silently fall through to placeholder
behavior.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


_REPO_LOCAL_TIC_ROOT = Path(__file__).resolve().parents[1] / "talis_tic"


def get_tic_root() -> Path:
    """Resolve the talis-tic root directory.

    Returns the directory that should be added to ``sys.path`` so that
    ``import tic.desk.models`` (and friends) work. This is the parent of
    the `tic/` package — the package directory itself contains
    ``tic/tic.db`` which we use as an existence sentinel.

    Resolution:
      - If ``TALIS_TIC_ROOT`` is set, use it. Raises if it doesn't
        contain ``tic/tic.db``.
      - Else fall back to repo-local ``talis_tic/``.
      - Else raise.
    """
    env = os.environ.get("TALIS_TIC_ROOT")
    if env:
        p = Path(env).expanduser().resolve()
        if not (p / "tic" / "tic.db").exists():
            raise RuntimeError(
                f"TALIS_TIC_ROOT={env!r} does not contain tic/tic.db. "
                f"Set the env var to the directory holding the `tic/` "
                f"package (parent of tic/tic.db)."
            )
        return p
    if (_REPO_LOCAL_TIC_ROOT / "tic" / "tic.db").exists():
        return _REPO_LOCAL_TIC_ROOT
    raise RuntimeError(
        "Cannot locate talis-tic. Set TALIS_TIC_ROOT env var to the "
        "directory containing `tic/tic.db`, or run from the standalone "
        "talis-desk repo with `talis_tic/` present."
    )


def ensure_tic_on_path() -> None:
    """Idempotently insert the talis-tic root into ``sys.path``.

    Safe to call repeatedly — only inserts when the resolved root is not
    already present. Raises ``RuntimeError`` if neither the env var nor
    repo-local `talis_tic/` resolves.
    """
    # Unit tests and worker harnesses often install a fake tic.desk.models
    # module directly into sys.modules. In that case there is no filesystem
    # root to resolve, and adding one would make the harness less portable.
    if "tic.desk.models" in sys.modules:
        return
    try:
        from tic.desk import models as _models  # type: ignore  # noqa: F401
        return
    except Exception:
        pass
    root = str(get_tic_root())
    if root not in sys.path:
        sys.path.insert(0, root)
