"""Data-substrate inventory and connection expansion planning.

Layer 1 should never feel like "four tool calls and a vibe." The desk has a
larger data estate: event feeds, Hydromancer, our node, wallet route state,
market microstructure, source health, citations, filings/news/social, and
future mempool readers. This module makes that estate explicit so scout runs
can show what they touched, what they missed, and which targeted evidence calls
should happen next.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Iterable


@dataclass(frozen=True)
class DataSurface:
    key: str
    title: str
    role: str
    question: str
    example_tools: tuple[str, ...] = ()
    edge_types: tuple[str, ...] = ()
    source_family: str = ""
    status: str = "available"


@dataclass(frozen=True)
class DataSurfaceTouch:
    surface: DataSurface
    touched: bool
    receipt_ids: tuple[str, ...] = ()
    evidence_summary: str = ""


@dataclass(frozen=True)
class ConnectionExpansion:
    title: str
    why: str
    target_surface_key: str
    suggested_tools: tuple[str, ...]
    expected_edge: str
    priority: str = "medium"


@dataclass(frozen=True)
class DataSubstrateSummary:
    touched: tuple[DataSurfaceTouch, ...]
    expansions: tuple[ConnectionExpansion, ...]
    connection_edges: tuple[str, ...]
    coverage_score: float
    active_receipts: int
    total_surfaces: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "touched": [
                {
                    **asdict(t.surface),
                    "touched": t.touched,
                    "receipt_ids": list(t.receipt_ids),
                    "evidence_summary": t.evidence_summary,
                }
                for t in self.touched
            ],
            "expansions": [asdict(e) for e in self.expansions],
            "connection_edges": list(self.connection_edges),
            "coverage_score": self.coverage_score,
            "active_receipts": self.active_receipts,
            "total_surfaces": self.total_surfaces,
        }


DATA_SURFACES: tuple[DataSurface, ...] = (
    DataSurface(
        key="event_feed",
        title="Event feed",
        role="What happened",
        question="Did a fresh event enter the market clock?",
        example_tools=("tic://tool/builtin/query_events_recent@v1",),
        edge_types=("event -> entity", "event -> actor", "event -> clock"),
        source_family="event",
    ),
    DataSurface(
        key="market_state",
        title="Market state",
        role="Can it matter",
        question="What do depth, open interest, funding, and tape context say?",
        example_tools=("tic://tool/builtin/query_timeseries@v1",),
        edge_types=("entity -> depth", "entity -> open_interest", "entity -> funding"),
        source_family="market",
    ),
    DataSurface(
        key="hydromancer_actor_graph",
        title="Hydromancer actor graph",
        role="Who is involved",
        question="Are the actors skilled, noisy, crowded, or historically predictive?",
        example_tools=(
            "tic://tool/hydromancer/get_hl_pnl_leaderboard@v1",
            "tic://tool/hydromancer/get_wallet_completed_trades@v1",
        ),
        edge_types=("wallet -> pnl", "wallet -> win_rate", "wallet -> coin"),
        source_family="hydromancer",
    ),
    DataSurface(
        key="our_hl_node",
        title="Our HL node",
        role="What our node knows",
        question="Do our own rejects, fills, and state deltas show healthy or stressed flow?",
        example_tools=("tic://tool/builtin/hl_reject_corpus@v1",),
        edge_types=("wallet -> rejects", "order_state -> quality", "node -> source_ref"),
        source_family="our_hl_node",
    ),
    DataSurface(
        key="builder_flow",
        title="Builder flow",
        role="Where orders route",
        question="Which builders, users, and coins are carrying the flow?",
        example_tools=("tic://tool/hydromancer/get_builder_fills@v1",),
        edge_types=("builder -> wallet", "builder -> coin", "wallet -> fills"),
        source_family="hydromancer",
    ),
    DataSurface(
        key="wallet_route_state",
        title="Wallet route state",
        role="Where supply goes",
        question="Does the actor restake, bridge, idle, route to CEX, or hit sellable liquidity?",
        example_tools=(
            "tic://tool/hydromancer/get_wallet_historical_orders@v1",
            "tic://tool/learned/wallet_route_state_monitor@v1",
        ),
        edge_types=("wallet -> route", "route -> liquidity", "event -> followup_state"),
        source_family="node",
    ),
    DataSurface(
        key="mempool_pending_intent",
        title="Mempool and pending intent",
        role="What may happen next",
        question="Are pending transactions or router interactions visible before settlement?",
        example_tools=("tic://tool/learned/hyperevm_mempool_actor_watch@v1",),
        edge_types=("pending_tx -> actor", "pending_tx -> contract", "intent -> event"),
        source_family="mempool",
        status="tool_gap",
    ),
    DataSurface(
        key="source_health_citations",
        title="Source health and citations",
        role="Can we trust it",
        question="Are sources fresh, resolved, and citation-addressable?",
        example_tools=(
            "tic://tool/builtin/query_source_health@v1",
            "tic://tool/learned/evidence_ref_resolver@v1",
        ),
        edge_types=("source -> health", "claim -> citation", "tool_call -> artifact"),
        source_family="provenance",
    ),
    DataSurface(
        key="filings_news_social",
        title="Filings, news, and social",
        role="Why attention changes",
        question="Is there primary-source or attention evidence explaining the move?",
        example_tools=(
            "tic://tool/builtin/query_events_recent@v1",
            "tic://tool/builtin/semantic_search@v1",
        ),
        edge_types=("source -> claim", "post -> primary_artifact", "filing -> event"),
        source_family="source_library",
    ),
    DataSurface(
        key="parallel_web_attention",
        title="Parallel and web search",
        role="What the open web knows",
        question="Do live web/Parallel sources add primary evidence, consensus context, or contradictions?",
        example_tools=(
            "tic://tool/parallel/parallel_search@v1",
            "tic://tool/perplexity/web_search@v1",
        ),
        edge_types=("web_source -> claim", "citation -> contradiction", "search_result -> source_diversity"),
        source_family="web",
    ),
    DataSurface(
        key="prediction_markets",
        title="Prediction markets",
        role="What odds imply",
        question="Do Polymarket or HL outcome markets disagree with market pricing or news attention?",
        example_tools=("tic://source/polymarket/polymarket_search", "tic://source/hl/hl_outcomes"),
        edge_types=("market_probability -> catalyst", "odds_change -> attention", "event_market -> hedge"),
        source_family="prediction_market",
    ),
    DataSurface(
        key="options_vol_derivatives",
        title="Options, vol, and derivatives",
        role="How risk is priced",
        question="Do IV, skew, funding, OI, liquidations, or basis confirm/crowd the claim?",
        example_tools=(
            "tic://tool/builtin/get_deribit_options@v1",
            "tic://source/misc/coinalyze",
            "tic://source/deribit/deribit_options",
        ),
        edge_types=("claim -> implied_vol", "flow -> funding", "event -> options_skew"),
        source_family="derivatives",
    ),
    DataSurface(
        key="macro_official",
        title="Official macro",
        role="What the regime allows",
        question="Do FRED, Fed, Treasury, EIA, DOL, BEA, or survey feeds change the regime backdrop?",
        example_tools=(
            "tic://source/macro/fred_macro",
            "tic://source/macro/fed_h41",
            "tic://source/macro/treasury_auctions",
        ),
        edge_types=("macro_series -> regime", "calendar -> liquidity", "official_release -> repricing"),
        source_family="macro_official",
    ),
    DataSurface(
        key="equity_fundamental_filings",
        title="Fundamentals and filings",
        role="What companies disclosed",
        question="Do filings, estimates, ownership, insider trades, or corporate actions change the asset-specific base rate?",
        example_tools=(
            "tic://source/misc/sec_edgar_search",
            "tic://source/misc/analyst_estimates",
            "tic://tool/builtin/get_sec_recent_8k@v1",
        ),
        edge_types=("filing -> claim", "estimate -> revision_pressure", "ownership -> positioning"),
        source_family="fundamentals",
    ),
    DataSurface(
        key="real_economy_alt",
        title="Real economy alternatives",
        role="What is happening off-screen",
        question="Do weather, shipping, mobility, recalls, health, fires, or ag data reveal stress before markets price it?",
        example_tools=(
            "tic://source/misc/weather_climate",
            "tic://source/misc/shipping_supply",
            "tic://source/misc/nasa_firms_fires",
        ),
        edge_types=("physical_world -> supply_chain", "alt_data -> demand", "real_activity -> earnings_risk"),
        source_family="real_economy_alt",
    ),
    DataSurface(
        key="regulatory_innovation_gov",
        title="Regulatory and innovation layer",
        role="Where policy and invention move",
        question="Do grants, patents, visas, lobbying, Congress, USA Spending, or regulatory rules reveal future pressure?",
        example_tools=(
            "tic://source/misc/finnhub_patents",
            "tic://source/misc/finnhub_lobbying",
            "tic://source/misc/usaspending_advanced",
        ),
        edge_types=("policy -> beneficiary", "patent -> innovation_rate", "grant -> demand_signal"),
        source_family="regulatory_innovation",
    ),
    DataSurface(
        key="celestial_cycles",
        title="Celestial and cycle priors",
        role="Experimental regime prior",
        question="Do sunspot, lunar, or celestial-cycle features line up with statistically tested historical regime behavior?",
        example_tools=(
            "tic://source/misc/astro_cycles",
            "tic://tool/builtin/get_celestial_state@v1",
            "tic://tool/builtin/get_celestial_history@v1",
        ),
        edge_types=("cycle_state -> regime_prior", "cycle_history -> backtest", "claim -> confounder_test"),
        source_family="celestial_cycles",
        status="experimental_requires_stat_test",
    ),
    DataSurface(
        key="order_flow_sim",
        title="Order-flow and simulation",
        role="How expression should change",
        question="Do tape, volume profile, internals, and flow simulation agree with the claim?",
        example_tools=("tic://tool/order_flow/analyze_tape_window@v1",),
        edge_types=("tape -> pressure", "profile -> level", "flow_sim -> expression"),
        source_family="order_flow",
    ),
)


def summarize_data_substrate(
    tool_evidence: Iterable[dict[str, Any]],
    *,
    allowed_tools: Iterable[str] = (),
    entity: str = "",
    horizon: str = "",
    lens: str = "",
) -> DataSubstrateSummary:
    evidence = [x for x in tool_evidence if isinstance(x, dict)]
    allowed = set(str(x) for x in allowed_tools if x)
    touches = tuple(
        _surface_touch(surface, evidence)
        for surface in DATA_SURFACES
    )
    touched_keys = {t.surface.key for t in touches if t.touched}
    receipt_count = sum(len(t.receipt_ids) for t in touches if t.touched)
    connection_edges = tuple(_connection_edges(touches, entity=entity))
    expansions = tuple(_connection_expansions(
        touched_keys=touched_keys,
        allowed_tools=allowed,
        entity=entity,
        horizon=horizon,
        lens=lens,
    ))
    coverage_score = round(
        min(1.0, (len(touched_keys) / max(1, len(DATA_SURFACES))) * 0.65 + min(0.35, 0.07 * len(connection_edges))),
        3,
    )
    return DataSubstrateSummary(
        touched=touches,
        expansions=expansions,
        connection_edges=connection_edges,
        coverage_score=coverage_score,
        active_receipts=receipt_count,
        total_surfaces=len(DATA_SURFACES),
    )


def _surface_touch(surface: DataSurface, evidence: list[dict[str, Any]]) -> DataSurfaceTouch:
    matches = [item for item in evidence if _evidence_matches(surface, item)]
    receipt_ids = tuple(
        str(item.get("tool_call_log_id") or item.get("uri") or surface.key)
        for item in matches
    )
    return DataSurfaceTouch(
        surface=surface,
        touched=bool(matches),
        receipt_ids=receipt_ids,
        evidence_summary=_summarize_evidence(matches[:2]),
    )


def _evidence_matches(surface: DataSurface, item: dict[str, Any]) -> bool:
    uri = str(item.get("uri") or "").lower()
    source = str((item.get("result") or {}).get("source") or "").lower() if isinstance(item.get("result"), dict) else ""
    summary = str(item.get("summary") or item.get("result") or "").lower()
    key = surface.key
    if key == "event_feed":
        return "query_events_recent" in uri or '"events"' in summary or "'events'" in summary
    if key == "market_state":
        return "query_timeseries" in uri or "orderbook" in summary or "open_interest" in summary or "funding" in summary
    if key == "hydromancer_actor_graph":
        return "hydromancer" in uri and ("leaderboard" in uri or "wallet" in uri or "pnl" in summary)
    if key == "our_hl_node":
        return "hl_reject" in uri or source == "our_hl_node" or "reject_rate" in summary
    if key == "builder_flow":
        return "builder_fills" in uri or "builder" in summary
    if key == "wallet_route_state":
        return "wallet_historical_orders" in uri or "route" in summary or "cex" in summary or "bridge" in summary
    if key == "mempool_pending_intent":
        return "mempool" in uri or "pending_tx" in summary
    if key == "source_health_citations":
        return "source_health" in uri or "citation" in uri or "evidence_ref" in uri
    if key == "filings_news_social":
        return "semantic_search" in uri or "recent_news" in uri or "filing" in summary or "social" in summary or "post" in summary
    if key == "parallel_web_attention":
        return "parallel_search" in uri or "web_search" in uri or "perplexity" in uri or "search_result" in summary
    if key == "prediction_markets":
        return "polymarket" in uri or "hl_outcomes" in uri or "outcome" in summary or "probability" in summary
    if key == "options_vol_derivatives":
        return any(token in uri or token in summary for token in ("option", "deribit", "dvol", "coinalyze", "funding", "liquidation", "basis", "skew", "vol"))
    if key == "macro_official":
        return any(token in uri or token in summary for token in ("fred", "fed_h41", "fed_h8", "fomc", "treasury", "econ", "macro", "eia", "dol", "bea", "cbo", "ism", "ofr"))
    if key == "equity_fundamental_filings":
        return any(token in uri or token in summary for token in ("sec_", "edgar", "analyst", "fundamental", "earnings", "insider", "ownership", "corporate_actions", "filing"))
    if key == "real_economy_alt":
        return any(token in uri or token in summary for token in ("weather", "shipping", "freight", "mobility", "nhtsa", "cdc", "nasa_firms", "usda", "cms", "epa"))
    if key == "regulatory_innovation_gov":
        return any(token in uri or token in summary for token in ("regulatory", "congress", "lobbying", "patent", "visa", "grants", "usaspending", "crunchbase", "research_innovation"))
    if key == "celestial_cycles":
        return any(token in uri or token in summary for token in ("astro", "celestial", "sunspot", "lunar", "planetary"))
    if key == "order_flow_sim":
        return "order_flow" in uri or "flow_sim" in uri or "volume_profile" in uri or "tape" in summary
    return any(str(tool).lower() in uri for tool in surface.example_tools)


def _summarize_evidence(matches: list[dict[str, Any]]) -> str:
    if not matches:
        return ""
    bits: list[str] = []
    for item in matches:
        ref = str(item.get("tool_call_log_id") or item.get("uri") or "receipt")
        uri = str(item.get("uri") or "")
        bits.append(f"{ref}: {uri.rsplit('/', 1)[-1] if uri else 'evidence'}")
    return "; ".join(bits)


def _connection_edges(touches: tuple[DataSurfaceTouch, ...], *, entity: str) -> list[str]:
    touched = {t.surface.key for t in touches if t.touched}
    label = entity or "entity"
    edges: list[str] = []
    if {"event_feed", "market_state"} <= touched:
        edges.append(f"event -> {label} market_state")
    if {"event_feed", "hydromancer_actor_graph"} <= touched:
        edges.append("event -> actor_quality")
    if {"hydromancer_actor_graph", "our_hl_node"} <= touched:
        edges.append("wallet -> node_order_quality")
    if {"event_feed", "our_hl_node"} <= touched:
        edges.append("event -> node_receipts")
    if {"market_state", "our_hl_node"} <= touched:
        edges.append("order_state -> absorption_context")
    if {"builder_flow", "our_hl_node"} <= touched:
        edges.append("builder -> node_flow_quality")
    if {"parallel_web_attention", "filings_news_social"} <= touched:
        edges.append("web_attention -> primary_source_crosscheck")
    if {"prediction_markets", "filings_news_social"} <= touched:
        edges.append("prediction_market -> news_disagreement")
    if {"options_vol_derivatives", "event_feed"} <= touched:
        edges.append("event -> vol_positioning")
    if {"macro_official", "market_state"} <= touched:
        edges.append("macro_regime -> market_state_constraint")
    if {"equity_fundamental_filings", "filings_news_social"} <= touched:
        edges.append("company_disclosure -> attention_change")
    if {"real_economy_alt", "equity_fundamental_filings"} <= touched:
        edges.append("real_activity -> earnings_revision_risk")
    if {"regulatory_innovation_gov", "equity_fundamental_filings"} <= touched:
        edges.append("policy_innovation -> company_fundamentals")
    if {"celestial_cycles", "macro_official"} <= touched:
        edges.append("cycle_prior -> macro_confounder_test")
    return edges


def _connection_expansions(
    *,
    touched_keys: set[str],
    allowed_tools: set[str],
    entity: str,
    horizon: str,
    lens: str,
) -> list[ConnectionExpansion]:
    expansions: list[ConnectionExpansion] = []
    coin = entity or "asset"
    if "wallet_route_state" not in touched_keys:
        expansions.append(ConnectionExpansion(
            title="Resolve actor route state",
            why=f"The scout knows a {coin} event exists, but not where the actor routes after the event.",
            target_surface_key="wallet_route_state",
            suggested_tools=("tic://tool/learned/wallet_route_state_monitor@v1",),
            expected_edge="event -> wallet_route -> sellable_liquidity",
            priority="high",
        ))
    if "builder_flow" not in touched_keys:
        expansions.append(ConnectionExpansion(
            title="Join builder flow",
            why="Builder fills can reveal whether the flow is concentrated, informed, or mechanically routed.",
            target_surface_key="builder_flow",
            suggested_tools=("tic://tool/hydromancer/get_builder_fills@v1",),
            expected_edge="wallet -> builder -> fills",
            priority="high" if lens in {"on_chain", "flow_absorption"} else "medium",
        ))
    if "source_health_citations" not in touched_keys:
        expansions.append(ConnectionExpansion(
            title="Resolve source health and citations",
            why="A high-quality string should know whether every receipt is fresh and addressable.",
            target_surface_key="source_health_citations",
            suggested_tools=("tic://tool/builtin/query_source_health@v1", "tic://tool/learned/evidence_ref_resolver@v1"),
            expected_edge="claim -> citation -> source_health",
            priority="high",
        ))
    if "parallel_web_attention" not in touched_keys and lens in {"sentiment", "catalyst", "filing", "polymarket", "structural", "anomaly"}:
        expansions.append(ConnectionExpansion(
            title="Cross-check open web attention",
            why="Parallel/web search can find fresh primary links, consensus narratives, and contradictions outside the stored feeds.",
            target_surface_key="parallel_web_attention",
            suggested_tools=("tic://tool/parallel/parallel_search@v1", "tic://tool/perplexity/web_search@v1"),
            expected_edge="claim -> source_diversity -> contradiction_check",
            priority="medium",
        ))
    if "prediction_markets" not in touched_keys and lens in {"polymarket", "catalyst", "sentiment"}:
        expansions.append(ConnectionExpansion(
            title="Compare event-market odds",
            why="Prediction markets can disagree with headlines and reveal where catalyst probability is mispriced.",
            target_surface_key="prediction_markets",
            suggested_tools=("tic://source/polymarket/polymarket_search", "tic://source/hl/hl_outcomes"),
            expected_edge="event_market_probability -> catalyst_disagreement",
            priority="medium",
        ))
    if "options_vol_derivatives" not in touched_keys and lens in {"options_flow", "vol_surface", "microstructure", "catalyst"}:
        expansions.append(ConnectionExpansion(
            title="Attach vol and positioning",
            why="Options, OI, funding, liquidations, and basis decide whether a claim is fresh alpha or already crowded.",
            target_surface_key="options_vol_derivatives",
            suggested_tools=("tic://tool/builtin/get_deribit_options@v1", "tic://source/misc/coinalyze"),
            expected_edge="claim -> derivatives_positioning -> crowding",
            priority="high" if lens in {"options_flow", "vol_surface"} else "medium",
        ))
    if "macro_official" not in touched_keys and lens in {"macro", "rotation", "money_velocity", "structural"}:
        expansions.append(ConnectionExpansion(
            title="Anchor official macro regime",
            why="Official macro feeds constrain whether a local signal can matter at the selected horizon.",
            target_surface_key="macro_official",
            suggested_tools=("tic://source/macro/fred_macro", "tic://source/macro/treasury_auctions"),
            expected_edge="official_macro -> regime_constraint",
            priority="medium",
        ))
    if "equity_fundamental_filings" not in touched_keys and lens in {"filing", "catalyst", "factor"}:
        expansions.append(ConnectionExpansion(
            title="Join company disclosure layer",
            why="Filings, estimates, ownership, and corporate actions separate real fundamental change from attention noise.",
            target_surface_key="equity_fundamental_filings",
            suggested_tools=("tic://source/misc/sec_edgar_search", "tic://source/misc/analyst_estimates"),
            expected_edge="company_disclosure -> revision_pressure",
            priority="high" if lens == "filing" else "medium",
        ))
    if "celestial_cycles" not in touched_keys and lens in {"anomaly", "structural"}:
        expansions.append(ConnectionExpansion(
            title="Test experimental cycle prior",
            why="Celestial/astro feeds are only useful as tested regime priors with confounder checks, never standalone trade evidence.",
            target_surface_key="celestial_cycles",
            suggested_tools=("tic://source/misc/astro_cycles", "tic://tool/builtin/get_celestial_state@v1"),
            expected_edge="cycle_state -> historical_backtest -> confounder_test",
            priority="low",
        ))
    if "mempool_pending_intent" not in touched_keys and lens in {"on_chain", "flow_absorption", "why_now"}:
        expansions.append(ConnectionExpansion(
            title="Watch pending intent",
            why="Pending router or contract interactions can turn node intelligence from reactive to early.",
            target_surface_key="mempool_pending_intent",
            suggested_tools=("tic://tool/learned/hyperevm_mempool_actor_watch@v1",),
            expected_edge="pending_tx -> actor_intent -> event",
            priority="medium",
        ))
    if "order_flow_sim" not in touched_keys and horizon in {"intraday", "1d"}:
        expansions.append(ConnectionExpansion(
            title="Connect to order-flow expression",
            why="The data layer should connect evidence to executable expression only after tape/profile context agrees.",
            target_surface_key="order_flow_sim",
            suggested_tools=("tic://tool/order_flow/analyze_tape_window@v1",),
            expected_edge="claim -> tape_pressure -> expression_gate",
            priority="medium",
        ))
    if allowed_tools:
        expansions = [_mark_available_tool(e, allowed_tools) for e in expansions]
    return expansions[:6]


def _mark_available_tool(expansion: ConnectionExpansion, allowed_tools: set[str]) -> ConnectionExpansion:
    tools = tuple(
        tool if tool in allowed_tools else f"{tool} (needs atlas/proposal)"
        for tool in expansion.suggested_tools
    )
    return ConnectionExpansion(
        title=expansion.title,
        why=expansion.why,
        target_surface_key=expansion.target_surface_key,
        suggested_tools=tools,
        expected_edge=expansion.expected_edge,
        priority=expansion.priority,
    )
