"""Operating cadence for the Talis intelligence loop.

The desk has two jobs:

1. Produce brief-grade full runs on a human cadence.
2. Keep cheap Flash scouts awake between those runs so information/price
   divergence is never discovered only after consensus has moved.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def default_intelligence_cadence_policy(*, generated_at: str | None = None) -> dict[str, Any]:
    """Return the production-intent cadence as a serializable policy."""
    return {
        "schema_version": "talis_intelligence_cadence_policy_v1",
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "full_pipeline": {
            "mode": "brief_grade_full_pass",
            "cadence": "twice_daily",
            "windows_local": ["morning_brief", "evening_reconciliation"],
            "purpose": (
                "Run the full scout, synthesis, verifier, specialist, evolution, "
                "and brief composition loop for durable daily-brief output."
            ),
            "default_shape": {
                "scouts": 1000,
                "requires_explicit_spend_gate": True,
                "compose_daily_brief": True,
                "write_manifest": True,
            },
        },
        "always_on_flash": {
            "mode": "continuous_sentinel",
            "cadence": "rolling",
            "interval_minutes": 5,
            "scouts_per_tick": {"min": 8, "target": 24, "max": 64},
            "purpose": (
                "Continuously compare new information strings against price, "
                "attention, node, and source geometry so the map notices early."
            ),
            "default_shape": {
                "max_tool_iterations": 1,
                "prefer_read_only_tools": True,
                "no_verifier_spend_unless_triggered": True,
                "aggregate_into_next_full_brief": True,
            },
        },
        "sentinel_triggers": [
            {
                "id": "price_information_divergence",
                "watch": "information_pressure rises while price has not yet moved",
                "action": "widen scouts, attach market_state, then queue verifier if pressure persists",
            },
            {
                "id": "fresh_social_alpha",
                "watch": "Grok/X finds credible early posts, screenshots, or reply-chain confirmations",
                "action": "cross-check with Parallel/web, Hydromancer, node, and source health",
            },
            {
                "id": "node_or_mempool_edge",
                "watch": "node, Hydromancer, builder, route, or pending-intent edge changes",
                "action": "spawn focused actor-route scouts and preserve graph edge receipts",
            },
            {
                "id": "map_gap_self_heal",
                "watch": "geometry shows sparse source families, stale citations, or missing edges",
                "action": "assign patch scouts or tool-creation proposals before the next full run",
            },
        ],
        "daily_brief_contract": {
            "brief_reads": [
                "latest_full_pipeline_synthesis",
                "always_on_flash_sentinel_deltas",
                "price_vs_information_divergence",
                "verified_trade_candidates",
                "unresolved_gaps_and_watchlist",
            ],
            "operator_promise": (
                "The brief is not a single snapshot. It is the twice-daily full "
                "pass plus the continuously maintained market memory since the last pass."
            ),
        },
    }


__all__ = ["default_intelligence_cadence_policy"]
