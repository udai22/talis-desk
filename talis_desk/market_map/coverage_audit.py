"""Deterministic market-map coverage audit.

This answers the uncomfortable question directly: given the known universe,
which market cells are covered, stale, or missing?
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .universe import build_market_universe


def build_coverage_gap_manifest(
    *,
    cycle_id: str,
    conn: sqlite3.Connection,
    stale_after_hours: float = 72.0,
    max_gap_examples: int = 200,
) -> dict[str, Any]:
    from ..swarm.seed_generator import (
        BIAS_MODES,
        DEFAULT_ENTITIES,
        HORIZONS,
        valid_lenses_for_entity,
    )

    universe = build_market_universe(default_entities=DEFAULT_ENTITIES)
    entities = universe.entity_symbols()
    valid_cells: list[tuple[str, str, str, str]] = []
    for entity in entities:
        for horizon in HORIZONS:
            for lens in valid_lenses_for_entity(entity):
                for bias in BIAS_MODES:
                    valid_cells.append((entity.upper(), horizon, lens, bias))

    valid_set = set(valid_cells)
    coverage_rows = _coverage_rows(conn)
    covered: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    stale_cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_after_hours)
    stale: list[dict[str, Any]] = []
    out_of_universe: list[dict[str, Any]] = []
    for row in coverage_rows:
        key = (
            str(row.get("entity") or "").upper(),
            str(row.get("horizon") or ""),
            str(row.get("lens") or ""),
            str(row.get("bias_mode") or ""),
        )
        if key not in valid_set:
            out_of_universe.append(row)
            continue
        existing = covered.get(key)
        if existing is None or str(row.get("last_sampled_at") or "") > str(existing.get("last_sampled_at") or ""):
            covered[key] = row
        last_sampled = _parse_time(row.get("last_sampled_at"))
        if last_sampled and last_sampled < stale_cutoff:
            stale.append(row)

    covered_set = set(covered)
    missing_cells = [cell for cell in valid_cells if cell not in covered_set]
    route_counts: dict[str, int] = {}
    for row in coverage_rows:
        source = str(row.get("source") or "unknown")
        route_counts[source] = route_counts.get(source, 0) + 1

    return {
        "schema_version": "coverage_gap_manifest_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cycle_id": cycle_id,
        "universe": {
            "entity_count": len(entities),
            "source_quality": universe.source_quality,
            "source_counts": universe.source_counts,
            "errors": list(universe.errors),
        },
        "grid": {
            "valid_cell_count": len(valid_cells),
            "axes": {
                "entity": len(entities),
                "horizon": len(HORIZONS),
                "bias_mode": len(BIAS_MODES),
            },
        },
        "coverage": {
            "covered_count": len(covered_set),
            "missing_count": len(missing_cells),
            "stale_count": len(stale),
            "out_of_universe_count": len(out_of_universe),
            "coverage_ratio": round(len(covered_set) / max(1, len(valid_cells)), 6),
            "route_counts": route_counts,
        },
        "gap_examples": [
            _cell_to_dict(cell)
            for cell in missing_cells[:max(0, int(max_gap_examples))]
        ],
        "stale_examples": stale[:50],
        "out_of_universe_examples": out_of_universe[:50],
        "completion_claim": (
            "A full map is proven only when covered_count approaches valid_cell_count "
            "within the freshness window and every remaining missing cell is intentionally "
            "excluded, expired, or delegated to a work order."
        ),
    }


def _coverage_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(conn, "coverage_cells"):
        return []
    rows = conn.execute(
        """
        SELECT cell_key, entity, horizon, lens, source, bias_mode, theme,
               n_samples, n_promoted, n_killed, novelty_score, density_score,
               expected_value_usd, last_sampled_at, last_promoted_at, payload
        FROM coverage_cells
        WHERE transaction_to IS NULL
        """
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _row_to_dict(row: Any) -> dict[str, Any]:
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(row)


def _parse_time(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _cell_to_dict(cell: tuple[str, str, str, str]) -> dict[str, str]:
    entity, horizon, lens, bias_mode = cell
    return {
        "entity": entity,
        "horizon": horizon,
        "lens": lens,
        "bias_mode": bias_mode,
        "status": "missing",
        "repair_action": "assign_seed_or_expire_intentionally",
    }


__all__ = ["build_coverage_gap_manifest"]
