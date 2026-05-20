"""2D + 3D projections of hypothesis embeddings.

We use UMAP when available (best quality, can take a few seconds for
1000 points), otherwise fall back to scikit-learn's TSNE, otherwise to
a pure-NumPy SVD/PCA. The choice never affects correctness — the
density map is still meaningful — only the shape of the projection.

Output: a list[dict] with keys hypothesis_id, x, y (and z for 3D).
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from .embed import Embedded


logger = logging.getLogger(__name__)


def project_2d(embeddings: list[Embedded]) -> list[dict]:
    """Project embeddings to 2D. Returns list of dicts."""
    if not embeddings:
        return []
    vecs = [e.vector for e in embeddings]
    try:
        coords = _project_umap(vecs, n_components=2)
        method = "umap"
    except Exception:
        try:
            coords = _project_tsne(vecs, n_components=2)
            method = "tsne"
        except Exception:
            coords = _project_pca(vecs, n_components=2)
            method = "pca"
    out: list[dict] = []
    for e, (x, y) in zip(embeddings, coords):
        out.append({
            "hypothesis_id": e.hypothesis_id,
            "text": e.text,
            "entity": e.entity,
            "lens": e.lens,
            "x": float(x),
            "y": float(y),
            "method": method,
        })
    return out


def project_3d(embeddings: list[Embedded]) -> list[dict]:
    if not embeddings:
        return []
    vecs = [e.vector for e in embeddings]
    try:
        coords = _project_umap(vecs, n_components=3)
        method = "umap"
    except Exception:
        try:
            coords = _project_tsne(vecs, n_components=3)
            method = "tsne"
        except Exception:
            coords = _project_pca(vecs, n_components=3)
            method = "pca"
    out: list[dict] = []
    for e, (x, y, z) in zip(embeddings, coords):
        out.append({
            "hypothesis_id": e.hypothesis_id,
            "text": e.text,
            "entity": e.entity,
            "lens": e.lens,
            "x": float(x), "y": float(y), "z": float(z),
            "method": method,
        })
    return out


def _project_umap(vecs: list[list[float]], n_components: int):
    try:
        import numpy as np  # type: ignore
        import umap  # type: ignore
    except ImportError as e:
        raise RuntimeError(f"umap unavailable: {e}")
    reducer = umap.UMAP(n_components=n_components, n_neighbors=min(15, len(vecs) - 1),
                        min_dist=0.1, random_state=42)
    arr = np.asarray(vecs, dtype=float)
    return reducer.fit_transform(arr).tolist()


def _project_tsne(vecs: list[list[float]], n_components: int):
    try:
        import numpy as np  # type: ignore
        from sklearn.manifold import TSNE  # type: ignore
    except ImportError as e:
        raise RuntimeError(f"sklearn unavailable: {e}")
    arr = np.asarray(vecs, dtype=float)
    perplexity = min(30, max(5, len(vecs) // 4))
    return TSNE(
        n_components=n_components, perplexity=perplexity, random_state=42,
        init="pca", learning_rate="auto",
    ).fit_transform(arr).tolist()


def _project_pca(vecs: list[list[float]], n_components: int):
    """Pure-NumPy SVD fallback so we always have *some* projection."""
    try:
        import numpy as np  # type: ignore
    except ImportError as e:
        # Last resort — deterministic hash projection (correctness only,
        # no quality claim). Pure-Python fallback.
        return _hash_projection(vecs, n_components)
    arr = np.asarray(vecs, dtype=float)
    arr -= arr.mean(axis=0, keepdims=True)
    # SVD on centered data == PCA
    u, s, vt = np.linalg.svd(arr, full_matrices=False)
    coords = u[:, :n_components] * s[:n_components]
    return coords.tolist()


def _hash_projection(vecs: list[list[float]], n_components: int):
    """Hash-based deterministic projection — strictly a placeholder when
    NumPy isn't installed. Density estimation still works on the result.
    """
    out = []
    for v in vecs:
        # Sum even/odd indices into two scalars; for 3D also bucket-sum.
        x = sum(v[::2])
        y = sum(v[1::2])
        if n_components == 2:
            out.append((x, y))
        else:
            z = sum(v[::3])
            out.append((x, y, z))
    return out
