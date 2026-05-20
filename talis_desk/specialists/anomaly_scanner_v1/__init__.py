"""Anomaly Scanner Specialist — v1.0.

Pure anomaly hunter. Reads `query_anomalies_active`, runs cross-source
convergence checks via `find_confluence`, uses `run_code` to compute
custom z-scores, regime-break tests, and matrix-profile scans. Surfaces
setups no other specialist sees.

See `persona.py` for the constructor and `prompt.md` for the full
system prompt. Both are versioned together as v1.0.
"""
from .persona import (
    build_anomaly_scanner_v1,
    register_anomaly_scanner_v1,
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
    "build_anomaly_scanner_v1",
    "register_anomaly_scanner_v1",
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
