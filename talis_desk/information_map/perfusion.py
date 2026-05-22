"""Information perfusion matrix for continuous market intelligence.

Alpha geometry tells us the shape of the information map. Perfusion tells us
where the map needs flow right now: information pressure, price absorption,
source oxygenation, resistance, and the next scout/tool routing action.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..store import get_desk_store
from .alpha_geometry import load_alpha_geometry
from .outcomes import load_information_price_outcomes
from .store import recent_information_strings


PERFUSION_SCHEMA_VERSION = "information_perfusion_matrix_v1"


@dataclass
class InformationPerfusionCell:
    cell_key: str
    cycle_id: str
    entity: str
    horizon: str
    lens: str
    theme: str = ""
    string_count: int = 0
    scout_count: int = 0
    outcome_count: int = 0
    observed_outcome_count: int = 0
    source_families: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    route_directive: str = "maintain"
    recommended_scouts: int = 0
    suggested_tools: list[str] = field(default_factory=list)
    quality_flags: list[str] = field(default_factory=list)

    @property
    def dilation_score(self) -> float:
        return float(self.metrics.get("dilation_score") or 0.0)

    @property
    def pressure_gradient(self) -> float:
        return float(self.metrics.get("pressure_gradient") or 0.0)


@dataclass
class InformationPerfusionSnapshot:
    cycle_id: str
    created_at: str
    cells: list[InformationPerfusionCell] = field(default_factory=list)
    global_metrics: dict[str, float] = field(default_factory=dict)
    quality_flags: list[str] = field(default_factory=list)


def compute_information_perfusion(
    *,
    cycle_id: str,
    scout_budget: int = 24,
    limit: int = 2000,
    persist: bool = True,
    conn: Optional[sqlite3.Connection] = None,
) -> InformationPerfusionSnapshot:
    """Compute the information-pressure routing matrix for one cycle."""
    db = conn or get_desk_store().conn
    rows = recent_information_strings(cycle_id=cycle_id, limit=limit, conn=db)
    outcomes = load_information_price_outcomes(cycle_id=cycle_id, limit=limit, conn=db)
    geometry_rows = load_alpha_geometry(cycle_id=cycle_id, limit=limit, conn=db)
    outcome_by_string: dict[str, list[dict[str, Any]]] = {}
    for outcome in outcomes:
        outcome_by_string.setdefault(str(outcome.get("string_id") or ""), []).append(outcome)
    geometry_by_cell = {str(row.get("cell_key") or ""): row for row in geometry_rows}
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(_cell_key(row), []).append(row)
    cells = [
        _cell_from_rows(
            cycle_id=cycle_id,
            cell_key=cell_key,
            rows=cell_rows,
            outcome_by_string=outcome_by_string,
            geometry_row=geometry_by_cell.get(cell_key, {}),
        )
        for cell_key, cell_rows in groups.items()
    ]
    _allocate_scouts(cells, scout_budget=scout_budget)
    cells.sort(key=lambda c: (c.dilation_score, c.pressure_gradient), reverse=True)
    now = datetime.now(timezone.utc).isoformat()
    snapshot = InformationPerfusionSnapshot(
        cycle_id=cycle_id,
        created_at=now,
        cells=cells,
        global_metrics=_global_metrics(cells),
        quality_flags=_snapshot_flags(cells, rows, outcomes),
    )
    if persist:
        persist_information_perfusion(snapshot, conn=db)
    return snapshot


def persist_information_perfusion(
    snapshot: InformationPerfusionSnapshot,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> list[str]:
    db = conn or get_desk_store().conn
    _ensure_information_perfusion_table(db)
    ids: list[str] = []
    for cell in snapshot.cells:
        row_id = _perfusion_id(snapshot.cycle_id, cell.cell_key)
        ids.append(row_id)
        db.execute(
            """
            INSERT OR REPLACE INTO information_perfusion_snapshots (
                id, cycle_id, cell_key, entity, theme, horizon, lens,
                string_count, scout_count, outcome_count, observed_outcome_count,
                source_families_json, metrics_json, route_directive,
                recommended_scouts, suggested_tools_json, quality_flags,
                created_at, valid_from, transaction_from, transaction_to
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                row_id,
                snapshot.cycle_id,
                cell.cell_key,
                cell.entity,
                cell.theme,
                cell.horizon,
                cell.lens,
                int(cell.string_count),
                int(cell.scout_count),
                int(cell.outcome_count),
                int(cell.observed_outcome_count),
                json.dumps(cell.source_families),
                json.dumps(cell.metrics, sort_keys=True),
                cell.route_directive,
                int(cell.recommended_scouts),
                json.dumps(cell.suggested_tools),
                json.dumps(cell.quality_flags),
                snapshot.created_at,
                snapshot.created_at,
                snapshot.created_at,
            ),
        )
    db.commit()
    return ids


def load_information_perfusion(
    *,
    cycle_id: str,
    limit: int = 64,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    db = conn or get_desk_store().conn
    _ensure_information_perfusion_table(db)
    rows = db.execute(
        """
        SELECT *
        FROM information_perfusion_snapshots
        WHERE cycle_id = ? AND transaction_to IS NULL
        ORDER BY
            CASE route_directive
                WHEN 'dilate_scouts' THEN 5
                WHEN 'attach_price_sensors' THEN 4
                WHEN 'oxygenate_sources' THEN 3
                WHEN 'decongest_or_repair' THEN 2
                WHEN 'harvest_outcome' THEN 1
                ELSE 0
            END DESC,
            json_extract(metrics_json, '$.dilation_score') DESC,
            recommended_scouts DESC
        LIMIT ?
        """,
        (cycle_id, int(limit)),
    ).fetchall()
    return [_perfusion_row_to_dict(row) for row in rows]


def latest_information_perfusion_cycle(
    *,
    exclude_cycle_id: str = "",
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    db = conn or get_desk_store().conn
    _ensure_information_perfusion_table(db)
    params: list[Any] = []
    where = "transaction_to IS NULL"
    if exclude_cycle_id:
        where += " AND cycle_id != ?"
        params.append(exclude_cycle_id)
    row = db.execute(
        f"""
        SELECT cycle_id, MAX(created_at) AS last_created_at
        FROM information_perfusion_snapshots
        WHERE {where}
        GROUP BY cycle_id
        ORDER BY last_created_at DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    return str(row["cycle_id"]) if row else ""


def information_perfusion_seed_directives(
    snapshot: InformationPerfusionSnapshot,
    *,
    max_items: int = 12,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cell in snapshot.cells:
        if cell.route_directive == "maintain":
            continue
        out.append({
            "cell_key": cell.cell_key,
            "entity": cell.entity,
            "theme": cell.theme,
            "horizon": cell.horizon,
            "lens": cell.lens,
            "route_directive": cell.route_directive,
            "recommended_scouts": cell.recommended_scouts,
            "pressure_gradient": cell.pressure_gradient,
            "dilation_score": cell.dilation_score,
            "suggested_tools": cell.suggested_tools,
            "metrics": cell.metrics,
            "quality_flags": cell.quality_flags,
        })
        if len(out) >= max_items:
            break
    return out


def _cell_from_rows(
    *,
    cycle_id: str,
    cell_key: str,
    rows: list[dict[str, Any]],
    outcome_by_string: dict[str, list[dict[str, Any]]],
    geometry_row: dict[str, Any],
) -> InformationPerfusionCell:
    first = rows[0] if rows else {}
    scout_ids = {str(row.get("scout_id") or "") for row in rows if row.get("scout_id")}
    source_families = sorted({
        family
        for row in rows
        for family in _source_families(row)
        if family
    })
    cell_outcomes = [
        outcome
        for row in rows
        for outcome in outcome_by_string.get(str(row.get("id") or ""), [])
    ]
    observed = [row for row in cell_outcomes if _outcome_observed(row)]
    string_count = len(rows)
    evidence_coverage = sum(1 for row in rows if row.get("evidence_refs") or row.get("source_tool_call_ids")) / max(1, string_count)
    scout_independence = min(1.0, len(scout_ids) / max(1, min(4, string_count)))
    source_independence = min(1.0, _entropy(source_families) * min(1.0, len(source_families) / 3.0))
    avg_attention = _avg([_float(row.get("attention_score"), 0.0) for row in rows])
    avg_conviction = _avg([_float(row.get("conviction"), 0.0) for row in rows])
    avg_novelty = _avg([_float(row.get("novelty_score"), 0.0) for row in rows])
    avg_crowding = _avg([_float(row.get("crowdedness"), 0.5) for row in rows])
    information_pressure = _clamp01(
        avg_attention * 0.34
        + avg_conviction * 0.24
        + avg_novelty * (1.0 - avg_crowding) * 0.24
        + evidence_coverage * 0.10
        + scout_independence * 0.08
    )
    observed_rate = len(observed) / max(1, len(cell_outcomes)) if cell_outcomes else 0.0
    avg_realized_edge = _avg([_float(row.get("realized_edge_score"), 0.0) for row in observed])
    price_absorption = _clamp01(max(0.0, avg_realized_edge - 0.50) * 2.0)
    source_oxygenation = _clamp01(
        source_independence * 0.34
        + evidence_coverage * 0.24
        + observed_rate * 0.22
        + scout_independence * 0.20
    )
    geometry_metrics = geometry_row.get("metrics") if isinstance(geometry_row.get("metrics"), dict) else {}
    fragility = _float(geometry_metrics.get("fragility"), _fallback_fragility(rows, source_oxygenation, evidence_coverage))
    frontier_pressure = _float(geometry_metrics.get("frontier_pressure"), 0.0)
    staleness = _temporal_staleness(rows)
    resistance = _clamp01(
        staleness * 0.26
        + avg_crowding * 0.20
        + fragility * 0.22
        + (1.0 - source_oxygenation) * 0.32
    )
    pressure_gradient = _clamp01(information_pressure * (1.0 - price_absorption))
    dilation_score = _clamp01(
        pressure_gradient * 0.48
        + source_oxygenation * 0.18
        + (1.0 - resistance) * 0.18
        + frontier_pressure * 0.16
    )
    route_directive = _route_directive(
        information_pressure=information_pressure,
        price_absorption=price_absorption,
        pressure_gradient=pressure_gradient,
        source_oxygenation=source_oxygenation,
        observed_rate=observed_rate,
        resistance=resistance,
        dilation_score=dilation_score,
    )
    metrics = {
        "information_pressure": round(information_pressure, 4),
        "price_absorption": round(price_absorption, 4),
        "pressure_gradient": round(pressure_gradient, 4),
        "source_oxygenation": round(source_oxygenation, 4),
        "resistance": round(resistance, 4),
        "dilation_score": round(dilation_score, 4),
        "observed_rate": round(observed_rate, 4),
        "avg_realized_edge_score": round(avg_realized_edge, 4),
        "evidence_coverage": round(evidence_coverage, 4),
        "source_independence": round(source_independence, 4),
        "scout_independence": round(scout_independence, 4),
        "avg_attention": round(avg_attention, 4),
        "avg_conviction": round(avg_conviction, 4),
        "avg_novelty": round(avg_novelty, 4),
        "avg_crowding": round(avg_crowding, 4),
        "staleness": round(staleness, 4),
        "geometry_fragility": round(fragility, 4),
        "geometry_frontier_pressure": round(frontier_pressure, 4),
    }
    return InformationPerfusionCell(
        cell_key=cell_key,
        cycle_id=cycle_id,
        entity=str(first.get("entity") or "UNKNOWN").upper(),
        horizon=str(first.get("horizon") or first.get("time_horizon") or "intraday"),
        lens=str(first.get("lens") or "anomaly"),
        theme=str(first.get("theme") or ""),
        string_count=string_count,
        scout_count=len(scout_ids),
        outcome_count=len(cell_outcomes),
        observed_outcome_count=len(observed),
        source_families=source_families,
        metrics=metrics,
        route_directive=route_directive,
        suggested_tools=_suggested_tools(route_directive, source_families),
        quality_flags=_cell_flags(route_directive, metrics, cell_outcomes),
    )


def _allocate_scouts(cells: list[InformationPerfusionCell], *, scout_budget: int) -> None:
    budget = max(0, int(scout_budget or 0))
    routed = [cell for cell in cells if cell.route_directive != "maintain" and cell.dilation_score > 0.0]
    if budget <= 0 or not routed:
        return
    weights = [
        cell.dilation_score
        * {
            "dilate_scouts": 1.40,
            "attach_price_sensors": 1.20,
            "oxygenate_sources": 1.05,
            "decongest_or_repair": 0.85,
            "harvest_outcome": 0.50,
        }.get(cell.route_directive, 0.25)
        for cell in routed
    ]
    total = sum(weights) or 1.0
    remaining = budget
    for cell, weight in zip(routed, weights):
        scouts = max(1, int(round(budget * weight / total)))
        cell.recommended_scouts = min(remaining, scouts)
        remaining -= cell.recommended_scouts
        if remaining <= 0:
            break
    idx = 0
    while remaining > 0 and routed:
        routed[idx % len(routed)].recommended_scouts += 1
        remaining -= 1
        idx += 1


def _route_directive(
    *,
    information_pressure: float,
    price_absorption: float,
    pressure_gradient: float,
    source_oxygenation: float,
    observed_rate: float,
    resistance: float,
    dilation_score: float,
) -> str:
    if information_pressure >= 0.55 and price_absorption >= 0.70:
        return "harvest_outcome"
    if pressure_gradient >= 0.48 and observed_rate < 0.50:
        return "attach_price_sensors"
    if pressure_gradient >= 0.50 and source_oxygenation >= 0.52 and resistance <= 0.58:
        return "dilate_scouts"
    if pressure_gradient >= 0.42 and source_oxygenation < 0.52:
        return "oxygenate_sources"
    if information_pressure >= 0.46 and resistance >= 0.62:
        return "decongest_or_repair"
    if dilation_score >= 0.58 and pressure_gradient >= 0.36:
        return "dilate_scouts"
    return "maintain"


def _suggested_tools(route_directive: str, source_families: list[str]) -> list[str]:
    tools = {
        "dilate_scouts": [
            "tic://tool/talis_native/compute_information_perfusion@v1",
            "tic://tool/builtin/query_timeseries@v1",
            "tic://tool/parallel/parallel_search@v1",
        ],
        "attach_price_sensors": [
            "tic://tool/talis_native/compute_information_perfusion@v1",
            "tic://tool/builtin/query_timeseries@v1",
        ],
        "oxygenate_sources": [
            "tic://tool/parallel/parallel_search@v1",
            "tic://tool/builtin/query_source_health@v1",
        ],
        "decongest_or_repair": [
            "tic://tool/agent_native/find_similar_setups@v1",
            "tic://tool/builtin/query_claims_by_entity@v1",
        ],
        "harvest_outcome": [
            "tic://tool/agent_native/replay_artifact_reasoning@v1",
            "tic://tool/agent_native/find_confluence@v1",
        ],
    }.get(route_directive, [])
    if "our_hl_node" not in source_families:
        tools.append("tic://tool/builtin/query_node@v1")
    if "grok_x_alpha" not in source_families:
        tools.append("tic://tool/talis_native/farm_grok_x_alpha@v1")
    return _dedupe(tools)[:8]


def _global_metrics(cells: list[InformationPerfusionCell]) -> dict[str, float]:
    routed = [cell for cell in cells if cell.route_directive != "maintain"]
    return {
        "cell_count": float(len(cells)),
        "routed_cell_count": float(len(routed)),
        "avg_information_pressure": round(_avg([cell.metrics.get("information_pressure", 0.0) for cell in cells]), 4),
        "avg_pressure_gradient": round(_avg([cell.pressure_gradient for cell in cells]), 4),
        "avg_source_oxygenation": round(_avg([cell.metrics.get("source_oxygenation", 0.0) for cell in cells]), 4),
        "avg_resistance": round(_avg([cell.metrics.get("resistance", 0.0) for cell in cells]), 4),
        "max_dilation_score": round(max([cell.dilation_score for cell in cells] or [0.0]), 4),
        "recommended_scouts": float(sum(cell.recommended_scouts for cell in cells)),
    }


def _snapshot_flags(
    cells: list[InformationPerfusionCell],
    rows: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> list[str]:
    flags = {PERFUSION_SCHEMA_VERSION}
    if not rows:
        flags.add("no_information_strings")
    if rows and not outcomes:
        flags.add("price_outcomes_missing")
    if any(cell.route_directive == "dilate_scouts" for cell in cells):
        flags.add("has_dilation_candidates")
    if any(cell.route_directive == "attach_price_sensors" for cell in cells):
        flags.add("has_price_sensor_gaps")
    if any(cell.route_directive == "oxygenate_sources" for cell in cells):
        flags.add("has_source_oxygenation_gaps")
    return sorted(flags)


def _cell_flags(
    route_directive: str,
    metrics: dict[str, float],
    outcomes: list[dict[str, Any]],
) -> list[str]:
    flags = {f"perfusion_route:{route_directive}"}
    if not outcomes:
        flags.add("no_price_outcome")
    if metrics.get("source_oxygenation", 0.0) < 0.45:
        flags.add("low_source_oxygenation")
    if metrics.get("resistance", 0.0) >= 0.62:
        flags.add("high_resistance")
    if metrics.get("pressure_gradient", 0.0) >= 0.50:
        flags.add("high_pressure_gradient")
    if metrics.get("price_absorption", 0.0) <= 0.35 and metrics.get("information_pressure", 0.0) >= 0.50:
        flags.add("information_not_absorbed_by_price")
    return sorted(flags)


def _ensure_information_perfusion_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS information_perfusion_snapshots (
            id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            cell_key TEXT NOT NULL,
            entity TEXT,
            theme TEXT,
            horizon TEXT,
            lens TEXT,
            string_count INTEGER NOT NULL DEFAULT 0,
            scout_count INTEGER NOT NULL DEFAULT 0,
            outcome_count INTEGER NOT NULL DEFAULT 0,
            observed_outcome_count INTEGER NOT NULL DEFAULT 0,
            source_families_json TEXT NOT NULL DEFAULT '[]',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            route_directive TEXT NOT NULL DEFAULT 'maintain',
            recommended_scouts INTEGER NOT NULL DEFAULT 0,
            suggested_tools_json TEXT NOT NULL DEFAULT '[]',
            quality_flags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_perfusion_cycle "
        "ON information_perfusion_snapshots(cycle_id, recommended_scouts DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_perfusion_route "
        "ON information_perfusion_snapshots(route_directive, cycle_id)"
    )


def _perfusion_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for src, dst, default in (
        ("source_families_json", "source_families", []),
        ("metrics_json", "metrics", {}),
        ("suggested_tools_json", "suggested_tools", []),
        ("quality_flags", "quality_flags", []),
    ):
        try:
            d[dst] = json.loads(d.get(src) or json.dumps(default))
        except Exception:
            d[dst] = default
        d.pop(src, None)
    return d


def _cell_key(row: dict[str, Any]) -> str:
    explicit = str(row.get("coverage_cell_key") or "").strip()
    if explicit:
        return explicit
    return "|".join([
        str(row.get("entity") or "UNKNOWN"),
        str(row.get("horizon") or row.get("time_horizon") or "intraday"),
        str(row.get("lens") or "anomaly"),
        str(row.get("theme") or ""),
    ])


def _source_families(row: dict[str, Any]) -> list[str]:
    families: list[str] = []
    for flag in _string_list(row.get("quality_flags")):
        if flag.startswith("source_family:"):
            families.append(flag.split(":", 1)[1])
    for ref in [*_string_list(row.get("evidence_refs")), *_string_list(row.get("source_tool_call_ids"))]:
        family = _family_from_ref(ref)
        if family:
            families.append(family)
    return _dedupe(families)


def _family_from_ref(ref: str) -> str:
    text = str(ref or "").lower()
    if any(tok in text for tok in ("grok", "x_search", "twitter", "x.com")):
        return "grok_x_alpha"
    if "hydromancer" in text or "wallet" in text or "builder" in text:
        return "hydromancer"
    if "our_hl_node" in text or "node" in text or "mempool" in text:
        return "our_hl_node"
    if "orderbook" in text or "funding" in text or "microstructure" in text:
        return "market_microstructure"
    if "web" in text or "parallel" in text or "news" in text:
        return "web_attention"
    if "sec" in text or "filing" in text or "analyst" in text:
        return "fundamentals_filings"
    if "macro" in text or "fred" in text or "fomc" in text:
        return "macro_official"
    return ""


def _outcome_observed(row: dict[str, Any]) -> bool:
    flags = _string_list(row.get("quality_flags"))
    return (
        "missing_baseline_price" not in flags
        and "missing_outcome_price" not in flags
        and str(row.get("expected_direction") or "") in {"up", "down"}
    )


def _temporal_staleness(rows: list[dict[str, Any]]) -> float:
    now = datetime.now(timezone.utc)
    penalties: list[float] = []
    for row in rows:
        if any("stale" in flag for flag in _string_list(row.get("quality_flags"))):
            penalties.append(0.85)
            continue
        expires = _parse_dt(str(row.get("expires_at") or ""))
        if expires is not None:
            penalties.append(1.0 if expires < now else 0.0)
            continue
        observed = _parse_dt(str(row.get("observed_at") or row.get("ingested_at") or ""))
        if observed is None:
            penalties.append(0.35)
            continue
        age_h = max(0.0, (now - observed).total_seconds() / 3600.0)
        horizon_h = _horizon_hours(str(row.get("time_horizon") or row.get("horizon") or ""))
        penalties.append(_clamp01(age_h / max(1.0, horizon_h * 2.0)))
    return _avg(penalties)


def _horizon_hours(raw: str) -> float:
    text = str(raw or "").lower()
    if "tick" in text or "minute" in text:
        return 1.0
    if "hour" in text or "intraday" in text:
        return 8.0
    if "1d" in text or "day" in text:
        return 24.0
    if "1w" in text or "week" in text:
        return 24.0 * 7.0
    if "1m" in text or "month" in text:
        return 24.0 * 30.0
    return 24.0 * 90.0


def _fallback_fragility(rows: list[dict[str, Any]], source_oxygenation: float, evidence_coverage: float) -> float:
    row_flags = [flag for row in rows for flag in _string_list(row.get("quality_flags"))]
    failure_rate = sum(
        1
        for flag in row_flags
        if flag in {"missing_mechanism", "missing_depth_layers", "failed_call_as_evidence"}
    ) / max(1, len(row_flags))
    return _clamp01((1.0 - source_oxygenation) * 0.50 + (1.0 - evidence_coverage) * 0.30 + failure_rate * 0.20)


def _perfusion_id(cycle_id: str, cell_key: str) -> str:
    return "iperf_" + hashlib.sha256(f"{cycle_id}|{cell_key}".encode()).hexdigest()[:16]


def _parse_dt(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _entropy(values: list[str]) -> float:
    if not values:
        return 0.0
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    total = float(sum(counts.values()) or 1)
    return -sum((count / total) * math.log(count / total, 2) for count in counts.values()) / max(1.0, math.log(max(2, len(counts)), 2))


def _avg(values: list[float]) -> float:
    clean = [float(value) for value in values if value is not None]
    return sum(clean) / len(clean) if clean else 0.0


def _float(raw: Any, default: float) -> float:
    try:
        return float(raw)
    except Exception:
        return float(default)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value or 0.0)))


def _string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return [raw] if raw.strip() else []
        return _string_list(parsed)
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item or "").strip()]
    return []


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
