"""talis-desk — autonomous Hyperliquid research desk.

Layer 2 of the Talis stack. Depends on talis-tic (Layer 1 data foundation).
See wiki/SOTA_DESK_ARCHITECTURE.md for the locked v2 specification.
See wiki/REPO_BOUNDARY.md for the boundary contract with talis-tic.
"""

__version__ = "0.1.0"

from .store import DeskStore, get_desk_store
from .schema import apply_sota_schema
from .replay import build_replay_context
from .tool_atlas import (
    regenerate_tool_atlas,
    dispatch_uri,
    resolve_tool_uri,
    parse_tool_uri,
)

__all__ = [
    "DeskStore",
    "get_desk_store",
    "apply_sota_schema",
    "build_replay_context",
    "regenerate_tool_atlas",
    "dispatch_uri",
    "resolve_tool_uri",
    "parse_tool_uri",
]
