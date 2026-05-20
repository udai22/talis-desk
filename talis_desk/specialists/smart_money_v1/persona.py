"""Smart Money / On-Chain Flow Specialist — v1.0 persona constructor.

Builds and registers the canonical smart_money persona row in
`specialist_states`. Idempotent — re-importing this module or re-calling
`register_smart_money_v1` will NOT duplicate rows.

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

SPECIALIST_ID = "smart_money"
PERSONA_VERSION = "v1.0"
PERSONA_NAME = "Smart Money / On-Chain Flow Specialist"
PERSONA_SCOPE = (
    "Track who is actually positioning across Hyperliquid and adjacent "
    "perp DEXs. Wallet PnL leaderboards, cluster correlations, noob-vs-"
    "whale contrast, recent large entries/exits, cross-DEX collision "
    "(same wallet across HL + other venues), HIP-4 outcomes. Translate "
    "wallet flow into perp positioning calls and contradict the desk's "
    "narrative when the actual money disagrees."
)

# Preferred model — the loop's `chat()` call falls back through the
# provider chain if Anthropic is down. Opus chosen for the reasoning
# depth required by wallet-cluster pattern recognition + contradiction-
# seeking hypothesis generation.
PREFERRED_MODEL = "anthropic:claude-opus-4-7"

# Inter-agent topics this specialist subscribes to. The scratchpad
# router uses these to route peer messages from other specialists.
SUBSCRIBED_TOPICS = [
    "smart_money",           # direct address
    "all",                   # broadcast
    "whale_flow",            # cross-cutting wallet positioning
    "wallet_clusters",       # cluster behavior alerts
    "hip4",                  # HIP-4 launches + outcomes
    "research_director",     # assignments from the director
]


# Initial priors — the "world model" the specialist starts with on its
# first cycle. Updated via REFLECT each cycle; supersedes chain visible
# in `specialist_states`.
#
# Snapshot date: 2026-05-20 (matches the codebase context).
INITIAL_PRIORS: dict[str, Any] = {
    "top_decile_pnl_trend": "stable",          # top 10% wallet PnL trend
    "whale_net_bias": "balanced",              # net direction of top 50
    "noob_capitulation_signal": False,         # noob cohort liquidation cluster
    "cluster_concentration_pct": 30,           # % of OI held by top 50 wallets
    "recent_hip4_outcomes": [                  # recent HIP-4 launch outcomes
        "auction_outcome_pending_next_window",
        "last_winner_concentration_high",
    ],
    "cross_dex_collision_rate": "moderate",    # how often top wallets show on other DEXs
    "confidence": 0.6,
    "uncertain_about": [
        "sub-account fragmentation hiding true position size",
        "MM vs directional bias attribution for top wallets",
        "HL builder-code routing distorting wallet PnL leaderboards",
    ],
    "watch_list": [
        "top-50 net bias flip > 1 std",
        "noob cohort 24h liquidation cluster",
        "single wallet > 5% OI in any HIP-3 ticker",
        "cross-DEX collision: top HL wallet appears on dYdX/Aevo same side",
        "HIP-4 winner concentration > 60% post-auction",
    ],
}


# Curated tool subset — the 15 URIs this specialist sees. Per v2 §5: NOT
# the full atlas. The loop runner builds the function-calling tool list
# from this subset.
#
# Selection rationale: smart_money needs wallet-level queries (PnL,
# deep dive, completed trades, historical orders), aggregate stats
# (compute_wallet_stats, find_related_wallets), cohort tooling
# (get_noob_wallets, whale_check), HIP-4 outcome lookup, cross-DEX
# collision, confluence + semantic + source health, and ad-hoc compute.
# NO macro tools (those belong to macro_regime), NO L2/funding tools
# (microstructure), NO chart pattern tools (chartist).
CURATED_TOOL_URIS: list[str] = [
    # --- HL PnL leaderboard + wallet deep dives ---
    "tic://tool/builtin/get_hl_pnl_leaderboard@v1",
    "tic://tool/builtin/get_wallet_deep_dive@v1",
    "tic://tool/builtin/get_wallet_pnl_summary@v1",
    "tic://tool/builtin/get_wallet_completed_trades@v1",
    "tic://tool/builtin/get_wallet_historical_orders@v1",
    # --- Wallet stats + cluster correlation ---
    "tic://tool/builtin/compute_wallet_stats@v1",
    "tic://tool/builtin/find_related_wallets@v1",
    # --- Cohort + classifiers ---
    "tic://tool/builtin/get_noob_wallets@v1",
    "tic://tool/builtin/whale_check@v1",
    # --- HIP-4 + cross-DEX ---
    "tic://tool/builtin/get_hip4_outcomes@v1",
    "tic://tool/builtin/cross_dex_collision@v1",
    # --- Cross-asset confluence + knowledge ---
    "tic://tool/builtin/find_confluence@v1",
    "tic://tool/builtin/semantic_search@v1",
    # --- Source health + stats ---
    "tic://tool/builtin/query_source_health@v1",
    "tic://tool/builtin/compute_stat@v1",
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
            f"smart_money prompt.md not found at {_PROMPT_PATH}. "
            f"This is a packaging bug — the file must ship with the module."
        )
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"smart_money prompt.md is empty at {_PROMPT_PATH}")
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
            f"smart_money prompt.md is missing required sections: {missing}. "
            f"See talis_desk/specialists/smart_money_v1/prompt.md."
        )
    return text


# ============================================================================
# Builders
# ============================================================================

def build_smart_money_v1() -> SpecialistPersona:
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


def register_smart_money_v1() -> SpecialistState:
    """Idempotent registration. Returns the SpecialistState for the open
    persona row — existing if already registered, new if first call.

    See `base.register_persona` for the idempotency contract:
      - If (specialist_id, persona_version, prompt_hash) already has an
        open row → returns that row unchanged.
      - If specialist_id has an open row at a different version/hash →
        closes the old row (transaction_to=now), inserts new with
        supersedes set, returns new.
    """
    persona = build_smart_money_v1()
    return register_persona(persona)
