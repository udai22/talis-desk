"""Polymarket Divergence Specialist — v1.0.

Hunts divergences between Polymarket implied probabilities and HL perp,
FRED, news, and event-calendar data. Mines mispriced prediction markets
for cross-domain signals into crypto positioning.

See `persona.py` for the constructor and `prompt.md` for the full
system prompt. Both are versioned together as v1.0.
"""
from .persona import (
    build_polymarket_divergence_v1,
    register_polymarket_divergence_v1,
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
    "build_polymarket_divergence_v1",
    "register_polymarket_divergence_v1",
    "INITIAL_PRIORS",
    "CURATED_TOOL_URIS",
    "SPECIALIST_ID",
    "PERSONA_VERSION",
    "PERSONA_NAME",
    "PERSONA_SCOPE",
    "PREFERRED_MODEL",
    "SUBSCRIBED_TOPICS",
    "SpecialistPersona",
    "SpecialistState",
    "register_persona",
    "get_current_persona",
    "list_personas",
]
