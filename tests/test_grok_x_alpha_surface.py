from __future__ import annotations

from talis_desk.information_map.data_substrate import summarize_data_substrate
from talis_desk.swarm.scout_runner import _infer_tool_args
from talis_desk.swarm.seed_generator import SeedCell
from talis_desk.tool_atlas.native_tools import farm_grok_x_alpha_tool


GROK_TOOL_URI = "tic://tool/talis_native/farm_grok_x_alpha@v1"


def test_grok_x_alpha_tool_is_explicit_spend_and_citation_shaped() -> None:
    out = farm_grok_x_alpha_tool(
        entity="VVV",
        horizon="intraday",
        lens="sentiment",
        query="VVV early bullish social alpha",
        allow_live=False,
    )

    assert out["status"] == "configured_not_called"
    assert out["source_family"] == "grok_x_alpha"
    assert out["provider"] == "xai"
    assert out["query"] == "VVV early bullish social alpha"
    assert out["tool_config"]["type"] == "x_search"
    assert out["tool_config"]["enable_image_understanding"] is True
    assert out["alpha_candidates"] == []
    assert "no_synthetic_social_alpha" in out["quality_flags"]
    assert out["request_payload"]["tools"][0]["type"] == "x_search"


def test_data_substrate_promotes_grok_x_as_missing_social_alpha_surface() -> None:
    summary = summarize_data_substrate(
        [],
        allowed_tools=[GROK_TOOL_URI],
        entity="VVV",
        horizon="intraday",
        lens="sentiment",
    )

    expansion = next(e for e in summary.expansions if e.target_surface_key == "grok_x_social_alpha")
    assert expansion.priority == "high"
    assert expansion.suggested_tools == (GROK_TOOL_URI,)
    assert "repricing_pressure" in expansion.expected_edge


def test_data_substrate_touches_grok_x_and_joins_market_pressure() -> None:
    evidence = [
        {
            "tool_call_log_id": "tc_x_vvv",
            "uri": GROK_TOOL_URI,
            "result": {
                "source_family": "grok_x_alpha",
                "alpha_candidates": [
                    {
                        "claim": "VVV builders are surfacing fresh launch screenshots before consensus notices.",
                        "evidence_refs": ["https://x.com/example/status/1"],
                    }
                ],
            },
        },
        {
            "tool_call_log_id": "tc_market_vvv",
            "uri": "tic://tool/builtin/query_timeseries@v1",
            "result": {"funding": 0.01, "open_interest": 100},
        },
    ]
    summary = summarize_data_substrate(evidence, entity="VVV", horizon="intraday", lens="sentiment")

    touched = {touch.surface.key for touch in summary.touched if touch.touched}
    assert "grok_x_social_alpha" in touched
    assert "market_state" in touched
    assert "x_attention -> market_state_pressure" in summary.connection_edges


def test_scout_args_keep_grok_x_live_gate_closed_by_default(monkeypatch) -> None:
    monkeypatch.delenv("TALIS_ALLOW_LIVE_GROK_X_ALPHA", raising=False)
    seed = SeedCell(
        seed_id="seed_vvv_social",
        entity="VVV",
        horizon="intraday",
        lens="sentiment",
        bias_mode="frontier",
        theme="early social alpha",
    )

    args = _infer_tool_args(GROK_TOOL_URI, seed)

    assert args == {
        "entity": "VVV",
        "horizon": "intraday",
        "lens": "sentiment",
        "query": "VVV early social alpha sentiment intraday",
        "max_candidates": 8,
        "allow_live": False,
    }
