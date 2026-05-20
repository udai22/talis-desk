"""Information topology engine — Layer 5 of the v5 architecture.

Embed every hypothesis from the current cycle, project to 2D + 3D, run
kernel density estimation, render 8 RRG-style projections, and write
the resulting density map back to `topology_density_map` so Tier 0
seed generator can route the next cycle's scouts toward sparse
(frontier) regions.

Public surface:
  - `run_topology_cycle(cycle_id)` -> TopologyResult
  - `TopologyResult.density_rows` -> the rows persisted
  - `TopologyResult.svg_path`, `TopologyResult.json_path`

NO STUBS. If embeddings cannot be computed (e.g. OpenAI key missing),
we surface `quality_flag=['topology_embeddings_unavailable']` and skip
the rest of the cycle gracefully — Tier 0 falls back to uniform
density routing.
"""
from .engine import (
    TopologyResult,
    run_topology_cycle,
)
from .embed import embed_hypotheses
from .project import project_2d, project_3d
from .density import compute_density

__all__ = [
    "TopologyResult",
    "run_topology_cycle",
    "embed_hypotheses",
    "project_2d",
    "project_3d",
    "compute_density",
]
