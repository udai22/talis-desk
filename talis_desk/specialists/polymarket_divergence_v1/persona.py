"""Polymarket Divergence Specialist — v1.0 persona constructor.

Builds and registers the canonical polymarket_divergence persona row in
`specialist_states`. Idempotent — re-importing this module or re-calling
`register_polymarket_divergence_v1` will NOT duplicate rows.

Source of truth for the persona contract: see `..base.py`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..base import SpecialistPersona, SpecialistState, register_persona


# ============================================================================
# Constants — exported for reuse by the loop runner / tests
# ============================================================================

SPECIALIST_ID = "polymarket_divergence"
PERSONA_VERSION = "v1.0"
PERSONA_NAME = "Polymarket Divergence Specialist"
PERSONA_SCOPE = (
    "Prediction-market quant. Hunt divergences between Polymarket implied "
    "probabilities and HL perp prices, FRED macro data, news tone, and the "
    "event calendar. When Polymarket says X but spot/options/news say not-X, "
    "that gap is the trade. Emit hypotheses and trade ideas grounded in "
    "cross-domain mispricings with explicit liquidity floors."
)

# Preferred model — chosen for cross-domain reasoning across prediction
# markets, news, options, perps, and macro calendars.
PREFERRED_MODEL = "anthropic:claude-opus-4-7"

# Inter-agent topics this specialist subscribes to.
SUBSCRIBED_TOPICS = [
    "polymarket_divergence",     # direct address
    "all",                       # broadcast
    "prediction_markets",        # cross-cutting
    "news",                      # cross-cutting (news vs PM alignment)
    "events",                    # cross-cutting (event-calendar context)
    "research_director",         # assignments from the director
]


# Initial priors — Snapshot date: 2026-05-20.
INITIAL_PRIORS: dict[str, Any] = {
    "n_active_pm_markets": 42,                              # active markets we track
    "top_divergence_market_id": "pm_btc_100k_eoy_2026",     # placeholder; updated each cycle
    "recent_divergence_max_pct": 18.0,                      # max abs PM-implied vs spot-implied gap
    "event_calendar_density": "normal",                     # sparse | normal | crowded
    "news_pm_alignment": "mixed",                           # aligned | divergent | mixed
    "confidence": 0.55,
    "uncertain_about": [
        "Polymarket UMA oracle resolution disputes on tail-event markets",
        "thin-liquidity market price discovery vs informed-trader signal",
        "Polymarket vs Kalshi divergence (regulatory arbitrage signal?)",
    ],
    "watch_list": [
        "any PM-implied vs HL-perp gap > 15 pct points on same-underlying market",
        "PM-implied probability move > 10 pct points in 24h with < $50k volume (manipulation)",
        "news tone shift > 1 stdev with no PM-implied move (lagging market)",
        "Fed-decision PM markets diverging from rates-futures-implied",
        "ETF-approval markets with widening ETHE/GBTC discount mismatch",
    ],
    "polymarket_liquidity_floor_usd": 10000,                # below this, fade as noise
}


# Curated tool subset — the 15 URIs this specialist sees.
#
# Selection rationale: polymarket_divergence needs Polymarket
# probabilities, news (recent + tone), event calendars (FOMC, treasury,
# generic econ), time-series for cross-checking PM vs spot/rates,
# confluence, anomalies, source health, semantic search across past PM
# divergences, parallel search for multi-domain quick scans, and run_code
# for custom divergence z-scores.
CURATED_TOOL_URIS: list[str] = [
    # --- Polymarket primary tool ---
    "tic://tool/builtin/get_polymarket_probs@v1",
    # --- News (tone + recent headlines) ---
    "tic://tool/builtin/query_recent_news@v1",
    "tic://tool/builtin/get_geopolitical_tone@v1",
    # --- Time-series cross-check ---
    "tic://tool/builtin/query_timeseries@v1",
    # --- Event calendars (the core context for PM markets) ---
    "tic://tool/builtin/query_events_recent@v1",
    "tic://tool/builtin/get_econ_event_today@v1",
    "tic://tool/builtin/get_fomc_next_event@v1",
    "tic://tool/builtin/get_treasury_auction_calendar@v1",
    # --- Knowledge + analogs ---
    "tic://tool/builtin/semantic_search@v1",
    # --- Confluence + stats ---
    "tic://tool/builtin/find_confluence@v1",
    "tic://tool/builtin/compute_stat@v1",
    # --- Anomalies + source health ---
    "tic://tool/builtin/query_anomalies_active@v1",
    "tic://tool/builtin/query_source_health@v1",
    # --- Parallel multi-domain search ---
    "tic://tool/builtin/parallel_search@v1",
    # --- Ad-hoc divergence math ---
    "tic://tool/builtin/run_code@v1",
]


# ============================================================================
# Prompt loader
# ============================================================================

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def _load_system_prompt() -> str:
    if not _PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"polymarket_divergence prompt.md not found at {_PROMPT_PATH}. "
            f"This is a packaging bug — the file must ship with the module."
        )
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(
            f"polymarket_divergence prompt.md is empty at {_PROMPT_PATH}"
        )
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
            f"polymarket_divergence prompt.md is missing required sections: {missing}. "
            f"See talis_desk/specialists/polymarket_divergence_v1/prompt.md."
        )
    return text


# ============================================================================
# Builders
# ============================================================================

def build_polymarket_divergence_v1() -> SpecialistPersona:
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


def register_polymarket_divergence_v1() -> SpecialistState:
    persona = build_polymarket_divergence_v1()
    return register_persona(persona)
