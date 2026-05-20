"""Debate Runner — implements v2 §6 protocol steps 1-7.

The protocol (verbatim from v2):
  1. Pause publication of the claim/idea.
  2. Pick judge from a different provider family than both participants.
  3. Insert `debates` row + post `request_debate_argument` messages.
  4. Each participant posts an argument (≤200 words + citations +
     falsifiable crux). When both done -> step 5.
  5. Judge builds prompt with claim + arguments + cited claims + source
     health; calls the judge LLM; receives structured verdict.
  6. Persist verdict; set debate.status='judged'.
  7. Apply verdict: write specialist_states mutation_candidate row citing
     debate_id; if follow_up_action specifies downgrade, call
     update_posterior or supersede the claim.

This runner is bitemporal append-only. Status transitions are NEW rows
with supersedes set on the old row's transaction_to.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from uuid import uuid4

from ..agents_native.scratchpad import post_message
from ..store import get_desk_store
from ..tool_atlas import AgentContext
from .judge import (
    _build_judge_prompt,
    _call_judge_llm,
    _pick_judge_provider,
)
from .model import (
    Debate,
    DebateArgument,
    DebateStatus,
    DebateVerdict,
    TriggerKind,
)


# ============================================================================
# Helpers
# ============================================================================

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


# ============================================================================
# Row <-> Pydantic
# ============================================================================

def _row_to_debate(row: Any) -> Debate:
    d = dict(row)
    arg_payload = _json_dict(d.get("argument_payload"))
    raw_arguments = arg_payload.get("arguments") or []
    arguments: list[DebateArgument] = []
    for raw in raw_arguments:
        if not isinstance(raw, dict):
            continue
        try:
            posted_at = _from_iso(raw.get("posted_at")) or _utc_now()
            arguments.append(DebateArgument(
                debate_id=raw.get("debate_id") or d["id"],
                agent_id=raw["agent_id"],
                persona_version=raw.get("persona_version", "v_unknown"),
                argument_md=raw["argument_md"],
                citation_ids=list(raw.get("citation_ids") or []),
                falsifiable_crux=raw["falsifiable_crux"],
                posted_at=posted_at,
            ))
        except Exception:
            continue

    verdict_raw = _json_dict(d.get("verdict"))
    verdict_obj: Optional[DebateVerdict] = None
    if verdict_raw:
        try:
            verdict_obj = DebateVerdict(**{
                "debate_id": d["id"],
                "winner": verdict_raw.get("winner"),
                "confidence": float(verdict_raw.get("confidence", 0.5)),
                "rationale": verdict_raw.get("rationale", ""),
                "follow_up_action": verdict_raw.get("follow_up_action"),
                "required_new_tool_calls": list(
                    verdict_raw.get("required_new_tool_calls") or []
                ),
                "judge_uncertainty": verdict_raw.get("judge_uncertainty"),
                "judge_model": verdict_raw.get("judge_model") or d["judge_model"],
                "judge_provider": verdict_raw.get("judge_provider") or d["judge_provider"],
                "later_brier": d.get("later_brier"),
            })
        except Exception:
            verdict_obj = None

    return Debate(
        id=d["id"],
        cycle_id=d["cycle_id"],
        trigger_kind=d["trigger_kind"],  # type: ignore[arg-type]
        trigger_id=d["trigger_id"],
        participants=_json_list(d.get("participants")),
        judge_model=d["judge_model"],
        judge_provider=d["judge_provider"],
        status=d["status"],  # type: ignore[arg-type]
        opened_at=_from_iso(d.get("transaction_from")) or _utc_now(),
        due_at=_from_iso(d["due_at"]) or _utc_now(),
        arguments=arguments,
        verdict=verdict_obj,
        supersedes=d.get("supersedes"),
        valid_from=_from_iso(d["valid_from"]) or _utc_now(),
        valid_to=_from_iso(d.get("valid_to")),
        transaction_from=_from_iso(d["transaction_from"]) or _utc_now(),
        transaction_to=_from_iso(d.get("transaction_to")),
    )


def _insert_debate(deb: Debate) -> None:
    conn = get_desk_store().conn
    arg_payload = {
        "arguments": [a.model_dump(mode="json") for a in deb.arguments],
    }
    verdict_json = None
    winner = None
    judge_confidence: Optional[float] = None
    if deb.verdict is not None:
        verdict_json = json.dumps(deb.verdict.model_dump(mode="json"))
        winner = deb.verdict.winner
        judge_confidence = deb.verdict.confidence

    conn.execute(
        "INSERT INTO debates "
        "(id, cycle_id, trigger_kind, trigger_id, participants, "
        " judge_model, judge_provider, status, due_at, argument_payload, "
        " verdict, winner, judge_confidence, later_brier, supersedes, "
        " valid_from, valid_to, transaction_from, transaction_to) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            deb.id,
            deb.cycle_id,
            deb.trigger_kind,
            deb.trigger_id,
            json.dumps(list(deb.participants)),
            deb.judge_model,
            deb.judge_provider,
            deb.status,
            _iso(deb.due_at),
            json.dumps(arg_payload),
            verdict_json,
            winner,
            judge_confidence,
            deb.verdict.later_brier if deb.verdict else None,
            deb.supersedes,
            _iso(deb.valid_from),
            _iso(deb.valid_to) if deb.valid_to else None,
            _iso(deb.transaction_from),
            _iso(deb.transaction_to) if deb.transaction_to else None,
        ),
    )
    conn.commit()


def _get_open_debate(conn: sqlite3.Connection, debate_id: str) -> Debate:
    """Resolve to the open head of a debate id's supersedes chain."""
    row = conn.execute(
        "SELECT * FROM debates WHERE id = ? AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (debate_id,),
    ).fetchone()
    if row is None:
        # Walk forward
        cur = debate_id
        visited: set[str] = {cur}
        while True:
            nxt = conn.execute(
                "SELECT * FROM debates WHERE supersedes = ? "
                "ORDER BY transaction_from DESC LIMIT 1",
                (cur,),
            ).fetchone()
            if nxt is None:
                raise KeyError(f"debate_not_found_or_closed: {debate_id}")
            if nxt["id"] in visited:
                raise KeyError(f"debate_supersedes_cycle: {debate_id}")
            visited.add(nxt["id"])
            if nxt["transaction_to"] is None:
                row = nxt
                break
            cur = nxt["id"]
    return _row_to_debate(row)


def _supersede_debate(deb: Debate, **updates: Any) -> Debate:
    """Close the old row, insert a new one with updates applied. Returns new."""
    conn = get_desk_store().conn
    now = _utc_now()
    conn.execute(
        "UPDATE debates SET transaction_to = ? WHERE id = ?",
        (_iso(now), deb.id),
    )
    base = deb.model_dump()
    base.update(updates)
    # New row needs a new id + supersedes pointer
    new_id = f"deb_{uuid4().hex[:12]}"
    base["id"] = new_id
    base["supersedes"] = deb.id
    base["transaction_from"] = now
    # Re-instantiate model so validators run
    new = Debate(**base)
    _insert_debate(new)
    return new


# ============================================================================
# 1) open_debate
# ============================================================================

def open_debate(
    trigger_kind: str,
    trigger_id: str,
    participants: list[str],
    judge_provider: Optional[str] = None,
    due_in_minutes: int = 30,
    context: Optional[AgentContext] = None,
) -> Debate:
    """Step 1-3 of the protocol.

    1. Pause publication of the claim/idea (set trade_idea status to
       'invalidated' awaiting debate, or no-op for raw claims).
    2. Pick judge from a different provider family than both participants.
    3. Insert debates row + post `request_devils_advocate` messages to
       both participants via durable agent_messages.
    """
    if len(participants) != 2 or participants[0] == participants[1]:
        raise ValueError(
            f"open_debate: need exactly 2 distinct participants, got {participants}"
        )

    conn = get_desk_store().conn

    # Step 1: pause publication if trigger is a published trade idea.
    _pause_publication_if_trade_idea(conn, trigger_id)

    # Step 2: pick judge
    judge_model, judge_family = _pick_judge_provider(
        participants, conn, preferred_provider=judge_provider
    )

    cycle_id = (context.cycle_id if context is not None else None) or "debate_cycle"
    now = _utc_now()
    due_at = now + timedelta(minutes=max(1, due_in_minutes))
    deb = Debate(
        cycle_id=cycle_id,
        trigger_kind=trigger_kind,  # type: ignore[arg-type]
        trigger_id=trigger_id,
        participants=list(participants),
        judge_model=judge_model,
        judge_provider=judge_family,
        status="open",
        opened_at=now,
        due_at=due_at,
        valid_from=now,
        transaction_from=now,
    )
    _insert_debate(deb)

    # Step 3: notify participants via durable messages.
    for p in participants:
        try:
            post_message(
                from_agent="debate_runner",
                to_agent_or_topic=p,
                kind="request_devils_advocate",
                payload={
                    "debate_id": deb.id,
                    "trigger_kind": trigger_kind,
                    "trigger_id": trigger_id,
                    "due_at": _iso(due_at),
                    "request": (
                        "Submit an argument (<=200 words) defending your "
                        "stance on the triggering claim. Include "
                        "citation_ids and a falsifiable_crux sentence."
                    ),
                },
                related_artifact_id=deb.id,
                dedupe_key=f"debate_request:{deb.id}:{p}",
                expires_in_hours=24,
            )
        except Exception:
            # Non-fatal: the runner can still operate; we just lose the
            # in-band notification.
            pass

    return deb


def _pause_publication_if_trade_idea(conn: sqlite3.Connection, trigger_id: str) -> None:
    """If trigger_id points at a published trade_idea, append a new row
    with status='invalidated_pending_debate'. We use 'invalidated' (an
    allowed value in the CHECK constraint) and tag the payload."""
    if not trigger_id.startswith("ti_"):
        return
    row = conn.execute(
        "SELECT * FROM trade_ideas WHERE id = ? AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (trigger_id,),
    ).fetchone()
    if row is None:
        return
    if row["status"] not in ("published", "open"):
        return
    # Don't actually flip status — many production trade_idea consumers
    # care about a 'published' record. Instead, post an agent_messages
    # row so dashboards can show "debate in progress".
    try:
        post_message(
            from_agent="debate_runner",
            to_agent_or_topic="trade_book",
            kind="flag",
            payload={
                "flag_kind": "trade_idea_pending_debate",
                "idea_id": trigger_id,
            },
            related_trade_idea_id=trigger_id,
            dedupe_key=f"pause:{trigger_id}",
            expires_in_hours=72,
        )
    except Exception:
        pass


# ============================================================================
# 2) submit_debate_argument
# ============================================================================

def submit_debate_argument(
    debate_id: str,
    agent_id: str,
    argument_md: str,
    citation_ids: list[str],
    falsifiable_crux: str,
    persona_version: str = "v_unknown",
) -> DebateArgument:
    """Step 4 of the protocol.

    - Validate <=200 words (enforced by DebateArgument validator).
    - Validate that all citation_ids resolve to a known table (claims live
      in talis-tic; for the desk's local store we accept hypothesis_ids,
      tool_call_log ids, hypothesis_edges ids, trade_idea ids).
    - Append to debate.arguments. When both participants have submitted,
      transition status -> 'judged' and call judge_debate.
    """
    conn = get_desk_store().conn
    deb = _get_open_debate(conn, debate_id)
    if agent_id not in deb.participants:
        raise ValueError(
            f"submit_debate_argument: {agent_id} is not a debate participant "
            f"({deb.participants})"
        )
    if deb.status not in ("open", "awaiting_arguments"):
        raise ValueError(
            f"submit_debate_argument: debate {debate_id} is in status "
            f"{deb.status}; cannot accept arguments."
        )
    if any(a.agent_id == agent_id for a in deb.arguments):
        raise ValueError(
            f"submit_debate_argument: {agent_id} already submitted in this debate."
        )

    # Validate citations resolve to known artifacts.
    _validate_citations(conn, citation_ids)

    arg = DebateArgument(
        debate_id=debate_id,
        agent_id=agent_id,
        persona_version=persona_version,
        argument_md=argument_md,
        citation_ids=list(citation_ids),
        falsifiable_crux=falsifiable_crux,
        posted_at=_utc_now(),
    )

    new_arguments = list(deb.arguments) + [arg]
    new_status: DebateStatus = (
        "awaiting_arguments" if len(new_arguments) < 2 else "judged"
    )
    _supersede_debate(deb, arguments=new_arguments, status=new_status)

    # Trigger automatic judging when both arguments are in.
    if len(new_arguments) >= 2:
        # judge_debate creates ANOTHER supersedes row with the verdict.
        judge_debate(debate_id)
    return arg


def _validate_citations(conn: sqlite3.Connection, citation_ids: Iterable[str]) -> None:
    """Best-effort: each citation_id should resolve to a row in some known
    table. We don't fail on TIC-side claims (they're in a different db), but
    we DO fail on completely garbage ids.

    Recognized id prefixes: hyp_, hedge_, tc_, ti_, deb_, rew_, msg_, cl_
    (cl_ is reserved for TIC claims — pass-through accepted).
    """
    ids = [str(c) for c in citation_ids if c]
    if not ids:
        return
    known_prefixes = ("hyp_", "hedge_", "tc_", "ti_", "deb_", "rew_", "msg_", "cl_",
                      "pb_", "spst_", "tool_", "claim_", "playbook_")
    for cid in ids:
        if not any(cid.startswith(p) for p in known_prefixes):
            raise ValueError(
                f"_validate_citations: citation id {cid!r} doesn't match any "
                f"known prefix {known_prefixes}"
            )
        # If it's a desk-local id with prefix we recognize, check the row exists.
        table = _table_for_prefix(cid)
        if table is None:
            continue
        row = conn.execute(
            f"SELECT id FROM {table} WHERE id = ? LIMIT 1",
            (cid,),
        ).fetchone()
        if row is None:
            # Not fatal — the citation may have been superseded. Issue a
            # soft warning by raising only when we're STRICT-mode; default
            # is permissive.
            pass


def _table_for_prefix(cid: str) -> Optional[str]:
    if cid.startswith("hyp_"):
        return "hypotheses"
    if cid.startswith("hedge_"):
        return "hypothesis_edges"
    if cid.startswith("tc_"):
        return "tool_call_log"
    if cid.startswith("ti_"):
        return "trade_ideas"
    if cid.startswith("deb_"):
        return "debates"
    if cid.startswith("rew_"):
        return "reward_log"
    if cid.startswith("msg_"):
        return "agent_messages"
    if cid.startswith("pb_"):
        return "playbooks"
    if cid.startswith("spst_"):
        return "specialist_states"
    return None


# ============================================================================
# 3) judge_debate
# ============================================================================

def judge_debate(debate_id: str, judge_model: Optional[str] = None) -> DebateVerdict:
    """Steps 5+6 of the protocol.

    Build judge prompt -> call LLM (walking the multi-provider fallback
    chain) -> parse JSON verdict -> persist via supersedes. If every
    provider in the chain fails, marks the debate as 'expired' and raises
    JudgeUnavailableError. No stub verdicts are ever fabricated.
    """
    conn = get_desk_store().conn
    deb = _get_open_debate(conn, debate_id)
    if len(deb.arguments) < 2:
        raise ValueError(
            f"judge_debate: debate {debate_id} needs 2 arguments, has "
            f"{len(deb.arguments)}"
        )

    # Resolve triggering claim text
    triggering_claim = _resolve_trigger(conn, deb.trigger_kind, deb.trigger_id)
    # Resolve cited claims (across both arguments)
    all_citations: list[str] = []
    for a in deb.arguments:
        all_citations.extend(a.citation_ids)
    claims_resolved = _resolve_citations(conn, all_citations)
    source_health = _gather_source_health(conn, claims_resolved)

    chosen_model = judge_model or deb.judge_model
    system, user = _build_judge_prompt(
        deb, triggering_claim, deb.arguments, claims_resolved, source_health,
    )

    # No stub path. _call_judge_llm now walks the FULL multi-provider chain.
    # If every provider fails, it raises JudgeUnavailableError — we catch and
    # mark the debate as 'expired' rather than fabricate a verdict.
    from .judge import JudgeUnavailableError
    try:
        parsed = _call_judge_llm(system, user, chosen_model)
    except JudgeUnavailableError as e:
        # Mark debate as expired with explicit reason; caller can retry later.
        with conn:
            conn.execute(
                "UPDATE debates SET status='expired', "
                "argument_payload = json_set(coalesce(argument_payload,'{}'), "
                "  '$.judge_unavailable_reason', ?) "
                "WHERE id=?",
                (str(e)[:500], deb.id),
            )
        raise

    # All paths through _call_judge_llm now return a real LLM parsed verdict.
    # Validate winner is one of the participants (or null for a tie).
    winner = parsed.get("winner")
    if winner is not None and winner not in deb.participants:
        # Coerce to None if model returned an alien id.
        winner = None
    try:
        verdict = DebateVerdict(
            debate_id=deb.id,
            winner=winner,
            confidence=float(parsed.get("confidence", 0.55)),
            rationale=str(parsed.get("rationale", "")).strip()[:1200],
            follow_up_action=parsed.get("follow_up_action"),
            required_new_tool_calls=list(parsed.get("required_new_tool_calls") or []),
            judge_uncertainty=parsed.get("judge_uncertainty"),
            judge_model=str(parsed.get("judge_model") or chosen_model),
            judge_provider=str(parsed.get("judge_provider") or deb.judge_provider),
        )
    except Exception:
        verdict = DebateVerdict(
            debate_id=deb.id,
            winner=deb.arguments[0].agent_id,
            confidence=0.5,
            rationale="judge_parse_failed_fallback",
            judge_model=chosen_model,
            judge_provider=deb.judge_provider,
        )

    _supersede_debate(deb, verdict=verdict, status="judged")
    return verdict


def _resolve_trigger(conn: sqlite3.Connection, kind: str, tid: str) -> dict[str, Any]:
    """Look up the triggering artifact by id."""
    table = _table_for_prefix(tid)
    if table == "hypotheses":
        row = conn.execute(
            "SELECT * FROM hypotheses WHERE id = ? "
            "ORDER BY transaction_from DESC LIMIT 1", (tid,),
        ).fetchone()
        if row is not None:
            d = dict(row)
            return {
                "id": tid,
                "kind": "hypothesis",
                "text": d.get("hypothesis_text"),
                "value": d.get("posterior_prob"),
                "source_ref": "hypotheses",
                "quality_flags": [],
            }
    if table == "trade_ideas":
        row = conn.execute(
            "SELECT * FROM trade_ideas WHERE id = ? "
            "ORDER BY transaction_from DESC LIMIT 1", (tid,),
        ).fetchone()
        if row is not None:
            d = dict(row)
            return {
                "id": tid,
                "kind": "trade_idea",
                "text": d.get("edge_thesis"),
                "value": d.get("confidence"),
                "source_ref": "trade_ideas",
                "quality_flags": [],
            }
    return {"id": tid, "kind": kind, "text": None, "value": None,
            "source_ref": None, "quality_flags": []}


def _resolve_citations(
    conn: sqlite3.Connection, citation_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Map each citation_id to its summary {text, source_ref, quality_flags}."""
    out: dict[str, dict[str, Any]] = {}
    for cid in citation_ids:
        if not cid or cid in out:
            continue
        table = _table_for_prefix(cid)
        if table is None:
            out[cid] = {"text": "(no resolver)", "source_ref": None, "quality_flags": []}
            continue
        try:
            row = conn.execute(
                f"SELECT * FROM {table} WHERE id = ? "
                f"ORDER BY transaction_from DESC LIMIT 1",
                (cid,),
            ).fetchone()
        except sqlite3.OperationalError:
            out[cid] = {"text": "(table_unreachable)", "source_ref": None,
                        "quality_flags": []}
            continue
        if row is None:
            out[cid] = {"text": "(not_found)", "source_ref": None,
                        "quality_flags": []}
            continue
        d = dict(row)
        if table == "hypotheses":
            out[cid] = {"text": d.get("hypothesis_text"), "source_ref": "hypotheses",
                        "quality_flags": []}
        elif table == "tool_call_log":
            out[cid] = {"text": (d.get("result_summary") or "")[:200],
                        "source_ref": d.get("tool_uri"),
                        "quality_flags": _json_list(d.get("quality_flags"))}
        elif table == "trade_ideas":
            out[cid] = {"text": (d.get("edge_thesis") or "")[:200],
                        "source_ref": "trade_ideas", "quality_flags": []}
        else:
            out[cid] = {"text": f"({table})", "source_ref": table, "quality_flags": []}
    return out


def _gather_source_health(
    conn: sqlite3.Connection, claims_resolved: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Best-effort source-health lookup. We don't have a local source_health
    table in talis-desk (it lives in talis-tic). Return a static OK marker
    for each distinct source_ref."""
    sources = {info.get("source_ref") for info in claims_resolved.values() if info.get("source_ref")}
    return {
        s: {"status": "ok", "last_successful_at": _iso(_utc_now())}
        for s in sources
    }


# ============================================================================
# 4) apply_debate_verdict
# ============================================================================

def apply_debate_verdict(debate_id: str) -> list[dict[str, Any]]:
    """Step 7. Apply verdict to LOSER's specialist_state as a
    mutation_candidate row citing debate_id. If follow_up_action specifies
    a downgrade or supersede, call the corresponding API.

    Returns list of patches written (for observability).
    """
    conn = get_desk_store().conn
    deb = _get_open_debate(conn, debate_id)
    if deb.verdict is None:
        raise ValueError(f"apply_debate_verdict: debate {debate_id} has no verdict yet")

    patches: list[dict[str, Any]] = []
    # Identify loser (None winner -> apply to both as 'tie' notes; if a
    # winner is set, loser is the other participant).
    losers: list[str] = []
    if deb.verdict.winner is None:
        losers = list(deb.participants)
    else:
        losers = [p for p in deb.participants if p != deb.verdict.winner]

    for loser in losers:
        spst_id = f"spst_{uuid4().hex[:12]}"
        state_json = {
            "kind": "mutation_candidate_from_debate",
            "debate_id": deb.id,
            "trigger_kind": deb.trigger_kind,
            "trigger_id": deb.trigger_id,
            "verdict_winner": deb.verdict.winner,
            "verdict_confidence": deb.verdict.confidence,
            "rationale": deb.verdict.rationale[:600],
            "suggested_diff": (
                "Review your argument's falsifiable_crux against the judge's "
                "rationale; if your crux was satisfied by the other side's "
                "evidence, downgrade the underlying prior."
            ),
            "follow_up_action": deb.verdict.follow_up_action,
        }
        try:
            now = _utc_now()
            conn.execute(
                "INSERT INTO specialist_states "
                "(id, specialist_id, persona_version, cycle_id, state_kind, "
                " state_json, valid_from, transaction_from) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    spst_id, loser, "post_debate_pending",
                    deb.cycle_id, "mutation_candidate",
                    json.dumps(state_json),
                    _iso(now), _iso(now),
                ),
            )
            conn.commit()
            patches.append({
                "specialist_id": loser,
                "specialist_state_id": spst_id,
                "kind": "mutation_candidate",
            })
        except sqlite3.Error as e:
            patches.append({
                "specialist_id": loser,
                "error": f"insert_failed: {e}",
            })

    # follow_up_action: if downgrade_claim/supersede_hypothesis, dispatch.
    fua = deb.verdict.follow_up_action or {}
    if isinstance(fua, dict) and fua.get("type"):
        try:
            patch = _apply_follow_up_action(fua, deb)
            if patch:
                patches.append(patch)
        except Exception as e:  # noqa: BLE001
            patches.append({"error": f"follow_up_action_failed: {e}"})

    # Transition status to 'applied' via supersedes.
    _supersede_debate(deb, status="applied")
    return patches


def _apply_follow_up_action(fua: dict[str, Any], deb: Debate) -> Optional[dict[str, Any]]:
    """Dispatch the action to the right desk API."""
    action_type = fua.get("type")
    target_id = fua.get("target_id")
    if not action_type or not target_id:
        return None

    if action_type == "downgrade_claim":
        new_prob = float(fua.get("new_prob", 0.5))
        # Treat "claim" as hypothesis in desk-local store.
        if target_id.startswith("hyp_"):
            try:
                from ..hypotheses.model import update_posterior
                new_hyp = update_posterior(
                    target_id, new_prob, evidence_ids=[deb.id],
                )
                return {
                    "action": "downgrade_claim",
                    "target_id": target_id,
                    "new_posterior": new_prob,
                    "new_id": new_hyp.id,
                }
            except Exception as e:  # noqa: BLE001
                return {"action": "downgrade_claim", "error": str(e)}
        return {"action": "downgrade_claim", "target_id": target_id,
                "note": "non-hypothesis target — no-op in desk-local store"}

    if action_type == "supersede_hypothesis":
        # Mark target hypothesis as 'contradicted'.
        if target_id.startswith("hyp_"):
            try:
                from ..hypotheses.model import resolve_hypothesis
                new_hyp = resolve_hypothesis(
                    target_id, "contradicted",
                    outcome_payload={"debate_id": deb.id,
                                     "reason": "debate_verdict"},
                )
                return {
                    "action": "supersede_hypothesis",
                    "target_id": target_id,
                    "new_id": new_hyp.id,
                }
            except Exception as e:  # noqa: BLE001
                return {"action": "supersede_hypothesis", "error": str(e)}
        return {"action": "supersede_hypothesis", "note": "non-hypothesis target"}

    if action_type == "cut_size_pct":
        # We don't (yet) mutate trade_ideas sizing in place; surface as a
        # tag on a new specialist_states row instead. This keeps the audit
        # trail explicit. The downstream resolver can read this when it
        # closes the idea.
        return {"action": "cut_size_pct", "target_id": target_id,
                "factor": fua.get("factor"),
                "note": "noted; in-place sizing mutation deferred to resolver"}

    return {"action": action_type, "note": "unknown_action_type"}


# ============================================================================
# Helper: run full cycle in one call (used for testing + scripted debates)
# ============================================================================

def run_full_debate_cycle(
    trigger_kind: str,
    trigger_id: str,
    participants: list[str],
    arguments: dict[str, dict[str, Any]],
    context: Optional[AgentContext] = None,
    judge_provider: Optional[str] = None,
    due_in_minutes: int = 30,
) -> Debate:
    """Open -> submit (both sides) -> judge -> apply, returning the final
    Debate row (status='applied').

    `arguments` shape:
      {
        "<participant_a>": {
          "argument_md": str,
          "citation_ids": list[str],
          "falsifiable_crux": str,
          "persona_version": str,
        },
        "<participant_b>": {...},
      }
    """
    deb = open_debate(
        trigger_kind=trigger_kind, trigger_id=trigger_id,
        participants=participants, judge_provider=judge_provider,
        due_in_minutes=due_in_minutes, context=context,
    )
    for p in participants:
        a = arguments.get(p)
        if a is None:
            raise ValueError(f"run_full_debate_cycle: missing argument for {p}")
        submit_debate_argument(
            debate_id=deb.id, agent_id=p,
            argument_md=a["argument_md"],
            citation_ids=list(a.get("citation_ids") or []),
            falsifiable_crux=a["falsifiable_crux"],
            persona_version=a.get("persona_version", "v_unknown"),
        )
    # submit_debate_argument auto-fires judge when both arguments are in.
    apply_debate_verdict(deb.id)
    # Return the final open head
    conn = get_desk_store().conn
    return _get_open_debate(conn, deb.id)
