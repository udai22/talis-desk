"""Tier 0 — stratified seed generator.

Samples 1000 cells from the (entity x horizon x lens x bias_mode) space
using Latin Hypercube sampling. Applies three modifiers:

  1. Coverage-history penalty — query `coverage_log`; reduce sample
     weight for cells covered recently (within last 72h) that produced
     no downstream publish. Multiplier from REFLECTION_AND_REFACTOR_v5
     §3-3: w *= 0.3 if covered <24h ago, 0.6 if <72h, 1.0 otherwise.
  2. Theme injection — top 10 themes from yesterday's
     `meta/themes_active.json` get 100 dedicated scouts; the remaining
     900 are stratified across the full grid.
  3. Manifold density routing — query `topology_density_map` from the
     prior cycle; bias seeds toward sparse (frontier) regions.

NO STUBS: if `coverage_log` is empty (fresh DB), weights are uniform; if
`topology_density_map` is empty, density routing is skipped. We never
fabricate prior history.
"""
from __future__ import annotations

import json
import logging
import math
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ..store import get_desk_store
from ..coordination import touch_coverage_cell


logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Sampling axes — explicit so the swarm has a stable seed space.
# ----------------------------------------------------------------------

# Default entity universe — mega caps + crypto majors + key macro tickers.
# Override by passing `entities=...` to `generate_seeds`.
DEFAULT_ENTITIES: list[str] = [
    # Mega-caps
    "NVDA", "MSFT", "AAPL", "GOOGL", "AMZN", "META", "TSLA", "AVGO",
    "BRK-B", "JPM", "V", "MA", "UNH", "LLY", "JNJ", "HD", "PG", "XOM",
    "CVX", "WMT",
    # Index proxies + macro
    "SPY", "QQQ", "IWM", "DIA", "DXY", "TLT", "GLD",
    # Crypto / on-chain
    "BTC", "ETH", "SOL", "HYPE",
    # Tier-2 high-vol names
    "COIN", "MSTR", "PLTR", "AMD", "SMCI", "NFLX", "ORCL", "CRM",
]

HORIZONS: list[str] = ["intraday", "1d", "1w", "1m", "1q", "structural"]

LENSES: list[str] = [
    "macro", "microstructure", "options_flow", "smart_money",
    "sentiment", "rotation", "factor", "vol_surface",
    "catalyst", "filing", "polymarket", "anomaly",
    "on_chain", "money_velocity", "structural",
]

CRYPTO_ENTITIES: set[str] = {"BTC", "ETH", "SOL", "HYPE"}
INDEX_MACRO_ENTITIES: set[str] = {"SPY", "QQQ", "IWM", "DIA", "DXY", "TLT", "GLD"}
EQUITY_BLOCKED_LENSES: set[str] = {"on_chain", "smart_money"}
INDEX_BLOCKED_LENSES: set[str] = {"on_chain", "smart_money", "filing"}
CRYPTO_BLOCKED_LENSES: set[str] = {"filing", "factor"}
HL_PERP_BLOCKED_LENSES: set[str] = {"filing"}
_HL_PERP_ENTITY_CACHE: Optional[set[str]] = None

LENS_TOOL_TERMS: dict[str, list[str]] = {
    "macro": ["macro", "econ", "fed", "fomc", "timeseries", "treasury"],
    "microstructure": ["orderbook", "funding", "liquidation", "perp", "depth"],
    "options_flow": ["option", "vol", "skew", "gamma"],
    "smart_money": [
        "wallet", "whale", "flow", "onchain", "leaderboard", "address",
        "cluster", "unstake", "staking", "deposit", "withdrawal",
        "hydromancer", "pnl", "clearinghouse", "builder", "reject",
        "grok", "x_search", "twitter", "screenshot", "social alpha",
    ],
    "sentiment": [
        "news", "sentiment", "headline", "gdelt", "social", "grok",
        "x_search", "twitter", "x.com", "mindshare", "reply", "thread",
    ],
    "rotation": ["rotation", "relative", "rrg", "flow", "sector"],
    "factor": ["factor", "return", "beta", "momentum", "quality"],
    "vol_surface": ["vol", "variance", "skew", "surface"],
    "catalyst": [
        "event", "calendar", "earnings", "filing", "catalyst", "grok",
        "x_search", "twitter", "thread", "reply", "screenshot",
    ],
    "filing": ["sec", "edgar", "filing", "10-k", "13f", "financial"],
    "polymarket": ["polymarket", "prediction", "event", "probability", "grok", "x_search", "twitter"],
    "anomaly": ["anomaly", "outlier", "scan", "novelty", "grok", "x_search", "twitter", "social alpha"],
    "on_chain": [
        "onchain", "wallet", "chain", "token", "holder", "unstake",
        "staking", "unlock", "validator", "deposit", "withdrawal",
        "hydromancer", "clearinghouse", "builder", "reject",
        "grok", "x_search", "twitter", "reply", "thread", "screenshot",
    ],
    "money_velocity": ["m2", "velocity", "liquidity", "fed", "flow"],
    "structural": ["source_library", "framework", "cycle", "structural", "grok", "x_search", "twitter"],
}

BIAS_MODES: list[str] = [
    "contrarian", "consensus_confirm", "frontier", "tail_risk",
    "mean_reversion", "momentum",
]

ALPHA_GEOMETRY_ACTION_TOOL_URI = "tic://tool/talis_native/plan_alpha_geometry_actions@v1"
GROK_X_ALPHA_TOOL_URI = "tic://tool/talis_native/farm_grok_x_alpha@v1"


@dataclass
class SeedCell:
    """One scout's sampled task — what to investigate + how."""
    seed_id: str
    entity: str
    horizon: str
    lens: str
    bias_mode: str
    theme: Optional[str] = None
    weight: float = 1.0
    frontier_boost: float = 1.0
    coverage_penalty: float = 1.0
    payload: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "seed_id": self.seed_id,
            "entity": self.entity,
            "horizon": self.horizon,
            "lens": self.lens,
            "bias_mode": self.bias_mode,
            "asset_class": entity_asset_class(self.entity),
            "theme": self.theme,
            "weight": self.weight,
            "frontier_boost": self.frontier_boost,
            "coverage_penalty": self.coverage_penalty,
            **self.payload,
        }


def entity_asset_class(entity: str) -> str:
    """Coarse asset class used to keep scout lenses physically valid."""
    e = entity.upper()
    if e in _hyperliquid_perp_entities():
        return "hyperliquid_perp"
    if e in CRYPTO_ENTITIES:
        return "crypto"
    if e in INDEX_MACRO_ENTITIES:
        return "index_macro"
    return "equity"


def valid_lenses_for_entity(entity: str) -> list[str]:
    asset_class = entity_asset_class(entity)
    if asset_class == "crypto":
        blocked = CRYPTO_BLOCKED_LENSES
    elif asset_class == "hyperliquid_perp":
        blocked = HL_PERP_BLOCKED_LENSES
    elif asset_class == "index_macro":
        blocked = INDEX_BLOCKED_LENSES
    else:
        blocked = EQUITY_BLOCKED_LENSES
    return [lens for lens in LENSES if lens not in blocked]


def _hyperliquid_perp_entities() -> set[str]:
    global _HL_PERP_ENTITY_CACHE
    if _HL_PERP_ENTITY_CACHE is not None:
        return _HL_PERP_ENTITY_CACHE
    try:
        from ..execution.hl_catalog import list_supported_perps

        _HL_PERP_ENTITY_CACHE = {str(x).upper() for x in list_supported_perps()}
    except Exception:
        _HL_PERP_ENTITY_CACHE = set()
    return _HL_PERP_ENTITY_CACHE


def _resolve_seed_entity_pool() -> list[str]:
    """Default to the curated watchlist plus the live HL tradeable universe."""
    try:
        from ..market_map.universe import build_market_universe

        manifest = build_market_universe(default_entities=DEFAULT_ENTITIES)
        symbols = manifest.entity_symbols()
        if symbols:
            return symbols
    except Exception as exc:
        logger.info("market universe manifest unavailable, using static defaults: %s", exc)
    return list(DEFAULT_ENTITIES)


def _sample_valid_lens(entity: str, rng: random.Random) -> str:
    valid = valid_lenses_for_entity(entity)
    return rng.choice(valid or LENSES)


def _coerce_lens_for_entity(entity: str, lens: str, idx_hint: int = 0) -> str:
    valid = valid_lenses_for_entity(entity)
    if lens in valid:
        return lens
    if not valid:
        return lens
    return valid[idx_hint % len(valid)]


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------

def generate_seeds(
    n_seeds: int = 1000,
    cycle_id: str = "",
    entities: Optional[list[str]] = None,
    themes: Optional[list[str]] = None,
    rng_seed: Optional[int] = None,
    theme_share: float = 0.10,
) -> list[SeedCell]:
    """Generate `n_seeds` stratified scout tasks.

    `themes` (optional) — top themes from yesterday's
    `meta/themes_active.json` (Director output). When provided, the
    first `int(n_seeds * theme_share)` seeds are pinned to these themes.

    `entities` (optional) — entity universe override. Default = the 36
    DEFAULT_ENTITIES above.
    """
    rng = random.Random(rng_seed if rng_seed is not None else int(datetime.now().timestamp()))
    pool = list(entities or _resolve_seed_entity_pool())
    if not pool:
        raise ValueError("entities pool empty")

    coverage = _load_coverage_weights()
    density = _load_density_weights(cycle_id=cycle_id)

    seeds: list[SeedCell] = []
    n_themed = int(n_seeds * max(0.0, min(1.0, theme_share))) if themes else 0
    n_stratified = n_seeds - n_themed

    # ---- Themed seeds (top themes get 100 dedicated scouts) ----
    for i in range(n_themed):
        th = themes[i % len(themes)]
        entity = rng.choice(pool)
        horizon = rng.choice(HORIZONS)
        lens = _sample_valid_lens(entity, rng)
        bias = rng.choice(BIAS_MODES)
        cov = coverage.get((entity, horizon, lens), 1.0)
        dens = density.get((entity, lens), 1.0)
        sid = f"seed_{cycle_id}_T_{i:04d}"
        seeds.append(SeedCell(
            seed_id=sid, entity=entity, horizon=horizon, lens=lens,
            bias_mode=bias, theme=th, weight=cov * dens,
            coverage_penalty=cov, frontier_boost=dens,
            payload={"source": "theme_injection"},
        ))

    # ---- Stratified Latin Hypercube samples ----
    # Build (entity, horizon, lens, bias) Latin Hypercube indices, then
    # apply weight-biased rejection to sample n_stratified.
    grid = _latin_hypercube_indices(n_stratified, len(pool), len(HORIZONS),
                                     len(LENSES), len(BIAS_MODES), rng)
    for i, (ei, hi, li, bi) in enumerate(grid):
        entity = pool[ei]
        horizon = HORIZONS[hi]
        lens = _coerce_lens_for_entity(entity, LENSES[li], li)
        bias = BIAS_MODES[bi]
        cov = coverage.get((entity, horizon, lens), 1.0)
        dens = density.get((entity, lens), 1.0)
        weight = cov * dens
        # Stochastic acceptance: low-weight cells still get sampled, just rarer
        if rng.random() > weight * 0.7 + 0.3:
            # Resample axes once
            entity = rng.choice(pool)
            horizon = rng.choice(HORIZONS)
            lens = _sample_valid_lens(entity, rng)
            bias = rng.choice(BIAS_MODES)
            cov = coverage.get((entity, horizon, lens), 1.0)
            dens = density.get((entity, lens), 1.0)
            weight = cov * dens
        sid = f"seed_{cycle_id}_S_{i:04d}"
        seeds.append(SeedCell(
            seed_id=sid, entity=entity, horizon=horizon, lens=lens,
            bias_mode=bias, weight=weight,
            coverage_penalty=cov, frontier_boost=dens,
            payload={"source": "stratified"},
        ))

    for seed in seeds:
        seed.payload["tool_candidates"] = narrow_tools_for_seed(seed)
    return seeds


def generate_alpha_geometry_route_seeds(
    *,
    cycle_id: str,
    n_seed_budget: int,
    source_cycle_id: Optional[str] = None,
    program: Any = None,
    max_seeds: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[SeedCell]:
    """Turn the previous alpha-geometry shape into next-cycle scout seeds.

    This is the closed loop between "the map screamed" and "the next scout
    allocation changed." It reads persisted geometry routes from the latest
    completed cycle and pins a small, policy-budgeted slice of Tier 0 to
    verify, repair, resolve, or widen those cells.
    """
    db = conn or get_desk_store().conn
    source_cycle = source_cycle_id or _latest_geometry_cycle(exclude_cycle_id=cycle_id, conn=db)
    if not source_cycle:
        return []
    try:
        from ..information_map import load_alpha_geometry, plan_alpha_geometry_actions
    except Exception:
        return []
    try:
        rows = load_alpha_geometry(cycle_id=source_cycle, limit=256, conn=db)
    except Exception:
        return []
    genome = getattr(program, "genome", None) or {}
    routing_thresholds = dict((genome.get("routing_thresholds") if isinstance(genome, dict) else {}) or {})
    geometry_weights = (
        (genome.get("geometry_weights") if isinstance(genome, dict) else None)
        if genome else None
    )
    action_by_cell: dict[str, dict[str, Any]] = {}
    global_shape: dict[str, Any] = {}
    try:
        action_plan = plan_alpha_geometry_actions(
            cycle_id=source_cycle,
            limit=256,
            geometry_weights=geometry_weights,
            routing_thresholds=routing_thresholds,
            conn=db,
        )
        global_shape = (
            action_plan.get("global_shape")
            if isinstance(action_plan.get("global_shape"), dict)
            else {}
        )
        action_by_cell = {
            str(action.get("cell_key") or ""): action
            for action in (action_plan.get("actions") or [])
            if isinstance(action, dict) and action.get("cell_key")
        }
    except Exception as exc:
        logger.debug("alpha geometry action plan unavailable for route seeds: %s", exc)
    route_rows = [
        row for row in rows
        if str(row.get("route_directive") or "observe") != "observe"
    ]
    if not route_rows:
        return []

    budget = _alpha_geometry_seed_budget(
        n_seed_budget=n_seed_budget,
        program=program,
        max_seeds=max_seeds,
    )
    if budget <= 0:
        return []
    route_rows.sort(key=_geometry_route_sort_key, reverse=True)

    out: list[SeedCell] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in route_rows:
        directive = str(row.get("route_directive") or "observe")
        entity = str(row.get("entity") or "UNKNOWN").upper()
        horizon = str(row.get("horizon") or "intraday")
        lens = _coerce_lens_for_entity(entity, str(row.get("lens") or "anomaly"))
        theme = str(row.get("theme") or f"alpha_geometry_{directive}")[:80]
        bias = _bias_for_geometry_route(directive)
        key = (entity, horizon, lens, directive)
        if key in seen:
            continue
        seen.add(key)
        cell_key = str(row.get("cell_key") or "|".join([entity, horizon, lens, theme]))
        metrics = dict(row.get("metrics") or {})
        action = dict(action_by_cell.get(cell_key) or {})
        suggested_tools = [
            str(tool)
            for tool in (action.get("suggested_next_tools") or [])
            if str(tool).strip()
        ]
        seed = SeedCell(
            seed_id=f"seed_{cycle_id}_G_{len(out):04d}_{_short_hash(source_cycle, cell_key, directive)}",
            entity=entity,
            horizon=horizon,
            lens=lens,
            bias_mode=bias,
            theme=theme,
            weight=_geometry_route_weight(directive, metrics),
            frontier_boost=max(1.0, 1.0 + float(metrics.get("frontier_pressure") or 0.0)),
            coverage_penalty=1.0,
            payload={
                "source": "alpha_geometry_route",
                "alpha_geometry_source_cycle_id": source_cycle,
                "alpha_geometry_cell_key": cell_key,
                "alpha_geometry_route_directive": directive,
                "alpha_geometry_trade_scream_score": float(row.get("trade_scream_score") or 0.0),
                "alpha_geometry_verifier_readiness": float(row.get("verifier_readiness") or 0.0),
                "alpha_geometry_metrics": metrics,
                "alpha_geometry_global_shape": global_shape,
                "alpha_geometry_route_task_id": action.get("route_task_id"),
                "alpha_geometry_action": action.get("action"),
                "alpha_geometry_action_owner": action.get("owner"),
                "alpha_geometry_action_priority": action.get("priority_score"),
                "alpha_geometry_action_reason": action.get("reason"),
                "alpha_geometry_success_gate": action.get("success_gate"),
                "alpha_geometry_missing_edges": action.get("missing_edges") or [],
                "alpha_geometry_suggested_next_tools": suggested_tools,
                "why_this_seed_exists": _geometry_route_seed_reason(directive),
            },
        )
        seed.payload["tool_candidates"] = _merge_tool_candidates(
            [ALPHA_GEOMETRY_ACTION_TOOL_URI],
            suggested_tools,
            narrow_tools_for_seed(seed),
            k=24,
        )
        out.append(seed)
        if len(out) >= budget:
            break
    return out


def generate_market_map_governor_seeds(
    *,
    cycle_id: str,
    n_seed_budget: int,
    program: Any = None,
    max_seeds: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
    use_llm: Optional[bool] = None,
    llm_model: Optional[str] = None,
    geometry_cycle_id: Optional[str] = None,
) -> list[SeedCell]:
    """Turn market-map coverage gaps into scout seeds.

    Alpha-geometry route seeds exploit the shape of known strings. Governor
    seeds do the complementary job: explore and repair the finite known market
    lattice where the coverage ledger says we are blind or stale.
    """
    if int(n_seed_budget) <= 0:
        return []
    db = conn or get_desk_store().conn
    budget = _market_governor_seed_budget(
        n_seed_budget=n_seed_budget,
        program=program,
        max_seeds=max_seeds,
    )
    if budget <= 0:
        return []
    genome = getattr(program, "genome", None) or {}
    routing_policy = dict((genome.get("routing_thresholds") if isinstance(genome, dict) else {}) or {})
    if use_llm is None:
        use_llm = bool(routing_policy.get("use_frontier_llm_governor", False))
    try:
        from ..market_map.governor import FRONTIER_LLM_DEFAULT_MODEL, build_market_map_governor_plan
    except Exception:
        return []
    try:
        plan = build_market_map_governor_plan(
            cycle_id=cycle_id,
            conn=db,
            scout_budget=n_seed_budget,
            max_ranked_gaps=max(64, budget * 2),
            max_seed_cells=budget,
            use_llm=bool(use_llm),
            model=llm_model or str(routing_policy.get("frontier_llm_governor_model") or FRONTIER_LLM_DEFAULT_MODEL),
            llm_seed_share=float(routing_policy.get("frontier_llm_governor_seed_share") or 0.25),
            geometry_cycle_id=geometry_cycle_id,
            geometry_weights=(
                (genome.get("geometry_weights") if isinstance(genome, dict) else None)
                if genome else None
            ),
            routing_thresholds=routing_policy,
        )
    except Exception as exc:
        logger.info("market map governor seed generation skipped: %s", exc)
        return []
    raw_cells = [x for x in (plan.get("suggested_seed_cells") or []) if isinstance(x, dict)]
    out: list[SeedCell] = []
    seen: set[tuple[str, str, str, str]] = set()
    for idx, raw in enumerate(raw_cells):
        entity = str(raw.get("entity") or "UNKNOWN").upper()
        horizon = str(raw.get("horizon") or "intraday")
        lens = _coerce_lens_for_entity(entity, str(raw.get("lens") or "anomaly"), idx)
        bias = str(raw.get("bias_mode") or "frontier")
        if bias not in BIAS_MODES:
            bias = "frontier"
        key = (entity, horizon, lens, bias)
        if key in seen:
            continue
        seen.add(key)
        payload = dict(raw.get("payload") or {})
        suggested_tools = [
            str(tool)
            for tool in (payload.get("suggested_tools") or raw.get("suggested_tools") or [])
            if str(tool).strip()
        ]
        seed = SeedCell(
            seed_id=str(raw.get("seed_id") or f"seed_{cycle_id}_M_{len(out):04d}"),
            entity=entity,
            horizon=horizon,
            lens=lens,
            bias_mode=bias,
            theme=str(raw.get("theme") or "market_map_gap_repair"),
            weight=max(0.1, min(4.0, float(raw.get("weight") or 1.0))),
            frontier_boost=max(1.0, min(4.0, float(raw.get("frontier_boost") or 1.0))),
            coverage_penalty=max(0.1, min(1.0, float(raw.get("coverage_penalty") or 1.0))),
            payload={
                **payload,
                "source": "market_map_governor",
                "market_map_governor_schema": plan.get("schema_version"),
                "market_map_governor_status": plan.get("status"),
                "market_map_completion_pressure": plan.get("completion_pressure"),
                "market_map_full_boundary": (plan.get("full_market_definition") or {}).get("boundary"),
                "market_map_known_entity_count": (plan.get("full_market_definition") or {}).get("entity_count"),
                "market_map_valid_cell_count": (plan.get("full_market_definition") or {}).get("valid_cell_count"),
                "market_map_coverage_state": plan.get("coverage_state"),
                "market_map_budget_lanes": plan.get("budget_lanes"),
                "market_map_alpha_geometry_context": plan.get("alpha_geometry_context"),
                "suggested_tools": suggested_tools,
            },
        )
        seed.payload["tool_candidates"] = _merge_tool_candidates(
            suggested_tools,
            narrow_tools_for_seed(seed, k=max(8, min(24, len(suggested_tools) + 6))),
            k=24,
        )
        out.append(seed)
        if len(out) >= budget:
            break
    return out


# ----------------------------------------------------------------------
# Coverage history loading
# ----------------------------------------------------------------------

def _load_coverage_weights() -> dict[tuple[str, str, str], float]:
    """Read coverage_log and return per-cell penalty multipliers.

    Multipliers per REFLECTION_AND_REFACTOR_v5 §3-3:
      - cell covered within last 24h with NO downstream publish -> 0.3
      - cell covered within last 72h with NO downstream publish -> 0.6
      - otherwise -> 1.0
      - cell covered AND downstream-published -> 0.5 (still cover but less)
    """
    out: dict[tuple[str, str, str], float] = {}
    try:
        conn = get_desk_store().conn
    except Exception:
        return out
    try:
        rows = conn.execute(
            "SELECT entity, horizon, lens, last_covered_at, "
            "       downstream_published_at "
            "FROM coverage_log "
            "WHERE last_covered_at >= ? ",
            ((datetime.now(timezone.utc) - timedelta(hours=72)).isoformat(),),
        ).fetchall()
    except sqlite3.OperationalError:
        return out
    now = datetime.now(timezone.utc)
    for r in rows:
        ent = r["entity"]
        hor = r["horizon"]
        lns = r["lens"]
        if not all([ent, hor, lns]):
            continue
        try:
            last = datetime.fromisoformat(r["last_covered_at"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            age_h = (now - last).total_seconds() / 3600.0
        except Exception:
            continue
        published = bool(r["downstream_published_at"])
        if published:
            mult = 0.5
        elif age_h < 24:
            mult = 0.3
        elif age_h < 72:
            mult = 0.6
        else:
            mult = 1.0
        key = (ent, hor, lns)
        # Multiplicative composition for repeated covers.
        out[key] = min(out.get(key, 1.0), mult)
    return out


# ----------------------------------------------------------------------
# Manifold density loading
# ----------------------------------------------------------------------

def _load_density_weights(cycle_id: str = "") -> dict[tuple[str, str], float]:
    """Read most recent topology_density_map snapshot and return
    per-(entity, lens) frontier-boost multipliers.

    Frontier regions (low density) get >1.0 boost (over-sample). Dense
    (consensus) regions get <1.0 (under-sample). Default 1.0 when no
    snapshot is available.
    """
    out: dict[tuple[str, str], float] = {}
    try:
        conn = get_desk_store().conn
    except Exception:
        return out
    try:
        rows = conn.execute(
            "SELECT region_id, projection_view, density, is_frontier, "
            "       member_hypothesis_ids, label "
            "FROM topology_density_map "
            "ORDER BY transaction_from DESC LIMIT 200"
        ).fetchall()
    except sqlite3.OperationalError:
        return out
    # Label parsing: we expect labels in `<entity>:<lens>` form when the
    # topology engine has projected through that view. Otherwise skip.
    for r in rows:
        label = r["label"] or ""
        if ":" not in label:
            continue
        ent, lns = label.split(":", 1)
        density = float(r["density"] or 0.5)
        is_frontier = bool(r["is_frontier"])
        # Boost: frontier 1.5; consensus 0.7; mid 1.0
        if is_frontier:
            mult = 1.5
        elif density >= 0.7:
            mult = 0.7
        else:
            mult = 1.0
        key = (ent.strip(), lns.strip())
        out[key] = min(out.get(key, 1.0), mult) if mult < 1.0 else max(out.get(key, 1.0), mult)
    return out


def _latest_geometry_cycle(
    *,
    exclude_cycle_id: str,
    conn: sqlite3.Connection,
) -> Optional[str]:
    try:
        row = conn.execute(
            """
            SELECT cycle_id, MAX(created_at) AS last_created_at
            FROM information_geometry_snapshots
            WHERE transaction_to IS NULL
              AND cycle_id != ?
            GROUP BY cycle_id
            ORDER BY last_created_at DESC
            LIMIT 1
            """,
            (exclude_cycle_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return str(row["cycle_id"] if hasattr(row, "keys") else row[0])


def _alpha_geometry_seed_budget(
    *,
    n_seed_budget: int,
    program: Any = None,
    max_seeds: Optional[int] = None,
) -> int:
    if max_seeds is not None:
        return max(0, min(128, int(max_seeds)))
    if int(n_seed_budget) <= 0:
        return 0
    genome = getattr(program, "genome", None) or {}
    routing = dict((genome.get("routing_thresholds") if isinstance(genome, dict) else {}) or {})
    exploit_share = _bounded_share(routing.get("frontier_exploitation_budget_share"), default=0.04)
    explore_share = _bounded_share(routing.get("coverage_exploration_budget_share"), default=0.04)
    share = max(0.02, min(0.18, exploit_share + explore_share))
    return max(1, min(96, int(round(max(0, int(n_seed_budget)) * share))))


def _market_governor_seed_budget(
    *,
    n_seed_budget: int,
    program: Any = None,
    max_seeds: Optional[int] = None,
) -> int:
    if max_seeds is not None:
        return max(0, min(256, int(max_seeds)))
    if int(n_seed_budget) <= 0:
        return 0
    genome = getattr(program, "genome", None) or {}
    routing = dict((genome.get("routing_thresholds") if isinstance(genome, dict) else {}) or {})
    tool_policy = dict((genome.get("tool_request_policy") if isinstance(genome, dict) else {}) or {})
    coverage_share = _bounded_share(
        routing.get("coverage_gap_budget_share"),
        default=0.10,
    )
    repair_share = _bounded_share(
        tool_policy.get("source_repair_budget_share"),
        default=0.04,
    )
    share = max(0.04, min(0.24, coverage_share + repair_share))
    return max(1, min(160, int(round(max(0, int(n_seed_budget)) * share))))


def _bounded_share(raw: Any, *, default: float) -> float:
    try:
        value = float(raw)
    except Exception:
        value = default
    return max(0.0, min(0.30, value))


def _geometry_route_sort_key(row: dict[str, Any]) -> tuple[float, float, float, float]:
    directive = str(row.get("route_directive") or "")
    metrics = dict(row.get("metrics") or {})
    priority = {
        "verify_now": 5.0,
        "resolve_tension": 4.0,
        "repair_sources": 3.5,
        "widen_sources": 3.0,
        "widen_scouts": 2.5,
    }.get(directive, 0.0)
    return (
        priority,
        float(row.get("trade_scream_score") or metrics.get("trade_scream_score") or 0.0),
        float(row.get("verifier_readiness") or metrics.get("verifier_readiness") or 0.0),
        float(metrics.get("frontier_pressure") or 0.0),
    )


def _bias_for_geometry_route(directive: str) -> str:
    return {
        "verify_now": "consensus_confirm",
        "repair_sources": "tail_risk",
        "resolve_tension": "contrarian",
        "widen_sources": "frontier",
        "widen_scouts": "frontier",
    }.get(directive, "frontier")


def _geometry_route_weight(directive: str, metrics: dict[str, Any]) -> float:
    base = {
        "verify_now": 3.0,
        "resolve_tension": 2.6,
        "repair_sources": 2.4,
        "widen_sources": 2.1,
        "widen_scouts": 1.8,
    }.get(directive, 1.5)
    return round(base + min(1.0, float(metrics.get("trade_scream_score") or 0.0)), 4)


def _geometry_route_seed_reason(directive: str) -> str:
    return {
        "verify_now": "Prior alpha geometry crossed the verifier gate; allocate a scout to gather final source-backed confirmation.",
        "repair_sources": "Prior alpha geometry was fragile; allocate a scout to repair evidence and source independence before spend-heavy verification.",
        "resolve_tension": "Prior alpha geometry found contradiction; allocate a scout to resolve the market mechanism before promotion.",
        "widen_sources": "Prior alpha geometry had frontier pressure with weak source independence; allocate a scout to find missing source families.",
        "widen_scouts": "Prior alpha geometry was thin but novel; allocate another scout to test whether the cell is real.",
    }.get(directive, "Prior alpha geometry requested more attention for this market cell.")


def _short_hash(*parts: str) -> str:
    import hashlib

    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:8]


# ----------------------------------------------------------------------
# Latin Hypercube sampler
# ----------------------------------------------------------------------

def _latin_hypercube_indices(
    n: int, n_e: int, n_h: int, n_l: int, n_b: int, rng: random.Random,
) -> list[tuple[int, int, int, int]]:
    """Return n samples from a 4-D Latin Hypercube over the index space.

    LHS guarantees each axis is sampled at every quantile at least once
    when n >= the axis cardinality. When n < axis cardinality, axis is
    sampled uniformly without replacement up to its cardinality, then
    with replacement (since we can't avoid it).
    """
    def _axis_samples(axis_size: int) -> list[int]:
        # Permute 0..axis_size-1 then tile up to n. Shuffle final ordering.
        if axis_size == 0:
            return [0] * n
        reps = (n + axis_size - 1) // axis_size
        seq: list[int] = []
        for _ in range(reps):
            block = list(range(axis_size))
            rng.shuffle(block)
            seq.extend(block)
        seq = seq[:n]
        rng.shuffle(seq)
        return seq

    e = _axis_samples(n_e)
    h = _axis_samples(n_h)
    l_ = _axis_samples(n_l)
    b = _axis_samples(n_b)
    return list(zip(e, h, l_, b))


# ----------------------------------------------------------------------
# Theme loading helper (called by Tier 0 entry point in the swarm)
# ----------------------------------------------------------------------

def load_themes_from_meta() -> list[str]:
    """Best-effort read of `~/.talis/storage/meta/themes_active.json`.

    Returns [] if the file doesn't exist (first cycle) or can't be parsed.
    """
    candidates = [
        Path.home() / ".talis" / "storage" / "meta" / "themes_active.json",
        Path.home() / ".talis" / "meta" / "themes_active.json",
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text())
            if isinstance(data, list):
                return [str(x) for x in data][:10]
            if isinstance(data, dict) and "themes" in data:
                return [str(x) for x in (data["themes"] or [])][:10]
        except Exception as e:
            logger.warning("themes load %s failed: %s", p, e)
    return []


def record_coverage(
    cycle_id: str,
    seed: SeedCell,
    scout_id: str,
    published: bool = False,
) -> None:
    """Stamp a coverage_log row after a scout output is produced."""
    try:
        conn = get_desk_store().conn
    except Exception:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            "INSERT INTO coverage_log "
            "(entity, horizon, lens, theme, cycle_id, scout_id, scout_count, "
            " downstream_published_at, last_covered_at, payload, "
            " valid_from, transaction_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                seed.entity, seed.horizon, seed.lens, seed.theme,
                cycle_id, scout_id, 1,
                now_iso if published else None,
                now_iso,
                json.dumps({"weight": seed.weight, "bias_mode": seed.bias_mode}),
                now_iso, now_iso,
            ),
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        logger.warning("coverage_log insert failed: %s", e)
    try:
        touch_coverage_cell(
            entity=seed.entity,
            horizon=seed.horizon,
            lens=seed.lens,
            source="tier1_scout",
            bias_mode=seed.bias_mode,
            theme=seed.theme,
            promoted=published,
            novelty_score=min(1.0, float(seed.weight)),
            density_score=1.0 / max(0.01, float(seed.frontier_boost)),
            payload={
                "cycle_id": cycle_id,
                "seed_id": seed.seed_id,
                "scout_id": scout_id,
                "seed_source": (seed.payload or {}).get("source"),
                "gap_id": (seed.payload or {}).get("gap_id"),
                "market_map_completion_pressure": (seed.payload or {}).get("market_map_completion_pressure"),
                "missing_surfaces": (seed.payload or {}).get("missing_surfaces"),
                "expected_edges": (seed.payload or {}).get("expected_edges"),
            },
            conn=conn,
        )
    except Exception as e:
        logger.debug("coverage_cells upsert failed: %s", e)


def narrow_tools_for_seed(seed: SeedCell, k: Optional[int] = None) -> list[str]:
    """Pick a small real-tool menu from the atlas for this seed.

    This is the first implementation of dynamic tool narrowing. It is simple
    lexical retrieval by design: cheap, deterministic, and inspectable. The
    verifier should reject any scout that invents tools outside this menu.
    """
    if k is None:
        try:
            k = int((seed.payload or {}).get("tool_candidate_limit") or 0)
        except Exception:
            k = 0
    if not k:
        try:
            from ..information_map.market_evolve import load_active_market_evolve_program

            program = load_active_market_evolve_program()
            policy = (program.genome or {}).get("tool_request_policy") or {}
            k = int(policy.get("max_tool_candidates_per_seed") or 8)
        except Exception:
            k = 8
    k = max(4, min(24, int(k)))
    try:
        conn = get_desk_store().conn
        rows = conn.execute(
            """
            SELECT tool_uri, tool_name, description, provider, kind, source_dependencies
            FROM tool_atlas
            WHERE transaction_to IS NULL
              AND status = 'active'
              AND tool_uri LIKE 'tic://tool/%'
            """
        ).fetchall()
    except Exception:
        return []
    terms = [
        seed.entity.lower(),
        seed.horizon.lower(),
        seed.lens.lower(),
        seed.bias_mode.lower(),
    ]
    if seed.theme:
        terms.append(seed.theme.lower())
    terms.extend(LENS_TOOL_TERMS.get(seed.lens, []))
    payload = seed.payload or {}
    source_family_targets_raw = payload.get("source_family_targets") or []
    if isinstance(source_family_targets_raw, str):
        source_family_targets_raw = [source_family_targets_raw]
    source_family_targets = {
        str(x).lower()
        for x in source_family_targets_raw
        if str(x or "").strip()
    }
    shape_guided_slice = _seed_needs_shape_reader(seed)
    prefer_learned_tools = bool(payload.get("prefer_learned_tools"))
    try:
        learned_tool_priority_boost = float(payload.get("learned_tool_priority_boost") or 0.0)
    except Exception:
        learned_tool_priority_boost = 0.0
    crypto_node_slice = (
        seed.entity in CRYPTO_ENTITIES
        and (
            seed.lens in {"on_chain", "smart_money", "microstructure", "money_velocity"}
            or any(tok in str(seed.theme or "").lower() for tok in (
                "unstake", "staking", "unlock", "validator", "wallet", "flow", "cex",
            ))
        )
    )
    grok_x_slice = (
        seed.lens in {
            "sentiment",
            "catalyst",
            "polymarket",
            "anomaly",
            "on_chain",
            "smart_money",
            "structural",
            "microstructure",
        }
        or "grok_x_alpha" in source_family_targets
        or any(tok in str(seed.theme or "").lower() for tok in (
            "twitter",
            "x.com",
            "grok",
            "mindshare",
            "social alpha",
        ))
    )
    scored: list[tuple[float, str]] = []
    for r in rows:
        uri = str(r["tool_uri"])
        hay = " ".join(
            str(r[x] or "")
            for x in ("tool_uri", "tool_name", "description", "provider", "kind", "source_dependencies")
        ).lower()
        score = 0.0
        for term in terms:
            if not term:
                continue
            if term in hay:
                score += 3.0 if term == seed.entity.lower() else 1.0
        if "query_timeseries" in uri:
            score += 0.5
        if "query_claims_by_entity" in uri:
            score += 0.4
        if "source_health" in uri:
            score += 0.2
        if crypto_node_slice and "/hydromancer/" in uri:
            score += 4.0
        if crypto_node_slice and any(name in uri for name in (
            "get_hl_pnl_leaderboard",
            "get_builder_fills",
            "get_wallet_historical_orders",
            "batch_get_clearinghouse_states",
            "hydromancer_query",
        )):
            score += 2.0
        if shape_guided_slice and uri == ALPHA_GEOMETRY_ACTION_TOOL_URI:
            score += 20.0
        if grok_x_slice and uri == GROK_X_ALPHA_TOOL_URI:
            score += 7.0
        if str(r["kind"] or "") == "learned":
            for target in source_family_targets:
                if target in hay:
                    score += 2.0
            if prefer_learned_tools:
                score += max(1.0, min(4.0, learned_tool_priority_boost or 1.0))
        if score > 0:
            scored.append((score, uri))
    scored.sort(key=lambda x: (-x[0], x[1]))
    tools = []
    seen = set()
    for _, uri in scored:
        if uri in seen:
            continue
        seen.add(uri)
        tools.append(uri)
        if len(tools) >= k:
            break
    if tools:
        return _ensure_shape_reader_candidate(tools, rows=rows, seed=seed, k=k)
    # Fallback to universal audit-safe primitives if lexical retrieval misses.
    fallback_names = (
        "query_timeseries",
        "query_claims_by_entity",
        "get_hl_pnl_leaderboard",
        "get_builder_fills",
        "query_source_health",
        "get_econ_event_today",
    )
    for r in rows:
        uri = str(r["tool_uri"])
        if any(name in uri for name in fallback_names):
            tools.append(uri)
        if len(tools) >= k:
            break
    return _ensure_shape_reader_candidate(tools, rows=rows, seed=seed, k=k)


def _seed_needs_shape_reader(seed: SeedCell) -> bool:
    payload = seed.payload or {}
    source = str(payload.get("source") or "")
    return (
        source in {"alpha_geometry_route", "market_map_governor", "frontier_llm_governor"}
        or bool(payload.get("alpha_geometry_route_directive"))
        or bool(payload.get("market_map_alpha_geometry_context"))
    )


def _ensure_shape_reader_candidate(
    tools: list[str],
    *,
    rows: list[Any],
    seed: SeedCell,
    k: int,
) -> list[str]:
    if not _seed_needs_shape_reader(seed):
        return tools[:k]
    active_uris = {str(row["tool_uri"]) for row in rows}
    if ALPHA_GEOMETRY_ACTION_TOOL_URI not in active_uris:
        return tools[:k]
    return _merge_tool_candidates([ALPHA_GEOMETRY_ACTION_TOOL_URI], tools, k=k)


def _merge_tool_candidates(*groups: list[str], k: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for raw in group:
            uri = str(raw or "").strip()
            if not uri or uri in seen:
                continue
            seen.add(uri)
            out.append(uri)
            if len(out) >= k:
                return out
    return out


# Back-compat for any callers that imported the private name while v5 was
# being assembled.
_narrow_tools_for_seed = narrow_tools_for_seed
