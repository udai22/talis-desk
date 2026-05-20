"""Sentiment & Event Specialist — v1.0.

The narrative + event-driven voice on the Talis research desk. Driven by
the canonical loop runner; emits hypotheses + trade ideas grounded in the
macro + crypto event calendar (FOMC, NFP, CPI, treasury auctions, HIP-4
outcomes, SEC 8-K filings), news + social tone (fear-greed, GDELT, recent
news clusters), and Polymarket implied probabilities as a forward-looking
sentiment proxy.

The "what is the narrative + when does it break?" voice on the desk.
Cross-checks macro and microstructure calls against the story arc and the
calendar: a clean macro thesis that walks straight into an FOMC blackout
or a known HIP-4 outcome window is downgraded before sizing.

See `persona.py` for the constructor and `prompt.md` for the full
system prompt. Both are versioned together as v1.0.
"""
from .persona import (
    build_sentiment_event_v1,
    register_sentiment_event_v1,
    INITIAL_PRIORS,
    CURATED_TOOL_URIS,
    SPECIALIST_ID,
    PERSONA_VERSION,
    PERSONA_NAME,
    PERSONA_SCOPE,
    PREFERRED_MODEL,
    SUBSCRIBED_TOPICS,
)
from ..base import (
    SpecialistPersona,
    SpecialistState,
    register_persona,
    get_current_persona,
    list_personas,
)

__all__ = [
    # Builders
    "build_sentiment_event_v1",
    "register_sentiment_event_v1",
    # Constants
    "INITIAL_PRIORS",
    "CURATED_TOOL_URIS",
    "SPECIALIST_ID",
    "PERSONA_VERSION",
    "PERSONA_NAME",
    "PERSONA_SCOPE",
    "PREFERRED_MODEL",
    "SUBSCRIBED_TOPICS",
    # Re-exports from base
    "SpecialistPersona",
    "SpecialistState",
    "register_persona",
    "get_current_persona",
    "list_personas",
]
