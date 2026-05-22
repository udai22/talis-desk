from __future__ import annotations

from talis_desk.information_map import collect_hyperliquid_mid_price_observations


def test_hyperliquid_mid_collector_normalizes_entities_and_preserves_source() -> None:
    def fake_fetch(endpoint: str, timeout_s: float):
        assert endpoint == "https://node.example/info"
        assert timeout_s == 3.0
        return {
            "BTC": "70000.5",
            "VVV": "5.25",
            "kFLOKI": "0.0123",
        }

    batch = collect_hyperliquid_mid_price_observations(
        ["BTC-USD", "VVV", "KFLOKI", "UNKNOWN"],
        timeout_s=3.0,
        sources=[("our_hl_node", "https://node.example")],
        fetch_all_mids=fake_fetch,
        observed_at="2026-05-22T10:00:00Z",
    )

    assert batch.source == "our_hl_node"
    assert batch.endpoint == "https://node.example/info"
    assert batch.resolved_entities == ["BTC-USD", "VVV", "KFLOKI"]
    assert batch.missing_entities == ["UNKNOWN"]
    assert "missing_requested_entities" in batch.quality_flags
    prices = {obs.entity: obs.price for obs in batch.observations}
    assert prices["BTC-USD"] == 70000.5
    assert prices["VVV"] == 5.25
    assert prices["KFLOKI"] == 0.0123
    assert batch.observations[0].payload["source_family"] == "our_hl_node"


def test_hyperliquid_mid_collector_falls_back_after_source_error() -> None:
    calls: list[str] = []

    def fake_fetch(endpoint: str, timeout_s: float):
        calls.append(endpoint)
        if "node" in endpoint:
            raise TimeoutError("node slow")
        return {"HYPE": "33.5"}

    batch = collect_hyperliquid_mid_price_observations(
        ["HYPE"],
        sources=[
            ("our_hl_node", "https://node.example/info"),
            ("hyperliquid_public_api", "https://api.hyperliquid.xyz/info"),
        ],
        fetch_all_mids=fake_fetch,
    )

    assert calls == ["https://node.example/info", "https://api.hyperliquid.xyz/info"]
    assert batch.source == "hyperliquid_public_api"
    assert batch.resolved_entities == ["HYPE"]
    assert batch.observations[0].price == 33.5
    assert "source_error:our_hl_node:TimeoutError" in batch.quality_flags
