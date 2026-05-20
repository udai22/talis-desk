"""Specialist persona base — contract + bitemporal registration.

Source of truth: `wiki/SOTA_DESK_ARCHITECTURE.md` v2 §4 (Agent Loop) + §5
(Self-Improvement). A specialist's persona is a row in `specialist_states`
with `state_kind='persona'` — the canonical artifact the loop runner
hydrates at the top of every cycle.

Design rules:
- Append-only. Re-registration with a new prompt/version inserts a NEW
  row; the old row's `transaction_to` is closed and the new row's
  `supersedes` points back. Mirrors `hypotheses` / `trade_ideas`.
- Bitemporal. `valid_from` is "the moment this persona becomes the
  canonical voice"; `transaction_from` is "the moment we recorded it".
  Reads go through `talis_desk.replay.build_replay_context` — never
  hand-roll temporal predicates here.
- Each specialist sees ONLY its curated tool subset. Per v2 §5: the
  atlas is the full superset but the persona scopes which URIs the
  function-calling LLM is actually shown each cycle.
- LLM model strings reference `tic.desk.models.MODELS` keys directly so
  the loop's `chat()` call uses the real provider fallback chain.

Honest gaps:
- `register_persona` is idempotent on `(specialist_id, persona_version,
  prompt_hash)` — if any of those three change you get a new row. We
  do NOT diff individual fields beyond the hash; callers wanting a
  finer-grained mutation diff should compare two `SpecialistPersona`
  payloads themselves.
- `prompt_hash` is sha256 of `system_prompt`. If callers want to include
  tool_uris / priors in the identity hash they should compose a new
  composite string before passing it as `system_prompt` (or extend this
  module). For v1 the prompt itself is the dominant signal.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from ..replay import build_replay_context
from ..store import get_desk_store


# ============================================================================
# Persona contract
# ============================================================================

class SpecialistPersona(BaseModel):
    """The persona a specialist hydrates at the top of every cycle.

    Serialized into `specialist_states.state_json` as a dict; the
    `prompt_hash` column is the sha256 of `system_prompt` for cheap
    idempotency lookups.
    """

    specialist_id: str = Field(..., description="canonical id, e.g. 'macro_regime'")
    persona_version: str = Field(..., description="semantic version, e.g. 'v1.0'")
    name: str = Field(..., description="human-readable name")
    scope: str = Field(..., description="one-line scope description")
    system_prompt: str = Field(..., description="full system prompt (the LLM sees this verbatim)")

    # Curated subset — the LLM function-calling surface is restricted to
    # these tic:// URIs. Per v2 §5: NOT the full atlas; each specialist
    # sees only its tools.
    tool_uris: list[str] = Field(default_factory=list)

    # Preferred LLM provider key (from `tic.desk.models.MODELS`). Fallback
    # chain is handled by `chat()`'s `fallback=` arg in the loop runner.
    preferred_model: str = Field(
        default="anthropic:claude-opus-4-7",
        description="MODELS key from tic.desk.models",
    )

    # Inter-agent message topics this specialist subscribes to.
    subscribed_topics: list[str] = Field(default_factory=list)

    # Initial priors as JSON-serializable dict.
    initial_priors: dict[str, Any] = Field(default_factory=dict)

    # Persona metadata.
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    author: str = "talis-desk"

    @field_validator("specialist_id")
    @classmethod
    def _check_specialist_id(cls, v: str) -> str:
        if not v or not v.replace("_", "").isalnum():
            raise ValueError(
                f"specialist_id must be alnum/underscore, got {v!r}"
            )
        return v

    @field_validator("persona_version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        if not v.startswith("v"):
            raise ValueError(f"persona_version must start with 'v', got {v!r}")
        return v

    @field_validator("tool_uris")
    @classmethod
    def _check_uris(cls, v: list[str]) -> list[str]:
        for uri in v:
            if not uri.startswith("tic://"):
                raise ValueError(
                    f"tool_uris entries must be tic:// URIs, got {uri!r}"
                )
        return v

    def prompt_hash(self) -> str:
        """sha256 of `system_prompt`. Used for idempotent registration."""
        return hashlib.sha256(self.system_prompt.encode("utf-8")).hexdigest()

    def to_state_json(self) -> dict[str, Any]:
        """Serialize for `specialist_states.state_json` (canonical column)."""
        return {
            "specialist_id": self.specialist_id,
            "persona_version": self.persona_version,
            "name": self.name,
            "scope": self.scope,
            "system_prompt": self.system_prompt,
            "tool_uris": list(self.tool_uris),
            "preferred_model": self.preferred_model,
            "subscribed_topics": list(self.subscribed_topics),
            "initial_priors": dict(self.initial_priors),
            "author": self.author,
            "created_at": self.created_at.astimezone(timezone.utc).isoformat(),
        }

    @classmethod
    def from_state_json(cls, state_json: dict[str, Any]) -> "SpecialistPersona":
        """Inverse of `to_state_json`. Tolerates missing optional fields."""
        created_at_raw = state_json.get("created_at")
        if isinstance(created_at_raw, str):
            try:
                created_at = datetime.fromisoformat(created_at_raw)
            except ValueError:
                created_at = datetime.now(timezone.utc)
        else:
            created_at = datetime.now(timezone.utc)
        return cls(
            specialist_id=state_json["specialist_id"],
            persona_version=state_json["persona_version"],
            name=state_json.get("name", state_json["specialist_id"]),
            scope=state_json.get("scope", ""),
            system_prompt=state_json["system_prompt"],
            tool_uris=list(state_json.get("tool_uris", [])),
            preferred_model=state_json.get(
                "preferred_model", "anthropic:claude-opus-4-7"
            ),
            subscribed_topics=list(state_json.get("subscribed_topics", [])),
            initial_priors=dict(state_json.get("initial_priors", {})),
            created_at=created_at,
            author=state_json.get("author", "talis-desk"),
        )


# ============================================================================
# SpecialistState — thin wrapper over the row we just wrote
# ============================================================================

@dataclass
class SpecialistState:
    """Hydrated row from `specialist_states`. Returned by registration."""

    id: str
    specialist_id: str
    persona_version: str
    cycle_id: str
    state_kind: str
    prompt_hash: Optional[str]
    parent_state_id: Optional[str]
    valid_from: datetime
    transaction_from: datetime
    persona: SpecialistPersona


# ============================================================================
# Registration
# ============================================================================

# `cycle_id` for persona seeding — these rows are not tied to a research
# cycle; they describe the agent itself. Convention: 'bootstrap'.
_BOOTSTRAP_CYCLE_ID = "bootstrap"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _find_existing_persona_row(
    conn: Any,
    specialist_id: str,
    persona_version: str,
    prompt_hash: str,
) -> Optional[dict[str, Any]]:
    """Return the open `(specialist_id, persona_version, prompt_hash)` row
    if one exists, else None. 'Open' = transaction_to IS NULL.

    Idempotency contract: if the same (id, version, hash) triple already
    has an open row, `register_persona` should NOT insert a new row.
    """
    row = conn.execute(
        "SELECT * FROM specialist_states "
        "WHERE specialist_id = ? AND persona_version = ? "
        "  AND prompt_hash = ? AND state_kind = 'persona' "
        "  AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (specialist_id, persona_version, prompt_hash),
    ).fetchone()
    return dict(row) if row is not None else None


def _find_open_persona_any_version(
    conn: Any, specialist_id: str
) -> Optional[dict[str, Any]]:
    """Return the latest open persona row for `specialist_id` regardless of
    version/hash. Used to set `supersedes` + `parent_state_id` when bumping."""
    row = conn.execute(
        "SELECT * FROM specialist_states "
        "WHERE specialist_id = ? AND state_kind = 'persona' "
        "  AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (specialist_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def register_persona(persona: SpecialistPersona) -> SpecialistState:
    """Insert a persona row into `specialist_states` with `state_kind='persona'`.

    Idempotency: if a row with identical (specialist_id, persona_version,
    prompt_hash) is already open, returns that existing row WITHOUT
    inserting. If a row with the same specialist_id but a different
    version/hash is open, this becomes a supersedes step: the old row's
    `transaction_to` is closed and a new row is inserted with
    `supersedes` + `parent_state_id` set.

    Returns the SpecialistState describing the open row (existing or new).
    """
    conn = get_desk_store().conn
    prompt_hash = persona.prompt_hash()

    # 1) Identical row already exists? Idempotent return.
    existing = _find_existing_persona_row(
        conn, persona.specialist_id, persona.persona_version, prompt_hash
    )
    if existing is not None:
        return _row_to_specialist_state(existing)

    # 2) Different version/hash exists? Close it before inserting the new.
    prior_open = _find_open_persona_any_version(conn, persona.specialist_id)
    now = _utc_now()
    now_iso = _iso(now)
    supersedes_id: Optional[str] = None
    parent_state_id: Optional[str] = None
    if prior_open is not None:
        prior_id = prior_open["id"]
        conn.execute(
            "UPDATE specialist_states SET transaction_to = ? "
            "WHERE id = ? AND transaction_to IS NULL",
            (now_iso, prior_id),
        )
        supersedes_id = prior_id
        parent_state_id = prior_id

    # 3) Insert. Let the DDL DEFAULT generate the id.
    state_json = persona.to_state_json()
    cur = conn.execute(
        "INSERT INTO specialist_states "
        "(specialist_id, persona_version, cycle_id, state_kind, state_json, "
        " prompt_hash, parent_state_id, supersedes, "
        " valid_from, transaction_from) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            persona.specialist_id,
            persona.persona_version,
            _BOOTSTRAP_CYCLE_ID,
            "persona",
            json.dumps(state_json),
            prompt_hash,
            parent_state_id,
            supersedes_id,
            now_iso,
            now_iso,
        ),
    )
    conn.commit()
    # Re-read the row we just wrote to capture the generated id.
    new_row = conn.execute(
        "SELECT * FROM specialist_states "
        "WHERE specialist_id = ? AND persona_version = ? "
        "  AND prompt_hash = ? AND state_kind = 'persona' "
        "  AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (persona.specialist_id, persona.persona_version, prompt_hash),
    ).fetchone()
    if new_row is None:
        # cur.lastrowid is the rowid, not our generated PK — fall back to a
        # broader query.
        new_row = conn.execute(
            "SELECT * FROM specialist_states "
            "WHERE specialist_id = ? AND state_kind = 'persona' "
            "  AND transaction_to IS NULL "
            "ORDER BY transaction_from DESC LIMIT 1",
            (persona.specialist_id,),
        ).fetchone()
    assert new_row is not None, "INSERT succeeded but row not found (bug)"
    return _row_to_specialist_state(dict(new_row))


def _row_to_specialist_state(row: dict[str, Any]) -> SpecialistState:
    """Convert a SQLite row dict into a SpecialistState dataclass."""
    state_json_raw = row.get("state_json") or "{}"
    if isinstance(state_json_raw, str):
        try:
            state_json = json.loads(state_json_raw)
        except json.JSONDecodeError:
            state_json = {}
    else:
        state_json = dict(state_json_raw)

    persona = SpecialistPersona.from_state_json(state_json)
    valid_from = _parse_iso(row["valid_from"])
    transaction_from = _parse_iso(row["transaction_from"])
    return SpecialistState(
        id=row["id"],
        specialist_id=row["specialist_id"],
        persona_version=row["persona_version"],
        cycle_id=row["cycle_id"],
        state_kind=row["state_kind"],
        prompt_hash=row.get("prompt_hash"),
        parent_state_id=row.get("parent_state_id"),
        valid_from=valid_from,
        transaction_from=transaction_from,
        persona=persona,
    )


def _parse_iso(s: Optional[str]) -> datetime:
    if not s:
        return _utc_now()
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return _utc_now()


# ============================================================================
# Read API
# ============================================================================

def get_current_persona(
    specialist_id: str, as_of: Optional[datetime] = None
) -> SpecialistPersona:
    """Return the latest `state_kind='persona'` row for `specialist_id`.

    `as_of` defaults to now and applies to BOTH bitemporal axes. Raises
    KeyError if no persona row exists.
    """
    conn = get_desk_store().conn
    if as_of is None:
        as_of = _utc_now()
    ctx = build_replay_context(as_of_valid=as_of)
    where, params = ctx.where_clause("specialist_states")
    sql = (
        f"SELECT * FROM specialist_states "
        f"WHERE specialist_id = ? AND state_kind = 'persona' AND {where} "
        f"ORDER BY transaction_from DESC LIMIT 1"
    )
    row = conn.execute(sql, [specialist_id, *params]).fetchone()
    if row is None:
        raise KeyError(f"no_persona_for_specialist: {specialist_id}")
    state = _row_to_specialist_state(dict(row))
    return state.persona


def list_personas(as_of: Optional[datetime] = None) -> list[SpecialistPersona]:
    """All registered specialist personas (latest open row per specialist)."""
    conn = get_desk_store().conn
    if as_of is None:
        as_of = _utc_now()
    ctx = build_replay_context(as_of_valid=as_of)
    where, params = ctx.where_clause("specialist_states")
    sql = (
        f"SELECT * FROM specialist_states "
        f"WHERE state_kind = 'persona' AND {where} "
        f"ORDER BY specialist_id, transaction_from DESC"
    )
    rows = conn.execute(sql, params).fetchall()
    seen: set[str] = set()
    out: list[SpecialistPersona] = []
    for r in rows:
        sid = r["specialist_id"]
        if sid in seen:
            continue
        seen.add(sid)
        out.append(_row_to_specialist_state(dict(r)).persona)
    return out
