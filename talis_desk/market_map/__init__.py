"""Market information-map primitives.

This package owns the scout-swarm allocation layer: how the desk decides
which corners of the market-information surface to probe next.
"""

from .seeds import (
    DEFAULT_HORIZONS,
    DEFAULT_INFORMATION_TYPES,
    DEFAULT_LENSES,
    ScoutSeed,
    allocate_diverse_seeds,
    seed_cell_key,
)
from .universe import (
    MarketUniverseEntity,
    MarketUniverseManifest,
    build_market_universe,
)
from .self_healing import (
    MarketMapWorkOrder,
    build_market_map_self_healing_plan,
    execute_market_map_self_healing_tasks,
    post_market_map_self_healing_work_orders,
    render_market_map_self_healing_markdown,
)
from .coverage_audit import build_coverage_gap_manifest
from .governor import (
    FRONTIER_LLM_DEFAULT_MODEL,
    GOVERNOR_SCHEMA_VERSION,
    RankedMarketGap,
    ScoutBudgetLane,
    build_market_map_governor_plan,
    render_market_map_governor_markdown,
)

__all__ = [
    "DEFAULT_HORIZONS",
    "DEFAULT_INFORMATION_TYPES",
    "DEFAULT_LENSES",
    "ScoutSeed",
    "allocate_diverse_seeds",
    "MarketUniverseEntity",
    "MarketUniverseManifest",
    "build_market_universe",
    "GOVERNOR_SCHEMA_VERSION",
    "FRONTIER_LLM_DEFAULT_MODEL",
    "RankedMarketGap",
    "ScoutBudgetLane",
    "build_market_map_governor_plan",
    "render_market_map_governor_markdown",
    "MarketMapWorkOrder",
    "build_market_map_self_healing_plan",
    "execute_market_map_self_healing_tasks",
    "post_market_map_self_healing_work_orders",
    "render_market_map_self_healing_markdown",
    "build_coverage_gap_manifest",
    "seed_cell_key",
]
