"""Hypothesis graph — Layer 2 of the SOTA Desk Architecture (Phase 4).

Pydantic models + CRUD over the `hypotheses` and `hypothesis_edges` tables
defined in `wiki/SOTA_DESK_ARCHITECTURE.md` v2 §3 (lines 178-216).
"""
from .model import (
    Hypothesis,
    HypothesisDraft,
    HypothesisEdge,
    HypothesisEdgeDraft,
    HypothesisGraph,
    add_edge,
    get_active_hypotheses,
    get_hypothesis_graph,
    propose_hypothesis,
    resolve_hypothesis,
    update_posterior,
)

__all__ = [
    "Hypothesis",
    "HypothesisDraft",
    "HypothesisEdge",
    "HypothesisEdgeDraft",
    "HypothesisGraph",
    "propose_hypothesis",
    "add_edge",
    "update_posterior",
    "resolve_hypothesis",
    "get_active_hypotheses",
    "get_hypothesis_graph",
]
