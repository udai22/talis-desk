"""Diversity-first scout seed allocation.

The scout swarm should map the whole market-information surface, not let
1,000 cheap agents independently rediscover the same mega-cap catalyst.
This module defines the small deterministic object each scout receives and
the scoring/sampling logic that keeps coverage broad.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional


DEFAULT_HORIZONS = ("intraday", "1d", "7d", "30d", "90d")

DEFAULT_INFORMATION_TYPES = (
    "price_action",
    "volume",
    "order_book",
    "funding",
    "open_interest",
    "options",
    "filings",
    "earnings",
    "headlines",
    "social",
    "onchain",
    "wallet_flow",
    "etf_flow",
    "macro",
    "policy",
    "supply_chain",
    "regulatory",
    "token_unlock",
    "staking_unstake",
    "validator_flow",
    "dev_activity",
    "stablecoin_liquidity",
    "source_library_analog",
)

DEFAULT_LENSES = (
    "momentum",
    "reversal",
    "crowding",
    "catalyst",
    "flow_absorption",
    "dispersion",
    "relative_value",
    "base_rate_anomaly",
    "contrarian",
    "why_now",
)


@dataclass(frozen=True)
class ScoutSeed:
    """One leased market-map cell for a cheap scout to investigate."""

    seed_id: str
    entity: str
    entity_type: str
    horizon: str
    information_type: str
    lens: str
    required_first_tool_intent: str
    anomaly_score: float = 0.0
    novelty_score: float = 0.0
    coverage_gap_score: float = 0.0
    expected_value_score: float = 0.0
    duplicate_similarity_penalty: float = 0.0
    recent_coverage_penalty: float = 0.0
    known_dead_thesis_penalty: float = 0.0
    reserved_until: Optional[datetime] = None
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def score(self) -> float:
        return (
            self.anomaly_score
            + self.novelty_score
            + self.coverage_gap_score
            + self.expected_value_score
            - self.duplicate_similarity_penalty
            - self.recent_coverage_penalty
            - self.known_dead_thesis_penalty
        )


def seed_cell_key(seed: ScoutSeed) -> str:
    """Stable uniqueness cell for active scout leases."""
    return ":".join((
        seed.entity.upper(),
        seed.horizon,
        seed.information_type,
        seed.lens,
    ))


def make_seed_id(
    entity: str,
    horizon: str,
    information_type: str,
    lens: str,
) -> str:
    raw = f"{entity}|{horizon}|{information_type}|{lens}".encode()
    return "seed_" + hashlib.sha256(raw).hexdigest()[:12]


def fingerprint_hypothesis_card(text: str) -> str:
    """Cheap near-duplicate fingerprint for first-pass clustering.

    This is intentionally simple and deterministic. A later version can
    replace it with MinHash/SimHash over normalized shingles.
    """
    normalized = " ".join((text or "").lower().split())
    return "fp_" + hashlib.sha256(normalized.encode()).hexdigest()[:16]


def build_seed(
    *,
    entity: str,
    entity_type: str,
    horizon: str,
    information_type: str,
    lens: str,
    anomaly_score: float = 0.0,
    novelty_score: float = 0.0,
    coverage_gap_score: float = 0.0,
    expected_value_score: float = 0.0,
    duplicate_similarity_penalty: float = 0.0,
    recent_coverage_penalty: float = 0.0,
    known_dead_thesis_penalty: float = 0.0,
    lease_seconds: int = 900,
    payload: Optional[dict[str, Any]] = None,
) -> ScoutSeed:
    return ScoutSeed(
        seed_id=make_seed_id(entity, horizon, information_type, lens),
        entity=entity,
        entity_type=entity_type,
        horizon=horizon,
        information_type=information_type,
        lens=lens,
        required_first_tool_intent=_first_tool_intent(
            entity=entity,
            horizon=horizon,
            information_type=information_type,
            lens=lens,
        ),
        anomaly_score=float(anomaly_score),
        novelty_score=float(novelty_score),
        coverage_gap_score=float(coverage_gap_score),
        expected_value_score=float(expected_value_score),
        duplicate_similarity_penalty=float(duplicate_similarity_penalty),
        recent_coverage_penalty=float(recent_coverage_penalty),
        known_dead_thesis_penalty=float(known_dead_thesis_penalty),
        reserved_until=datetime.now(timezone.utc) + timedelta(seconds=lease_seconds),
        payload=dict(payload or {}),
    )


def _first_tool_intent(
    *,
    entity: str,
    horizon: str,
    information_type: str,
    lens: str,
) -> str:
    return (
        f"Find the freshest {information_type} evidence for {entity} over "
        f"{horizon}, viewed through a {lens} lens."
    )


def allocate_diverse_seeds(
    candidates: Iterable[ScoutSeed],
    *,
    k: int,
    active_cell_keys: Optional[set[str]] = None,
    random_exploration_pct: float = 0.15,
    rng_seed: Optional[int] = None,
) -> list[ScoutSeed]:
    """Weighted sample without replacement while avoiding active duplicates.

    The allocator is deliberately not purely greedy. Greedy selection makes
    every scout chase the same anomaly. Weighted sampling keeps high-score
    ideas likely while preserving enough randomness to map sparse regions.
    """
    active = active_cell_keys or set()
    pool = [s for s in candidates if seed_cell_key(s) not in active]
    if k <= 0 or not pool:
        return []

    rng = random.Random(rng_seed)
    random_n = max(0, min(k, round(k * random_exploration_pct)))
    score_n = max(0, k - random_n)

    selected: list[ScoutSeed] = []
    selected_cells: set[str] = set()

    ranked = sorted(pool, key=lambda s: s.score, reverse=True)
    for seed in ranked:
        if len(selected) >= score_n:
            break
        cell = seed_cell_key(seed)
        if cell in selected_cells:
            continue
        selected.append(seed)
        selected_cells.add(cell)

    remaining = [
        s for s in pool
        if seed_cell_key(s) not in selected_cells
    ]
    rng.shuffle(remaining)
    for seed in remaining:
        if len(selected) >= k:
            break
        cell = seed_cell_key(seed)
        if cell in selected_cells:
            continue
        selected.append(seed)
        selected_cells.add(cell)

    return selected
