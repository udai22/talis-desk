"""Real LLM-driven trade-idea synthesizer.

Replaces `loop/runner.py::_build_minimal_trade_idea_draft` (which
fabricated placeholder prices 100/99/101 and frequently emitted
`direction="flat"`) with a specialist-voiced LLM call that:

  1. Reads the supported hypotheses + the actual resolved tool evidence
     (claims, price points, indicators, contradictions found in BFS).
  2. Pulls a real market snapshot via `tic.desk.tools.get_hl_l2_snapshot`
     and `get_hl_funding_history` for every instrument cited.
  3. Asks the specialist's LLM (in the persona's voice) to propose 0-3
     `TradeIdeaDraft` JSON objects with REAL entry / stop / target
     prices, risk sizing inside the validator's 10-50 bps band, and
     explicit contradicting_evidence.
  4. Validates each candidate with `trade_ideas.model.validate_trade_idea`
     and routes failures into `block_reasons` (the caller's
     watchlist/blocked-ideas agent owns that table — we just expose them).

# Design contract

  * ONE LLM call per synthesizer invocation (the model can propose 0-3
    drafts in a single JSON array).
  * Walks the full multi-provider fallback chain like judge.py /
    composer.py. NO STUBS — if every provider in the chain returns
    empty/unparseable text, raise `IdeaSynthesisUnavailableError`.
  * Market snapshot pulled via real HL info-API tools. If unavailable
    for ALL instruments, raise `IdeaSynthesisUnavailableError` (no
    fabricated prices). Per-instrument snapshot failures degrade
    gracefully — affected instruments are dropped from the proposal
    prompt with a flag, the rest proceed.
  * Validation errors become structured `block_reasons` so the
    watchlist/blocked-ideas agent can persist a row.

# Why a separate module

The loop runner is already over 1700 lines and handles HYDRATE / PLAN /
EXPLORE / SYNTHESIZE / REFLECT / DEHYDRATE. Folding heavy LLM synthesis
and HL market-data fetching into runner.py would mix layers. Keeping
the synthesizer in its own module matches the brief composer / debate
judge layout and gives the runner a single import surface.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import re
import sys
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from ..trade_ideas import (
    ContradictionItem,
    EntryPlan,
    SizingPlan,
    StopPlan,
    TIME_HORIZON_HOURS,
    TradeIdeaDraft,
    validate_trade_idea,
)
from ..trade_ideas.model import (
    MARKET_ASSUMPTION_VALUES,
    MAX_KELLY_FRACTION,
    MAX_LEVERAGE_CAP,
    MAX_RISK_PCT,
    MIN_RISK_PCT,
    TargetPlan,
)


# ============================================================================
# Ensure the sibling `tic` package is importable for chat() + HL tools —
# same pattern debate/judge.py and brief/composer.py use.
# ============================================================================

_TIC_SIBLING_PATH = "/Users/udaikhattar/jarvis-ios/docs/research/brief_experiments"


def _ensure_tic_on_path() -> None:
    if _TIC_SIBLING_PATH not in sys.path:
        sys.path.insert(0, _TIC_SIBLING_PATH)


# ============================================================================
# Result types + errors
# ============================================================================

@dataclass
class IdeaSynthesisResult:
    """Output of `synthesize_trade_ideas`."""
    drafts: list[TradeIdeaDraft]      # validated drafts ready for emit
    n_proposed: int                   # how many the LLM proposed (incl. invalid)
    n_validated: int                  # how many passed validate_trade_idea
    n_blocked: int                    # how many failed (= len(block_reasons))
    block_reasons: list[dict[str, Any]]  # [{instrument, errors, raw}]
    cost_usd: float
    model_used: str
    market_snapshot: dict[str, dict[str, Any]] = field(default_factory=dict)


class IdeaSynthesisUnavailableError(RuntimeError):
    """Raised when every provider in the synthesizer fallback chain has
    failed OR when no instrument's market snapshot could be resolved.
    NO stubs — the caller must explicitly handle the unavailable case
    (retry next cycle, skip synthesis) rather than fabricating prices."""


# ============================================================================
# Multi-provider fallback chain — heavy reasoning first, then cheaper
# ============================================================================

_SYNTH_FALLBACK_CHAIN: list[str] = [
    "anthropic:claude-opus-4-7",
    "anthropic:claude-sonnet-4-6",
    "openai:gpt-5.5",
    "xai:grok-4",
    "deepseek:v4-pro",
    "moonshot:v1-32k",
    "anthropic:claude-haiku-4-5",
    "perplexity:sonar-pro",
]

#: Cost per million-token estimate (rough). Same table as runner.py.
_COST_PER_MTOK: dict[str, float] = {
    "anthropic:claude-opus-4-7": 18.0,
    "anthropic:claude-sonnet-4-6": 6.0,
    "anthropic:claude-haiku-4-5": 1.2,
    "openai:gpt-5.5": 12.0,
    "xai:grok-4": 7.0,
    "deepseek:v4-pro": 1.2,
    "moonshot:v1-32k": 2.0,
    "perplexity:sonar-pro": 5.0,
}


def _build_provider_chain(primary: str) -> list[str]:
    seen: set[str] = {primary}
    chain: list[str] = [primary]
    for m in _SYNTH_FALLBACK_CHAIN:
        if m not in seen:
            chain.append(m)
            seen.add(m)
    return chain


def _estimate_cost_usd(model: str, system: str, user: str, completion: str) -> float:
    rate = _COST_PER_MTOK.get(model, 6.0)
    in_tokens = (len(system or "") + len(user or "")) / 4.0
    out_tokens = len(completion or "") / 4.0
    total = in_tokens + out_tokens
    if total <= 0:
        return 0.0
    return (total / 1_000_000.0) * rate


# ============================================================================
# Market snapshot helper
# ============================================================================

def _fetch_market_snapshot(instruments: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Pull a fresh mid-price + L2 imbalance + recent funding for each
    instrument. Returns `{instrument: {mid_px, spread_bps, imbalance_pct,
    funding_8h_bps, last_funding_at}}`.

    Per-instrument failures (HL timeout, bad symbol, etc.) are logged
    via warnings and the instrument is omitted from the result. The
    synthesizer caller raises `IdeaSynthesisUnavailableError` if EVERY
    instrument failed (zero usable snapshots).
    """
    _ensure_tic_on_path()
    try:
        from tic.desk.tools.hl_history_tools import (  # type: ignore
            get_hl_funding_history,
            get_hl_l2_snapshot,
        )
    except Exception as e:
        warnings.warn(
            f"synthesizer: HL history tools unavailable: {e!s}. "
            f"No market snapshots will be fetched."
        )
        return {}

    out: dict[str, dict[str, Any]] = {}
    for inst in instruments:
        if not inst:
            continue
        coin = str(inst).upper().strip()
        snap: dict[str, Any] = {"instrument": coin}
        try:
            l2 = get_hl_l2_snapshot(coin=coin, depth=10) or {}
            if isinstance(l2, dict) and not l2.get("error"):
                summary = l2.get("summary") or {}
                snap["mid_px"] = summary.get("mid_px")
                snap["spread_bps"] = summary.get("spread_bps")
                snap["imbalance_pct"] = summary.get("imbalance_pct")
                snap["bid_depth_5lvl_usd"] = summary.get("bid_depth_5lvl_usd")
                snap["ask_depth_5lvl_usd"] = summary.get("ask_depth_5lvl_usd")
                snap["as_of"] = l2.get("as_of")
        except Exception as e:  # pragma: no cover - best-effort fetch
            warnings.warn(f"synthesizer: l2 snapshot failed for {coin}: {e!s}")
        try:
            fund = get_hl_funding_history(coin=coin, lookback_hours=72) or {}
            if isinstance(fund, dict) and not fund.get("error"):
                recent = (fund.get("recent") or fund.get("history") or [])[-1:]
                if recent and isinstance(recent[0], dict):
                    snap["funding_8h_bps"] = recent[0].get("funding_bps") or recent[0].get("rate_bps")
                    snap["last_funding_at"] = recent[0].get("time") or recent[0].get("ts")
                # Carry stat summary if present.
                if "summary" in fund:
                    snap["funding_summary"] = fund["summary"]
        except Exception as e:  # pragma: no cover - best-effort fetch
            warnings.warn(f"synthesizer: funding history failed for {coin}: {e!s}")

        # Keep only snapshots that resolved at least a mid price OR a
        # recent funding number — otherwise the LLM would still have to
        # invent prices.
        if snap.get("mid_px") is not None or snap.get("funding_8h_bps") is not None:
            out[coin] = snap
        else:
            warnings.warn(
                f"synthesizer: no usable market data for {coin}; dropping "
                f"from synthesis prompt."
            )
    return out


# ============================================================================
# Prompt
# ============================================================================

_BASE_SYSTEM_INSTRUCTIONS = (
    "\n\n## Trade idea synthesis task\n"
    "You are now in the SYNTHESIZE stage of the desk's research cycle. "
    "The user prompt below contains:\n"
    "  - your specialist's supported hypotheses (posterior >= 0.7)\n"
    "  - the resolved tool evidence (claims, prices, indicators) you cited\n"
    "  - a FRESH market snapshot from Hyperliquid (mid, spread bps, L2 "
    "imbalance, 8h funding) for every cited instrument\n\n"
    "Your job is to propose 0..3 Hyperliquid trade ideas as JSON. Be "
    "concrete: use the snapshot's REAL `mid_px` for entry, compute stop "
    "and target as offsets from that mid (not invented round numbers), "
    "and pick risk sizing inside the validator band (10-50 bps).\n\n"
    "Output STRICT JSON (no prose outside the JSON). Top-level shape:\n"
    "{\n"
    '  "drafts": [\n'
    "    {\n"
    '      "instrument": "BTC",\n'
    '      "direction": "long" | "short" | "flat" | "spread",\n'
    '      "time_horizon": "intraday"|"12h"|"1d"|"3d"|"7d"|"14d"|"30d",\n'
    '      "entry": {\n'
    '        "trigger": "market" | "limit @ <px>" | "on funding flip" | ...,\n'
    '        "limit_px": <number|null>,\n'
    '        "market_assumption": "tight_ladder"|"liquid"|"thin_book"|"illiquid",\n'
    '        "invalidation": "<conditions that kill the idea pre-entry>"\n'
    "      },\n"
    '      "stop": {\n'
    '        "px": <number>,\n'
    '        "max_loss_usd": <number>0>,\n'
    '        "stop_kind": "hard" | "trailing" | "time"\n'
    "      },\n"
    '      "target": {"px": <number>, "take_profit_pct": <number>} | null,\n'
    '      "sizing": {\n'
    '        "risk_pct": 0.001..0.005,\n'
    '        "notional_cap_usd": <number|null>,\n'
    '        "kelly_fraction": 0..0.25,\n'
    '        "leverage_cap": 1..2\n'
    "      },\n"
    '      "confidence": 0..1,\n'
    '      "confluence_score": 0..1,\n'
    '      "edge_thesis": "<150-1200 chars; the WHY>",\n'
    '      "contradicting_evidence": [\n'
    "        {\n"
    '          "claim_id": "<tool_call_log_id or hypothesis_id>",\n'
    '          "reason": "<one sentence why this contradicts>",\n'
    '          "weight": 0..1\n'
    "        }\n"
    "      ]\n"
    "    }\n"
    "  ]\n"
    "}\n\n"
    "Hard rules:\n"
    "  - confidence >= 0.7 REQUIRES at least 1 contradicting_evidence entry "
    "(cite a real tool_call_log_id or hypothesis_id from the inputs).\n"
    "  - risk_pct must be inside [0.001, 0.005] (10-50 bps).\n"
    "  - kelly_fraction <= 0.25; leverage_cap <= 2.0.\n"
    "  - stop.max_loss_usd MUST be > 0 (compute it from |entry - stop| * "
    "implied units; 100-500 is reasonable for default sizing).\n"
    "  - For 'long': stop.px < entry, target.px > entry. For 'short': "
    "stop.px > entry, target.px < entry.\n"
    "  - Use the snapshot mid_px as your entry anchor. Do NOT invent prices "
    "or round numbers like 100/99/101.\n"
    "  - If you have no high-conviction setup, return drafts=[]. An empty "
    "list is a valid honest answer — DO NOT fabricate a flat/no-entry draft.\n"
)


def _format_hypothesis_block(
    hypothesis: Any,
    evidence: list[dict[str, Any]],
) -> str:
    """Render one supported hypothesis + its evidence as a prompt chunk."""
    lines: list[str] = []
    hyp_id = getattr(hypothesis, "id", None) or "?"
    title = getattr(hypothesis, "title", None) or "(untitled)"
    text = getattr(hypothesis, "hypothesis_text", None) or ""
    post = getattr(hypothesis, "posterior_prob", None)
    entities = getattr(hypothesis, "entity_ids", None) or []
    claim_ids = getattr(hypothesis, "claim_ids", None) or []
    lines.append(f"### Hypothesis {hyp_id}")
    lines.append(f"- title: {title}")
    lines.append(f"- posterior: {post}")
    lines.append(f"- entities: {list(entities)}")
    if claim_ids:
        lines.append(f"- claim_ids: {list(claim_ids)[:10]}")
    if text:
        clipped = text if len(text) <= 800 else text[:800] + "..."
        lines.append(f"- text: {clipped}")
    if evidence:
        lines.append("- evidence:")
        for ev in evidence[:8]:
            tcid = ev.get("tool_call_log_id") or "?"
            uri = ev.get("tool_uri") or "?"
            delta = ev.get("posterior_delta")
            contradicts = ev.get("contradicts")
            rationale = (ev.get("rationale") or "")[:180]
            lines.append(
                f"    * tool_call_log_id={tcid} uri={uri} "
                f"delta={delta} contradicts={contradicts} :: {rationale}"
            )
    return "\n".join(lines)


def _format_market_snapshot_block(snapshot: dict[str, dict[str, Any]]) -> str:
    if not snapshot:
        return "  (no market snapshots resolved)"
    out: list[str] = []
    for inst, snap in snapshot.items():
        out.append(f"- {inst}:")
        for k, v in snap.items():
            if k == "instrument":
                continue
            out.append(f"    {k}: {v}")
    return "\n".join(out)


def _build_user_prompt(
    specialist_id: str,
    supported_hypotheses: list[Any],
    tool_evidence: list[dict[str, Any]],
    market_snapshot: dict[str, dict[str, Any]],
) -> str:
    """Render the full user prompt: supported hypotheses (with their
    evidence) + market snapshot + instruction reminder."""
    # Group evidence by hypothesis_id when present.
    by_hyp: dict[str, list[dict[str, Any]]] = {}
    for ev in tool_evidence or []:
        hid = ev.get("hypothesis_id") or ""
        by_hyp.setdefault(hid, []).append(ev)

    lines: list[str] = []
    lines.append(f"## Specialist: {specialist_id}")
    lines.append(f"## Number of supported hypotheses: {len(supported_hypotheses)}")
    lines.append("")
    lines.append("## Supported hypotheses (posterior >= 0.7)")
    for hyp in supported_hypotheses:
        hyp_id = getattr(hyp, "id", "?")
        ev_for_hyp = by_hyp.get(hyp_id, []) + by_hyp.get("", [])
        lines.append(_format_hypothesis_block(hyp, ev_for_hyp))
        lines.append("")

    lines.append("## Live Hyperliquid market snapshot")
    lines.append(_format_market_snapshot_block(market_snapshot))
    lines.append("")
    lines.append(
        "## Instruction\n"
        "Propose 0..3 trade ideas as JSON per the schema in the system "
        "prompt. Use the snapshot mid_px for entry. Cite at least one "
        "tool_call_log_id or hypothesis_id per draft (use the ids "
        "shown above). For any draft with confidence >= 0.7, include "
        "at least one contradicting_evidence entry. Output the JSON "
        "object only."
    )
    return "\n".join(lines)


# ============================================================================
# JSON repair (same tolerant parser as judge.py / evidence_scorer.py)
# ============================================================================

def _parse_json(text: str) -> Optional[dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    fences = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    for chunk in fences:
        try:
            obj = json.loads(chunk.strip())
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None


# ============================================================================
# Chat bridge
# ============================================================================

def _run_chat_sync(chat_fn: Any, model: str, system: str, user: str) -> dict[str, Any]:
    """Call chat() with fallback=None (we walk the chain ourselves)."""
    async def _do() -> dict[str, Any]:
        return await chat_fn(model, system, user, fallback=None)

    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(asyncio.run, _do())
            return fut.result(timeout=180)
    except RuntimeError:
        return asyncio.run(_do())


# ============================================================================
# Draft coercion + validation
# ============================================================================

def _coerce_to_draft(
    raw: dict[str, Any],
    *,
    specialist_id: str,
    cycle_id: str,
    persona_version: str,
    supported_hypotheses: list[Any],
) -> tuple[Optional[TradeIdeaDraft], list[str]]:
    """Coerce one raw LLM-proposed draft dict into a TradeIdeaDraft.

    Returns (draft_or_None, coercion_errors). The draft (when returned)
    is then validated via `validate_trade_idea`.
    """
    errors: list[str] = []

    instrument = str(raw.get("instrument") or "").strip()
    if not instrument:
        errors.append("missing_instrument")
        return None, errors

    direction = str(raw.get("direction") or "").lower().strip()
    if direction not in ("long", "short", "flat", "spread"):
        errors.append(f"bad_direction:{direction!r}")
        return None, errors

    horizon = str(raw.get("time_horizon") or "").strip()
    if horizon not in TIME_HORIZON_HOURS:
        errors.append(f"bad_time_horizon:{horizon!r}")
        return None, errors

    # ---- Sizing -----------------------------------------------------------
    sizing_raw = raw.get("sizing") or {}
    try:
        sizing = SizingPlan(
            risk_pct=float(sizing_raw.get("risk_pct", MIN_RISK_PCT)),
            notional_cap_usd=(
                float(sizing_raw["notional_cap_usd"])
                if sizing_raw.get("notional_cap_usd") is not None else None
            ),
            kelly_fraction=min(
                MAX_KELLY_FRACTION,
                float(sizing_raw.get("kelly_fraction", 0.10)),
            ),
            leverage_cap=min(
                MAX_LEVERAGE_CAP,
                float(sizing_raw.get("leverage_cap", 1.0)),
            ),
        )
    except Exception as e:
        errors.append(f"sizing_parse_error:{e!s}")
        return None, errors

    # ---- Entry ------------------------------------------------------------
    entry_raw = raw.get("entry") or {}
    market_assumption = str(entry_raw.get("market_assumption") or "liquid").strip()
    if market_assumption not in MARKET_ASSUMPTION_VALUES:
        market_assumption = "liquid"
    try:
        entry = EntryPlan(
            trigger=str(entry_raw.get("trigger") or "market"),
            limit_px=(
                float(entry_raw["limit_px"])
                if entry_raw.get("limit_px") is not None else None
            ),
            market_assumption=market_assumption,  # type: ignore[arg-type]
            invalidation=str(entry_raw.get("invalidation") or "no invalidation specified"),
        )
    except Exception as e:
        errors.append(f"entry_parse_error:{e!s}")
        return None, errors

    # ---- Stop -------------------------------------------------------------
    stop_raw = raw.get("stop") or {}
    try:
        stop = StopPlan(
            px=float(stop_raw["px"]),
            max_loss_usd=float(stop_raw.get("max_loss_usd", 100.0)),
            stop_kind=str(stop_raw.get("stop_kind") or "hard"),  # type: ignore[arg-type]
        )
    except Exception as e:
        errors.append(f"stop_parse_error:{e!s}")
        return None, errors

    # ---- Target (optional) ------------------------------------------------
    target: Optional[TargetPlan] = None
    target_raw = raw.get("target")
    if isinstance(target_raw, dict) and target_raw.get("px") is not None:
        try:
            tpct_raw = target_raw.get("take_profit_pct")
            if tpct_raw is None and entry.limit_px is not None:
                # Derive from entry if model omits it.
                tpct = abs(float(target_raw["px"]) - entry.limit_px) / entry.limit_px * 100.0
            else:
                tpct = float(tpct_raw or 0.0)
            target = TargetPlan(px=float(target_raw["px"]), take_profit_pct=tpct)
        except Exception as e:
            errors.append(f"target_parse_error:{e!s}")
            target = None

    # ---- Contradictions ---------------------------------------------------
    contradictions: list[ContradictionItem] = []
    for c in (raw.get("contradicting_evidence") or [])[:6]:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("claim_id") or "").strip()
        reason = str(c.get("reason") or "").strip()
        if not cid or not reason:
            continue
        try:
            weight = float(c.get("weight", 0.5))
        except (TypeError, ValueError):
            weight = 0.5
        contradictions.append(ContradictionItem(
            claim_id=cid,
            reason=reason,
            weight=max(0.0, min(1.0, weight)),
        ))

    # ---- Citation ids -----------------------------------------------------
    hypothesis_ids = [getattr(h, "id", None) for h in supported_hypotheses]
    hypothesis_ids = [h for h in hypothesis_ids if h]
    # Pull tool_call_log_ids from contradictions as a hint.
    tool_call_ids: list[str] = []
    for c in contradictions:
        if c.claim_id.startswith("tc_"):
            tool_call_ids.append(c.claim_id)

    # ---- Confidence / horizon timing -------------------------------------
    try:
        confidence = float(raw.get("confidence", 0.65))
    except (TypeError, ValueError):
        confidence = 0.65
    confidence = max(0.0, min(1.0, confidence))
    try:
        confluence = float(raw.get("confluence_score", 0.0))
    except (TypeError, ValueError):
        confluence = 0.0
    confluence = max(0.0, confluence)

    edge_thesis = str(raw.get("edge_thesis") or "").strip()
    if not edge_thesis:
        # Fall back to the first hypothesis text if the model omitted it.
        edge_thesis = (
            getattr(supported_hypotheses[0], "hypothesis_text", "")
            if supported_hypotheses else ""
        )
    edge_thesis = edge_thesis[:1200]

    now_utc = datetime.now(timezone.utc)
    horizon_hours = TIME_HORIZON_HOURS[horizon]
    expires_at = now_utc + timedelta(hours=horizon_hours)

    try:
        draft = TradeIdeaDraft(
            cycle_id=cycle_id,
            specialist_id=specialist_id,
            persona_version=persona_version,
            instrument=instrument,
            venue="hyperliquid",
            direction=direction,  # type: ignore[arg-type]
            sizing=sizing,
            entry=entry,
            stop=stop,
            target=target,
            time_horizon=horizon,
            edge_thesis=edge_thesis,
            claim_ids=[],
            hypothesis_ids=hypothesis_ids,
            forecast_ids=[],
            debate_ids=[],
            tool_call_ids=tool_call_ids[:20],
            contradicting_evidence=contradictions,
            confluence_score=confluence,
            confidence=confidence,
            expires_at=expires_at,
            valid_from=now_utc,
            payload={
                "synthesizer_note": "auto_emitted_from_llm_synthesis",
                "synthesizer_version": "idea_synthesizer_v1",
            },
        )
    except Exception as e:
        errors.append(f"draft_construction_error:{e!s}")
        return None, errors

    return draft, errors


# ============================================================================
# Public API
# ============================================================================

def synthesize_trade_ideas(
    specialist_id: str,
    persona_prompt: str,
    supported_hypotheses: list[Any],
    tool_evidence: list[dict[str, Any]],
    market_snapshot: Optional[dict[str, dict[str, Any]]] = None,
    *,
    cycle_id: str,
    persona_version: str,
    model: str = "anthropic:claude-opus-4-7",
) -> IdeaSynthesisResult:
    """LLM-driven trade-idea synthesis for one specialist.

    Args:
      specialist_id: e.g. "microstructure". Used to set the trade-idea
        attribution + show up in the prompt header.
      persona_prompt: the specialist's full system prompt (loaded by the
        loop runner from the persona prompt.md). The synthesizer appends
        a SYNTHESIZE-stage instruction block to it.
      supported_hypotheses: list of `Hypothesis` objects with
        `posterior_prob >= 0.7` that the specialist resolved this cycle.
      tool_evidence: list of evidence dicts (one per scored BFS step
        that contributed to a supported hypothesis). Each entry should
        include {tool_call_log_id, tool_uri, posterior_delta,
        contradicts, rationale, hypothesis_id?}.
      market_snapshot: optional pre-fetched snapshot (saves an HL call if
        the caller already pulled it). When None, the function fetches
        live via `_fetch_market_snapshot` for every cited instrument.
      cycle_id: passed onto the draft + audit.
      persona_version: same.
      model: starting model. Function walks the fallback chain on any
        provider failure.

    Returns:
      IdeaSynthesisResult with `drafts` (validated, ready for emit) and
      `block_reasons` (failed validations the caller can route to the
      blocked-ideas table).

    Raises:
      IdeaSynthesisUnavailableError if every provider failed OR if no
      instrument's market snapshot could be resolved (we never fabricate
      prices to keep the call alive).
    """
    if not supported_hypotheses:
        return IdeaSynthesisResult(
            drafts=[], n_proposed=0, n_validated=0, n_blocked=0,
            block_reasons=[], cost_usd=0.0, model_used="",
            market_snapshot={},
        )

    # 1. Gather instruments from the hypotheses.
    instruments: list[str] = []
    seen_inst: set[str] = set()
    for hyp in supported_hypotheses:
        for ent in (getattr(hyp, "entity_ids", None) or []):
            if ent and ent not in seen_inst:
                instruments.append(str(ent))
                seen_inst.add(str(ent))

    # 2. Market snapshot — caller-supplied or freshly fetched.
    if market_snapshot is None:
        snapshot = _fetch_market_snapshot(instruments)
    else:
        snapshot = dict(market_snapshot)

    if instruments and not snapshot:
        # The codex review explicitly bans fabricated prices. If we
        # have hypotheses citing instruments but NO instrument resolved
        # a market snapshot, refuse to synthesize.
        raise IdeaSynthesisUnavailableError(
            f"No usable market snapshot for any of {instruments[:5]}. "
            f"Refusing to fabricate prices — caller should retry next cycle."
        )

    # 3. Build prompts.
    system = (persona_prompt or "").rstrip() + _BASE_SYSTEM_INSTRUCTIONS
    user = _build_user_prompt(
        specialist_id=specialist_id,
        supported_hypotheses=supported_hypotheses,
        tool_evidence=tool_evidence or [],
        market_snapshot=snapshot,
    )

    # 4. Walk the multi-provider fallback chain.
    _ensure_tic_on_path()
    try:
        from tic.desk.models import chat as _chat  # type: ignore
    except Exception as e:
        raise IdeaSynthesisUnavailableError(
            f"tic.desk.models.chat unavailable: {e!s}"
        ) from e

    chain = _build_provider_chain(model)
    last_error: Optional[str] = None
    parsed: Optional[dict[str, Any]] = None
    text_out: str = ""
    model_used: str = ""
    cost_total: float = 0.0
    for m in chain:
        try:
            res = _run_chat_sync(_chat, m, system, user)
        except Exception as e:  # noqa: BLE001
            last_error = f"{m}: chat_call_failed: {e!s}"
            continue
        text = (res.get("text") or "").strip()
        err = res.get("error")
        if err:
            last_error = f"{m}: {err}"
            continue
        if not text:
            last_error = f"{m}: empty_completion"
            continue
        p = _parse_json(text)
        if not p:
            last_error = f"{m}: unparseable_json"
            continue
        parsed = p
        text_out = text
        model_used = res.get("model_used") or m
        cost_total = _estimate_cost_usd(model_used, system, user, text)
        break

    if parsed is None:
        raise IdeaSynthesisUnavailableError(
            f"All {len(chain)} providers in the idea-synthesizer fallback "
            f"chain failed. Last error: {last_error}. Chain: {chain}"
        )

    # 5. Coerce + validate each proposed draft.
    raw_drafts = parsed.get("drafts") or []
    if not isinstance(raw_drafts, list):
        raw_drafts = []
    n_proposed = len(raw_drafts)
    drafts: list[TradeIdeaDraft] = []
    block_reasons: list[dict[str, Any]] = []

    for raw in raw_drafts[:6]:  # hard cap at 6 even if the model proposes more
        if not isinstance(raw, dict):
            block_reasons.append({
                "instrument": None,
                "errors": ["non_dict_draft"],
                "raw": str(raw)[:300],
            })
            continue
        draft, coercion_errors = _coerce_to_draft(
            raw,
            specialist_id=specialist_id,
            cycle_id=cycle_id,
            persona_version=persona_version,
            supported_hypotheses=supported_hypotheses,
        )
        if draft is None:
            block_reasons.append({
                "instrument": raw.get("instrument"),
                "errors": coercion_errors,
                "raw": json.dumps(raw, default=str)[:500],
            })
            continue
        # Run the production validator.
        try:
            report = validate_trade_idea(draft.to_trade_idea())
        except Exception as e:
            block_reasons.append({
                "instrument": draft.instrument,
                "errors": [f"validator_crashed:{e!s}"],
                "raw": json.dumps(raw, default=str)[:500],
            })
            continue
        if report.ok:
            drafts.append(draft)
        else:
            block_reasons.append({
                "instrument": draft.instrument,
                "errors": list(report.errors),
                "raw": json.dumps(raw, default=str)[:500],
            })

    return IdeaSynthesisResult(
        drafts=drafts,
        n_proposed=n_proposed,
        n_validated=len(drafts),
        n_blocked=len(block_reasons),
        block_reasons=block_reasons,
        cost_usd=cost_total,
        model_used=model_used,
        market_snapshot=snapshot,
    )
