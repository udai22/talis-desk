"""Macro Regime Specialist — v1.0 persona constructor.

Builds and registers the canonical macro_regime persona row in
`specialist_states`. Idempotent — re-importing this module or re-calling
`register_macro_regime_v1` will NOT duplicate rows.

The persona's system prompt lives in `prompt.md` (adjacent file) so it can
be read, diffed, and reviewed without diving into Python source. The
prompt and the curated tool subset together fully define the v1 persona.

Source of truth for the persona contract: see `..base.py`.
Source of truth for the system-prompt structure: this file's prompt.md +
`wiki/SOTA_DESK_ARCHITECTURE.md` §4 (Agent Loop) + §5 (Self-Improvement).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..base import SpecialistPersona, SpecialistState, register_persona


# ============================================================================
# Constants — exported for reuse by the loop runner / tests
# ============================================================================

SPECIALIST_ID = "macro_regime"
PERSONA_VERSION = "v1.0"
PERSONA_NAME = "Macro Regime Specialist"
PERSONA_SCOPE = (
    "Identify the current macroeconomic + cross-asset regime and "
    "translate it into Hyperliquid perp positioning calls (BTC, ETH, SOL, "
    "HYPE, top 50). Rates / growth / inflation / credit / liquidity / "
    "USD / gold / COT lens."
)

# Preferred model — the loop's `chat()` call falls back through the
# provider chain if Anthropic is down. Opus chosen for the reasoning
# depth required by contradiction-seeking hypothesis generation.
PREFERRED_MODEL = "anthropic:claude-opus-4-7"

# Inter-agent topics this specialist subscribes to. The scratchpad
# router uses these to route peer messages from other specialists.
SUBSCRIBED_TOPICS = [
    "macro_regime",          # direct address
    "all",                   # broadcast
    "rates",                 # cross-cutting
    "liquidity",
    "credit",
    "usd",
    "research_director",     # assignments from the director
]


# Initial priors — the "world model" the specialist starts with on its
# first cycle. Updated via REFLECT each cycle; supersedes chain visible
# in `specialist_states`.
#
# Snapshot date: 2026-05-20 (matches the codebase context).
INITIAL_PRIORS: dict[str, Any] = {
    "rates_regime": "restrictive",           # FFR ~5%, term premium positive
    "growth_regime": "decelerating",         # JOLTS softening, ISM <50
    "credit_regime": "tightening",           # HY OAS widening
    "btc_regime": "sideways_since_2026_04",  # multi-month range
    "liquidity_regime": "draining",          # RRP zero, TGA building
    "usd_regime": "strong_but_topping",      # DXY mid-100s
    "confidence": 0.65,
    "uncertain_about": [
        "Fed reaction function timing",
        "credit transmission lag",
        "stablecoin supply growth as offsetting liquidity force",
    ],
    "watch_list": [
        "FOMC meeting (next)",
        "10Y/30Y auction tail size",
        "HY OAS > 400bps trigger",
        "RRP balance vs reserves",
        "BTC perp funding < -10bps as washout signal",
    ],
}


# Curated tool subset — the 15 URIs this specialist sees. Per v2 §5: NOT
# the full atlas. The loop runner builds the function-calling tool list
# from this subset.
#
# Selection rationale: macro_regime needs time-series queries (FRED/BLS),
# event/calendar tools (FOMC, Treasury auctions), positioning data (COT),
# cross-asset confluence, ad-hoc compute, semantic retrieval, and
# bitemporal replay. NO microstructure tools (those belong to the
# microstructure specialist), NO wallet tools (smart_money), NO chart
# pattern tools (chartist).
CURATED_TOOL_URIS: list[str] = [
    # --- Time-series queries (FRED, BLS, etc.) ---
    "tic://tool/builtin/query_timeseries@v1",
    "tic://tool/builtin/query_claims_by_entity@v1",
    "tic://tool/builtin/query_events_recent@v1",
    # --- Macro-specific ---
    "tic://tool/builtin/get_fed_balance_sheet_state@v1",
    "tic://tool/builtin/get_fomc_next_event@v1",
    "tic://tool/builtin/get_econ_event_today@v1",
    "tic://tool/builtin/get_treasury_auction_calendar@v1",
    "tic://tool/builtin/get_cot_positioning@v1",
    # --- Cross-asset confluence ---
    "tic://tool/builtin/get_cross_asset_corr@v1",
    "tic://tool/builtin/find_confluence@v1",
    # --- Stats + ad-hoc ---
    "tic://tool/builtin/compute_stat@v1",
    "tic://tool/builtin/run_code@v1",
    # --- Knowledge + replay ---
    "tic://tool/builtin/semantic_search@v1",
    "tic://tool/builtin/time_machine_snapshot@v1",
    "tic://tool/builtin/query_source_health@v1",
]


# ============================================================================
# Prompt loader
# ============================================================================

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def _load_system_prompt() -> str:
    """Read prompt.md from the adjacent file. Raises FileNotFoundError if
    the file is missing — this is a packaging bug, not a runtime fallback."""
    if not _PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"macro_regime prompt.md not found at {_PROMPT_PATH}. "
            f"This is a packaging bug — the file must ship with the module."
        )
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"macro_regime prompt.md is empty at {_PROMPT_PATH}")
    # Quick structural check: the 5 required sections must be present.
    required_sections = [
        "## ROLE",
        "## BEHAVIORAL DEFAULTS",
        "## TOOL SELECTION DECISION TREE",
        "## 3 WORKED EXAMPLES",
        "## OUTPUT CONTRACT",
    ]
    missing = [s for s in required_sections if s not in text]
    if missing:
        raise ValueError(
            f"macro_regime prompt.md is missing required sections: {missing}. "
            f"See talis_desk/specialists/macro_regime/prompt.md."
        )
    return text


# ============================================================================
# Builders
# ============================================================================

def build_macro_regime_v1() -> SpecialistPersona:
    """Construct the v1 persona. Reads system_prompt from prompt.md.

    Returns a fully-populated `SpecialistPersona` ready to pass into
    `register_persona`. Pure — does not touch the DB.
    """
    system_prompt = _load_system_prompt()
    return SpecialistPersona(
        specialist_id=SPECIALIST_ID,
        persona_version=PERSONA_VERSION,
        name=PERSONA_NAME,
        scope=PERSONA_SCOPE,
        system_prompt=system_prompt,
        tool_uris=list(CURATED_TOOL_URIS),
        preferred_model=PREFERRED_MODEL,
        subscribed_topics=list(SUBSCRIBED_TOPICS),
        initial_priors=dict(INITIAL_PRIORS),
        created_at=datetime.now(timezone.utc),
        author="talis-desk",
    )


def register_macro_regime_v1() -> SpecialistState:
    """Idempotent registration. Returns the SpecialistState for the open
    persona row — existing if already registered, new if first call.

    See `base.register_persona` for the idempotency contract:
      - If (specialist_id, persona_version, prompt_hash) already has an
        open row → returns that row unchanged.
      - If specialist_id has an open row at a different version/hash →
        closes the old row (transaction_to=now), inserts new with
        supersedes set, returns new.
    """
    persona = build_macro_regime_v1()
    return register_persona(persona)
