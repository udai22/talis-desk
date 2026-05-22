"""Server-side canonical HL perp catalog.

Codex critique:
"Resolve symbols server-side from the trading engine's canonical
catalog; the iOS widget catalog should be display/cache, not the source
of trading truth."

Lazy-loads from HL `/info?type=meta`. Caches per-process. Falls back to
a tiny frozen snapshot for offline/test contexts.

NOT a runtime dependency. Failure to fetch the live meta returns the
snapshot — but stamps `data_quality="snapshot_only"` so the preview
gate marks the trade as not-executable rather than silently using stale
specs.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger(__name__)

# Conservative tick/size assumptions for a handful of majors. These are
# the *fallback* — never used when the live meta endpoint is reachable.
# Numbers are HL-canonical at time of writing; the live meta call
# returns authoritative values.
_FALLBACK_PERPS: dict[str, dict] = {
    "BTC":  {"name": "BTC",  "sz_decimals": 5, "max_leverage": 50, "tick_size": 0.5,   "min_notional": 10.0, "asset_id": 0,  "dex": "main", "is_hip3": False},
    "ETH":  {"name": "ETH",  "sz_decimals": 4, "max_leverage": 50, "tick_size": 0.05,  "min_notional": 10.0, "asset_id": 1,  "dex": "main", "is_hip3": False},
    "SOL":  {"name": "SOL",  "sz_decimals": 2, "max_leverage": 50, "tick_size": 0.01,  "min_notional": 10.0, "asset_id": 5,  "dex": "main", "is_hip3": False},
    "HYPE": {"name": "HYPE", "sz_decimals": 2, "max_leverage": 5,  "tick_size": 0.001, "min_notional": 10.0, "asset_id": 159, "dex": "main", "is_hip3": False},
}


HL_META_URL = "https://api.hyperliquid.xyz/info"
_CACHE_TTL_SECONDS = 60 * 30  # 30min in-process cache


@dataclass(frozen=True)
class HLPerpSpec:
    """Canonical per-coin trading spec sourced from HL meta."""
    coin: str                       # e.g. "BTC", "xyz:NVDA"
    name: str                       # display name
    sz_decimals: int                # size step = 10^-sz_decimals
    max_leverage: int               # exchange cap
    tick_size: float                # smallest price increment
    min_notional_usd: float         # smallest tradeable notional
    asset_id: int                   # HL internal asset index
    dex: str                        # 'main' | hip3 prefix
    is_hip3: bool
    source: str = "live"            # 'live' | 'snapshot_only' | 'fallback'

    def round_price(self, price: float) -> float:
        return round(price / self.tick_size) * self.tick_size

    def round_size(self, size: float) -> float:
        step = 10.0 ** -self.sz_decimals
        return round(size / step) * step

    def is_supported(self) -> bool:
        return self.tick_size > 0 and self.sz_decimals >= 0


class _CatalogState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_fetch_at: float = 0.0
        self.specs: dict[str, HLPerpSpec] = {}
        self.source: str = "uninitialized"


_STATE = _CatalogState()


def _build_from_meta(meta: dict) -> dict[str, HLPerpSpec]:
    """Parse HL /info?type=meta payload into HLPerpSpec rows."""
    universe = meta.get("universe") or []
    out: dict[str, HLPerpSpec] = {}
    for idx, entry in enumerate(universe):
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        sz = int(entry.get("szDecimals") or 0)
        max_lev = int(entry.get("maxLeverage") or 1)
        # HL doesn't expose tick_size or min_notional directly in meta.
        # Tick is derived from price precision; HL convention: tick is
        # the smallest 1e-N value such that price has at most 5 sig figs.
        # For v1 we approximate from sz_decimals; real value comes from
        # `priceDecimals` on the trade endpoint at order time.
        # Min notional on HL is $10 across the board (verified empirically).
        tick = max(1e-8, 10 ** -(max(0, 5 - sz)))
        is_hip3 = ":" in name or name.startswith("xyz:")
        out[name.upper()] = HLPerpSpec(
            coin=name,
            name=name,
            sz_decimals=sz,
            max_leverage=max_lev,
            tick_size=tick,
            min_notional_usd=10.0,
            asset_id=idx,
            dex="main" if not is_hip3 else name.split(":")[0],
            is_hip3=is_hip3,
            source="live",
        )
    return out


def _fetch_meta_sync() -> Optional[dict]:
    """Best-effort live fetch. Returns None on failure (network, parse, timeout)."""
    if os.environ.get("TALIS_DISABLE_HL_META", "").lower() in {"1", "true"}:
        return None
    try:
        import urllib.request
        req = urllib.request.Request(
            HL_META_URL,
            data=json.dumps({"type": "meta"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            body = resp.read()
        return json.loads(body)
    except Exception as e:
        logger.info("HL meta fetch failed: %s", e)
        return None


def _ensure_loaded() -> None:
    with _STATE.lock:
        now = time.time()
        if _STATE.specs and (now - _STATE.last_fetch_at) < _CACHE_TTL_SECONDS:
            return
        meta = _fetch_meta_sync()
        if meta is not None:
            try:
                parsed = _build_from_meta(meta)
                if parsed:
                    _STATE.specs = parsed
                    _STATE.source = "live"
                    _STATE.last_fetch_at = now
                    return
            except Exception as e:
                logger.warning("HL meta parse failed: %s", e)
        # Fallback path.
        _STATE.specs = {
            k.upper(): HLPerpSpec(
                coin=v["name"], name=v["name"], sz_decimals=v["sz_decimals"],
                max_leverage=v["max_leverage"], tick_size=v["tick_size"],
                min_notional_usd=v["min_notional"], asset_id=v["asset_id"],
                dex=v["dex"], is_hip3=v["is_hip3"], source="snapshot_only",
            )
            for k, v in _FALLBACK_PERPS.items()
        }
        _STATE.source = "snapshot_only"
        _STATE.last_fetch_at = now


def get_perp_spec(coin: str) -> Optional[HLPerpSpec]:
    """Resolve a coin → HLPerpSpec. Returns None if not in catalog."""
    if not coin:
        return None
    _ensure_loaded()
    return _STATE.specs.get(coin.strip().upper())


def list_supported_perps() -> list[str]:
    _ensure_loaded()
    return sorted(_STATE.specs.keys())


def list_perp_specs() -> list[HLPerpSpec]:
    """Return the currently loaded canonical HL perp specs."""
    _ensure_loaded()
    return sorted(_STATE.specs.values(), key=lambda spec: spec.coin.upper())


def catalog_source() -> str:
    _ensure_loaded()
    return _STATE.source
