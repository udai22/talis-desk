"""RRG Rotation Specialist — v1.0.

Relative Rotation Graph + sector/factor rotation specialist. Trades the
*delta* between assets, never absolute levels. Identifies leadership
quadrant transitions (Leading / Weakening / Lagging / Improving) and
emits pair / rotation trade ideas.

See `persona.py` for the constructor and `prompt.md` for the full
system prompt. Both are versioned together as v1.0.
"""
from .persona import (
    build_rrg_rotation_v1,
    register_rrg_rotation_v1,
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
    "build_rrg_rotation_v1",
    "register_rrg_rotation_v1",
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
