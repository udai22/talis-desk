"""Kernel density estimation over the projected hypothesis cloud.

For each projection, we bin the 2D space into a coarse grid (default
20x20) and count + normalize hypothesis density per cell. Cells in the
bottom quartile of density are flagged as `is_frontier=1` so Tier 0
seed gen knows to over-sample them next cycle.

The output is a list of region records ready to be inserted into
`topology_density_map`.
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from typing import Any


logger = logging.getLogger(__name__)


def compute_density(
    projected: list[dict],
    n_bins: int = 20,
    projection_view: str = "umap_2d",
) -> list[dict]:
    """Bin projected hypotheses + return density rows.

    Each output row contains:
      - region_id (e.g. "r_3_7")
      - projection_view
      - density (0..1 normalized)
      - centroid_x, centroid_y
      - radius (bin half-width)
      - n_members
      - member_hypothesis_ids
      - label (most common entity:lens in bin)
      - is_frontier (1 if density < q1)
    """
    if not projected:
        return []
    xs = [p["x"] for p in projected]
    ys = [p["y"] for p in projected]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    if xmin == xmax:
        xmax = xmin + 1.0
    if ymin == ymax:
        ymax = ymin + 1.0
    bin_w = (xmax - xmin) / n_bins
    bin_h = (ymax - ymin) / n_bins
    bins: dict[tuple[int, int], list[dict]] = {}
    for p in projected:
        i = min(n_bins - 1, max(0, int((p["x"] - xmin) / bin_w)))
        j = min(n_bins - 1, max(0, int((p["y"] - ymin) / bin_h)))
        bins.setdefault((i, j), []).append(p)

    raw_densities = [len(v) for v in bins.values()]
    if not raw_densities:
        return []
    max_d = max(raw_densities)
    # Quartile threshold for frontier.
    sorted_dens = sorted(raw_densities)
    q1_idx = max(0, len(sorted_dens) // 4 - 1)
    q1 = sorted_dens[q1_idx]

    rows: list[dict] = []
    for (i, j), members in bins.items():
        n = len(members)
        density = n / float(max_d) if max_d else 0.0
        cx = xmin + (i + 0.5) * bin_w
        cy = ymin + (j + 0.5) * bin_h
        radius = math.hypot(bin_w, bin_h) / 2.0
        label_counter = Counter()
        for m in members:
            ent = m.get("entity") or ""
            lns = m.get("lens") or ""
            label_counter[f"{ent}:{lns}"] += 1
        label, _ = label_counter.most_common(1)[0] if label_counter else ("", 0)
        is_frontier = 1 if n <= q1 else 0
        rows.append({
            "region_id": f"r_{i}_{j}",
            "projection_view": projection_view,
            "density": density,
            "centroid_x": cx,
            "centroid_y": cy,
            "radius": radius,
            "n_members": n,
            "member_hypothesis_ids": [m["hypothesis_id"] for m in members],
            "label": label,
            "is_frontier": is_frontier,
        })
    return rows
