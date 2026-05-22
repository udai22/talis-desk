"""Embedding-based hypothesis dedup (Gap 3).

After Tier 1 scouts run, 1000+ hypotheses may contain many near-duplicates
(same entity/lens/bias often produces similar text). Cell-key collision
catches exact stratum overlaps; this module catches *semantic* duplicates.

Approach:
  1. Embed each hypothesis_text via OpenAI text-embedding-3-small
     (~$0.001 / 1k embeddings — already used by topology engine).
  2. Compute cosine similarity, cluster by single-linkage at threshold
     (default 0.92).
  3. From each cluster keep the highest-confidence scout output; mark
     the rest with `quality_flag='deduped'` so they're excluded from
     the verifier council pass.

NO STUBS. If embeddings are unavailable (no OpenAI key, transient
failure) we return the original list unchanged with a quality flag on
each scout indicating dedup was skipped — the cycle continues without
this optimization rather than silently dropping work.

Cost target: <$0.005 per 1000-hypothesis cycle.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional, Sequence

from ..topology.embed import embed_hypotheses, EmbeddingsUnavailableError
from .scout_runner import ScoutOutput


logger = logging.getLogger(__name__)


# Cosine similarity above this collapses two hypotheses into the same
# cluster. 0.92 is conservative — keeps thematically distinct angles
# even when they share an entity.
DEFAULT_SIM_THRESHOLD = 0.92


@dataclass
class DedupResult:
    kept: list[ScoutOutput]
    dropped: list[ScoutOutput]
    n_clusters: int
    threshold: float
    quality_flags: list[str]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _cluster_by_threshold(
    vectors: list[list[float]],
    threshold: float,
) -> list[int]:
    """Union-find single-linkage clustering. Returns a cluster_id per
    vector. O(N^2) — fine for N<=2000.
    """
    n = len(vectors)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if _cosine(vectors[i], vectors[j]) >= threshold:
                union(i, j)
    return [find(i) for i in range(n)]


def dedup_scouts(
    scouts: list[ScoutOutput],
    *,
    threshold: float = DEFAULT_SIM_THRESHOLD,
) -> DedupResult:
    """Embedding-based dedup of scout outputs.

    Only scouts with non-empty hypothesis_text and no error participate.
    Within each entity × lens partition we cluster and keep the highest-
    confidence scout per cluster.

    Returns DedupResult — `kept` becomes the verifier-council input.
    """
    eligible = [s for s in scouts if s.hypothesis_text and not s.error]
    ineligible = [s for s in scouts if not (s.hypothesis_text and not s.error)]

    if len(eligible) < 2:
        return DedupResult(
            kept=eligible + ineligible,
            dropped=[],
            n_clusters=len(eligible),
            threshold=threshold,
            quality_flags=[],
        )

    # Partition by (entity, lens) — cross-partition dedup adds little
    # value and inflates cost.
    partitions: dict[tuple[str, str], list[ScoutOutput]] = {}
    for s in eligible:
        key = (s.entity, s.lens)
        partitions.setdefault(key, []).append(s)

    # Build embedding input — one batch per partition keeps the
    # OpenAI batch endpoint warm.
    hypothesis_payload = [
        {
            "id": s.scout_id,
            "text": s.hypothesis_text[:1200],
            "entity": s.entity,
            "lens": s.lens,
        }
        for s in eligible
    ]
    try:
        embeddings = embed_hypotheses(hypothesis_payload)
    except EmbeddingsUnavailableError as e:
        logger.warning("dedup skipped: %s", e)
        flag = f"dedup_skipped:{e}"
        for s in eligible:
            if flag not in s.quality_flags:
                s.quality_flags.append(flag)
        return DedupResult(
            kept=eligible + ineligible,
            dropped=[],
            n_clusters=len(eligible),
            threshold=threshold,
            quality_flags=[flag],
        )
    except Exception as e:
        logger.warning("dedup skipped (unexpected): %s", e)
        flag = f"dedup_skipped:{type(e).__name__}"
        for s in eligible:
            if flag not in s.quality_flags:
                s.quality_flags.append(flag)
        return DedupResult(
            kept=eligible + ineligible,
            dropped=[],
            n_clusters=len(eligible),
            threshold=threshold,
            quality_flags=[flag],
        )

    by_id: dict[str, list[float]] = {e.hypothesis_id: e.vector for e in embeddings}

    kept: list[ScoutOutput] = []
    dropped: list[ScoutOutput] = []
    n_clusters = 0

    for (entity, lens), group in partitions.items():
        if len(group) == 1:
            kept.extend(group)
            n_clusters += 1
            continue
        vecs = [by_id.get(s.scout_id, []) for s in group]
        # Drop any rows that didn't get embedded (defensive).
        valid_idx = [i for i, v in enumerate(vecs) if v]
        if len(valid_idx) < 2:
            kept.extend(group)
            n_clusters += len(group)
            continue
        cluster_ids = _cluster_by_threshold(
            [vecs[i] for i in valid_idx], threshold,
        )
        # Map back: cluster_id -> list of (orig_index_in_group)
        clusters: dict[int, list[int]] = {}
        for vi, cid in zip(valid_idx, cluster_ids):
            clusters.setdefault(cid, []).append(vi)
        for members in clusters.values():
            cluster_scouts = [group[i] for i in members]
            best = max(cluster_scouts, key=lambda s: s.confidence)
            kept.append(best)
            n_clusters += 1
            for s in cluster_scouts:
                if s is best:
                    continue
                if "deduped" not in s.quality_flags:
                    s.quality_flags.append("deduped")
                dropped.append(s)

    return DedupResult(
        kept=kept + ineligible,
        dropped=dropped,
        n_clusters=n_clusters,
        threshold=threshold,
        quality_flags=[],
    )


__all__ = ["DedupResult", "dedup_scouts"]
