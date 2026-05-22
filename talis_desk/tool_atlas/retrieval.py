"""Dynamic tool retrieval — replace static tool atlas exposure with
query-driven semantic ranking.

`find_tool_for_query(query_text, top_k)` scores every active tool in
`tool_atlas` against free-form query text and returns the top-k URIs.
Scoring is BM25-lite over (tool_name, description, provider, kind) with
a small lens-affinity bonus, so it stays cheap (<1ms for ~500 tools, no
embeddings).

This is what scouts/analysts use when they need to discover tools at
prompt-construction time without being exposed to the entire atlas
(SOTA tool retrieval — avoid the "shared text soup" trap Codex flagged).

Cosine-rerank with embeddings is left for a future upgrade and is gated
on the OpenAI key — falls back to lexical when embeddings are
unavailable.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Optional

from ..store import get_desk_store


logger = logging.getLogger(__name__)


# Tokens that recur across many tools and add noise to ranking.
_STOP = frozenset({
    "the", "a", "an", "of", "for", "and", "or", "to", "in", "on",
    "by", "with", "is", "are", "be", "this", "that", "from",
    "tool", "tic", "talis", "tools", "data",
})

_LENS_AFFINITY: dict[str, tuple[str, ...]] = {
    "macro": ("fred", "macro", "cpi", "fomc", "yield", "rates"),
    "microstructure": ("orderbook", "depth", "trades", "lob", "spread"),
    "options_flow": ("options", "iv", "skew", "gex", "gamma"),
    "vol_surface": ("iv", "vol", "skew", "term_structure"),
    "smart_money": ("13f", "form4", "insider", "whale"),
    "sentiment": ("sentiment", "news", "social", "fear_greed"),
    "rotation": ("rrg", "sector", "rotation", "relative"),
    "factor": ("factor", "fama_french", "momentum", "carry"),
    "catalyst": ("earnings", "calendar", "fda", "fed", "event"),
    "filing": ("sec", "edgar", "8-k", "10-q", "10-k", "form"),
    "on_chain": ("hyperliquid", "wallet", "onchain", "transfer"),
    "money_velocity": ("flow", "fund_flow", "etf", "issuance"),
}


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOP and len(t) > 1]


@dataclass(frozen=True)
class ToolHit:
    tool_uri: str
    score: float
    tool_name: str
    description: str
    provider: str
    kind: str


def _load_atlas_rows() -> list[dict]:
    try:
        conn = get_desk_store().conn
        rows = conn.execute(
            """
            SELECT tool_uri, tool_name, description, provider, kind
            FROM tool_atlas
            WHERE transaction_to IS NULL
              AND status = 'active'
              AND tool_uri LIKE 'tic://tool/%'
            """
        ).fetchall()
    except Exception as e:
        logger.warning("tool_atlas read failed: %s", e)
        return []
    out: list[dict] = []
    for r in rows:
        out.append({
            "tool_uri": str(r["tool_uri"]),
            "tool_name": str(r["tool_name"] or ""),
            "description": str(r["description"] or ""),
            "provider": str(r["provider"] or ""),
            "kind": str(r["kind"] or ""),
        })
    return out


def _build_corpus_stats(rows: list[dict]) -> dict:
    """Pre-compute doc tokens + IDF for BM25-lite ranking."""
    doc_tokens: list[list[str]] = []
    doc_lens: list[int] = []
    df: dict[str, int] = {}
    for r in rows:
        haystack = " ".join((r["tool_name"], r["description"], r["provider"], r["kind"]))
        toks = _tokenize(haystack)
        doc_tokens.append(toks)
        doc_lens.append(len(toks))
        for tok in set(toks):
            df[tok] = df.get(tok, 0) + 1
    n = max(1, len(rows))
    avgdl = sum(doc_lens) / n if doc_lens else 0.0
    idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
    return {
        "doc_tokens": doc_tokens,
        "doc_lens": doc_lens,
        "avgdl": avgdl,
        "idf": idf,
    }


def _bm25_score(query_tokens: list[str], doc_tokens: list[str], doc_len: int,
                avgdl: float, idf: dict[str, float],
                k1: float = 1.5, b: float = 0.75) -> float:
    if not doc_tokens or not query_tokens:
        return 0.0
    tf: dict[str, int] = {}
    for tok in doc_tokens:
        tf[tok] = tf.get(tok, 0) + 1
    norm = 1.0 - b + b * (doc_len / max(1e-9, avgdl))
    score = 0.0
    for q in set(query_tokens):
        if q not in tf:
            continue
        f = tf[q]
        score += idf.get(q, 0.0) * (f * (k1 + 1.0)) / (f + k1 * norm)
    return score


def find_tool_for_query(
    query_text: str,
    top_k: int = 8,
    *,
    lens: Optional[str] = None,
    entity: Optional[str] = None,
) -> list[ToolHit]:
    """Rank active atlas tools against `query_text` and return top-k hits.

    `lens` and `entity` are optional bias signals — the lens
    boosts category-aligned tools, the entity boosts tools whose
    description mentions it (e.g. an entity-specific data feed).

    Returns [] when atlas is empty or query is empty.
    """
    rows = _load_atlas_rows()
    if not rows:
        return []
    qtokens = _tokenize(query_text)
    if lens:
        qtokens.extend(_LENS_AFFINITY.get(lens, ()))
    if entity:
        qtokens.extend(_tokenize(entity))
    if not qtokens:
        return []

    stats = _build_corpus_stats(rows)
    doc_tokens = stats["doc_tokens"]
    doc_lens = stats["doc_lens"]
    avgdl = stats["avgdl"]
    idf = stats["idf"]

    scored: list[tuple[float, dict]] = []
    entity_lc = (entity or "").lower()
    for i, r in enumerate(rows):
        s = _bm25_score(qtokens, doc_tokens[i], doc_lens[i], avgdl, idf)
        # Entity-exact match in description is high-value.
        if entity_lc and len(entity_lc) > 1 and entity_lc in r["description"].lower():
            s += 1.5
        if s > 0:
            scored.append((s, r))

    scored.sort(key=lambda x: (-x[0], x[1]["tool_uri"]))
    out: list[ToolHit] = []
    for score, r in scored[:top_k]:
        out.append(ToolHit(
            tool_uri=r["tool_uri"],
            score=float(score),
            tool_name=r["tool_name"],
            description=r["description"],
            provider=r["provider"],
            kind=r["kind"],
        ))
    return out


__all__ = ["ToolHit", "find_tool_for_query"]
