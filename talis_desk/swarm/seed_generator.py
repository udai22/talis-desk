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

LENS_TOOL_TERMS: dict[str, list[str]] = {
    "macro": ["macro", "econ", "fed", "fomc", "timeseries", "treasury"],
    "microstructure": ["orderbook", "funding", "liquidation", "perp", "depth"],
    "options_flow": ["option", "vol", "skew", "gamma"],
    "smart_money": ["wallet", "whale", "flow", "onchain", "leaderboard"],
    "sentiment": ["news", "sentiment", "headline", "gdelt", "social"],
    "rotation": ["rotation", "relative", "rrg", "flow", "sector"],
    "factor": ["factor", "return", "beta", "momentum", "quality"],
    "vol_surface": ["vol", "variance", "skew", "surface"],
    "catalyst": ["event", "calendar", "earnings", "filing", "catalyst"],
    "filing": ["sec", "edgar", "filing", "10-k", "13f", "financial"],
    "polymarket": ["polymarket", "prediction", "event", "probability"],
    "anomaly": ["anomaly", "outlier", "scan", "novelty"],
    "on_chain": ["onchain", "wallet", "chain", "token", "holder"],
    "money_velocity": ["m2", "velocity", "liquidity", "fed", "flow"],
    "structural": ["source_library", "framework", "cycle", "structural"],
}

BIAS_MODES: list[str] = [
    "contrarian", "consensus_confirm", "frontier", "tail_risk",
    "mean_reversion", "momentum",
]


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
            "theme": self.theme,
            "weight": self.weight,
            "frontier_boost": self.frontier_boost,
            "coverage_penalty": self.coverage_penalty,
            **self.payload,
        }


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
    pool = list(entities or DEFAULT_ENTITIES)
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
        lens = rng.choice(LENSES)
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
        lens = LENSES[li]
        bias = BIAS_MODES[bi]
        cov = coverage.get((entity, horizon, lens), 1.0)
        dens = density.get((entity, lens), 1.0)
        weight = cov * dens
        # Stochastic acceptance: low-weight cells still get sampled, just rarer
        if rng.random() > weight * 0.7 + 0.3:
            # Resample axes once
            entity = rng.choice(pool)
            horizon = rng.choice(HORIZONS)
            lens = rng.choice(LENSES)
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
            payload={"cycle_id": cycle_id, "seed_id": seed.seed_id, "scout_id": scout_id},
            conn=conn,
        )
    except Exception as e:
        logger.debug("coverage_cells upsert failed: %s", e)


def narrow_tools_for_seed(seed: SeedCell, k: int = 8) -> list[str]:
    """Pick a small real-tool menu from the atlas for this seed.

    This is the first implementation of dynamic tool narrowing. It is simple
    lexical retrieval by design: cheap, deterministic, and inspectable. The
    verifier should reject any scout that invents tools outside this menu.
    """
    try:
        conn = get_desk_store().conn
        rows = conn.execute(
            """
            SELECT tool_uri, tool_name, description, provider, kind
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
    scored: list[tuple[float, str]] = []
    for r in rows:
        uri = str(r["tool_uri"])
        hay = " ".join(
            str(r[x] or "") for x in ("tool_uri", "tool_name", "description", "provider", "kind")
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
        return tools
    # Fallback to universal audit-safe primitives if lexical retrieval misses.
    fallback_names = (
        "query_timeseries",
        "query_claims_by_entity",
        "query_source_health",
        "get_econ_event_today",
    )
    for r in rows:
        uri = str(r["tool_uri"])
        if any(name in uri for name in fallback_names):
            tools.append(uri)
        if len(tools) >= k:
            break
    return tools


# Back-compat for any callers that imported the private name while v5 was
# being assembled.
_narrow_tools_for_seed = narrow_tools_for_seed
