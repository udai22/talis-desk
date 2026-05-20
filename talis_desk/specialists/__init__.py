"""Specialist personas — the curated voices the loop runner drives.

A specialist is defined by:
  - a `SpecialistPersona` (system prompt, curated tool URIs, priors)
  - a row in `specialist_states` with `state_kind='persona'`

The loop runner reads the open persona row at the top of each cycle,
hands the system prompt + tool subset to `tic.desk.models.chat()`, and
drives the 6-stage agent loop. This module owns the persona contract;
the loop is in `talis_desk.loop` (separate agent).

Public API:
  SpecialistPersona, SpecialistState  — pydantic + dataclass
  register_persona(persona)           — idempotent insert into specialist_states
  get_current_persona(id, as_of=None) — bitemporally-sliced read
  list_personas(as_of=None)           — all latest personas
"""
from .base import (
    SpecialistPersona,
    SpecialistState,
    register_persona,
    get_current_persona,
    list_personas,
)

# Re-export the macro_regime persona constructor so callers can do
# `from talis_desk.specialists import build_macro_regime_v1`.
from .macro_regime import (
    build_macro_regime_v1,
    register_macro_regime_v1,
    INITIAL_PRIORS as MACRO_REGIME_INITIAL_PRIORS,
    CURATED_TOOL_URIS as MACRO_REGIME_CURATED_TOOL_URIS,
)

__all__ = [
    "SpecialistPersona",
    "SpecialistState",
    "register_persona",
    "get_current_persona",
    "list_personas",
    "build_macro_regime_v1",
    "register_macro_regime_v1",
    "MACRO_REGIME_INITIAL_PRIORS",
    "MACRO_REGIME_CURATED_TOOL_URIS",
]
