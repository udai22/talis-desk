"""Durable information strings and graph updates.

The important design choice: cheap scouts do not merely produce ephemeral
hypotheses. They write causal strings that survive the cycle and update a
market-information graph. Verification and PM layers still gate trades; this
module only stores the desk's evolving perception.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ..store import get_desk_store


@dataclass
class InformationString:
    """One causal thread emitted by a scout."""

    title: str
    thesis: str
    mechanism: str = ""
    expected_outcome: str = ""
    time_horizon: str = ""
    time_scale: str = ""
    event_time_start: str = ""
    event_time_end: str = ""
    observed_at: str = ""
    ingested_at: str = ""
    source_time_basis: str = ""
    kill_signal: str = ""
    extends_or_contradicts: str = "new"
    would_change_decision: bool = True
    expires_at: str = ""
    crowdedness: float = 0.5
    conviction: float = 0.5
    novelty_score: float = 0.5
    entities_chain: list[str] = field(default_factory=list)
    depth_layers: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    prior_thread_refs: list[str] = field(default_factory=list)
    rollup_parent_ids: list[str] = field(default_factory=list)
    lower_timeframe_refs: list[str] = field(default_factory=list)
    higher_timeframe_context_refs: list[str] = field(default_factory=list)
    temporal_confidence: float = 0.5
    quality_flags: list[str] = field(default_factory=list)
    string_id: Optional[str] = None


def normalize_information_string(raw: Any) -> Optional[InformationString]:
    if not isinstance(raw, dict):
        return None
    thesis = str(raw.get("thesis") or raw.get("hypothesis") or "").strip()
    mechanism = str(raw.get("mechanism") or "").strip()
    if not thesis:
        return None
    title = str(raw.get("title") or thesis[:96]).strip()[:140]
    return InformationString(
        title=title,
        thesis=thesis[:2000],
        mechanism=mechanism[:2000],
        expected_outcome=str(raw.get("expected_outcome") or "").strip()[:1000],
        time_horizon=str(raw.get("time_horizon") or raw.get("horizon") or "").strip()[:120],
        time_scale=str(raw.get("time_scale") or raw.get("time_horizon") or raw.get("horizon") or "").strip()[:80],
        event_time_start=str(raw.get("event_time_start") or "").strip()[:120],
        event_time_end=str(raw.get("event_time_end") or "").strip()[:120],
        observed_at=str(raw.get("observed_at") or "").strip()[:120],
        ingested_at=str(raw.get("ingested_at") or "").strip()[:120],
        source_time_basis=str(raw.get("source_time_basis") or "").strip()[:120],
        kill_signal=str(raw.get("kill_signal") or "").strip()[:1000],
        extends_or_contradicts=_relation(raw.get("extends_or_contradicts")),
        would_change_decision=_bool(raw.get("would_change_decision"), default=True),
        expires_at=str(raw.get("expires_at") or "").strip()[:120],
        crowdedness=_clamp01(raw.get("crowdedness"), 0.5),
        conviction=_clamp01(raw.get("conviction"), _clamp01(raw.get("confidence"), 0.5)),
        novelty_score=_clamp01(raw.get("novelty_score"), 0.5),
        entities_chain=_string_list(raw.get("entities_chain") or raw.get("entities")),
        depth_layers=_dict_list(raw.get("depth_layers")),
        evidence_refs=_string_list(raw.get("evidence_refs") or raw.get("citations")),
        prior_thread_refs=_string_list(raw.get("prior_thread_refs")),
        rollup_parent_ids=_string_list(raw.get("rollup_parent_ids")),
        lower_timeframe_refs=_string_list(raw.get("lower_timeframe_refs")),
        higher_timeframe_context_refs=_string_list(raw.get("higher_timeframe_context_refs")),
        temporal_confidence=_clamp01(raw.get("temporal_confidence"), 0.5),
        quality_flags=_string_list(raw.get("quality_flags")),
    )


def persist_information_strings(
    *,
    cycle_id: str,
    scout_id: str,
    seed_id: str,
    entity: str,
    theme: Optional[str],
    horizon: str,
    lens: str,
    bias_mode: str,
    strings: Iterable[InformationString],
    coverage_cell_key: Optional[str] = None,
    source_tool_call_ids: Optional[list[str]] = None,
    model_used: str = "",
    provider: str = "",
    cost_usd: float = 0.0,
    conn: Optional[sqlite3.Connection] = None,
) -> list[str]:
    """Persist strings and fold them into graph nodes/edges."""
    db = conn or get_desk_store().conn
    now = datetime.now(timezone.utc).isoformat()
    ids: list[str] = []
    for s in strings:
        if not s.thesis:
            continue
        string_id = s.string_id or _string_id(cycle_id, scout_id, s)
        attention_score = _attention_score(s)
        ids.append(string_id)
        columns = [
            "id", "cycle_id", "coverage_cell_key", "scout_id", "seed_id",
            "entity", "theme", "horizon", "lens", "bias_mode", "title",
            "thesis", "mechanism", "expected_outcome", "time_horizon",
            "time_scale", "event_time_start", "event_time_end",
            "observed_at", "ingested_at", "source_time_basis",
            "kill_signal", "extends_or_contradicts", "would_change_decision",
            "expires_at", "crowdedness", "conviction", "novelty_score",
            "attention_score", "entities_chain", "depth_layers",
            "evidence_refs", "prior_thread_refs", "source_tool_call_ids",
            "rollup_parent_ids", "lower_timeframe_refs",
            "higher_timeframe_context_refs", "temporal_confidence",
            "model_used", "provider", "cost_usd", "quality_flags",
            "valid_from", "transaction_from", "transaction_to",
        ]
        values = [
            string_id,
            cycle_id,
            coverage_cell_key,
            scout_id,
            seed_id,
            entity,
            theme,
            horizon,
            lens,
            bias_mode,
            s.title,
            s.thesis,
            s.mechanism,
            s.expected_outcome,
            s.time_horizon,
            s.time_scale or s.time_horizon or horizon,
            s.event_time_start,
            s.event_time_end,
            s.observed_at,
            s.ingested_at or now,
            s.source_time_basis,
            s.kill_signal,
            s.extends_or_contradicts,
            1 if s.would_change_decision else 0,
            s.expires_at,
            s.crowdedness,
            s.conviction,
            s.novelty_score,
            attention_score,
            json.dumps(s.entities_chain),
            json.dumps(s.depth_layers),
            json.dumps(s.evidence_refs),
            json.dumps(s.prior_thread_refs),
            json.dumps(source_tool_call_ids or []),
            json.dumps(s.rollup_parent_ids),
            json.dumps(s.lower_timeframe_refs),
            json.dumps(s.higher_timeframe_context_refs),
            s.temporal_confidence,
            model_used,
            provider,
            float(cost_usd or 0.0),
            json.dumps(s.quality_flags),
            now,
            now,
            None,
        ]
        db.execute(
            f"""
            INSERT OR REPLACE INTO information_strings (
                {", ".join(columns)}
            ) VALUES ({", ".join("?" for _ in columns)})
            """,
            values,
        )
        for evidence_ref in sorted(set(s.evidence_refs + list(source_tool_call_ids or []))):
            _insert_string_evidence(
                db,
                cycle_id=cycle_id,
                string_id=string_id,
                evidence_ref=evidence_ref,
                now=now,
            )
        _upsert_graph_for_string(
            db,
            cycle_id=cycle_id,
            string_id=string_id,
            entity=entity,
            theme=theme,
            horizon=horizon,
            info=s,
            now=now,
        )
    db.commit()
    return ids


def recent_information_strings(
    *,
    cycle_id: Optional[str] = None,
    entity: Optional[str] = None,
    theme: Optional[str] = None,
    horizon: Optional[str] = None,
    lens: Optional[str] = None,
    bias_mode: Optional[str] = None,
    min_conviction: Optional[float] = None,
    min_novelty: Optional[float] = None,
    exclude_quality_flags: Optional[list[str]] = None,
    limit: int = 200,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    db = conn or get_desk_store().conn
    clauses: list[str] = []
    params_list: list[Any] = []
    if cycle_id:
        clauses.append("cycle_id = ?")
        params_list.append(cycle_id)
    if entity:
        clauses.append("entity = ?")
        params_list.append(entity)
    if theme:
        clauses.append("theme = ?")
        params_list.append(theme)
    if horizon:
        clauses.append("horizon = ?")
        params_list.append(horizon)
    if lens:
        clauses.append("lens = ?")
        params_list.append(lens)
    if bias_mode:
        clauses.append("bias_mode = ?")
        params_list.append(bias_mode)
    if min_conviction is not None:
        clauses.append("conviction >= ?")
        params_list.append(float(min_conviction))
    if min_novelty is not None:
        clauses.append("novelty_score >= ?")
        params_list.append(float(min_novelty))
    for flag in exclude_quality_flags or []:
        clauses.append("COALESCE(quality_flags, '') NOT LIKE ?")
        params_list.append(f"%{flag}%")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params_list.append(int(limit))
    rows = db.execute(
        f"""
        SELECT * FROM information_strings
        {where}
        ORDER BY
            COALESCE(attention_score, conviction * 0.50 + novelty_score * 0.35 + (1.0 - crowdedness) * 0.15) DESC,
            valid_from DESC
        LIMIT ?
        """,
        tuple(params_list),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def select_information_context(
    *,
    entity: Optional[str] = None,
    theme: Optional[str] = None,
    lens: Optional[str] = None,
    horizon: Optional[str] = None,
    limit: int = 6,
    candidate_limit: int = 80,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    """Return a compact, diverse prior context slice for a scout prompt.

    The scout layer is cheap because each call sees only the few prior
    strings most likely to change its answer. Selection favors scoped
    matches, novelty, conviction, lower crowdedness, and evidence-backed
    strings while preventing one entity/lens bucket from monopolizing the
    prompt.
    """
    candidates = recent_information_strings(
        entity=entity,
        limit=candidate_limit,
        exclude_quality_flags=["fallback_string_from_hypothesis"],
        conn=conn,
    )
    if theme:
        candidates.extend(recent_information_strings(
            theme=theme,
            limit=max(12, candidate_limit // 3),
            exclude_quality_flags=["fallback_string_from_hypothesis"],
            conn=conn,
        ))
    if lens:
        candidates.extend(recent_information_strings(
            lens=lens,
            limit=max(12, candidate_limit // 3),
            exclude_quality_flags=["fallback_string_from_hypothesis"],
            conn=conn,
        ))
    if not candidates:
        return []

    deduped: dict[str, dict[str, Any]] = {}
    for row in candidates:
        row_id = str(row.get("id") or "")
        if row_id:
            deduped[row_id] = row

    ranked = sorted(
        deduped.values(),
        key=lambda r: _context_score(r, entity=entity, theme=theme, lens=lens, horizon=horizon),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    buckets: set[tuple[str, str]] = set()
    for row in ranked:
        bucket = (str(row.get("entity") or ""), str(row.get("lens") or ""))
        if bucket in buckets and len(out) < max(2, limit // 2):
            continue
        out.append(row)
        buckets.add(bucket)
        if len(out) >= limit:
            break
    return out


def _upsert_graph_for_string(
    db: sqlite3.Connection,
    *,
    cycle_id: str,
    string_id: str,
    entity: str,
    theme: Optional[str],
    horizon: str,
    info: InformationString,
    now: str,
) -> None:
    chain = info.entities_chain or [entity]
    node_keys = [
        _upsert_node(
            db,
            node_type="entity",
            label=label,
            entity=label,
            theme=theme,
            cycle_id=cycle_id,
            strength=info.conviction,
            now=now,
        )
        for label in chain
        if label
    ]
    mechanism_key = ""
    if info.mechanism:
        mechanism_key = _upsert_node(
            db,
            node_type="mechanism",
            label=_short_label(info.mechanism),
            entity=entity,
            theme=theme,
            cycle_id=cycle_id,
            strength=info.conviction,
            now=now,
        )
    theme_key = ""
    if theme:
        theme_key = _upsert_node(
            db,
            node_type="theme",
            label=theme,
            entity=entity,
            theme=theme,
            cycle_id=cycle_id,
            strength=info.conviction,
            now=now,
        )
    for a, b in zip(node_keys, node_keys[1:]):
        _insert_edge(db, cycle_id, string_id, a, b, "causal_chain", info, horizon, now)
    if node_keys and mechanism_key:
        _insert_edge(db, cycle_id, string_id, node_keys[-1], mechanism_key, "mechanism", info, horizon, now)
    if theme_key and node_keys:
        _insert_edge(db, cycle_id, string_id, theme_key, node_keys[0], "theme_exposure", info, horizon, now)


def _upsert_node(
    db: sqlite3.Connection,
    *,
    node_type: str,
    label: str,
    entity: Optional[str],
    theme: Optional[str],
    cycle_id: str,
    strength: float,
    now: str,
) -> str:
    key = _node_key(node_type, label)
    row = db.execute(
        "SELECT strength, evidence_count, first_seen_cycle_id FROM information_map_nodes WHERE node_key = ?",
        (key,),
    ).fetchone()
    if row:
        db.execute(
            """
            UPDATE information_map_nodes
            SET last_seen_cycle_id = ?,
                strength = ?,
                evidence_count = ?,
                payload = ?,
                valid_from = ?,
                transaction_from = ?
            WHERE node_key = ?
            """,
            (
                cycle_id,
                max(float(row["strength"] or 0.0), float(strength or 0.0)),
                int(row["evidence_count"] or 0) + 1,
                json.dumps({"latest_label": label}),
                now,
                now,
                key,
            ),
        )
    else:
        db.execute(
            """
            INSERT INTO information_map_nodes (
                node_key, node_type, label, entity, theme, first_seen_cycle_id,
                last_seen_cycle_id, strength, evidence_count, payload,
                valid_from, transaction_from, transaction_to
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, NULL)
            """,
            (
                key,
                node_type,
                label[:240],
                entity,
                theme,
                cycle_id,
                cycle_id,
                float(strength or 0.0),
                json.dumps({"latest_label": label}),
                now,
                now,
            ),
        )
    return key


def _insert_edge(
    db: sqlite3.Connection,
    cycle_id: str,
    string_id: str,
    source: str,
    target: str,
    edge_type: str,
    info: InformationString,
    horizon: str,
    now: str,
) -> None:
    edge_id = "iedge_" + hashlib.sha256(
        f"{cycle_id}|{string_id}|{source}|{target}|{edge_type}".encode()
    ).hexdigest()[:16]
    db.execute(
        """
        INSERT OR REPLACE INTO information_map_edges (
            id, cycle_id, string_id, source_node_key, target_node_key,
            edge_type, mechanism, horizon, strength, evidence_refs, payload,
            valid_from, transaction_from, transaction_to
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            edge_id,
            cycle_id,
            string_id,
            source,
            target,
            edge_type,
            info.mechanism,
            horizon,
            float(info.conviction or 0.0),
            json.dumps(info.evidence_refs),
            json.dumps({"title": info.title, "kill_signal": info.kill_signal}),
            now,
            now,
        ),
    )


def _context_score(
    row: dict[str, Any],
    *,
    entity: Optional[str],
    theme: Optional[str],
    lens: Optional[str],
    horizon: Optional[str],
) -> float:
    score = (
        float(row.get("conviction") or 0.0) * 0.45
        + float(row.get("novelty_score") or 0.0) * 0.30
        + (1.0 - float(row.get("crowdedness") or 0.5)) * 0.15
    )
    if row.get("entity") == entity:
        score += 0.25
    if theme and row.get("theme") == theme:
        score += 0.16
    if lens and row.get("lens") == lens:
        score += 0.12
    if horizon and row.get("horizon") == horizon:
        score += 0.07
    try:
        if row.get("evidence_refs"):
            score += 0.08
    except Exception:
        pass
    return score


def _attention_score(info: InformationString) -> float:
    relation_boost = {
        "contradicts": 0.14,
        "extends": 0.10,
        "new": 0.06,
        "abandons": -0.08,
    }.get(info.extends_or_contradicts, 0.0)
    score = (
        float(info.conviction or 0.0) * 0.45
        + float(info.novelty_score or 0.0) * 0.30
        + (1.0 - float(info.crowdedness or 0.5)) * 0.15
        + relation_boost
    )
    if info.evidence_refs:
        score += 0.06
    if not info.would_change_decision:
        score -= 0.18
    return round(max(0.0, min(1.0, score)), 4)


def _insert_string_evidence(
    db: sqlite3.Connection,
    *,
    cycle_id: str,
    string_id: str,
    evidence_ref: str,
    now: str,
) -> None:
    ref = str(evidence_ref or "").strip()
    if not ref:
        return
    evidence_id = "isev_" + hashlib.sha256(
        f"{cycle_id}|{string_id}|{ref}".encode()
    ).hexdigest()[:16]
    evidence_kind = _evidence_kind(ref)
    db.execute(
        """
        INSERT OR REPLACE INTO information_string_evidence (
            id, cycle_id, string_id, evidence_ref, evidence_kind, role,
            created_at, valid_from, transaction_from, transaction_to
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            evidence_id,
            cycle_id,
            string_id,
            ref,
            evidence_kind,
            "source",
            now,
            now,
            now,
        ),
    )


def _evidence_kind(ref: str) -> str:
    if ref.startswith("tool_call") or ref.startswith("tc_"):
        return "tool_call"
    if ref.startswith("cit"):
        return "citation"
    if ref.startswith("claim"):
        return "claim"
    return "reference"


def _string_id(cycle_id: str, scout_id: str, info: InformationString) -> str:
    raw = f"{cycle_id}|{scout_id}|{info.title}|{info.thesis}".encode()
    return "istr_" + hashlib.sha256(raw).hexdigest()[:16]


def _node_key(node_type: str, label: str) -> str:
    clean = " ".join(str(label).lower().split())[:240]
    return f"{node_type}:" + hashlib.sha256(clean.encode()).hexdigest()[:16]


def _short_label(text: str) -> str:
    return " ".join(str(text).split())[:180]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for key in (
        "entities_chain",
        "depth_layers",
        "evidence_refs",
        "prior_thread_refs",
        "source_tool_call_ids",
        "rollup_parent_ids",
        "lower_timeframe_refs",
        "higher_timeframe_context_refs",
        "quality_flags",
    ):
        try:
            d[key] = json.loads(d.get(key) or "[]")
        except Exception:
            d[key] = []
    return d


def _string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [str(x).strip()[:500] for x in raw if str(x).strip()]
    return []


def _relation(raw: Any) -> str:
    rel = str(raw or "new").strip().lower()
    if rel in {"new", "extends", "contradicts", "abandons"}:
        return rel
    return "new"


def _bool(raw: Any, *, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return default


def _dict_list(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            out.append(item)
        elif item:
            out.append({"note": str(item)[:500]})
    return out[:12]


def _clamp01(raw: Any, default: float) -> float:
    try:
        value = float(raw)
    except Exception:
        value = default
    return max(0.0, min(1.0, value))
