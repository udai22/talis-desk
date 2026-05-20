"""Synthesis sub-package — LLM-driven trade-idea drafting.

Replaces the placeholder `_build_minimal_trade_idea_draft` fabrication
(which emits flat/no-entry drafts with prices 100/99/101) with real
specialist-authored trade idea synthesis backed by a heavy LLM and a
multi-provider fallback chain.

See `idea_synthesizer.py` for the public API.
"""
from __future__ import annotations

from .idea_synthesizer import (
    IdeaSynthesisResult,
    IdeaSynthesisUnavailableError,
    synthesize_trade_ideas,
)

__all__ = [
    "IdeaSynthesisResult",
    "IdeaSynthesisUnavailableError",
    "synthesize_trade_ideas",
]
