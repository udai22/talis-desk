"""Hypothesis + HypothesisEdge models and CRUD.

Source of truth: `wiki/SOTA_DESK_ARCHITECTURE.md` v2 §3 (lines 178-216).

Design rules (carried verbatim from v2):
- Append-only. Corrections insert a new row with `supersedes=<old_id>` and
  set the old row's `transaction_to` to "now".
- Bitemporal. Each row carries `valid_from/valid_to` (world-time) and
  `transaction_from/transaction_to` (knowledge-time). Reads go through
  `talis_desk.replay.build_replay_context` — never hand-roll temporal
  predicates here.
- Graph-native. `hypothesis_edges` connects any pair of node kinds
  (hypothesis / claim / event / tool_call / trade_idea / debate / artifact)
  with one of seven `edge_kind` values:
  ('supports', 'contradicts', 'derived_from', 'causes', 'hedges',
   'supersedes', 'spawned').
- `hypotheses.parent_hypothesis_id` is dropped per v2 §3 line 172 —
  parent-child is represented ONLY via `hypothesis_edges.edge_kind='spawned'`.

# Honest gaps
- `update_posterior` mutates the row's `supersedes` chain in append-only
  fashion (insert new row, close old `transaction_to`); we do NOT carry over
  the old row's tool_call_ids / claim_ids implicitly — callers must pass the
  merged list. This keeps the audit trail explicit.
- `get_hypothesis_graph` walks edges via BFS up to `max_depth`; cycles are
  detected by an in-memory visited set so it's safe on tangled graphs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from ..replay import build_replay_context
from ..store import get_desk_store
from ..tool_atlas import AgentContext


# ============================================================================
# Pydantic models — match DDL exactly (v2 §3 lines 178-216)
# ============================================================================

HypothesisStatus = Literal[
    "active", "supported", "contradicted", "resolved", "abandoned"
]

NodeKind = Literal[
    "hypothesis", "claim", "event", "tool_call", "trade_idea", "debate", "artifact"
]

EdgeKind = Literal[
    "supports", "contradicts", "derived_from", "causes", "hedges",
    "supersedes", "spawned",
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Hypothesis(BaseModel):
    """A specialist's claim with a mechanism and (optionally) a posterior.

    Mirrors the `hypotheses` table 1:1.
    """

    id: str = Field(default_factory=lambda: f"hyp_{uuid4().hex[:12]}")
    cycle_id: str
    specialist_id: str
    title: str = Field(..., max_length=120)  # short, indexable
    hypothesis_text: str                     # full claim with mechanism
    status: HypothesisStatus = "active"
    posterior_prob: Optional[float] = None   # 0..1
    heat_score: float = 0.0                  # 0..1, how 'hot' this is
    novelty_score: Optional[float] = None
    expected_resolution_at: Optional[datetime] = None
    entity_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    tool_call_ids: list[str] = Field(default_factory=list)
    supersedes: Optional[str] = None
    valid_from: datetime
    valid_to: Optional[datetime] = None
    transaction_from: datetime = Field(default_factory=_utc_now)
    transaction_to: Optional[datetime] = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("posterior_prob")
    @classmethod
    def _check_prob(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return v
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"posterior_prob must be in [0,1], got {v}")
        return v

    @field_validator("heat_score")
    @classmethod
    def _check_heat(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"heat_score must be in [0,1], got {v}")
        return v


class HypothesisDraft(BaseModel):
    """Input shape for `propose_hypothesis`. All non-ID columns the caller can
    set; ID, transaction_from, and status default sensibly."""

    cycle_id: str
    specialist_id: str
    title: str = Field(..., max_length=120)
    hypothesis_text: str
    posterior_prob: Optional[float] = 0.5
    heat_score: float = 0.0
    novelty_score: Optional[float] = None
    expected_resolution_at: Optional[datetime] = None
    entity_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    tool_call_ids: list[str] = Field(default_factory=list)
    valid_from: Optional[datetime] = None  # defaults to now if None
    payload: dict[str, Any] = Field(default_factory=dict)


class HypothesisEdge(BaseModel):
    """An edge in the hypothesis graph. Mirrors `hypothesis_edges` 1:1."""

    id: str = Field(default_factory=lambda: f"hedge_{uuid4().hex[:12]}")
    from_node_kind: NodeKind
    from_node_id: str
    to_node_kind: NodeKind
    to_node_id: str
    edge_kind: EdgeKind
    strength: Optional[float] = None       # 0..1
    citation_ids: list[str] = Field(default_factory=list)
    supersedes: Optional[str] = None
    valid_from: datetime
    valid_to: Optional[datetime] = None
    transaction_from: datetime = Field(default_factory=_utc_now)
    transaction_to: Optional[datetime] = None

    @field_validator("strength")
    @classmethod
    def _check_strength(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return v
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"strength must be in [0,1], got {v}")
        return v


class HypothesisEdgeDraft(BaseModel):
    """Input shape for `add_edge`."""

    from_node_kind: NodeKind
    from_node_id: str
    to_node_kind: NodeKind
    to_node_id: str
    edge_kind: EdgeKind
    strength: Optional[float] = None
    citation_ids: list[str] = Field(default_factory=list)
    valid_from: Optional[datetime] = None  # defaults to now


@dataclass
class HypothesisGraph:
    """Sub-graph returned by `get_hypothesis_graph`. Pure data — no logic."""

    root_id: str
    max_depth: int
    nodes: list[Hypothesis] = field(default_factory=list)
    edges: list[HypothesisEdge] = field(default_factory=list)

    def node_ids(self) -> set[str]:
        return {n.id for n in self.nodes}


# ============================================================================
# Serialization helpers (Hypothesis <-> SQLite row)
# ============================================================================

def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _from_iso(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _json_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return [str(x) for x in parsed] if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _json_dict(v: Any) -> dict[str, Any]:
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _row_to_hypothesis(row: Any) -> Hypothesis:
    d = dict(row)
    return Hypothesis(
        id=d["id"],
        cycle_id=d["cycle_id"],
        specialist_id=d["specialist_id"],
        title=d["title"],
        hypothesis_text=d["hypothesis_text"],
        status=d["status"],
        posterior_prob=d.get("posterior_prob"),
        heat_score=d.get("heat_score") or 0.0,
        novelty_score=d.get("novelty_score"),
        expected_resolution_at=_from_iso(d.get("expected_resolution_at")),
        entity_ids=_json_list(d.get("entity_ids")),
        source_ids=_json_list(d.get("source_ids")),
        claim_ids=_json_list(d.get("claim_ids")),
        tool_call_ids=_json_list(d.get("tool_call_ids")),
        supersedes=d.get("supersedes"),
        valid_from=_from_iso(d["valid_from"]) or _utc_now(),
        valid_to=_from_iso(d.get("valid_to")),
        transaction_from=_from_iso(d["transaction_from"]) or _utc_now(),
        transaction_to=_from_iso(d.get("transaction_to")),
        payload=_json_dict(d.get("payload")),
    )


def _row_to_edge(row: Any) -> HypothesisEdge:
    d = dict(row)
    return HypothesisEdge(
        id=d["id"],
        from_node_kind=d["from_node_kind"],
        from_node_id=d["from_node_id"],
        to_node_kind=d["to_node_kind"],
        to_node_id=d["to_node_id"],
        edge_kind=d["edge_kind"],
        strength=d.get("strength"),
        citation_ids=_json_list(d.get("citation_ids")),
        supersedes=d.get("supersedes"),
        valid_from=_from_iso(d["valid_from"]) or _utc_now(),
        valid_to=_from_iso(d.get("valid_to")),
        transaction_from=_from_iso(d["transaction_from"]) or _utc_now(),
        transaction_to=_from_iso(d.get("transaction_to")),
    )


# ============================================================================
# CRUD
# ============================================================================

def propose_hypothesis(
    spec: HypothesisDraft,
    context: AgentContext,
) -> Hypothesis:
    """Create + persist a new hypothesis. The caller's `context.cycle_id` and
    `context.specialist_id` override the draft if it's missing those (which
    shouldn't happen, but we'd rather use the canonical context than fail).
    """
    valid_from = spec.valid_from or _utc_now()
    transaction_from = _utc_now()
    cycle_id = spec.cycle_id or context.cycle_id
    specialist_id = spec.specialist_id or context.specialist_id

    hyp = Hypothesis(
        cycle_id=cycle_id,
        specialist_id=specialist_id,
        title=spec.title,
        hypothesis_text=spec.hypothesis_text,
        status="active",
        posterior_prob=spec.posterior_prob,
        heat_score=spec.heat_score,
        novelty_score=spec.novelty_score,
        expected_resolution_at=spec.expected_resolution_at,
        entity_ids=spec.entity_ids,
        source_ids=spec.source_ids,
        claim_ids=spec.claim_ids,
        tool_call_ids=spec.tool_call_ids,
        valid_from=valid_from,
        transaction_from=transaction_from,
        payload=spec.payload,
    )
    _insert_hypothesis(hyp)
    return hyp


def _insert_hypothesis(hyp: Hypothesis) -> None:
    conn = get_desk_store().conn
    conn.execute(
        "INSERT INTO hypotheses "
        "(id, cycle_id, specialist_id, title, hypothesis_text, status, "
        " posterior_prob, heat_score, novelty_score, expected_resolution_at, "
        " entity_ids, source_ids, claim_ids, tool_call_ids, supersedes, "
        " valid_from, valid_to, transaction_from, transaction_to, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            hyp.id,
            hyp.cycle_id,
            hyp.specialist_id,
            hyp.title,
            hyp.hypothesis_text,
            hyp.status,
            hyp.posterior_prob,
            hyp.heat_score,
            hyp.novelty_score,
            _iso(hyp.expected_resolution_at) if hyp.expected_resolution_at else None,
            json.dumps(hyp.entity_ids),
            json.dumps(hyp.source_ids),
            json.dumps(hyp.claim_ids),
            json.dumps(hyp.tool_call_ids),
            hyp.supersedes,
            _iso(hyp.valid_from),
            _iso(hyp.valid_to) if hyp.valid_to else None,
            _iso(hyp.transaction_from),
            _iso(hyp.transaction_to) if hyp.transaction_to else None,
            json.dumps(hyp.payload),
        ),
    )
    conn.commit()


def add_edge(edge: HypothesisEdgeDraft) -> HypothesisEdge:
    """Insert a hypothesis_edges row. All seven `edge_kind` values are
    accepted per the CHECK constraint in the DDL."""
    valid_from = edge.valid_from or _utc_now()
    transaction_from = _utc_now()
    he = HypothesisEdge(
        from_node_kind=edge.from_node_kind,
        from_node_id=edge.from_node_id,
        to_node_kind=edge.to_node_kind,
        to_node_id=edge.to_node_id,
        edge_kind=edge.edge_kind,
        strength=edge.strength,
        citation_ids=edge.citation_ids,
        valid_from=valid_from,
        transaction_from=transaction_from,
    )
    conn = get_desk_store().conn
    conn.execute(
        "INSERT INTO hypothesis_edges "
        "(id, from_node_kind, from_node_id, to_node_kind, to_node_id, "
        " edge_kind, strength, citation_ids, supersedes, "
        " valid_from, valid_to, transaction_from, transaction_to) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            he.id,
            he.from_node_kind,
            he.from_node_id,
            he.to_node_kind,
            he.to_node_id,
            he.edge_kind,
            he.strength,
            json.dumps(he.citation_ids),
            he.supersedes,
            _iso(he.valid_from),
            _iso(he.valid_to) if he.valid_to else None,
            _iso(he.transaction_from),
            _iso(he.transaction_to) if he.transaction_to else None,
        ),
    )
    conn.commit()
    return he


def update_posterior(
    hyp_id: str,
    new_prob: float,
    evidence_ids: list[str],
) -> Hypothesis:
    """Append-only update of a hypothesis's posterior probability.

    Mechanics (matches the supersedes pattern in v2 §3 + schema smoke test):
      1. Fetch the current open row (transaction_to IS NULL).
      2. Set its transaction_to = now.
      3. Insert a NEW row with a NEW id, `supersedes=<old_id>`,
         posterior_prob=<new_prob>, and tool_call_ids extended by
         `evidence_ids` (de-duped).
      4. Return the new row.

    Callers should treat `hyp_id` as logically pointing at the supersedes
    chain HEAD; subsequent updates that pass the original id walk the
    chain to find the latest open revision.

    Heat is recomputed simply: |new - old| > 0.2 -> 0.8, else damp toward
    0.0. Callers that want a different heat policy can update it directly
    via a second supersedes step.
    """
    if not 0.0 <= new_prob <= 1.0:
        raise ValueError(f"new_prob must be in [0,1], got {new_prob}")
    conn = get_desk_store().conn
    # Walk the supersedes chain to find the current open row. The caller
    # passes any id along the chain; we land on transaction_to IS NULL.
    head_row = _find_open_head(conn, hyp_id)
    if head_row is None:
        raise KeyError(f"hypothesis_not_found_or_already_closed: {hyp_id}")
    old = _row_to_hypothesis(head_row)
    now = _utc_now()
    now_iso = _iso(now)
    # 1) close the old row's transaction_to
    conn.execute(
        "UPDATE hypotheses SET transaction_to = ? WHERE id = ?",
        (now_iso, old.id),
    )
    # 2) build the new row carrying merged citations
    merged_tool_calls = list(dict.fromkeys(list(old.tool_call_ids) + list(evidence_ids)))
    delta = abs(new_prob - (old.posterior_prob if old.posterior_prob is not None else 0.5))
    new_heat = max(min(0.8 if delta > 0.2 else max(old.heat_score * 0.7, delta), 1.0), 0.0)
    new = Hypothesis(
        cycle_id=old.cycle_id,
        specialist_id=old.specialist_id,
        title=old.title,
        hypothesis_text=old.hypothesis_text,
        status=old.status,
        posterior_prob=new_prob,
        heat_score=new_heat,
        novelty_score=old.novelty_score,
        expected_resolution_at=old.expected_resolution_at,
        entity_ids=old.entity_ids,
        source_ids=old.source_ids,
        claim_ids=old.claim_ids,
        tool_call_ids=merged_tool_calls,
        supersedes=old.id,
        valid_from=old.valid_from,
        transaction_from=now,
        payload=old.payload,
    )
    # New row gets a NEW id (the schema enforces PRIMARY KEY UNIQUE on id);
    # the chain is followed via the supersedes column. `new.id` is left at
    # the auto-generated default from the Pydantic model.
    _insert_hypothesis(new)
    return new


def _find_open_head(conn: Any, hyp_id: str) -> Any:
    """Find the latest open revision in a supersedes chain anchored at
    `hyp_id`. Returns the sqlite Row or None."""
    # First check the direct id
    row = conn.execute(
        "SELECT * FROM hypotheses WHERE id = ? AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (hyp_id,),
    ).fetchone()
    if row is not None:
        return row
    # Otherwise walk forward via supersedes (DESCendants of hyp_id)
    visited: set[str] = {hyp_id}
    cur = hyp_id
    while True:
        nxt = conn.execute(
            "SELECT * FROM hypotheses WHERE supersedes = ? "
            "ORDER BY transaction_from DESC LIMIT 1",
            (cur,),
        ).fetchone()
        if nxt is None:
            return None
        if nxt["id"] in visited:
            return None  # cycle guard
        visited.add(nxt["id"])
        if nxt["transaction_to"] is None:
            return nxt
        cur = nxt["id"]


def resolve_hypothesis(
    hyp_id: str,
    status: str,
    outcome_payload: dict[str, Any],
) -> Hypothesis:
    """Mark a hypothesis as supported/contradicted/resolved/abandoned. Append
    a new row with the new status and an outcome payload."""
    if status not in ("supported", "contradicted", "resolved", "abandoned"):
        raise ValueError(
            f"resolve_hypothesis: status must be one of "
            f"('supported','contradicted','resolved','abandoned'), got {status!r}"
        )
    conn = get_desk_store().conn
    head_row = _find_open_head(conn, hyp_id)
    if head_row is None:
        raise KeyError(f"hypothesis_not_found_or_already_closed: {hyp_id}")
    old = _row_to_hypothesis(head_row)
    now = _utc_now()
    now_iso = _iso(now)
    conn.execute(
        "UPDATE hypotheses SET transaction_to = ? WHERE id = ?",
        (now_iso, old.id),
    )
    merged_payload = dict(old.payload)
    merged_payload["resolution"] = outcome_payload
    new = Hypothesis(
        cycle_id=old.cycle_id,
        specialist_id=old.specialist_id,
        title=old.title,
        hypothesis_text=old.hypothesis_text,
        status=status,  # type: ignore[arg-type]
        posterior_prob=old.posterior_prob,
        heat_score=old.heat_score,
        novelty_score=old.novelty_score,
        expected_resolution_at=old.expected_resolution_at,
        entity_ids=old.entity_ids,
        source_ids=old.source_ids,
        claim_ids=old.claim_ids,
        tool_call_ids=old.tool_call_ids,
        supersedes=old.id,
        valid_from=old.valid_from,
        valid_to=now,  # world-time end-of-life
        transaction_from=now,
        payload=merged_payload,
    )
    _insert_hypothesis(new)
    return new


def get_active_hypotheses(
    specialist_id: str,
    as_of: Optional[datetime] = None,
) -> list[Hypothesis]:
    """List active hypotheses owned by `specialist_id`, bitemporally sliced.

    `as_of` defaults to now and applies to BOTH axes (valid + transaction).
    Use the lower-level `build_replay_context` directly if you need to split
    them.
    """
    conn = get_desk_store().conn
    if as_of is None:
        as_of = _utc_now()
    ctx = build_replay_context(as_of_valid=as_of)
    where, params = ctx.where_clause("hypotheses")
    sql = (
        f"SELECT * FROM hypotheses "
        f"WHERE specialist_id = ? AND status = 'active' AND {where} "
        f"ORDER BY heat_score DESC, transaction_from DESC"
    )
    rows = conn.execute(sql, [specialist_id, *params]).fetchall()
    # When a hypothesis has multiple revisions (supersedes chain), we want the
    # LATEST open revision per id. With bitemporal filter applied + ORDER BY
    # transaction_from DESC, dedupe by id keeping the first occurrence.
    seen: set[str] = set()
    out: list[Hypothesis] = []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append(_row_to_hypothesis(r))
    return out


def get_hypothesis_graph(
    root_id: str,
    max_depth: int = 3,
) -> HypothesisGraph:
    """BFS over hypothesis_edges starting from `root_id`. Walks BOTH outbound
    (from_node_id=root) and inbound (to_node_id=root) edges. Returns the
    union of all hypothesis nodes touched within `max_depth` hops, plus all
    edges between them.

    Note: non-hypothesis nodes (claims, events, trade_ideas, etc.) appear as
    edge endpoints but are not hydrated into `nodes` here — this method is
    scoped to the hypothesis sub-graph. Use the edges to follow into other
    artifact stores if needed.
    """
    if max_depth < 0:
        raise ValueError(f"max_depth must be >= 0, got {max_depth}")
    conn = get_desk_store().conn

    visited_nodes: dict[str, Hypothesis] = {}
    visited_edge_ids: set[str] = set()
    edges_out: list[HypothesisEdge] = []

    # Seed: load the root hypothesis (latest open revision).
    root_row = conn.execute(
        "SELECT * FROM hypotheses WHERE id = ? AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (root_id,),
    ).fetchone()
    if root_row is None:
        # Try the latest historical revision if no open row exists.
        root_row = conn.execute(
            "SELECT * FROM hypotheses WHERE id = ? "
            "ORDER BY transaction_from DESC LIMIT 1",
            (root_id,),
        ).fetchone()
    if root_row is not None:
        h = _row_to_hypothesis(root_row)
        visited_nodes[h.id] = h

    frontier: list[str] = [root_id]
    for _depth in range(max_depth):
        next_frontier: list[str] = []
        for node_id in frontier:
            edge_rows = conn.execute(
                "SELECT * FROM hypothesis_edges "
                "WHERE (from_node_id = ? OR to_node_id = ?) "
                "AND transaction_to IS NULL",
                (node_id, node_id),
            ).fetchall()
            for er in edge_rows:
                if er["id"] in visited_edge_ids:
                    continue
                visited_edge_ids.add(er["id"])
                e = _row_to_edge(er)
                edges_out.append(e)
                # Identify the "other" node id and only follow it if it's a
                # hypothesis.
                neighbors: list[tuple[str, str]] = []
                if e.from_node_id != node_id:
                    neighbors.append((e.from_node_kind, e.from_node_id))
                if e.to_node_id != node_id:
                    neighbors.append((e.to_node_kind, e.to_node_id))
                for kind, nid in neighbors:
                    if kind != "hypothesis":
                        continue
                    if nid in visited_nodes:
                        continue
                    row = conn.execute(
                        "SELECT * FROM hypotheses WHERE id = ? AND transaction_to IS NULL "
                        "ORDER BY transaction_from DESC LIMIT 1",
                        (nid,),
                    ).fetchone()
                    if row is None:
                        # Tombstoned / closed; skip rather than fail.
                        continue
                    visited_nodes[nid] = _row_to_hypothesis(row)
                    next_frontier.append(nid)
        frontier = next_frontier
        if not frontier:
            break

    return HypothesisGraph(
        root_id=root_id,
        max_depth=max_depth,
        nodes=list(visited_nodes.values()),
        edges=edges_out,
    )


# ============================================================================
# Small convenience used by exploration BFS — link a trade idea / claim /
# tool_call to a hypothesis with the right edge_kind.
# ============================================================================

def link_evidence(
    hypothesis_id: str,
    evidence_kind: NodeKind,
    evidence_id: str,
    edge_kind: EdgeKind = "supports",
    strength: Optional[float] = None,
    citation_ids: Optional[Iterable[str]] = None,
) -> HypothesisEdge:
    """Convenience: add an edge with the hypothesis as the `to_node`."""
    return add_edge(HypothesisEdgeDraft(
        from_node_kind=evidence_kind,
        from_node_id=evidence_id,
        to_node_kind="hypothesis",
        to_node_id=hypothesis_id,
        edge_kind=edge_kind,
        strength=strength,
        citation_ids=list(citation_ids) if citation_ids else [],
    ))
