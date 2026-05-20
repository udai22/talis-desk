"""Persona mutation — Phase 6 Layer 5 of SOTA Desk Architecture v2.

Per wiki/SOTA_DESK_ARCHITECTURE.md v2 §2 Layer 5 (lines 138-152):

  1. Meta-agent reads `reward_log`, `mv_specialist_brier_rolling`, and
     `mv_weakness_map` for each specialist.
  2. Proposes a small prompt diff: add/remove one bullet OR change one
     threshold OR add/remove one tool URI.
  3. Appended as `specialist_states.state_kind='mutation_candidate'`.
  4. A/B runs base vs candidate on identical bitemporal snapshots
     + frozen tool atlas.
  5. Human veto window is 24h. After 24h with no veto and positive
     metrics, auto-promote.

# Bitemporal append-only contract
All writes are append-only. promote/rollback NEVER modify existing rows
in-place; they UPDATE the `transaction_to` of the prior row (closing the
audit window) and INSERT a new row pointing back via `supersedes` and
`parent_state_id`. This matches `talis_desk.specialists.base.register_persona`.

# Real LLM, no stubs
`run_nightly_auto_mutation` calls the mutation model via
`tic.desk.models.chat()` and walks the full 8-provider fallback chain
(`_MUTATION_FALLBACK_CHAIN`) ourselves. If every provider returns
empty / errors / unparseable diffs, we raise
`MutationProposalUnavailableError` and the caller SKIPS the specialist
for the cycle. We never fabricate a heuristic diff or write a candidate
based on a hand-coded rule — that would corrupt the mutation history
with non-LLM signals and pollute the dashboard's audit trail.

# Honest gaps
- Haiku's prompt-diff parsing is permissive: we accept any structured JSON
  with at least one of {prompt_bullet_added, prompt_bullet_removed,
  tool_uri_added, tool_uri_removed, threshold_changed}. If the model
  returns prose that can't be parsed, that provider's attempt is
  recorded as `unparseable_diff_json` in last_error and we move to the
  next provider in the chain. No heuristic backstop.
- `check_veto_window` only inspects `agent_messages` with
  `message_kind='veto'`. Real production would also gate on a manual
  approval UI; that's Phase 7.
- `promote_persona` writes a full new persona row using the candidate's
  diff applied to the base persona. It does NOT call A/B testing; the
  caller (auto-mutation loop) is expected to run A/B and pass the
  judge_verdict_id when promotion is the right move.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import sqlite3
import sys
import uuid

logger = logging.getLogger(__name__)
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional


# ============================================================================
# Constants
# ============================================================================

#: Veto window — humans get this long to post a `veto` message on a
#: mutation_candidate before auto-promote fires. Per v2 §2 line 150.
VETO_WINDOW_HOURS = 24

#: Default mutation-proposal model. Haiku is cheap (~$0.05/specialist/run)
#: and the diff is small, so we don't need Opus here.
DEFAULT_MUTATION_MODEL = "anthropic:claude-haiku-4-5"
DEFAULT_FALLBACK_MODEL = "anthropic:claude-sonnet-4-6"

#: Full multi-provider fallback chain for mutation proposals. Mirrors the
#: judge.py / brief composer pattern. NEVER stub — if every provider in
#: this chain fails, the caller skips that specialist for this cycle.
_MUTATION_FALLBACK_CHAIN = [
    "anthropic:claude-haiku-4-5",
    "anthropic:claude-sonnet-4-6",
    "anthropic:claude-opus-4-7",
    "openai:gpt-5.5",
    "deepseek:v4-pro",
    "xai:grok-4",
    "moonshot:v1-32k",
    "perplexity:sonar-pro",
]


class MutationProposalUnavailableError(RuntimeError):
    """Raised when every provider in the mutation fallback chain has
    failed (or returned unparseable diffs). NO stub diffs — the caller
    skips this specialist for the current cycle rather than mutating
    the persona based on a heuristic guess."""


#: We require this many rewards in the window before proposing a mutation
#: — too few signals and the mutation is noise.
MIN_REWARDS_FOR_MUTATION = 3


# ============================================================================
# Types
# ============================================================================

PersonaDiffKind = Literal[
    "prompt_bullet_added",
    "prompt_bullet_removed",
    "tool_uri_added",
    "tool_uri_removed",
    "threshold_changed",
]


@dataclass
class PersonaDiff:
    """A small, surgical change to a persona. One of the kinds below MUST be set.

    The mutator constructs one of these from Haiku's structured output (or a
    heuristic fallback if Haiku is unavailable). The diff is applied at promote
    time, not at propose time — propose is "this is what we'd do if approved".
    """
    kind: PersonaDiffKind
    rationale: str
    # Exactly one of these is populated depending on `kind`:
    prompt_bullet: Optional[str] = None
    tool_uri: Optional[str] = None
    threshold_name: Optional[str] = None
    threshold_old: Optional[float] = None
    threshold_new: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "rationale": self.rationale,
            "prompt_bullet": self.prompt_bullet,
            "tool_uri": self.tool_uri,
            "threshold_name": self.threshold_name,
            "threshold_old": self.threshold_old,
            "threshold_new": self.threshold_new,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PersonaDiff":
        return cls(
            kind=d["kind"],
            rationale=d.get("rationale", ""),
            prompt_bullet=d.get("prompt_bullet"),
            tool_uri=d.get("tool_uri"),
            threshold_name=d.get("threshold_name"),
            threshold_old=d.get("threshold_old"),
            threshold_new=d.get("threshold_new"),
        )


@dataclass
class PersonaMutationCandidate:
    """A row in specialist_states with state_kind='mutation_candidate'."""

    id: str
    specialist_id: str
    persona_version: str
    cycle_id: str
    parent_state_id: Optional[str]
    reason: str
    diff: PersonaDiff
    state_json: dict[str, Any]
    valid_from: datetime
    transaction_from: datetime
    quality_flags: list[str] = field(default_factory=list)


@dataclass
class VetoStatus:
    """Output of `check_veto_window` — enough to decide auto-promote vs wait."""

    candidate_id: str
    proposed_at: datetime
    age_hours: float
    veto_received: bool
    veto_message_id: Optional[str]
    elapsed_window: bool
    status: Literal["too_early", "vetoed", "ready_to_promote"]
    notes: list[str] = field(default_factory=list)


# ============================================================================
# DB helpers
# ============================================================================

def _resolve_conn(conn: Optional[sqlite3.Connection]) -> sqlite3.Connection:
    if conn is not None:
        return conn
    from ..store import get_desk_store
    return get_desk_store().conn


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================================
# Mutation candidate writer
# ============================================================================

def propose_persona_mutation(
    specialist_id: str,
    reason: str,
    diff: PersonaDiff,
    author: str = "auto_mutator",
    *,
    conn: Optional[sqlite3.Connection] = None,
    cycle_id: str = "auto_mutation_nightly",
    quality_flags: Optional[list[str]] = None,
) -> PersonaMutationCandidate:
    """Insert a specialist_states row with state_kind='mutation_candidate'.

    Bitemporal append-only. The current open persona row is NOT closed
    (mutations are PROPOSALS; the persona keeps running until promote).
    `parent_state_id` points at the current persona so the dashboard can
    show the diff base.
    """
    conn = _resolve_conn(conn)

    # Find the current open persona to use as the parent.
    parent_row = conn.execute(
        "SELECT id, persona_version, state_json FROM specialist_states "
        "WHERE specialist_id = ? AND state_kind = 'persona' "
        "AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (specialist_id,),
    ).fetchone()
    if parent_row is None:
        raise KeyError(
            f"propose_persona_mutation: no current persona for "
            f"specialist {specialist_id!r}"
        )
    parent_id = parent_row["id"]
    persona_version = parent_row["persona_version"]
    try:
        parent_state = json.loads(parent_row["state_json"] or "{}")
    except json.JSONDecodeError:
        parent_state = {}

    now = _utc_now()
    now_iso = _iso(now)
    candidate_id = "spst_" + uuid.uuid4().hex[:12]

    state_json = {
        "specialist_id": specialist_id,
        "candidate_for_persona_version": persona_version,
        "author": author,
        "reason": reason,
        "diff": diff.to_dict(),
        "parent_state_id": parent_id,
        "quality_flags": list(quality_flags or []),
    }

    conn.execute(
        "INSERT INTO specialist_states ("
        "id, specialist_id, persona_version, cycle_id, state_kind, state_json, "
        "prompt_hash, parent_state_id, valid_from, transaction_from"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            candidate_id, specialist_id, persona_version, cycle_id,
            "mutation_candidate", _canonical_json(state_json),
            None,  # prompt_hash filled at promote time
            parent_id,
            now_iso, now_iso,
        ),
    )

    return PersonaMutationCandidate(
        id=candidate_id,
        specialist_id=specialist_id,
        persona_version=persona_version,
        cycle_id=cycle_id,
        parent_state_id=parent_id,
        reason=reason,
        diff=diff,
        state_json=state_json,
        valid_from=now,
        transaction_from=now,
        quality_flags=list(quality_flags or []),
    )


# ============================================================================
# Nightly auto-mutation
# ============================================================================

def run_nightly_auto_mutation(
    as_of: Optional[datetime] = None,
    *,
    conn: Optional[sqlite3.Connection] = None,
    specialist_ids: Optional[list[str]] = None,
    window_days: int = 15,
    mutation_model: str = DEFAULT_MUTATION_MODEL,
    fallback_model: str = DEFAULT_FALLBACK_MODEL,
) -> list[PersonaMutationCandidate]:
    """Run the nightly meta-mutator across every specialist.

    For each specialist:
      1. Read aggregate_specialist_rewards over `window_days`.
      2. Read mv_specialist_brier_rolling.
      3. Identify the top 5 weaknesses (worst-scoring subjects).
      4. Ask Haiku to propose a small prompt/threshold/tool diff.
      5. Write the diff as a mutation_candidate row.
      6. (Auto-promote in 24h is handled by `check_veto_window` + a
         separate cron call to `promote_persona`. We do NOT auto-promote
         inside this function — that would skip the veto window.)

    Returns the list of candidates created this pass.
    """
    from ..eval.rewards import aggregate_specialist_rewards
    conn = _resolve_conn(conn)
    as_of = as_of or _utc_now()

    # If specialist_ids not given, derive from open persona rows.
    if specialist_ids is None:
        rows = conn.execute(
            "SELECT DISTINCT specialist_id FROM specialist_states "
            "WHERE state_kind = 'persona' AND transaction_to IS NULL"
        ).fetchall()
        specialist_ids = [r["specialist_id"] for r in rows]

    candidates: list[PersonaMutationCandidate] = []
    for sid in specialist_ids:
        try:
            agg = aggregate_specialist_rewards(
                sid, window_days=window_days, conn=conn, as_of=as_of,
            )
        except Exception as e:  # noqa: BLE001
            # Specialist has no rewards — skip; not enough signal.
            continue

        total_n = sum(b.get("n", 0.0) for b in agg.per_kind.values())
        if total_n < MIN_REWARDS_FOR_MUTATION:
            continue

        # Pull rolling Brier from mv_specialist_brier_rolling
        brier_rolling = _fetch_brier_rolling(conn, sid, window_days)

        # Pull current persona for the LLM to read + diff against.
        persona_row = conn.execute(
            "SELECT id, persona_version, state_json FROM specialist_states "
            "WHERE specialist_id = ? AND state_kind = 'persona' "
            "AND transaction_to IS NULL "
            "ORDER BY transaction_from DESC LIMIT 1",
            (sid,),
        ).fetchone()
        if persona_row is None:
            continue
        try:
            persona_state = json.loads(persona_row["state_json"] or "{}")
        except json.JSONDecodeError:
            persona_state = {}

        # Ask Haiku for a diff (real LLM call via the full fallback chain).
        # If every provider fails, SKIP this specialist for the cycle — we
        # never fabricate a heuristic diff. The dashboard will see "no
        # mutation proposed" with the underlying reason in the log.
        try:
            diff, quality_flags, llm_meta = _propose_diff_via_llm(
                specialist_id=sid,
                persona_state=persona_state,
                agg=agg,
                brier_rolling=brier_rolling,
                mutation_model=mutation_model,
                fallback_model=fallback_model,
            )
        except MutationProposalUnavailableError as e:
            logger.warning(
                "run_nightly_auto_mutation: skipping specialist=%s — "
                "every provider in the mutation fallback chain failed. "
                "reason=%s", sid, str(e)[:300],
            )
            continue

        # The reason explicitly cites the measurable weakness so the dashboard
        # can show "why this diff was proposed".
        weakness_summary = _format_weakness_summary(agg)
        reason = (
            f"nightly_auto_mutation: {weakness_summary} "
            f"(brier_rolling_avg={brier_rolling.get('brier_avg')}, "
            f"n_window_rewards={int(total_n)})"
        )
        cand = propose_persona_mutation(
            specialist_id=sid,
            reason=reason,
            diff=diff,
            author="auto_mutator",
            conn=conn,
            cycle_id=f"auto_mutation_{as_of.strftime('%Y%m%d')}",
            quality_flags=quality_flags,
        )
        # Attach LLM provenance to the candidate's payload for the dashboard
        cand.state_json["llm_meta"] = llm_meta
        cand.state_json["weakness_summary"] = weakness_summary
        # Re-write state_json with the extra metadata (idempotent UPDATE on
        # the row we just inserted — bitemporal-safe because we don't change
        # transaction_from/_to or supersedes).
        conn.execute(
            "UPDATE specialist_states SET state_json = ? WHERE id = ?",
            (_canonical_json(cand.state_json), cand.id),
        )
        candidates.append(cand)

    return candidates


def _format_weakness_summary(agg: Any) -> str:
    """Compact natural-language summary of the specialist's weaknesses, for
    inclusion in the candidate's `reason` field (audit trail)."""
    parts: list[str] = []
    pk = agg.per_kind
    if "correctness" in pk:
        parts.append(f"correctness_avg={pk['correctness']['score_avg']:.3f}")
    if "alpha" in pk:
        parts.append(f"alpha_avg={pk['alpha']['score_avg']:.3f}")
    if "cost_penalty" in pk and pk["cost_penalty"]["score_avg"] < 0:
        parts.append(f"cost_penalty_avg={pk['cost_penalty']['score_avg']:.3f}")
    if agg.weaknesses:
        worst = agg.weaknesses[0]
        parts.append(
            f"worst_subject={worst['subject_id']}@score={worst['score']:.3f}"
        )
    return "; ".join(parts) if parts else "no_dominant_weakness"


def _fetch_brier_rolling(
    conn: sqlite3.Connection,
    specialist_id: str,
    window_days: int,
) -> dict[str, Any]:
    """Read mv_specialist_brier_rolling for this specialist. Returns the
    last `window_days` of rows (or {} if empty)."""
    try:
        rows = conn.execute(
            "SELECT day, brier_avg, brier_delta_avg, n "
            "FROM mv_specialist_brier_rolling "
            "WHERE specialist_id = ? "
            "ORDER BY day DESC LIMIT ?",
            (specialist_id, window_days),
        ).fetchall()
    except sqlite3.Error:
        return {}
    if not rows:
        return {}
    briers = [float(r["brier_avg"]) for r in rows if r["brier_avg"] is not None]
    if not briers:
        return {}
    return {
        "n_days": len(rows),
        "brier_avg": sum(briers) / len(briers),
        "brier_min": min(briers),
        "brier_max": max(briers),
        "rows": [dict(r) for r in rows[:5]],  # top-5 for audit
    }


# ============================================================================
# LLM-driven diff proposal
# ============================================================================

_MUTATION_SYSTEM_PROMPT = (
    "You are a meta-research coordinator for a quantitative trading desk. "
    "A specialist agent has weaknesses in its recent performance. Your job: "
    "propose ONE small, surgical change to its persona — either:\n"
    "  - add or remove a single prompt bullet,\n"
    "  - add or remove a single tool URI,\n"
    "  - change a single numeric threshold (e.g., confidence floor).\n\n"
    "Constraints:\n"
    "  1. The diff must be small (single bullet/uri/threshold). NEVER rewrite "
    "the whole prompt.\n"
    "  2. Cite the SPECIFIC measurable weakness you are addressing.\n"
    "  3. Do NOT touch the black-swan caution prior (always keep).\n\n"
    "Output STRICTLY this JSON shape (no prose outside the JSON):\n"
    "{\n"
    '  "kind": "prompt_bullet_added"|"prompt_bullet_removed"|"tool_uri_added"|'
    '"tool_uri_removed"|"threshold_changed",\n'
    '  "rationale": "<=80 words citing the weakness",\n'
    '  "prompt_bullet": "<the bullet text if kind starts with prompt_>",\n'
    '  "tool_uri": "tic://tool/... if kind starts with tool_uri_",\n'
    '  "threshold_name": "<name if threshold_changed>",\n'
    '  "threshold_old": <number>,\n'
    '  "threshold_new": <number>\n'
    "}\n"
    "Set unused fields to null. Do NOT include other keys."
)


def _propose_diff_via_llm(
    *,
    specialist_id: str,
    persona_state: dict[str, Any],
    agg: Any,
    brier_rolling: dict[str, Any],
    mutation_model: str,
    fallback_model: str,
) -> tuple[PersonaDiff, list[str], dict[str, Any]]:
    """Walk the full multi-provider fallback chain to propose a persona diff.

    NO STUBS: if every provider in the chain returns empty / errors /
    unparseable JSON, raise MutationProposalUnavailableError. The caller
    (run_nightly_auto_mutation) catches and skips this specialist for the
    cycle — we do NOT fabricate a heuristic diff and feed it into the
    persona's mutation history.
    """
    user_prompt = _build_mutation_user_prompt(
        specialist_id=specialist_id,
        persona_state=persona_state,
        agg=agg,
        brier_rolling=brier_rolling,
    )

    # Build chain: caller-supplied primary + optional secondary, then the
    # canonical chain, deduplicated.
    seen: set[str] = set()
    chain: list[str] = []
    for m in (mutation_model, fallback_model):
        if m and m not in seen:
            chain.append(m); seen.add(m)
    for m in _MUTATION_FALLBACK_CHAIN:
        if m not in seen:
            chain.append(m); seen.add(m)

    last_error: Optional[str] = None
    last_meta: dict[str, Any] = {}
    for i, m in enumerate(chain):
        text, meta = _call_mutation_llm(
            system=_MUTATION_SYSTEM_PROMPT,
            user=user_prompt,
            model=m,
            fallback=None,  # WE walk the chain, not chat()'s internal fallback
        )
        last_meta = meta
        if meta.get("error"):
            last_error = f"{m}: {meta['error']}"
            continue
        if not text:
            last_error = f"{m}: empty_completion"
            continue
        diff = _parse_diff_json(text)
        if diff is None:
            last_error = f"{m}: unparseable_diff_json"
            continue
        meta["chain_position"] = i
        meta["fallback_used"] = (i > 0)
        return diff, [], meta

    raise MutationProposalUnavailableError(
        f"All {len(chain)} providers in the mutation fallback chain failed "
        f"to return a parseable persona diff. Last error: {last_error}. "
        f"Chain: {chain}. Last meta: {last_meta}"
    )


def _build_mutation_user_prompt(
    *,
    specialist_id: str,
    persona_state: dict[str, Any],
    agg: Any,
    brier_rolling: dict[str, Any],
) -> str:
    """Compose the user message the meta-mutator LLM sees."""
    lines: list[str] = []
    lines.append(f"# Specialist: {specialist_id}")
    lines.append(f"persona_version: {persona_state.get('persona_version','?')}")
    lines.append("")
    lines.append("## Current system prompt (first 2000 chars)")
    sp = (persona_state.get("system_prompt") or "")[:2000]
    lines.append("```")
    lines.append(sp)
    lines.append("```")
    lines.append("")
    lines.append("## Curated tool URIs (current)")
    for uri in (persona_state.get("tool_uris") or [])[:20]:
        lines.append(f"  - {uri}")
    lines.append("")
    lines.append("## Reward window aggregates")
    for kind, b in agg.per_kind.items():
        lines.append(
            f"- {kind}: n={int(b.get('n',0))}, "
            f"score_avg={b.get('score_avg', 0.0):.4f}, "
            f"delta_avg={b.get('delta_avg', 0.0):.4f}, "
            f"min={b.get('score_min', 0.0):.4f}, "
            f"max={b.get('score_max', 0.0):.4f}"
        )
    if agg.weaknesses:
        lines.append("")
        lines.append("## Top weaknesses (worst-scoring subjects)")
        for w in agg.weaknesses:
            lines.append(
                f"  - {w['reward_kind']} on {w['subject_kind']}/{w['subject_id']}: "
                f"score={w['score']:.4f}, delta={w['delta']:.4f}"
            )
    if brier_rolling:
        lines.append("")
        lines.append("## Rolling Brier")
        lines.append(
            f"  brier_avg={brier_rolling.get('brier_avg')}, "
            f"min={brier_rolling.get('brier_min')}, "
            f"max={brier_rolling.get('brier_max')}, "
            f"n_days={brier_rolling.get('n_days')}"
        )
    lines.append("")
    lines.append("Propose ONE surgical diff. Output JSON per the system schema.")
    return "\n".join(lines)


def _call_mutation_llm(
    *, system: str, user: str, model: str, fallback: Optional[str],
) -> tuple[str, dict[str, Any]]:
    """Synchronous wrapper around tic.desk.models.chat(). Returns
    (response_text, meta) where meta has {model_used, provider,
    fallback_used, error}. Empty text + error if all providers fail.
    """
    text: str = ""
    meta: dict[str, Any] = {"model_used": model, "provider": "?",
                            "fallback_used": False, "error": None}
    try:
        sib = "/Users/udaikhattar/jarvis-ios/docs/research/brief_experiments"
        if sib not in sys.path:
            sys.path.insert(0, sib)
        from tic.desk.models import chat as _chat  # type: ignore
    except Exception as e:  # noqa: BLE001
        meta["error"] = f"models_import_failed: {e}"
        return text, meta

    async def _run() -> dict[str, Any]:
        return await _chat(model, system, user, fallback=fallback)

    try:
        try:
            asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(asyncio.run, _run())
                res = fut.result(timeout=60)
        except RuntimeError:
            res = asyncio.run(_run())
    except Exception as e:  # noqa: BLE001
        meta["error"] = f"chat_call_failed: {type(e).__name__}: {e}"
        return text, meta

    text = res.get("text", "") or ""
    meta["model_used"] = res.get("model_used", model)
    meta["provider"] = res.get("provider", "?")
    meta["fallback_used"] = bool(res.get("fallback_used"))
    meta["error"] = res.get("error")
    return text, meta


def _parse_diff_json(text: str) -> Optional[PersonaDiff]:
    """Pull a JSON object out of the model response. Returns None if no
    valid diff can be extracted (caller falls through to heuristic)."""
    text = (text or "").strip()
    if not text:
        return None
    # Try direct
    obj: Optional[dict[str, Any]] = None
    try:
        obj = json.loads(text)
    except Exception:
        pass
    # Fenced
    if obj is None and "```" in text:
        chunks = text.split("```")
        for c in chunks:
            c = c.strip()
            if c.startswith("json"):
                c = c[4:].strip()
            try:
                obj = json.loads(c)
                break
            except Exception:
                continue
    # First-brace
    if obj is None:
        start = text.find("{")
        end = text.rfind("}")
        if 0 <= start < end:
            try:
                obj = json.loads(text[start:end + 1])
            except Exception:
                pass
    if not isinstance(obj, dict):
        return None
    kind = obj.get("kind")
    if kind not in (
        "prompt_bullet_added", "prompt_bullet_removed",
        "tool_uri_added", "tool_uri_removed", "threshold_changed",
    ):
        return None
    try:
        return PersonaDiff.from_dict(obj)
    except Exception:
        return None


# Removed `_heuristic_fallback_diff`. The no-stubs rule disallows fabricated
# mutation proposals — when every provider in the chain fails,
# _propose_diff_via_llm raises MutationProposalUnavailableError and the
# caller (run_nightly_auto_mutation) skips that specialist for the cycle.


# ============================================================================
# Promote / rollback
# ============================================================================

def promote_persona(
    candidate_id: str,
    judge_verdict_id: Optional[str] = None,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> Any:
    """Move candidate -> persona. Idempotent.

    Writes a NEW row with state_kind='persona' produced by applying the
    candidate's diff to the parent persona. The candidate row is closed
    (transaction_to = now) AND the prior persona row is closed. The new
    persona row points to candidate via supersedes and parent_state_id.

    Returns the new SpecialistState describing the promoted row.
    """
    from ..specialists.base import (
        SpecialistPersona, SpecialistState, _row_to_specialist_state,
    )
    conn = _resolve_conn(conn)

    cand_row = conn.execute(
        "SELECT * FROM specialist_states WHERE id = ? "
        "AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (candidate_id,),
    ).fetchone()
    if cand_row is None:
        raise KeyError(f"promote_persona: candidate {candidate_id!r} not found or already closed")
    if cand_row["state_kind"] != "mutation_candidate":
        # Idempotency: if it's already a persona, return it
        if cand_row["state_kind"] == "persona":
            return _row_to_specialist_state(dict(cand_row))
        raise ValueError(
            f"promote_persona: candidate {candidate_id!r} has "
            f"state_kind={cand_row['state_kind']!r}; expected 'mutation_candidate'"
        )

    cand_state = json.loads(cand_row["state_json"] or "{}")
    diff = PersonaDiff.from_dict(cand_state.get("diff", {}))
    # sqlite3.Row lacks .get(); pre-convert for safe lookup.
    cand_dict = dict(cand_row)
    parent_id = cand_state.get("parent_state_id") or cand_dict.get("parent_state_id")
    if not parent_id:
        raise ValueError(
            f"promote_persona: candidate {candidate_id!r} has no parent_state_id"
        )

    parent_row = conn.execute(
        "SELECT * FROM specialist_states WHERE id = ?",
        (parent_id,),
    ).fetchone()
    if parent_row is None:
        raise KeyError(f"promote_persona: parent persona {parent_id!r} not found")
    parent_state_json = json.loads(parent_row["state_json"] or "{}")

    # Apply diff
    new_state_json = _apply_diff(parent_state_json, diff)
    # Bump persona_version by appending a micro-tag derived from candidate id.
    base_version = parent_row["persona_version"]
    new_version = _bump_version(base_version, candidate_id)
    new_state_json["persona_version"] = new_version
    new_state_json["promoted_from_candidate"] = candidate_id
    if judge_verdict_id is not None:
        new_state_json["judge_verdict_id"] = judge_verdict_id

    # Hash the new system prompt for the prompt_hash column
    new_prompt = new_state_json.get("system_prompt", "")
    new_hash = hashlib.sha256(new_prompt.encode("utf-8")).hexdigest()

    now = _utc_now()
    now_iso = _iso(now)

    # Close parent persona row
    conn.execute(
        "UPDATE specialist_states SET transaction_to = ? "
        "WHERE id = ? AND transaction_to IS NULL",
        (now_iso, parent_id),
    )
    # Close candidate row
    conn.execute(
        "UPDATE specialist_states SET transaction_to = ? "
        "WHERE id = ? AND transaction_to IS NULL",
        (now_iso, candidate_id),
    )

    new_id = "spst_" + uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO specialist_states ("
        "id, specialist_id, persona_version, cycle_id, state_kind, state_json, "
        "prompt_hash, parent_state_id, supersedes, valid_from, transaction_from"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id, cand_row["specialist_id"], new_version,
            cand_row["cycle_id"], "persona", _canonical_json(new_state_json),
            new_hash, candidate_id, candidate_id,
            now_iso, now_iso,
        ),
    )

    new_row = conn.execute(
        "SELECT * FROM specialist_states WHERE id = ?", (new_id,)
    ).fetchone()
    return _row_to_specialist_state(dict(new_row))


def rollback_persona(
    specialist_id: str,
    target_version: str,
    reason: str,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> Any:
    """Append-only rollback. Writes a specialist_states row with
    state_kind='rollback' linking back to the target_version, then a
    state_kind='persona' row with the target's state_json as the now-current
    persona. Original rows are NEVER deleted.

    Raises KeyError if no persona row with `target_version` exists for this
    specialist.
    """
    from ..specialists.base import _row_to_specialist_state
    conn = _resolve_conn(conn)

    target_row = conn.execute(
        "SELECT * FROM specialist_states "
        "WHERE specialist_id = ? AND persona_version = ? "
        "AND state_kind = 'persona' "
        "ORDER BY transaction_from DESC LIMIT 1",
        (specialist_id, target_version),
    ).fetchone()
    if target_row is None:
        raise KeyError(
            f"rollback_persona: no persona with version {target_version!r} "
            f"for specialist {specialist_id!r}"
        )

    # Find any currently-open persona (different version) to close.
    current_row = conn.execute(
        "SELECT id, persona_version FROM specialist_states "
        "WHERE specialist_id = ? AND state_kind = 'persona' "
        "AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (specialist_id,),
    ).fetchone()

    now = _utc_now()
    now_iso = _iso(now)

    # 1) write the 'rollback' audit row
    rb_id = "spst_" + uuid.uuid4().hex[:12]
    rollback_state = {
        "specialist_id": specialist_id,
        "rolled_back_to_version": target_version,
        "rolled_back_from_version": current_row["persona_version"] if current_row else None,
        "rolled_back_from_state_id": current_row["id"] if current_row else None,
        "target_state_id": target_row["id"],
        "reason": reason,
    }
    conn.execute(
        "INSERT INTO specialist_states ("
        "id, specialist_id, persona_version, cycle_id, state_kind, state_json, "
        "parent_state_id, supersedes, valid_from, transaction_from"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rb_id, specialist_id, target_version,
            "rollback", "rollback", _canonical_json(rollback_state),
            current_row["id"] if current_row else None,
            current_row["id"] if current_row else None,
            now_iso, now_iso,
        ),
    )

    # 2) close the current persona row
    if current_row is not None:
        conn.execute(
            "UPDATE specialist_states SET transaction_to = ? "
            "WHERE id = ? AND transaction_to IS NULL",
            (now_iso, current_row["id"]),
        )

    # 3) re-insert the target persona as a NEW open row (append-only)
    target_state = json.loads(target_row["state_json"] or "{}")
    # sqlite3.Row supports column access but not .get(); convert to dict.
    target_dict = dict(target_row)
    new_id = "spst_" + uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO specialist_states ("
        "id, specialist_id, persona_version, cycle_id, state_kind, state_json, "
        "prompt_hash, parent_state_id, supersedes, valid_from, transaction_from"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id, specialist_id, target_version,
            f"rollback_{now.strftime('%Y%m%d')}",
            "persona", _canonical_json(target_state),
            target_dict.get("prompt_hash"),
            rb_id, rb_id,
            now_iso, now_iso,
        ),
    )

    new_row = conn.execute(
        "SELECT * FROM specialist_states WHERE id = ?", (new_id,)
    ).fetchone()
    return _row_to_specialist_state(dict(new_row))


def check_veto_window(
    candidate_id: str,
    *,
    conn: Optional[sqlite3.Connection] = None,
    as_of: Optional[datetime] = None,
) -> VetoStatus:
    """Has 24h elapsed since the candidate was proposed? Has a human
    posted an agent_messages with kind='veto' on this candidate?

    Returns enough info for the auto-promote loop to decide.
    """
    conn = _resolve_conn(conn)
    as_of = as_of or _utc_now()
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    cand_row = conn.execute(
        "SELECT id, specialist_id, transaction_from, state_kind "
        "FROM specialist_states WHERE id = ? "
        "ORDER BY transaction_from DESC LIMIT 1",
        (candidate_id,),
    ).fetchone()
    if cand_row is None:
        raise KeyError(f"check_veto_window: candidate {candidate_id!r} not found")

    proposed_at = _parse_iso(cand_row["transaction_from"]) or _utc_now()
    age = as_of - proposed_at
    age_hours = age.total_seconds() / 3600.0
    elapsed = age_hours >= VETO_WINDOW_HOURS

    # Look for any veto message tagged to this candidate. Conventions:
    # - related_artifact_id = candidate_id, OR
    # - payload contains {"candidate_id": candidate_id, "veto": true}
    veto_row = conn.execute(
        "SELECT id, payload FROM agent_messages "
        "WHERE (related_artifact_id = ? OR payload LIKE ?) "
        "AND (message_kind = 'veto' OR message_kind = 'flag') "
        "AND transaction_to IS NULL "
        "ORDER BY posted_at DESC LIMIT 1",
        (candidate_id, f'%"candidate_id":"{candidate_id}"%'),
    ).fetchone()

    veto_received = False
    veto_message_id: Optional[str] = None
    if veto_row is not None:
        try:
            pl = json.loads(veto_row["payload"]) if veto_row["payload"] else {}
            if pl.get("veto") is True or pl.get("kind") == "veto":
                veto_received = True
                veto_message_id = veto_row["id"]
        except Exception:
            # If the row exists tagged as veto, trust message_kind
            veto_received = True
            veto_message_id = veto_row["id"]

    if veto_received:
        status = "vetoed"
    elif elapsed:
        status = "ready_to_promote"
    else:
        status = "too_early"

    notes: list[str] = []
    if cand_row["state_kind"] != "mutation_candidate":
        notes.append(
            f"candidate state_kind is {cand_row['state_kind']!r} "
            f"(not 'mutation_candidate' — already promoted/rolled?)"
        )

    return VetoStatus(
        candidate_id=candidate_id,
        proposed_at=proposed_at,
        age_hours=age_hours,
        veto_received=veto_received,
        veto_message_id=veto_message_id,
        elapsed_window=elapsed,
        status=status,
        notes=notes,
    )


# ============================================================================
# Diff application
# ============================================================================

def _apply_diff(
    persona_state: dict[str, Any],
    diff: PersonaDiff,
) -> dict[str, Any]:
    """Apply a PersonaDiff to a persona state_json blob. Returns a NEW dict
    (does not mutate the input).
    """
    new_state = dict(persona_state)
    sp = new_state.get("system_prompt", "")
    tool_uris = list(new_state.get("tool_uris", []))

    if diff.kind == "prompt_bullet_added":
        bullet = (diff.prompt_bullet or "").strip()
        if bullet:
            # Append as a new bullet at the end of the prompt
            sep = "\n\n" if not sp.endswith("\n") else "\n"
            new_state["system_prompt"] = f"{sp}{sep}- {bullet}"
    elif diff.kind == "prompt_bullet_removed":
        bullet = (diff.prompt_bullet or "").strip()
        if bullet:
            # Remove first occurrence of "- {bullet}" or "  - {bullet}"
            candidates = [f"- {bullet}", f"  - {bullet}", bullet]
            for c in candidates:
                if c in sp:
                    sp = sp.replace(c, "", 1)
                    break
            new_state["system_prompt"] = sp
    elif diff.kind == "tool_uri_added":
        if diff.tool_uri and diff.tool_uri not in tool_uris:
            tool_uris.append(diff.tool_uri)
            new_state["tool_uris"] = tool_uris
    elif diff.kind == "tool_uri_removed":
        if diff.tool_uri and diff.tool_uri in tool_uris:
            tool_uris.remove(diff.tool_uri)
            new_state["tool_uris"] = tool_uris
    elif diff.kind == "threshold_changed":
        if diff.threshold_name:
            priors = dict(new_state.get("initial_priors", {}))
            priors[diff.threshold_name] = diff.threshold_new
            new_state["initial_priors"] = priors

    # Record the applied diff in the state for audit
    history = list(new_state.get("mutation_history", []))
    history.append({
        "applied_at": _iso(_utc_now()),
        "diff": diff.to_dict(),
    })
    new_state["mutation_history"] = history
    return new_state


def _bump_version(base_version: str, candidate_id: str) -> str:
    """Promotion version bumps the base by appending a short candidate tag.

    Example: 'v1.0' + candidate 'spst_abc123' -> 'v1.0+m_abc123'.
    The persona_version remains parseable (still starts with 'v') so
    SpecialistPersona validators don't reject it.
    """
    tag = candidate_id.replace("spst_", "")[:6]
    if "+" in base_version:
        # already has a tag; replace it
        head, _, _ = base_version.partition("+")
        return f"{head}+m_{tag}"
    return f"{base_version}+m_{tag}"
