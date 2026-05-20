"""Options & Vol Specialist — v1.0 persona constructor.

Builds and registers the canonical options_vol persona row in
`specialist_states`. Idempotent — re-importing this module or re-calling
`register_options_vol_v1` will NOT duplicate rows.

The persona's system prompt lives in `prompt.md` (adjacent file) so it can
be read, diffed, and reviewed without diving into Python source.

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

SPECIALIST_ID = "options_vol"
PERSONA_VERSION = "v1.0"
PERSONA_NAME = "Options & Vol Specialist"
PERSONA_SCOPE = (
    "Implied vs realized vol surface specialist. Pull Deribit + Yahoo "
    "options surfaces, compare to realized vol, read skew, term-structure, "
    "put/call ratios, and vol-of-vol regime. Identify cheap/expensive vol "
    "windows and vol-arb setups. Output: vol-aware trade ideas (straddles, "
    "strangles, risk reversals, calendar spreads, vol-hedged perp longs) "
    "with explicit IV-vs-RV theses on Hyperliquid-adjacent expiries."
)

# Preferred model — chosen for the multi-axis surface reasoning (strike +
# expiry + skew + vol-of-vol) where Opus's recall is necessary.
PREFERRED_MODEL = "anthropic:claude-opus-4-7"

# Inter-agent topics this specialist subscribes to.
SUBSCRIBED_TOPICS = [
    "options_vol",           # direct address
    "all",                   # broadcast
    "vol",                   # cross-cutting (vol regime)
    "skew",                  # cross-cutting (skew alerts)
    "hedge",                 # hedging ideas from other specialists
    "research_director",     # assignments from the director
]


# Initial priors — the "world model" the specialist starts with on its
# first cycle. Snapshot date: 2026-05-20.
INITIAL_PRIORS: dict[str, Any] = {
    "btc_iv_realized_spread_bps": 1600,             # 30d IV ~48% vs 30d RV ~32% → +1600 bps
    "eth_iv_realized_spread_bps": 1400,             # ETH similar magnitude, slightly tighter
    "dvol_regime": "low_compressed_below_55",       # DVOL persistently below 55
    "put_call_skew_25d": 4.0,                       # 25-delta put-call IV skew, +4 vol points
    "vol_of_vol_pct": 75.0,                         # VVIX-equivalent for crypto, 75% annualized
    "term_structure_shape": "contango",             # 90d IV > 30d IV > 7d IV
    "confidence": 0.65,
    "uncertain_about": [
        "whether IV-RV spread compresses via RV expansion or IV collapse",
        "Deribit DVOL coverage gaps over weekends / Asia hours",
        "HYPE realized vol regime stability (no liquid options market to anchor)",
    ],
    "watch_list": [
        "BTC 30d IV-RV spread crossing > 2000 bps (extreme vol-selling setup)",
        "ETH 25d put skew > +8 vol points (panic skew, fade signal)",
        "term structure flipping to backwardation (vol-event imminent)",
        "VVIX-equivalent > 100 (vol-of-vol explosion, hedge longs)",
        "DVOL crossing < 45 (multi-month vol bottom, buy gamma)",
    ],
}


# Curated tool subset — the 15 URIs this specialist sees.
#
# Selection rationale: options_vol needs both options surfaces (Deribit
# for crypto, Yahoo for equity/ETF cross-check), realized vol on the
# underlying, funding history (a perp-funding extreme often correlates
# with skew extremes), cross-venue basis (funding+basis+IV is one signal),
# time-series for RV / vol-of-vol computation, correlation matrices
# (vol-correlation regime), anomalies (vol spikes), confluence checks,
# Pyth reference price, semantic search over vol regimes, source health,
# run_code for custom Black-Scholes / vol-surface fits, find_similar_setups
# for analog historical vol setups.
CURATED_TOOL_URIS: list[str] = [
    # --- Options surfaces ---
    "tic://tool/builtin/get_deribit_options@v1",
    "tic://tool/builtin/get_yahoo_options@v1",
    # --- Realized vol on the underlying ---
    "tic://tool/builtin/get_realized_vol@v1",
    # --- Funding + basis (vol-adjacent positioning) ---
    "tic://tool/builtin/get_hl_funding_history@v1",
    "tic://tool/builtin/cross_venue_basis@v1",
    # --- Time-series for RV / VVIX reconstruction ---
    "tic://tool/builtin/query_timeseries@v1",
    # --- Stats + correlation matrix ---
    "tic://tool/builtin/compute_stat@v1",
    "tic://tool/builtin/get_correlation_matrix@v1",
    # --- Anomalies (vol regime breaks) ---
    "tic://tool/builtin/query_anomalies_active@v1",
    # --- Confluence ---
    "tic://tool/builtin/find_confluence@v1",
    # --- Reference price (anchor for IV calculations) ---
    "tic://tool/builtin/get_pyth_price@v1",
    # --- Knowledge + analogs ---
    "tic://tool/builtin/semantic_search@v1",
    "tic://tool/builtin/find_similar_setups@v1",
    # --- Source health ---
    "tic://tool/builtin/query_source_health@v1",
    # --- Ad-hoc code for Black-Scholes / surface fits ---
    "tic://tool/builtin/run_code@v1",
]


# ============================================================================
# Prompt loader
# ============================================================================

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def _load_system_prompt() -> str:
    if not _PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"options_vol prompt.md not found at {_PROMPT_PATH}. "
            f"This is a packaging bug — the file must ship with the module."
        )
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"options_vol prompt.md is empty at {_PROMPT_PATH}")
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
            f"options_vol prompt.md is missing required sections: {missing}. "
            f"See talis_desk/specialists/options_vol_v1/prompt.md."
        )
    return text


# ============================================================================
# Builders
# ============================================================================

def build_options_vol_v1() -> SpecialistPersona:
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


def register_options_vol_v1() -> SpecialistState:
    persona = build_options_vol_v1()
    return register_persona(persona)
