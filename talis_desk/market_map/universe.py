"""Market universe manifests for scout coverage.

The seed grid should not pretend that a hand-maintained ticker list is the
whole market. This module builds an explicit universe manifest from static
watchlists plus dynamic tradeable venues such as Hyperliquid perps.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


@dataclass(frozen=True)
class MarketUniverseEntity:
    symbol: str
    entity_type: str
    venue: str
    source: str
    source_quality: str
    tradable: bool = True
    seed_eligible: bool = True
    asset_id: int | None = None
    max_leverage: int | None = None
    is_hip3: bool = False
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketUniverseManifest:
    generated_at: str
    entities: tuple[MarketUniverseEntity, ...]
    source_quality: str
    source_counts: dict[str, int]
    errors: tuple[str, ...] = ()

    def entity_symbols(self) -> list[str]:
        return [entity.symbol for entity in self.entities if entity.seed_eligible]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "source_quality": self.source_quality,
            "source_counts": dict(self.source_counts),
            "errors": list(self.errors),
            "entities": [asdict(entity) for entity in self.entities],
            "seed_eligible_symbols": self.entity_symbols(),
        }


def build_market_universe(
    *,
    default_entities: Iterable[str] = (),
    include_hyperliquid: bool = True,
    max_hyperliquid_entities: int | None = None,
) -> MarketUniverseManifest:
    """Return a deduped universe manifest for seed generation and audit.

    Hyperliquid is treated as an authoritative tradeable venue when its live
    metadata endpoint is reachable. If the catalog is in snapshot mode, the
    manifest remains usable but carries `source_quality=snapshot_only`.
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    entities: dict[str, MarketUniverseEntity] = {}
    errors: list[str] = []

    for symbol in default_entities:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            continue
        entities.setdefault(
            normalized,
            MarketUniverseEntity(
                symbol=normalized,
                entity_type=_static_entity_type(normalized),
                venue="watchlist",
                source="static_default_entities",
                source_quality="curated",
                tradable=True,
                seed_eligible=True,
            ),
        )

    catalog_quality = "not_requested"
    if include_hyperliquid:
        try:
            from ..execution.hl_catalog import catalog_source, list_perp_specs

            specs = list_perp_specs()
            catalog_quality = catalog_source()
            if max_hyperliquid_entities is not None:
                specs = specs[: max(0, int(max_hyperliquid_entities))]
            for spec in specs:
                key = spec.coin.upper()
                existing = entities.get(key)
                payload = {
                    "name": spec.name,
                    "sz_decimals": spec.sz_decimals,
                    "tick_size": spec.tick_size,
                    "min_notional_usd": spec.min_notional_usd,
                    "dex": spec.dex,
                }
                entities[key] = MarketUniverseEntity(
                    symbol=spec.coin,
                    entity_type="hyperliquid_perp",
                    venue="hyperliquid",
                    source="hyperliquid_info_meta",
                    source_quality=spec.source,
                    tradable=True,
                    seed_eligible=True,
                    asset_id=spec.asset_id,
                    max_leverage=spec.max_leverage,
                    is_hip3=spec.is_hip3,
                    payload={
                        **(existing.payload if existing else {}),
                        **payload,
                        "also_in_static_watchlist": bool(existing),
                    },
                )
        except Exception as exc:
            catalog_quality = "unavailable"
            errors.append(f"hyperliquid_universe_failed:{type(exc).__name__}")

    source_counts: dict[str, int] = {}
    for entity in entities.values():
        source_counts[entity.source] = source_counts.get(entity.source, 0) + 1

    quality_rank = {
        "live": 4,
        "curated": 3,
        "snapshot_only": 2,
        "fallback": 2,
        "not_requested": 1,
        "unavailable": 0,
    }
    source_quality = catalog_quality
    if not include_hyperliquid:
        source_quality = "curated"
    elif catalog_quality not in quality_rank:
        source_quality = str(catalog_quality or "unknown")

    sorted_entities = tuple(sorted(entities.values(), key=lambda ent: (ent.venue, ent.symbol.upper())))
    return MarketUniverseManifest(
        generated_at=generated_at,
        entities=sorted_entities,
        source_quality=source_quality,
        source_counts=source_counts,
        errors=tuple(errors),
    )


def _static_entity_type(symbol: str) -> str:
    if symbol in {"BTC", "ETH", "SOL", "HYPE"}:
        return "crypto"
    if symbol in {"SPY", "QQQ", "IWM", "DIA", "DXY", "TLT", "GLD"}:
        return "index_macro"
    return "equity"


__all__ = [
    "MarketUniverseEntity",
    "MarketUniverseManifest",
    "build_market_universe",
]
