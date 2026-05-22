"""Talis-native read-only tools exposed through the tool atlas.

These are not TIC ingester tools. They are desk-intelligence tools: functions
that let agents inspect and act on Talis's own information map without reaching
around the harness.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
    "plan_alpha_geometry_actions_tool",
    "review_alpha_geometry_cortex_tool",
]
