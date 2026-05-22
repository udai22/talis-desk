"""Price observation sources for information-vs-price learning.

The always-on scout layer needs a cheap external feedback signal: what did
price do after a string claimed pressure was building? This module keeps the
first price surface deliberately boring and auditable: Hyperliquid ``allMids``
from a configured read chain, normalized into ``PriceObservation`` rows.
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from .outcomes import PriceObservation


DEFAULT_HL_INFO_URL = "https://api.hyperliquid.xyz/info"


@dataclass
class PriceObservationBatch:
    observed_at: str
    source: str
    endpoint: str
    requested_entities: list[str]
    resolved_entities: list[str] = field(default_factory=list)
    missing_entities: list[str] = field(default_factory=list)
    observations: list[PriceObservation] = field(default_factory=list)
    quality_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "observations": [asdict(obs) for obs in self.observations],
        }


FetchAllMids = Callable[[str, float], dict[str, Any]]


def collect_hyperliquid_mid_price_observations(
    entities: Iterable[str],
    *,
    observed_at: str | None = None,
    timeout_s: float = 8.0,
    sources: Optional[list[tuple[str, str]]] = None,
    fetch_all_mids: FetchAllMids | None = None,
) -> PriceObservationBatch:
    """Collect current Hyperliquid mids for the requested entities.

    ``sources`` is ordered ``[(source_name, info_url)]``. The first successful
    source wins. This lets production prefer our node or Hydromancer while
    preserving the official public API as the boring fallback.
    """
    requested = _dedupe_entities(entities)
    ts = _normalize_time(observed_at or datetime.now(timezone.utc).isoformat())
    chain = sources if sources is not None else default_hyperliquid_price_sources()
    fetcher = fetch_all_mids or fetch_hyperliquid_all_mids
    errors: list[str] = []
    if not requested:
        return PriceObservationBatch(
            observed_at=ts,
            source="none",
            endpoint="",
            requested_entities=[],
            quality_flags=["no_entities_requested"],
        )
    for source_name, url in chain:
        endpoint = _normalize_info_url(url)
        try:
            mids = fetcher(endpoint, float(timeout_s))
        except Exception as exc:
            errors.append(f"{source_name}:{type(exc).__name__}")
            continue
        if not isinstance(mids, dict) or not mids:
            errors.append(f"{source_name}:empty_all_mids")
            continue
        observations, missing = _observations_from_all_mids(
            mids,
            requested=requested,
            observed_at=ts,
            source_name=source_name,
            endpoint=endpoint,
        )
        flags = [f"source_error:{err}" for err in errors[:4]]
        if missing:
            flags.append("missing_requested_entities")
        if not observations:
            flags.append("no_requested_entity_prices")
        return PriceObservationBatch(
            observed_at=ts,
            source=source_name,
            endpoint=endpoint,
            requested_entities=requested,
            resolved_entities=[obs.entity for obs in observations],
            missing_entities=missing,
            observations=observations,
            quality_flags=sorted(set(flags)),
        )
    return PriceObservationBatch(
        observed_at=ts,
        source="unavailable",
        endpoint="",
        requested_entities=requested,
        missing_entities=requested,
        quality_flags=["all_price_sources_failed", *[f"source_error:{err}" for err in errors[:6]]],
    )


def fetch_hyperliquid_all_mids(info_url: str = DEFAULT_HL_INFO_URL, timeout_s: float = 8.0) -> dict[str, Any]:
    """Fetch Hyperliquid ``allMids`` from a REST ``/info`` endpoint."""
    payload = json.dumps({"type": "allMids"}).encode("utf-8")
    req = urllib.request.Request(
        _normalize_info_url(info_url),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:
        raw = json.load(resp)
    if not isinstance(raw, dict):
        raise RuntimeError("hyperliquid_all_mids_not_object")
    return raw


def default_hyperliquid_price_sources() -> list[tuple[str, str]]:
    """Return the configured market-data read chain.

    Environment variables intentionally name the role, not just a URL, so the
    persisted observation can tell whether price came from our node,
    Hydromancer, or the official public fallback.
    """
    out: list[tuple[str, str]] = []
    node_url = os.environ.get("TALIS_HL_NODE_INFO_URL", "").strip()
    hydromancer_url = os.environ.get("TALIS_HYDROMANCER_HL_INFO_URL", "").strip()
    public_url = os.environ.get("TALIS_HL_INFO_URL", "").strip() or DEFAULT_HL_INFO_URL
    if node_url:
        out.append(("our_hl_node", node_url))
    if hydromancer_url:
        out.append(("hydromancer", hydromancer_url))
    out.append(("hyperliquid_public_api", public_url))
    return out


def _observations_from_all_mids(
    mids: dict[str, Any],
    *,
    requested: list[str],
    observed_at: str,
    source_name: str,
    endpoint: str,
) -> tuple[list[PriceObservation], list[str]]:
    exact = {str(k): v for k, v in mids.items()}
    upper = {str(k).upper(): (str(k), v) for k, v in mids.items()}
    observations: list[PriceObservation] = []
    missing: list[str] = []
    for entity in requested:
        match = _match_mid(entity, exact=exact, upper=upper)
        if match is None:
            missing.append(entity)
            continue
        hl_coin, raw_price = match
        price = _as_float(raw_price)
        if price is None or price <= 0:
            missing.append(entity)
            continue
        observations.append(PriceObservation(
            entity=entity,
            observed_at=observed_at,
            price=price,
            source=source_name,
            payload={
                "source_family": source_name,
                "hl_coin": hl_coin,
                "endpoint": endpoint,
                "raw_price": raw_price,
            },
        ))
    return observations, missing


def _match_mid(
    entity: str,
    *,
    exact: dict[str, Any],
    upper: dict[str, tuple[str, Any]],
) -> tuple[str, Any] | None:
    for candidate in _entity_candidates(entity):
        if candidate in exact:
            return candidate, exact[candidate]
        upper_match = upper.get(candidate.upper())
        if upper_match is not None:
            return upper_match
    return None


def _entity_candidates(entity: str) -> list[str]:
    raw = str(entity or "").strip()
    if not raw:
        return []
    base = raw
    for sep in ("/", ":"):
        if sep in base:
            base = base.split(sep, 1)[0]
    suffixes = ("-USD", "-USDC", "-PERP", "USD", "USDC", "PERP")
    out = [raw, base]
    for suffix in suffixes:
        for item in list(out):
            if item.upper().endswith(suffix) and len(item) > len(suffix):
                out.append(item[: -len(suffix)])
    deduped: list[str] = []
    seen: set[str] = set()
    for item in out:
        text = str(item or "").strip()
        key = text.upper()
        if text and key not in seen:
            seen.add(key)
            deduped.append(text)
    return deduped


def _dedupe_entities(entities: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in entities:
        text = str(raw or "").strip()
        key = text.upper()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _normalize_info_url(url: str) -> str:
    text = str(url or "").strip() or DEFAULT_HL_INFO_URL
    return text if text.endswith("/info") else text.rstrip("/") + "/info"


def _normalize_time(raw: str) -> str:
    text = str(raw or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _as_float(raw: Any) -> float | None:
    try:
        return float(raw)
    except Exception:
        return None


__all__ = [
    "DEFAULT_HL_INFO_URL",
    "PriceObservationBatch",
    "collect_hyperliquid_mid_price_observations",
    "default_hyperliquid_price_sources",
    "fetch_hyperliquid_all_mids",
]
