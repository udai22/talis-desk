"""Microstructure Specialist — v1.0.

The tactical/execution voice on the Talis research desk. Driven by the
canonical loop runner; emits hypotheses + trade ideas grounded in HL
microstructure evidence (L2 imbalance, funding, basis, oracle drift,
realized vol, candle patterns, anomalies).

Cross-checks macro and smart-money calls against actual market plumbing.
If macro says "long BTC" but funding is -25bps and L2 is sell-dominant,
microstructure raises the contradiction before sizing.

See `persona.py` for the constructor and `prompt.md` for the full
system prompt. Both are versioned together as v1.0.
"""
from .persona import (
    build_microstructure_v1,
    register_microstructure_v1,
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
    "build_microstructure_v1",
    "register_microstructure_v1",
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
