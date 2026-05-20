"""Sentiment & Event Specialist — v1.0 persona constructor.

Builds and registers the canonical sentiment_event persona row in
`specialist_states`. Idempotent — re-importing this module or re-calling
`register_sentiment_event_v1` will NOT duplicate rows.

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

SPECIALIST_ID = "sentiment_event"
PERSONA_VERSION = "v1.0"
PERSONA_NAME = "Sentiment & Event Specialist"
PERSONA_SCOPE = (
    "The narrative + event-driven voice on the Talis desk. Track the "
    "macro calendar (FOMC, NFP, CPI, treasury auctions), crypto-specific "
    "events (HIP-4 outcomes, SEC 8-K filings on crypto-exposed names, "
    "congressional moves), social + news tone (fear-greed, GDELT, recent "
    "news clusters), and Polymarket implied probabilities as a forward-"
    "looking sentiment proxy. Translate 'what is the story + when does it "
    "break?' into Hyperliquid perp positioning calls."
)

# Preferred model — the loop's `chat()` call falls back through the
# provider chain if Anthropic is down. Opus chosen for the narrative
# synthesis + contradiction-seeking required when multiple unreliable
# sentiment streams disagree.
PREFERRED_MODEL = "anthropic:claude-opus-4-7"

# Inter-agent topics this specialist subscribes to. The scratchpad
# router uses these to route peer messages from other specialists.
SUBSCRIBED_TOPICS = [
    "sentiment",             # direct address
    "all",                   # broadcast
    "news",                  # cross-cutting headlines
    "events",                # scheduled calendar events
    "fomc",                  # Fed cross-cutting (overlap w/ macro_regime)
    "polymarket",            # implied-probability cross-cutting
    "narrative",             # story-arc tracking
    "research_director",     # assignments from the director
]


# Initial priors — the "world model" the specialist starts with on its
# first cycle. Updated via REFLECT each cycle; supersedes chain visible
# in `specialist_states`.
#
# Snapshot date: 2026-05-20.
INITIAL_PRIORS: dict[str, Any] = {
    # Dominant narrative regime label. Free-form but the REFLECT step
    # should snap it to one of: "risk_on_but_fragile" / "stagflation_fear"
    # / "rate_cut_hopium" / "regulatory_overhang" / "growth_scare" /
    # "narrative_vacuum".
    "narrative_regime": "risk_on_but_fragile",
    # Placeholder for the next highest-impact calendar event. The first
    # cycle overwrites this with the live FOMC / NFP / CPI / auction date.
    "next_high_impact_event": "FOMC: TBD",
    # Polymarket implied probability that BTC closes the year above
    # consensus strike. 0.5 = neutral; track drift away from 0.5 as the
    # primary forward-looking sentiment signal.
    "polymarket_skew_btc_eoy": 0.5,
    # CNN-style Fear & Greed index centered baseline. 0..100 scale;
    # 50 = neutral. <25 extreme fear, >75 extreme greed.
    "fear_greed_baseline": 50,
    # Coarse news-volume regime. One of: "quiet" / "moderate" / "elevated"
    # / "frenzy". Used to scale per-headline weight in confluence votes.
    "news_volume_baseline": "moderate",
    # Narrative is structurally noisier than rates/credit time series.
    # Start below macro_regime's 0.65 baseline confidence.
    "confidence": 0.55,
    "uncertain_about": [
        "FOMC reaction function timing — when does dot-plot drift price in?",
        "narrative durability past 48h — do news clusters survive a weekend?",
        "Polymarket liquidity in the tails — implied probs unreliable below ~$50k OI",
    ],
    "watch_list": [
        "FOMC statement language delta vs prior meeting (hawkish/dovish surprise)",
        "NFP print > +/- 1 standard deviation vs consensus on release morning",
        "SEC 8-K filing on a top-10 crypto-exposed equity within 24h",
        "Polymarket BTC-EOY contract probability moves > 5 percentage points in <6h",
        "GDELT crypto tone < -2.5 (capitulation signature) sustained > 12h",
    ],
    # GDELT-style aggregate tone score, theoretical range roughly
    # [-10, +10]. 0.0 = neutral global tone baseline.
    "geopolitical_tone_baseline": 0.0,
}


# Curated tool subset — the 15 URIs this specialist sees. Per v2 §5: NOT
# the full atlas. The loop runner builds the function-calling tool list
# from this subset.
#
# Selection rationale: sentiment_event needs event-calendar lookups
# (FOMC, econ release, treasury auction, HIP-4 outcome, SEC 8-K), news
# + tone retrieval (recent news, GDELT tone), forward-looking sentiment
# (Polymarket), bitemporal event replay, semantic search across the
# claim store, cross-source confluence, ad-hoc stats, anomaly checks,
# source-health verification, and parallel-search for multi-headline
# clusters. NO HL orderbook / funding tools (microstructure), NO wallet
# flow tools (smart_money), NO FRED macro time-series (macro_regime).
CURATED_TOOL_URIS: list[str] = [
    # --- Scheduled-event calendars (macro + crypto) ---
    "tic://tool/builtin/get_fomc_next_event@v1",
    "tic://tool/builtin/get_econ_event_today@v1",
    "tic://tool/builtin/get_treasury_auction_calendar@v1",
    "tic://tool/builtin/get_hip4_outcomes@v1",
    "tic://tool/builtin/get_sec_recent_8k@v1",
    # --- News + tone ---
    "tic://tool/builtin/query_recent_news@v1",
    "tic://tool/builtin/get_geopolitical_tone@v1",
    # --- Forward-looking sentiment (Polymarket) ---
    "tic://tool/builtin/get_polymarket_probs@v1",
    # --- Event store + retrieval ---
    "tic://tool/builtin/query_events_recent@v1",
    "tic://tool/builtin/semantic_search@v1",
    # --- Cross-source confluence ---
    "tic://tool/builtin/find_confluence@v1",
    # --- Stats + ad-hoc ---
    "tic://tool/builtin/compute_stat@v1",
    # --- Anomalies + health + parallel retrieval ---
    "tic://tool/builtin/query_anomalies_active@v1",
    "tic://tool/builtin/query_source_health@v1",
    "tic://tool/builtin/parallel_search@v1",
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
            f"sentiment_event prompt.md not found at {_PROMPT_PATH}. "
            f"This is a packaging bug — the file must ship with the module."
        )
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"sentiment_event prompt.md is empty at {_PROMPT_PATH}")
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
            f"sentiment_event prompt.md is missing required sections: {missing}. "
            f"See talis_desk/specialists/sentiment_event_v1/prompt.md."
        )
    return text


# ============================================================================
# Builders
# ============================================================================

def build_sentiment_event_v1() -> SpecialistPersona:
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


def register_sentiment_event_v1() -> SpecialistState:
    """Idempotent registration. Returns the SpecialistState for the open
    persona row — existing if already registered, new if first call.

    See `base.register_persona` for the idempotency contract:
      - If (specialist_id, persona_version, prompt_hash) already has an
        open row → returns that row unchanged.
      - If specialist_id has an open row at a different version/hash →
        closes the old row (transaction_to=now), inserts new with
        supersedes set, returns new.
    """
    persona = build_sentiment_event_v1()
    return register_persona(persona)
