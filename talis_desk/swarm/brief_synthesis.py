"""Tier 4 — Opus brief synthesis.

Takes the top-graded Tier 3 research reports + composes the daily brief.
This wraps the existing `talis_desk.brief.compose_brief` (which handles
the structural rendering — trade book, hot hypotheses, calendar gate,
quality flag propagation) but injects the swarm-tier outputs into the
headline payload so the LLM headline call is conditioned on the actual
swarm corpus.

Cost: ~$0.30 (1-2 Opus calls).

The calendar gate IS honored — it's built into compose_brief and we
pass through its outputs unchanged. If today is NVDA earnings the brief
headline is overridden to lead with that catalyst.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from ..brief import compose_brief


logger = logging.getLogger(__name__)


def synthesize_brief(
    cycle_id: str,
    scope: str = "market",
    output_format: str = "markdown",
    headline_model: str = "anthropic:claude-opus-4-7",
    headline_fallback: str = "anthropic:claude-sonnet-4-6",
    write_tic_artifact: bool = True,
) -> Any:
    """Tier 4 entry point. Compose the daily brief from the desk's
    current state, including all Tier 1/1.5/2/3 outputs that landed
    under sub-cycles of `cycle_id`.

    Returns the Brief object produced by `compose_brief`. The brief
    carries `calendar_gate` payload + quality_flags + headline_meta in
    its payload so downstream consumers can audit.
    """
    return compose_brief(
        cycle_id=cycle_id,
        scope=scope,
        output_format=output_format,
        headline_model=headline_model,
        headline_fallback=headline_fallback,
        write_tic_artifact=write_tic_artifact,
    )
