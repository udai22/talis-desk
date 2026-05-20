"""Smart Money / On-Chain Flow Specialist — v1.0.

The "who's actually trading" voice on the Talis research desk. Driven by
the canonical loop runner; emits hypotheses + trade ideas grounded in
HL wallet flow evidence (leaderboards, deep dives, cluster correlations,
noob-vs-whale contrast, HIP-4 outcomes, cross-DEX collision).

Cross-checks macro and microstructure calls against who is actually
positioning. If macro says "long BTC" but the top-decile-PnL cohort is
quietly unwinding and noob wallets are FOMO-buying, smart_money raises
the contradiction before sizing.

See `persona.py` for the constructor and `prompt.md` for the full
system prompt. Both are versioned together as v1.0.
"""
from .persona import (
    build_smart_money_v1,
    register_smart_money_v1,
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
    "build_smart_money_v1",
    "register_smart_money_v1",
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
