"""RRG Rotation Specialist — v1.0 persona constructor.

Builds and registers the canonical rrg_rotation persona row in
`specialist_states`. Idempotent — re-importing this module or re-calling
`register_rrg_rotation_v1` will NOT duplicate rows.

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

SPECIALIST_ID = "rrg_rotation"
PERSONA_VERSION = "v1.0"
PERSONA_NAME = "RRG Rotation Specialist"
PERSONA_SCOPE = (
    "Relative Rotation Graph factor + sector rotation specialist. Identify "
    "which assets are entering/exiting leadership quadrants (Leading / "
    "Weakening / Lagging / Improving) vs benchmark. Emit cross-sectional "
    "pair trades and rotation calls — long the rotator entering Leading, "
    "short the rotator entering Weakening, sized on the *delta* between "
    "the two legs, never on absolute price."
)

# Preferred model — the loop's `chat()` call falls back through the
# provider chain if Anthropic is down. Opus chosen for the multi-asset
# matrix reasoning required to spot non-obvious rotation pairs.
PREFERRED_MODEL = "anthropic:claude-opus-4-7"

# Inter-agent topics this specialist subscribes to. The scratchpad
# router uses these to route peer messages from other specialists.
SUBSCRIBED_TOPICS = [
    "rrg_rotation",          # direct address
    "all",                   # broadcast
    "rotation",              # cross-cutting (sector / factor)
    "pairs",                 # pair-trade ideation cross-cutting
    "correlation",           # correlation-regime cross-cutting
    "research_director",     # assignments from the director
]


# Initial priors — the "world model" the specialist starts with on its
# first cycle. Updated via REFLECT each cycle; supersedes chain visible
# in `specialist_states`.
#
# Snapshot date: 2026-05-20.
INITIAL_PRIORS: dict[str, Any] = {
    "current_rrg_regime": "crypto_improving_vs_equities",  # BTC/ETH gaining vs SPX/NDX
    "dominant_axis": "momentum",                            # JdK RS-Momentum dominant
    "n_assets_in_leading": 4,                               # BTC, ETH, SOL, gold-proxy
    "n_assets_in_lagging": 6,                               # mid-cap alts, treasury duration
    "rotation_velocity": 3,                                 # 1-5 scale, moderate
    "recent_quadrant_jumps": [
        "BTC: Improving -> Leading (3d ago)",
        "HYPE: Leading -> Weakening (5d ago)",
        "NDX (tech): Leading -> Weakening (7d ago)",
    ],
    "confidence": 0.6,
    "uncertain_about": [
        "whether crypto Leading rotation extends to alts or stays BTC-concentrated",
        "RS-Ratio mean-reversion timing for tech (NDX) after Weakening entry",
        "treasury curve 2Y vs 10Y rotation as a risk-on/off proxy",
    ],
    "watch_list": [
        "BTC vs SPX RS-Ratio crossing 102 (durable Leading entry)",
        "HYPE perp RS-Momentum vs BTC turning < 100 (Lagging confirmation)",
        "ETH/BTC ratio entering Improving (alt-season pre-signal)",
        "DXY vs gold RS rotation (USD strength loss = risk-on tell)",
        "2Y-10Y curve RS entering Improving (steepener trade signal)",
    ],
}


# Curated tool subset — the 15 URIs this specialist sees. Per v2 §5: NOT
# the full atlas. The loop runner builds the function-calling tool list
# from this subset.
#
# Selection rationale: rrg_rotation needs the dedicated RRG state tool,
# cross-asset correlation tools, time-series queries for RS computation,
# stats + ad-hoc code (for custom JdK RS-Ratio / RS-Momentum), confluence
# checks, similar-setup retrieval, semantic search over rotation-themed
# claims, bitemporal replay for "what did rotation look like before the
# last regime break", anomalies + source health. NO macro time-series
# tools (those belong to macro_regime), NO wallet flow tools (smart_money),
# NO orderbook tools (microstructure).
CURATED_TOOL_URIS: list[str] = [
    # --- RRG-specific synthesis primitive ---
    "tic://tool/builtin/rrg_state@v1",
    # --- Cross-asset correlation matrix (the rotation substrate) ---
    "tic://tool/builtin/get_cross_asset_corr@v1",
    "tic://tool/builtin/get_correlation_matrix@v1",
    # --- Time-series for RS-Ratio / RS-Momentum computation ---
    "tic://tool/builtin/query_timeseries@v1",
    # --- Stats + ad-hoc code for custom RS math ---
    "tic://tool/builtin/compute_stat@v1",
    "tic://tool/builtin/run_code@v1",
    # --- Confluence + collision (cross-source rotation confirmation) ---
    "tic://tool/builtin/find_confluence@v1",
    "tic://tool/builtin/cross_dex_collision@v1",
    # --- Reference prices for benchmark anchoring ---
    "tic://tool/builtin/get_pyth_price@v1",
    # --- Vol context for sizing the pair leg ---
    "tic://tool/builtin/get_realized_vol@v1",
    # --- Similar-setup retrieval (analog rotation events) ---
    "tic://tool/builtin/find_similar_setups@v1",
    # --- Knowledge + bitemporal replay ---
    "tic://tool/builtin/semantic_search@v1",
    "tic://tool/builtin/time_machine_snapshot@v1",
    # --- Anomalies + source health ---
    "tic://tool/builtin/query_anomalies_active@v1",
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
            f"rrg_rotation prompt.md not found at {_PROMPT_PATH}. "
            f"This is a packaging bug — the file must ship with the module."
        )
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"rrg_rotation prompt.md is empty at {_PROMPT_PATH}")
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
            f"rrg_rotation prompt.md is missing required sections: {missing}. "
            f"See talis_desk/specialists/rrg_rotation_v1/prompt.md."
        )
    return text


# ============================================================================
# Builders
# ============================================================================

def build_rrg_rotation_v1() -> SpecialistPersona:
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


def register_rrg_rotation_v1() -> SpecialistState:
    """Idempotent registration. Returns the SpecialistState for the open
    persona row — existing if already registered, new if first call.
    """
    persona = build_rrg_rotation_v1()
    return register_persona(persona)
