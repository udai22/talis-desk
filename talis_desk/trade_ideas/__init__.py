"""Trade Idea Pipeline — Layer 4 of the SOTA Desk Architecture v2.

The trade idea is the primary artifact of the desk (see
wiki/SOTA_DESK_ARCHITECTURE.md §2 lines 109-137, and §7). Briefs explain
the book; they are not the product.

Public API:
  - TradeIdea / TradeIdeaDraft       — the canonical pydantic shapes
  - SizingPlan, EntryPlan, StopPlan, TargetPlan, ContradictionItem — sub-models
  - validate_trade_idea(idea)        — hard-gates per v2 lines 130-134
  - emit_trade_idea(draft, context)  — persist to desk.db + post to trade_book
"""
from .candidates import (
    BlockedIdea,
    WatchlistSetup,
    emit_blocked_idea,
    emit_watchlist_setup,
    fetch_blocked_ideas_for_cycle,
    fetch_watchlist_setups_for_cycle,
)
from .model import (
    TradeIdea,
    TradeIdeaDraft,
    SizingPlan,
    EntryPlan,
    StopPlan,
    TargetPlan,
    ContradictionItem,
    ValidationReport,
    validate_trade_idea,
    emit_trade_idea,
    MARKET_ASSUMPTION_VALUES,
    TIME_HORIZON_HOURS,
    MIN_RISK_PCT,
    MAX_RISK_PCT,
    MAX_KELLY_FRACTION,
    MAX_LEVERAGE_CAP,
    CONFIDENCE_REQUIRING_CONTRADICTION,
)

__all__ = [
    "TradeIdea",
    "TradeIdeaDraft",
    "SizingPlan",
    "EntryPlan",
    "StopPlan",
    "TargetPlan",
    "ContradictionItem",
    "ValidationReport",
    "validate_trade_idea",
    "emit_trade_idea",
    "MARKET_ASSUMPTION_VALUES",
    "TIME_HORIZON_HOURS",
    "MIN_RISK_PCT",
    "MAX_RISK_PCT",
    "MAX_KELLY_FRACTION",
    "MAX_LEVERAGE_CAP",
    "CONFIDENCE_REQUIRING_CONTRADICTION",
    # Codex finding #7 — richer artifact types beyond binary supported.
    "WatchlistSetup",
    "BlockedIdea",
    "emit_watchlist_setup",
    "emit_blocked_idea",
    "fetch_watchlist_setups_for_cycle",
    "fetch_blocked_ideas_for_cycle",
]
