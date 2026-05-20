"""Novelty scoring — Phase 6 self-improvement signal.

Per wiki/SOTA_DESK_ARCHITECTURE.md v2 §1 (lines 29-36):

    is_novel = cosine_to_nearest_in_corpus < 0.65
               AND not present_in_external_research

We score the novelty of a claim/hypothesis at `as_of` by:

  1. Embedding the claim text via talis-tic's `semantic_index.embed_text`
     (OpenAI text-embedding-3-small; falls back to deterministic
     hash-vector if no API key — flagged in NoveltyScore.quality_flags).
  2. Searching the internal corpus via talis-tic's `semantic_search`:
     cosine over claims+events+artifacts visible at `as_of`.
  3. Checking external research for matches:
       - arXiv quant-fin (talis-tic's ingester writes claims with
         source_ref starting 'arxiv:'),
       - news.py / GDELT (source_ref 'gdelt:' or 'news:'),
       - asksurf mindshare (source_ref 'asksurf:').
     If any of those have a high-cosine match (>=0.70) at as_of, the
     claim is `present_in_external_research`.

# Honest gaps
  - The external-research check uses the SAME internal `semantic_search`
    against rows tagged with those source prefixes. If those ingesters
    aren't current, novelty is overestimated (we mark `is_novel=True` for
    things that just aren't indexed yet). The NoveltyScore carries a
    `quality_flags` list so callers can downweight stale signals.
  - We use `as_of_valid` only (the as-of-knowledge axis). `as_of_transaction`
    is implicitly "now" because the semantic_index doesn't store
    transaction time per row in talis-tic's schema today.
  - The COSINE_NOVEL_THRESHOLD (0.65) and EXTERNAL_MATCH_THRESHOLD (0.70)
    are heuristics from v2 line 36 + standard semantic-search cutoffs.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Union


# ============================================================================
# Tunables
# ============================================================================

#: Per v2 §1 line 36. Internal-corpus cosine BELOW this counts as novel.
COSINE_NOVEL_THRESHOLD = 0.65

#: External-research presence cosine. ABOVE this we count an external match.
EXTERNAL_MATCH_THRESHOLD = 0.70

#: Source-ref prefixes we treat as "external research" for novelty checks.
EXTERNAL_SOURCE_PREFIXES = ("arxiv:", "gdelt:", "news:", "asksurf:",
                            "perplexity:", "tweet:", "blog:")


# ============================================================================
# Types
# ============================================================================

@dataclass
class NoveltyScore:
    """The full novelty envelope per v2 §1 line 32."""

    claim_id: str
    as_of: datetime
    cosine_to_nearest_in_corpus: float
    present_in_external_research: bool
    nearest_internal_claim_id: Optional[str] = None
    nearest_external_url: Optional[str] = None
    is_novel: bool = False
    quality_flags: list[str] = field(default_factory=list)


# A ClaimLike is anything with .text + .id; we accept dicts too.
ClaimLike = Union[dict, Any]


# ============================================================================
# Path-setup helpers
# ============================================================================

def _ensure_tic_on_path() -> None:
    """Make `tic.agent_native.semantic_index` importable.

    Same trick as eval/benchmark.py — talis-tic is a sibling checkout, not
    pip-installed, so we splice it on demand.
    """
    try:
        import tic.agent_native.semantic_index  # noqa: F401
        return
    except ImportError:
        pass
    sibling = "/Users/udaikhattar/jarvis-ios/docs/research/brief_experiments"
    if sibling not in sys.path:
        sys.path.insert(0, sibling)


def _claim_attr(claim: ClaimLike, name: str, default: Any = None) -> Any:
    if isinstance(claim, dict):
        return claim.get(name, default)
    return getattr(claim, name, default)


def _claim_text(claim: ClaimLike) -> str:
    """Best-effort: pull the textual content of a claim/hypothesis."""
    t = _claim_attr(claim, "text")
    if t:
        return str(t)
    # Hypothesis shape
    title = _claim_attr(claim, "title")
    body = _claim_attr(claim, "hypothesis_text") or _claim_attr(claim, "body")
    if title and body:
        return f"{title}\n\n{body}"
    if title:
        return str(title)
    if body:
        return str(body)
    return ""


def _claim_id(claim: ClaimLike) -> str:
    cid = _claim_attr(claim, "id")
    return str(cid) if cid is not None else ""


# ============================================================================
# Core
# ============================================================================

def score_novelty(claim: ClaimLike, as_of: datetime) -> NoveltyScore:
    """Return NoveltyScore for `claim` at `as_of`.

    Algorithm:
      1. Pull text via _claim_text(claim). Empty text => cosine=1.0,
         is_novel=False, quality_flag='empty_claim_text'.
      2. semantic_search(k=10) over kinds=['claim','event','artifact'];
         clamp to rows whose embedded_at <= as_of.
      3. Identify the nearest INTERNAL hit (not external source prefix).
      4. Identify the nearest EXTERNAL hit (source_ref starts with one of
         EXTERNAL_SOURCE_PREFIXES). If its similarity >= EXTERNAL_MATCH_THRESHOLD,
         set present_in_external_research=True.
      5. is_novel = (internal_cosine < COSINE_NOVEL_THRESHOLD)
                    AND NOT present_in_external_research

    Returns NoveltyScore with quality_flags listing any degraded paths
    (no embedding API, no semantic_search available, empty corpus, etc.).
    """
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    text = _claim_text(claim)
    cid = _claim_id(claim)
    if not text:
        return NoveltyScore(
            claim_id=cid,
            as_of=as_of,
            cosine_to_nearest_in_corpus=1.0,
            present_in_external_research=False,
            is_novel=False,
            quality_flags=["empty_claim_text"],
        )

    _ensure_tic_on_path()
    try:
        from tic.agent_native.semantic_index import semantic_search  # type: ignore
    except Exception as e:  # noqa: BLE001
        # No talis-tic semantic_index available — fall back to "naively novel"
        return NoveltyScore(
            claim_id=cid,
            as_of=as_of,
            cosine_to_nearest_in_corpus=0.0,
            present_in_external_research=False,
            is_novel=True,
            quality_flags=[f"semantic_index_unavailable:{type(e).__name__}"],
        )

    # Pull k=20 hits across all object kinds. We pass `kinds=None` so we
    # see claims+events+artifacts uniformly.
    hits: list[dict[str, Any]] = []
    try:
        hits = semantic_search(text, kinds=None, k=20)
    except Exception as e:  # noqa: BLE001
        return NoveltyScore(
            claim_id=cid,
            as_of=as_of,
            cosine_to_nearest_in_corpus=0.0,
            present_in_external_research=False,
            is_novel=True,
            quality_flags=[f"semantic_search_failed:{type(e).__name__}"],
        )

    quality_flags: list[str] = []
    if not hits:
        return NoveltyScore(
            claim_id=cid,
            as_of=as_of,
            cosine_to_nearest_in_corpus=0.0,
            present_in_external_research=False,
            is_novel=True,
            quality_flags=["empty_corpus"],
        )

    # Apply `as_of` filter: only consider hits embedded at or before as_of.
    as_of_iso = as_of.isoformat()
    filtered: list[dict[str, Any]] = []
    for h in hits:
        emb_at = h.get("embedded_at")
        if emb_at is None or str(emb_at) <= as_of_iso:
            filtered.append(h)
    if not filtered:
        # All hits were embedded AFTER as_of — corpus has no "past" entries
        # we can compare against. Treat as novel and flag.
        return NoveltyScore(
            claim_id=cid,
            as_of=as_of,
            cosine_to_nearest_in_corpus=0.0,
            present_in_external_research=False,
            is_novel=True,
            quality_flags=["no_corpus_before_as_of"],
        )

    # Skip the claim's own embedding if it landed in the corpus already.
    filtered = [h for h in filtered if h.get("object_id") != cid]
    if not filtered:
        return NoveltyScore(
            claim_id=cid,
            as_of=as_of,
            cosine_to_nearest_in_corpus=0.0,
            present_in_external_research=False,
            is_novel=True,
            quality_flags=["only_self_in_corpus"],
        )

    # Classify each hit as INTERNAL vs EXTERNAL by source_ref prefix.
    def _is_external(hit: dict[str, Any]) -> bool:
        src = (hit.get("source_ref") or "").lower()
        return any(src.startswith(p) for p in EXTERNAL_SOURCE_PREFIXES)

    # `similarity` is the dense cosine [0, 1]; default to fused_score / 1.0
    # if similarity isn't set (sparse-only fallback path).
    def _hit_sim(hit: dict[str, Any]) -> float:
        s = hit.get("similarity")
        if s is None:
            s = hit.get("fused_score")
        try:
            return float(s) if s is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    internal_hits = [h for h in filtered if not _is_external(h)]
    external_hits = [h for h in filtered if _is_external(h)]

    # Sort each list by similarity descending so [0] is the nearest.
    internal_hits.sort(key=_hit_sim, reverse=True)
    external_hits.sort(key=_hit_sim, reverse=True)

    nearest_internal = internal_hits[0] if internal_hits else None
    nearest_external = external_hits[0] if external_hits else None

    internal_cosine = _hit_sim(nearest_internal) if nearest_internal else 0.0
    external_cosine = _hit_sim(nearest_external) if nearest_external else 0.0
    nearest_internal_id = nearest_internal.get("object_id") if nearest_internal else None
    nearest_external_ref = nearest_external.get("source_ref") if nearest_external else None

    present_externally = external_cosine >= EXTERNAL_MATCH_THRESHOLD

    # If we used the naive embedding fallback the cosines aren't real;
    # flag it but keep the comparison usable as a token-overlap proxy.
    if any("naive" in str(h.get("retrieval_mode", "")).lower() for h in filtered):
        quality_flags.append("naive_embedding_fallback")

    is_novel = (internal_cosine < COSINE_NOVEL_THRESHOLD) and (not present_externally)

    return NoveltyScore(
        claim_id=cid,
        as_of=as_of,
        cosine_to_nearest_in_corpus=internal_cosine,
        present_in_external_research=present_externally,
        nearest_internal_claim_id=nearest_internal_id,
        nearest_external_url=nearest_external_ref,
        is_novel=is_novel,
        quality_flags=quality_flags,
    )
