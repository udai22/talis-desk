"""Outcome evaluation for information strings.

AlphaEvolve-style learning needs an external score. For Talis, the minimum
external score is whether a string saw information pressure before price
started moving in the implied direction. This module keeps that loop explicit:
price observations come from adapters or fixtures, outcomes are persisted, and
MarketEvolve can score policy changes against realized repricing skill.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ..store import get_desk_store
from .store import recent_information_strings


OUTCOME_EVALUATOR_VERSION = "information_price_outcome_v1"


@dataclass
class PriceObservation:
    entity: str
    observed_at: str
    price: float
    source: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    observation_id: str = ""


def persist_price_observations(
    observations: Iterable[PriceObservation | dict[str, Any]],
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> list[str]:
    db = conn or get_desk_store().conn
    _ensure_outcome_tables(db)
    now = _now()
    ids: list[str] = []
    for raw in observations:
        obs = normalize_price_observation(raw)
        if obs is None:
            continue
        obs_id = obs.observation_id or _price_observation_id(obs)
        ids.append(obs_id)
        db.execute(
            """
            INSERT OR REPLACE INTO price_observations (
                id, entity, observed_at, price, source, payload_json,
                created_at, valid_from, transaction_from, transaction_to
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                obs_id,
                obs.entity,
                _normalize_time(obs.observed_at),
                float(obs.price),
                obs.source,
                json.dumps(obs.payload, sort_keys=True),
                now,
                now,
                now,
            ),
        )
    db.commit()
    return ids


def normalize_price_observation(raw: PriceObservation | dict[str, Any]) -> Optional[PriceObservation]:
    if isinstance(raw, PriceObservation):
        return raw if raw.entity and raw.observed_at and raw.price > 0 else None
    if not isinstance(raw, dict):
        return None
    entity = str(raw.get("entity") or raw.get("asset") or raw.get("symbol") or "").strip()
    observed_at = str(raw.get("observed_at") or raw.get("time") or raw.get("timestamp") or raw.get("ts") or "").strip()
    price = _float(raw.get("price") or raw.get("mark_price") or raw.get("mid") or raw.get("close"), 0.0)
    if not entity or not observed_at or price <= 0:
        return None
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {
        k: v for k, v in raw.items()
        if k not in {"entity", "asset", "symbol", "observed_at", "time", "timestamp", "ts", "price", "mark_price", "mid", "close", "source", "id"}
    }
    return PriceObservation(
        entity=entity,
        observed_at=observed_at,
        price=price,
        source=str(raw.get("source") or "").strip(),
        payload=payload,
        observation_id=str(raw.get("id") or raw.get("observation_id") or "").strip(),
    )


def evaluate_information_price_outcomes(
    *,
    cycle_id: str,
    price_observations: Optional[Iterable[PriceObservation | dict[str, Any]]] = None,
    min_move_threshold_pct: float = 0.02,
    limit: int = 500,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    db = conn or get_desk_store().conn
    _ensure_outcome_tables(db)
    if price_observations is not None:
        persist_price_observations(price_observations, conn=db)
    rows = recent_information_strings(cycle_id=cycle_id, limit=limit, conn=db)
    outcomes: list[dict[str, Any]] = []
    for row in rows:
        outcome = _evaluate_one_string(
            row,
            min_move_threshold_pct=max(0.0001, float(min_move_threshold_pct)),
            conn=db,
        )
        _persist_outcome(outcome, conn=db)
        outcomes.append(outcome)
    summary = summarize_information_price_outcomes(cycle_id=cycle_id, conn=db)
    return {
        "schema_version": "information_price_outcome_report_v1",
        "cycle_id": cycle_id,
        "evaluator_version": OUTCOME_EVALUATOR_VERSION,
        "evaluated_count": len(outcomes),
        "summary": summary,
        "outcomes": outcomes[:100],
    }


def load_information_price_outcomes(
    *,
    cycle_id: str = "",
    string_id: str = "",
    limit: int = 200,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    db = conn or get_desk_store().conn
    _ensure_outcome_tables(db)
    clauses = ["transaction_to IS NULL"]
    params: list[Any] = []
    if cycle_id:
        clauses.append("cycle_id = ?")
        params.append(cycle_id)
    if string_id:
        clauses.append("string_id = ?")
        params.append(string_id)
    params.append(int(limit))
    rows = db.execute(
        f"""
        SELECT *
        FROM information_string_outcomes
        WHERE {' AND '.join(clauses)}
        ORDER BY created_at DESC, realized_edge_score DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [_outcome_row_to_dict(row) for row in rows]


def summarize_information_price_outcomes(
    *,
    cycle_id: str,
    seed_ids: Optional[list[str]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, float]:
    db = conn or get_desk_store().conn
    _ensure_outcome_tables(db)
    if seed_ids:
        placeholders = ",".join("?" for _ in seed_ids)
        rows = db.execute(
            f"""
            SELECT o.*
            FROM information_string_outcomes o
            JOIN information_strings s ON s.id = o.string_id
            WHERE o.cycle_id = ?
              AND s.seed_id IN ({placeholders})
              AND o.transaction_to IS NULL
            """,
            (cycle_id, *seed_ids),
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT *
            FROM information_string_outcomes
            WHERE cycle_id = ? AND transaction_to IS NULL
            """,
            (cycle_id,),
        ).fetchall()
    outcomes = [_outcome_row_to_dict(row) for row in rows]
    observed = [
        row for row in outcomes
        if "missing_baseline_price" not in row["quality_flags"]
        and "missing_outcome_price" not in row["quality_flags"]
        and row["expected_direction"] in {"up", "down"}
    ]
    n = float(len(outcomes))
    observed_n = float(len(observed))
    hits = [row for row in observed if row["direction_hit"]]
    threshold_hits = [row for row in observed if row["threshold_hit"]]
    return {
        "outcome_eval_count": n,
        "outcome_observed_count": observed_n,
        "outcome_observed_rate": observed_n / max(1.0, n),
        "outcome_direction_hit_rate": float(len(hits)) / max(1.0, observed_n),
        "outcome_threshold_hit_rate": float(len(threshold_hits)) / max(1.0, observed_n),
        "avg_realized_edge_score": _avg([float(row["realized_edge_score"]) for row in observed]),
        "avg_abs_price_return_pct": _avg([abs(float(row["price_return_pct"])) for row in observed]),
        "avg_signed_return_pct": _avg([float(row["signed_return_pct"]) for row in observed]),
        "early_repricing_hit_rate": float(len(threshold_hits)) / max(1.0, observed_n),
    }


def _evaluate_one_string(
    row: dict[str, Any],
    *,
    min_move_threshold_pct: float,
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    entity = str(row.get("entity") or "")
    string_id = str(row.get("id") or "")
    cycle_id = str(row.get("cycle_id") or "")
    baseline_time = _string_baseline_time(row)
    horizon_minutes = _horizon_minutes(row)
    expected_direction = _infer_expected_direction(row)
    flags: list[str] = []
    if expected_direction == "unknown":
        flags.append("unknown_expected_direction")
    baseline = _price_at_or_before(entity, baseline_time, conn=conn)
    outcome = _price_at_or_after(entity, baseline_time, horizon_minutes=horizon_minutes, conn=conn)
    if baseline is None:
        flags.append("missing_baseline_price")
    if outcome is None:
        flags.append("missing_outcome_price")
    price_return = 0.0
    signed_return = 0.0
    direction_hit = False
    threshold_hit = False
    realized_edge_score = 0.0
    if baseline is not None and outcome is not None:
        price_return = (float(outcome["price"]) - float(baseline["price"])) / max(1e-12, float(baseline["price"]))
        signed_return = price_return if expected_direction == "up" else -price_return if expected_direction == "down" else 0.0
        direction_hit = signed_return > 0.0
        threshold_hit = signed_return >= min_move_threshold_pct
        realized_edge_score = _clamp01(0.5 + signed_return / max(min_move_threshold_pct * 2.0, 1e-12))
        if abs(price_return) < min_move_threshold_pct:
            flags.append("price_move_below_threshold")
        if direction_hit:
            flags.append("direction_hit")
        if threshold_hit:
            flags.append("threshold_hit")
    return {
        "id": _outcome_id(string_id=string_id, cycle_id=cycle_id),
        "string_id": string_id,
        "cycle_id": cycle_id,
        "entity": entity,
        "expected_direction": expected_direction,
        "horizon_minutes": horizon_minutes,
        "baseline_price": baseline.get("price") if baseline else None,
        "baseline_at": baseline.get("observed_at") if baseline else "",
        "outcome_price": outcome.get("price") if outcome else None,
        "outcome_at": outcome.get("observed_at") if outcome else "",
        "price_return_pct": round(price_return, 6),
        "signed_return_pct": round(signed_return, 6),
        "direction_hit": direction_hit,
        "threshold_hit": threshold_hit,
        "realized_edge_score": round(realized_edge_score, 4),
        "lead_time_minutes": _minutes_between(baseline.get("observed_at") if baseline else "", outcome.get("observed_at") if outcome else ""),
        "evaluator_version": OUTCOME_EVALUATOR_VERSION,
        "quality_flags": sorted(set(flags)),
    }


def _persist_outcome(outcome: dict[str, Any], *, conn: sqlite3.Connection) -> None:
    now = _now()
    conn.execute(
        """
        INSERT OR REPLACE INTO information_string_outcomes (
            id, string_id, cycle_id, entity, expected_direction,
            horizon_minutes, baseline_price, baseline_at, outcome_price,
            outcome_at, price_return_pct, signed_return_pct, direction_hit,
            threshold_hit, realized_edge_score, lead_time_minutes,
            evaluator_version, quality_flags, created_at, valid_from,
            transaction_from, transaction_to
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            outcome["id"],
            outcome["string_id"],
            outcome["cycle_id"],
            outcome["entity"],
            outcome["expected_direction"],
            float(outcome["horizon_minutes"]),
            outcome["baseline_price"],
            outcome["baseline_at"],
            outcome["outcome_price"],
            outcome["outcome_at"],
            float(outcome["price_return_pct"]),
            float(outcome["signed_return_pct"]),
            1 if outcome["direction_hit"] else 0,
            1 if outcome["threshold_hit"] else 0,
            float(outcome["realized_edge_score"]),
            float(outcome["lead_time_minutes"]),
            outcome["evaluator_version"],
            json.dumps(outcome["quality_flags"], sort_keys=True),
            now,
            now,
            now,
        ),
    )
    conn.commit()


def _price_at_or_before(entity: str, observed_at: str, *, conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT *
        FROM price_observations
        WHERE entity = ?
          AND observed_at <= ?
          AND transaction_to IS NULL
        ORDER BY observed_at DESC
        LIMIT 1
        """,
        (entity, observed_at),
    ).fetchone()
    return dict(row) if row else None


def _price_at_or_after(
    entity: str,
    observed_at: str,
    *,
    horizon_minutes: float,
    conn: sqlite3.Connection,
) -> Optional[dict[str, Any]]:
    target = _add_minutes(observed_at, max(1.0, horizon_minutes))
    row = conn.execute(
        """
        SELECT *
        FROM price_observations
        WHERE entity = ?
          AND observed_at >= ?
          AND transaction_to IS NULL
        ORDER BY observed_at ASC
        LIMIT 1
        """,
        (entity, target),
    ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT *
            FROM price_observations
            WHERE entity = ?
              AND observed_at > ?
              AND transaction_to IS NULL
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            (entity, observed_at),
        ).fetchone()
    return dict(row) if row else None


def _ensure_outcome_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_observations (
            id TEXT PRIMARY KEY,
            entity TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            price REAL NOT NULL,
            source TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_price_observations_entity_time "
        "ON price_observations(entity, observed_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS information_string_outcomes (
            id TEXT PRIMARY KEY,
            string_id TEXT NOT NULL,
            cycle_id TEXT NOT NULL,
            entity TEXT,
            expected_direction TEXT NOT NULL,
            horizon_minutes REAL NOT NULL DEFAULT 0.0,
            baseline_price REAL,
            baseline_at TEXT,
            outcome_price REAL,
            outcome_at TEXT,
            price_return_pct REAL NOT NULL DEFAULT 0.0,
            signed_return_pct REAL NOT NULL DEFAULT 0.0,
            direction_hit INTEGER NOT NULL DEFAULT 0,
            threshold_hit INTEGER NOT NULL DEFAULT 0,
            realized_edge_score REAL NOT NULL DEFAULT 0.0,
            lead_time_minutes REAL NOT NULL DEFAULT 0.0,
            evaluator_version TEXT NOT NULL,
            quality_flags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            transaction_from TEXT NOT NULL,
            transaction_to TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_outcomes_cycle "
        "ON information_string_outcomes(cycle_id, realized_edge_score DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_outcomes_string "
        "ON information_string_outcomes(string_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_info_outcomes_entity_time "
        "ON information_string_outcomes(entity, outcome_at)"
    )


def _string_baseline_time(row: dict[str, Any]) -> str:
    return _normalize_time(
        str(
            row.get("observed_at")
            or row.get("event_time_start")
            or row.get("ingested_at")
            or row.get("valid_from")
            or _now()
        )
    )


def _horizon_minutes(row: dict[str, Any]) -> float:
    raw = " ".join(str(row.get(k) or "") for k in ("time_horizon", "time_scale", "horizon")).lower()
    if "structural" in raw or "quarter" in raw or "1q" in raw:
        return 60.0 * 24.0 * 90.0
    if "month" in raw or "1m" in raw:
        return 60.0 * 24.0 * 30.0
    if "week" in raw or "1w" in raw:
        return 60.0 * 24.0 * 7.0
    if "day" in raw or "1d" in raw:
        return 60.0 * 24.0
    if "intraday" in raw:
        return 240.0
    if "hour" in raw:
        return 60.0
    if "minute" in raw or "tick" in raw:
        return 15.0
    return 60.0


def _infer_expected_direction(row: dict[str, Any]) -> str:
    text = " ".join(
        str(row.get(k) or "")
        for k in ("title", "thesis", "mechanism", "expected_outcome", "kill_signal")
    ).lower()
    up_tokens = (
        "up", "upside", "higher", "rise", "rises", "reprices higher", "priced upwards",
        "bull", "bullish", "long", "squeeze", "breakout", "bid", "accumulate",
    )
    down_tokens = (
        "down", "downside", "lower", "fall", "falls", "selloff", "sell-off",
        "bear", "bearish", "short", "unstake", "sell pressure", "supply overhang",
    )
    up = sum(1 for tok in up_tokens if tok in text)
    down = sum(1 for tok in down_tokens if tok in text)
    if up > down:
        return "up"
    if down > up:
        return "down"
    return "unknown"


def _outcome_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["direction_hit"] = bool(d.get("direction_hit"))
    d["threshold_hit"] = bool(d.get("threshold_hit"))
    d["quality_flags"] = _json_load(d.get("quality_flags"), [])
    return d


def _price_observation_id(obs: PriceObservation) -> str:
    raw = f"{obs.entity}|{_normalize_time(obs.observed_at)}|{float(obs.price):.12g}|{obs.source}"
    return "pobs_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _outcome_id(*, string_id: str, cycle_id: str) -> str:
    return "iout_" + hashlib.sha256(f"{cycle_id}|{string_id}".encode()).hexdigest()[:16]


def _normalize_time(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return _now()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc).isoformat()
    except Exception:
        return text


def _parse_time(raw: str) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except Exception:
        return None


def _add_minutes(raw: str, minutes: float) -> str:
    dt = _parse_time(raw)
    if dt is None:
        return raw
    return datetime.fromtimestamp(
        dt.timestamp() + float(minutes) * 60.0,
        tz=timezone.utc,
    ).isoformat()


def _minutes_between(a: str, b: str) -> float:
    da = _parse_time(a)
    db = _parse_time(b)
    if da is None or db is None:
        return 0.0
    return round(max(0.0, (db - da).total_seconds() / 60.0), 4)


def _float(raw: Any, default: float) -> float:
    try:
        return float(raw)
    except Exception:
        return default


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 6) if values else 0.0


def _clamp01(raw: float) -> float:
    return max(0.0, min(1.0, float(raw)))


def _json_load(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "OUTCOME_EVALUATOR_VERSION",
    "PriceObservation",
    "evaluate_information_price_outcomes",
    "load_information_price_outcomes",
    "normalize_price_observation",
    "persist_price_observations",
    "summarize_information_price_outcomes",
]
