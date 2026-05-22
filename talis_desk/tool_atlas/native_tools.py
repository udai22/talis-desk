"""Talis-native read-only tools exposed through the tool atlas.

These are not TIC ingester tools. They are desk-intelligence tools: functions
that let agents inspect and act on Talis's own information map without reaching
around the harness.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


@dataclass(frozen=True)
class NativeToolSpec:
    tool_uri: str
    tool_name: str
    version: str
    provider: str
    callable: Callable[..., Any]
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    source_dependencies: list[str] = field(default_factory=list)
    permission_scope: str = "read_only"
    network_hosts: list[str] = field(default_factory=list)
    cost_hint: dict[str, Any] = field(default_factory=lambda: {"usd_per_call_estimate": 0.0})


def plan_alpha_geometry_actions_tool(
    *,
    cycle_id: str,
    limit: int = 64,
    geometry_weights: dict[str, Any] | None = None,
    routing_thresholds: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Return the shape-derived agenda for a cycle's alpha geometry."""
    from ..information_map import plan_alpha_geometry_actions
    from ..store import get_desk_store

    if not str(cycle_id or "").strip():
        raise ValueError("cycle_id_required")
    return plan_alpha_geometry_actions(
        cycle_id=str(cycle_id),
        limit=max(1, min(512, int(limit or 64))),
        geometry_weights=geometry_weights,
        routing_thresholds=routing_thresholds,
        conn=get_desk_store().conn,
    )


def review_alpha_geometry_cortex_tool(
    *,
    cycle_id: str,
    limit: int = 64,
    use_llm: bool = False,
    model: str = "anthropic:claude-opus-4-7",
    **_: Any,
) -> dict[str, Any]:
    """Return the cortex review of geometry health and policy pressure."""
    from ..information_map import build_alpha_geometry_cortex_review
    from ..store import get_desk_store

    if not str(cycle_id or "").strip():
        raise ValueError("cycle_id_required")
    return build_alpha_geometry_cortex_review(
        cycle_id=str(cycle_id),
        limit=max(1, min(512, int(limit or 64))),
        use_llm=bool(use_llm),
        model=str(model or "anthropic:claude-opus-4-7"),
        conn=get_desk_store().conn,
    )


GROK_X_ALPHA_SYSTEM = (
    "You are the Talis X/social alpha farmer. Treat X as a live attention and "
    "insider-context sensor, not as truth. Find early market-relevant clues: "
    "credible builders, informed traders, screenshots, replies, denial/non-denial, "
    "attention deltas, and contradictions. Return compact JSON with alpha_candidates; "
    "every candidate needs cited X sources, what would make the market reprice, "
    "what would falsify it, and how it maps into the information graph."
)


def farm_grok_x_alpha_tool(
    *,
    entity: str,
    horizon: str = "intraday",
    lens: str = "sentiment",
    query: str = "",
    allowed_x_handles: list[str] | None = None,
    excluded_x_handles: list[str] | None = None,
    from_date: str = "",
    to_date: str = "",
    enable_image_understanding: bool = True,
    enable_video_understanding: bool = False,
    max_candidates: int = 8,
    model: str = "grok-4.3",
    allow_live: bool = False,
    **_: Any,
) -> dict[str, Any]:
    """Use Grok + X Search as a cited social-alpha sensor.

    By default this returns the exact request plan without spending. Live use
    requires both ``allow_live=True`` and ``XAI_API_KEY``. That keeps 1,000-scout
    runs from accidentally buying social search without an explicit gate.
    """
    entity = str(entity or "").strip().upper()
    if not entity:
        raise ValueError("entity_required")
    max_candidates = max(1, min(20, int(max_candidates or 8)))
    query_text = _grok_x_query(entity=entity, horizon=horizon, lens=lens, query=query)
    tool_config = _grok_x_tool_config(
        allowed_x_handles=allowed_x_handles,
        excluded_x_handles=excluded_x_handles,
        from_date=from_date,
        to_date=to_date,
        enable_image_understanding=enable_image_understanding,
        enable_video_understanding=enable_video_understanding,
    )
    user_packet = {
        "entity": entity,
        "horizon": horizon,
        "lens": lens,
        "query": query_text,
        "max_candidates": max_candidates,
        "output_contract": {
            "alpha_candidates": [
                {
                    "claim": "market-relevant social clue",
                    "direction": "up|down|mixed|unknown",
                    "why_early": "why this is before consensus",
                    "evidence_refs": ["x_post_or_thread_url"],
                    "actor_quality": "why source may be informed",
                    "map_edges": ["post -> actor -> claim -> market_pressure"],
                    "kill_signal": "what would falsify it",
                    "next_tool": "node/web/derivatives/source-health follow-up",
                    "confidence": 0.0,
                }
            ]
        },
    }
    request_payload = {
        "model": str(model or "grok-4.3"),
        "input": [
            {"role": "system", "content": GROK_X_ALPHA_SYSTEM},
            {"role": "user", "content": json.dumps(user_packet, sort_keys=True)},
        ],
        "tools": [tool_config],
    }
    api_key = os.environ.get("XAI_API_KEY", "").strip()
    if not allow_live:
        return _grok_x_plan_response(
            "configured_not_called",
            request_payload,
            "set allow_live=true to spend on Grok/X Search",
            query=query_text,
        )
    if not api_key:
        return _grok_x_plan_response(
            "missing_api_key",
            request_payload,
            "set XAI_API_KEY before live Grok/X Search",
            query=query_text,
        )
    try:
        raw = _post_xai_responses(request_payload, api_key=api_key)
    except Exception as exc:
        return {
            **_grok_x_plan_response("call_failed", request_payload, str(exc)[:240], query=query_text),
            "error": f"{type(exc).__name__}: {exc}",
        }
    text = _extract_xai_text(raw)
    parsed = _extract_first_json(text)
    candidates = []
    if isinstance(parsed, dict) and isinstance(parsed.get("alpha_candidates"), list):
        candidates = [x for x in parsed["alpha_candidates"] if isinstance(x, dict)][:max_candidates]
    return {
        "schema_version": "talis_grok_x_alpha_v1",
        "status": "ok",
        "source_family": "grok_x_alpha",
        "provider": "xai",
        "model": request_payload["model"],
        "query": query_text,
        "tool_config": tool_config,
        "alpha_candidates": candidates,
        "response_text": text[:6000],
        "citations": raw.get("citations") or raw.get("output_citations") or [],
        "raw_response_keys": sorted(raw.keys()),
        "quality_flags": _grok_x_quality_flags(candidates, text),
    }


def _grok_x_query(*, entity: str, horizon: str, lens: str, query: str) -> str:
    if query:
        return str(query)[:600]
    return (
        f"Find early X posts/threads/images for {entity} {horizon} {lens} alpha. "
        "Prioritize credible builders/traders, unusual screenshots, fresh narrative inflections, "
        "reply-chain confirmations/denials, and evidence that could reprice before consensus."
    )


def _grok_x_tool_config(
    *,
    allowed_x_handles: list[str] | None,
    excluded_x_handles: list[str] | None,
    from_date: str,
    to_date: str,
    enable_image_understanding: bool,
    enable_video_understanding: bool,
) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "type": "x_search",
        "enable_image_understanding": bool(enable_image_understanding),
        "enable_video_understanding": bool(enable_video_understanding),
    }
    allowed = _handles(allowed_x_handles)[:20]
    excluded = _handles(excluded_x_handles)[:20]
    if allowed and not excluded:
        cfg["allowed_x_handles"] = allowed
    elif excluded:
        cfg["excluded_x_handles"] = excluded
    if from_date:
        cfg["from_date"] = str(from_date)
    else:
        cfg["from_date"] = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    if to_date:
        cfg["to_date"] = str(to_date)
    return cfg


def _handles(raw: list[str] | None) -> list[str]:
    out: list[str] = []
    for item in raw or []:
        handle = str(item or "").strip().lstrip("@")
        if handle:
            out.append(handle)
    return list(dict.fromkeys(out))


def _post_xai_responses(payload: dict[str, Any], *, api_key: str) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.x.ai/v1/responses",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"xai_http_{exc.code}: {detail}") from exc


def _grok_x_plan_response(status: str, payload: dict[str, Any], reason: str, *, query: str = "") -> dict[str, Any]:
    return {
        "schema_version": "talis_grok_x_alpha_v1",
        "status": status,
        "source_family": "grok_x_alpha",
        "provider": "xai",
        "model": payload.get("model"),
        "query": query,
        "tool_config": (payload.get("tools") or [{}])[0],
        "request_payload": payload,
        "alpha_candidates": [],
        "quality_flags": [status, "no_synthetic_social_alpha"],
        "reason": reason,
    }


def _extract_xai_text(raw: dict[str, Any]) -> str:
    if raw.get("output_text"):
        return str(raw["output_text"])
    chunks: list[str] = []
    for item in raw.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("text"):
                chunks.append(str(content.get("text")))
    return "\n".join(chunks)


def _extract_first_json(text: str) -> Any:
    start = (text or "").find("{")
    if start < 0:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


def _grok_x_quality_flags(candidates: list[dict[str, Any]], text: str) -> list[str]:
    flags: list[str] = []
    if not candidates:
        flags.append("no_structured_alpha_candidates")
    if "x.com/" not in text and "twitter.com/" not in text:
        flags.append("missing_x_citations_in_text")
    for candidate in candidates:
        refs = candidate.get("evidence_refs") if isinstance(candidate.get("evidence_refs"), list) else []
        if not refs:
            flags.append("candidate_missing_evidence_refs")
            break
    return sorted(set(flags))


NATIVE_TOOL_SPECS: tuple[NativeToolSpec, ...] = (
    NativeToolSpec(
        tool_uri="tic://tool/talis_native/plan_alpha_geometry_actions@v1",
        tool_name="plan_alpha_geometry_actions",
        version="v1",
        provider="talis_desk",
        callable=plan_alpha_geometry_actions_tool,
        description=(
            "Read the alpha-geometry field for a cycle and return concrete next actions: "
            "verify, repair sources, resolve tension, widen sources, or replicate scouts."
        ),
        input_schema={
            "type": "object",
            "required": ["cycle_id"],
            "properties": {
                "cycle_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 512},
                "geometry_weights": {"type": "object"},
                "routing_thresholds": {"type": "object"},
            },
            "timeout_ms": 5000,
        },
        source_dependencies=[
            "information_strings",
            "information_geometry_snapshots",
            "tool_call_log",
        ],
        cost_hint={"usd_per_call_estimate": 0.0, "local_read_only": True},
    ),
    NativeToolSpec(
        tool_uri="tic://tool/talis_native/review_alpha_geometry_cortex@v1",
        tool_name="review_alpha_geometry_cortex",
        version="v1",
        provider="talis_desk",
        callable=review_alpha_geometry_cortex_tool,
        description=(
            "Review alpha-geometry shape health, evolution lineage, and route metrics, "
            "then return cortex work orders plus a small auditable geometry-policy patch."
        ),
        input_schema={
            "type": "object",
            "required": ["cycle_id"],
            "properties": {
                "cycle_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 512},
                "use_llm": {"type": "boolean"},
                "model": {"type": "string"},
            },
            "timeout_ms": 10000,
        },
        source_dependencies=[
            "information_geometry_snapshots",
            "market_evolve_programs",
            "market_evolve_experiment_results",
            "tool_call_log",
        ],
        cost_hint={"usd_per_call_estimate": 0.0, "local_read_only": True},
    ),
    NativeToolSpec(
        tool_uri="tic://tool/talis_native/farm_grok_x_alpha@v1",
        tool_name="farm_grok_x_alpha",
        version="v1",
        provider="xai",
        callable=farm_grok_x_alpha_tool,
        description=(
            "Use Grok with X Search as a cited social-alpha sensor for early "
            "posts, threads, images, reply-chain evidence, builder/trader clues, "
            "and narrative inflections that may reprice an asset before consensus."
        ),
        input_schema={
            "type": "object",
            "required": ["entity"],
            "properties": {
                "entity": {"type": "string"},
                "horizon": {"type": "string"},
                "lens": {"type": "string"},
                "query": {"type": "string"},
                "allowed_x_handles": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                "excluded_x_handles": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                "from_date": {"type": "string"},
                "to_date": {"type": "string"},
                "enable_image_understanding": {"type": "boolean"},
                "enable_video_understanding": {"type": "boolean"},
                "max_candidates": {"type": "integer", "minimum": 1, "maximum": 20},
                "model": {"type": "string"},
                "allow_live": {"type": "boolean"},
            },
            "timeout_ms": 60000,
        },
        source_dependencies=["x_search", "x_posts", "grok"],
        permission_scope="read_only",
        network_hosts=["api.x.ai"],
        cost_hint={
            "usd_per_call_estimate": 0.03,
            "requires_explicit_allow_live": True,
            "requires_env": "XAI_API_KEY",
        },
    ),
)


def native_tool_specs() -> tuple[NativeToolSpec, ...]:
    return NATIVE_TOOL_SPECS


def get_native_callable(tool_name: str) -> Callable[..., Any] | None:
    for spec in NATIVE_TOOL_SPECS:
        if spec.tool_name == tool_name:
            return spec.callable
    return None


__all__ = [
    "NativeToolSpec",
    "get_native_callable",
    "native_tool_specs",
    "farm_grok_x_alpha_tool",
    "plan_alpha_geometry_actions_tool",
    "review_alpha_geometry_cortex_tool",
]
