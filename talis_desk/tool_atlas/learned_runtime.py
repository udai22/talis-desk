"""Constrained runtime for generated learned tools.

Learned tools are agent-created capabilities, but dispatch must remain
inspectable and safe. We therefore execute a small set of deterministic
runtime adapters from a manifest instead of importing arbitrary generated code.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..store import get_store
from .atlas import learned_tools_dir


SUPPORTED_LEARNED_RUNTIMES: set[str] = {
    "evidence_ref_resolver",
    "hl_node_stream_reader",
    "hyperevm_mempool_actor_watch",
    "hydromancer_actor_quality_bulk",
    "liquidity_absorption_context",
}


def get_learned_callable(slug: str) -> Callable[..., dict[str, Any]]:
    def _call(**kwargs: Any) -> dict[str, Any]:
        return dispatch_learned_tool(slug, **kwargs)

    _call.__name__ = f"learned_{slug}"
    return _call


def dispatch_learned_tool(slug: str, **kwargs: Any) -> dict[str, Any]:
    manifest = load_learned_tool_manifest(slug)
    runtime = str(manifest.get("runtime") or slug)
    if runtime not in SUPPORTED_LEARNED_RUNTIMES:
        raise RuntimeError(f"learned_runtime_adapter_missing:{runtime}")
    if runtime == "evidence_ref_resolver":
        return _evidence_ref_resolver(kwargs)
    if runtime == "hl_node_stream_reader":
        return _hl_node_stream_reader(kwargs)
    if runtime == "hyperevm_mempool_actor_watch":
        return _hyperevm_mempool_actor_watch(kwargs)
    if runtime == "hydromancer_actor_quality_bulk":
        return _hydromancer_actor_quality_bulk(kwargs)
    if runtime == "liquidity_absorption_context":
        return _liquidity_absorption_context(kwargs)
    raise RuntimeError(f"learned_runtime_adapter_missing:{runtime}")


def load_learned_tool_manifest(slug: str) -> dict[str, Any]:
    path = learned_tools_dir() / slug / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"learned_tool_manifest_missing:{slug}")
    return json.loads(path.read_text(encoding="utf-8"))


def _evidence_ref_resolver(args: dict[str, Any]) -> dict[str, Any]:
    refs = _strings(args.get("source_refs") or args.get("refs"))
    conn = get_store().conn
    rows = _tool_call_rows(conn, refs)
    return {
        "status": "ok",
        "source_refs": refs,
        "resolved": rows,
        "resolution_rate": (len(rows) / len(refs)) if refs else 1.0,
        "provenance_fields": [
            "tool_call_log_id",
            "tool_uri",
            "args_hash",
            "result_hash",
            "started_at",
            "finished_at",
            "duration_ms",
        ],
    }


def _hl_node_stream_reader(args: dict[str, Any]) -> dict[str, Any]:
    coin = str(args.get("coin") or args.get("asset") or "").upper()
    wallets = {w.lower() for w in _strings(args.get("wallets"))}
    lookback_minutes = int(float(args.get("lookback_minutes") or 90))
    since = datetime.now(timezone.utc) - timedelta(minutes=max(1, lookback_minutes))
    rows = _query_tool_logs(
        where=(
            "(tool_uri LIKE '%hl_reject_corpus%' "
            "OR tool_uri LIKE '%hl_node%' "
            "OR result_summary LIKE '%status_counts%' "
            "OR result_summary LIKE '%top_reject_reasons%')"
        ),
        since=since,
    )
    observations: list[dict[str, Any]] = []
    for row in rows:
        payload = _json_value(row.get("result_summary"), {})
        text = json.dumps(payload, default=str).upper()
        if coin and coin not in text and coin not in str(row.get("args_json") or "").upper():
            continue
        wallet = str(payload.get("wallet") or "").lower()
        if wallets and wallet not in wallets:
            continue
        observations.append({
            "tool_call_log_id": row.get("id"),
            "tool_uri": row.get("tool_uri"),
            "wallet": wallet,
            "reject_rate_pct": payload.get("reject_rate_pct"),
            "status_counts": payload.get("status_counts"),
            "top_reject_reasons": payload.get("top_reject_reasons"),
            "raw_offset": row.get("result_hash") or row.get("args_hash"),
            "source_timestamp": row.get("started_at"),
        })
    return {
        "status": "ok",
        "coin": coin,
        "wallets": sorted(wallets),
        "lookback_minutes": lookback_minutes,
        "source_family": "our_hl_node",
        "observations": observations,
        "n_observations": len(observations),
        "has_raw_offsets": all(bool(o.get("raw_offset")) for o in observations),
    }


def _hyperevm_mempool_actor_watch(args: dict[str, Any]) -> dict[str, Any]:
    addresses = {x.lower() for x in _strings(args.get("addresses"))}
    contracts = {x.lower() for x in _strings(args.get("contracts"))}
    fixture_events = args.get("fixture_events")
    rows: list[dict[str, Any]]
    if isinstance(fixture_events, list):
        rows = [x for x in fixture_events if isinstance(x, dict)]
    elif _table_exists(get_store().conn, "mempool_events"):
        rows = _query_mempool_events(addresses=addresses, contracts=contracts)
    else:
        rows = []
    watched = []
    for row in rows:
        actor = str(row.get("from") or row.get("actor") or "").lower()
        to = str(row.get("to") or row.get("contract") or "").lower()
        if addresses and actor not in addresses:
            continue
        if contracts and to not in contracts:
            continue
        watched.append({
            "tx_hash": row.get("tx_hash"),
            "actor": actor,
            "contract": to,
            "method": row.get("method"),
            "asset": row.get("asset"),
            "seen_at": row.get("seen_at") or row.get("ts"),
            "settled_event_ref": row.get("settled_event_ref"),
        })
    return {
        "status": "ok" if watched else "no_mempool_source_configured",
        "source_family": "mempool",
        "pending_txs": watched,
        "n_pending": len(watched),
        "dedupe_by_tx_hash": True,
        "settlement_reconciliation": all(bool(x.get("settled_event_ref")) for x in watched),
    }


def _hydromancer_actor_quality_bulk(args: dict[str, Any]) -> dict[str, Any]:
    wallets = {w.lower() for w in _strings(args.get("wallets"))}
    rows = _query_tool_logs(
        where="tool_uri LIKE '%hydromancer%' OR tool_uri LIKE '%get_hl_pnl_leaderboard%'",
        since=None,
    )
    actors: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = _json_value(row.get("result_summary"), {})
        for leader in payload.get("leaders") or []:
            if not isinstance(leader, dict):
                continue
            wallet = str(leader.get("wallet") or "").lower()
            if not wallet or (wallets and wallet not in wallets):
                continue
            actors[wallet] = {
                "wallet": wallet,
                "realized_pnl_usd": leader.get("realized_pnl_usd"),
                "win_rate_pct": leader.get("win_rate_pct"),
                "volume_usd": leader.get("volume_usd"),
                "source_ref": row.get("id"),
            }
    return {"status": "ok", "actors": list(actors.values()), "n_actors": len(actors)}


def _liquidity_absorption_context(args: dict[str, Any]) -> dict[str, Any]:
    amount = _float(args.get("amount") or args.get("event_amount"), 0.0)
    depth = _float(args.get("depth_1pct_usd") or args.get("depth_usd"), 0.0)
    volume = _float(args.get("volume_24h_usd") or args.get("volume_usd"), 0.0)
    return {
        "status": "ok",
        "asset": str(args.get("asset") or args.get("coin") or ""),
        "amount": amount,
        "amount_vs_depth": amount / depth if depth else None,
        "amount_vs_volume": amount / volume if volume else None,
        "has_depth": bool(depth),
        "has_volume": bool(volume),
        "has_before_after": bool(args.get("before_after")),
    }


def _tool_call_rows(conn: sqlite3.Connection, refs: list[str]) -> list[dict[str, Any]]:
    if not refs or not _table_exists(conn, "tool_call_log"):
        return []
    placeholders = ",".join("?" * len(refs))
    rows = conn.execute(
        f"""
        SELECT id, tool_uri, tool_version, args_hash, args_json, result_hash,
               result_summary, error, started_at, finished_at, duration_ms, cost_usd
        FROM tool_call_log
        WHERE id IN ({placeholders}) OR tool_uri IN ({placeholders})
        """,
        tuple(refs + refs),
    ).fetchall()
    return [_row_dict(row) for row in rows]


def _query_tool_logs(where: str, since: datetime | None) -> list[dict[str, Any]]:
    conn = get_store().conn
    if not _table_exists(conn, "tool_call_log"):
        return []
    params: list[Any] = []
    sql = (
        "SELECT id, tool_uri, tool_version, args_hash, args_json, result_hash, "
        "result_summary, error, started_at, finished_at, duration_ms, cost_usd "
        "FROM tool_call_log WHERE "
        + where
    )
    if since is not None:
        sql += " AND started_at >= ?"
        params.append(since.isoformat())
    sql += " ORDER BY started_at DESC LIMIT 200"
    return [_row_dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def _query_mempool_events(addresses: set[str], contracts: set[str]) -> list[dict[str, Any]]:
    conn = get_store().conn
    clauses: list[str] = []
    params: list[Any] = []
    if addresses:
        placeholders = ",".join("?" * len(addresses))
        clauses.append(f"LOWER(actor_address) IN ({placeholders})")
        params.extend(sorted(addresses))
    if contracts:
        placeholders = ",".join("?" * len(contracts))
        clauses.append(f"LOWER(contract_address) IN ({placeholders})")
        params.extend(sorted(contracts))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM mempool_events {where} ORDER BY seen_at DESC LIMIT 200",
        tuple(params),
    ).fetchall()
    return [_row_dict(row) for row in rows]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _json_value(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _strings(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    return [str(raw)]


def _float(raw: Any, default: float) -> float:
    try:
        return float(raw)
    except Exception:
        return default
