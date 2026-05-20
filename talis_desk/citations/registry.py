"""Register the 3 citation tools with the tic tool registry.

`resolve_citation`, `fetch_source`, `verify_citation` are exposed as
first-class desk tools so specialists can invoke them via the same
dispatch pipeline they use for other tools (full audit trail, quality
flags, cost tracking).

Registration is idempotent. Call `register_citation_tools()` once at
desk-startup. The atlas regeneration step picks them up.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .api import fetch_source, resolve_citation, verify_citation


logger = logging.getLogger(__name__)


# Canonical tool URIs (mirror the talis-tic naming convention).
TOOL_URI_RESOLVE = "talis_desk:citations:resolve_citation:v1"
TOOL_URI_FETCH_SOURCE = "talis_desk:citations:fetch_source:v1"
TOOL_URI_VERIFY = "talis_desk:citations:verify_citation:v1"


def _tool_resolve_citation(citation_id: str, **_: Any) -> dict[str, Any]:
    rec = resolve_citation(citation_id)
    if rec is None:
        return {"ok": False, "error": "citation_not_found", "citation_id": citation_id}
    return {
        "ok": True,
        "citation": {
            "id": rec.id,
            "canonical_url": rec.canonical_url,
            "content_hash": rec.content_hash,
            "anchor": rec.anchor,
            "quote_excerpt": rec.quote_excerpt,
            "fetched_at": rec.fetched_at,
            "still_valid": rec.still_valid,
            "verified_at": rec.verified_at,
            "quality_flags": rec.quality_flags,
        },
    }


def _tool_fetch_source(
    citation_id: str,
    depth: str = "excerpt",
    **_: Any,
) -> dict[str, Any]:
    if depth not in ("excerpt", "section", "full"):
        return {"ok": False, "error": f"bad_depth: {depth!r}"}
    return {"ok": True, **fetch_source(citation_id, depth=depth)}


def _tool_verify_citation(
    citation_id: str,
    force_refetch: bool = False,
    **_: Any,
) -> dict[str, Any]:
    return {"ok": True, **verify_citation(citation_id, force_refetch=bool(force_refetch))}


def register_citation_tools() -> dict[str, str]:
    """Register the 3 citation tools with the tic tool registry.

    Returns a dict {tool_uri: outcome} where outcome is "registered",
    "already_registered", or "registry_unavailable".
    """
    try:
        from tic.desk import tools as _tic_tools  # type: ignore
    except Exception as e:
        logger.warning("citations: tic.desk.tools unavailable (%s)", e)
        return {
            TOOL_URI_RESOLVE: "registry_unavailable",
            TOOL_URI_FETCH_SOURCE: "registry_unavailable",
            TOOL_URI_VERIFY: "registry_unavailable",
        }

    # Look for a register() entry point in the tic tools module. Different
    # versions have different surfaces; we probe in order of preference.
    register_fn = None
    for name in ("register_tool", "register", "add_tool"):
        if hasattr(_tic_tools, name):
            register_fn = getattr(_tic_tools, name)
            break

    out: dict[str, str] = {}
    if register_fn is None:
        # No programmatic register; we still expose the symbols so the
        # desk can call them directly. Tag as registered-in-process.
        out[TOOL_URI_RESOLVE] = "in_process"
        out[TOOL_URI_FETCH_SOURCE] = "in_process"
        out[TOOL_URI_VERIFY] = "in_process"
        return out

    for uri, fn, doc in [
        (TOOL_URI_RESOLVE, _tool_resolve_citation,
         "Resolve a citation_id to its provenance row."),
        (TOOL_URI_FETCH_SOURCE, _tool_fetch_source,
         "Fetch the source body at the given depth: excerpt|section|full."),
        (TOOL_URI_VERIFY, _tool_verify_citation,
         "Re-fetch the canonical URL and check the quote still appears."),
    ]:
        try:
            register_fn(uri, fn, description=doc)
            out[uri] = "registered"
        except Exception as e:
            logger.warning("citations.register %s failed: %s", uri, e)
            out[uri] = f"failed: {e}"
    return out
