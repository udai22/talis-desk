"""Embed hypotheses via OpenAI text-embedding-3-small.

~$0.001 per 1000 hypotheses for the small model. We batch into one call
per 100 hypotheses to amortize overhead.

NO STUBS. If the OpenAI key isn't set, we raise
`EmbeddingsUnavailableError`. Callers handle gracefully (the topology
cycle is a non-fatal pass — desk still runs without it).
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

from .._tic_config import ensure_tic_on_path as _ensure_tic_on_path


logger = logging.getLogger(__name__)


class EmbeddingsUnavailableError(RuntimeError):
    pass


@dataclass
class Embedded:
    hypothesis_id: str
    text: str
    vector: list[float]
    entity: Optional[str] = None
    lens: Optional[str] = None


async def _embed_batch(
    client, model: str, texts: list[str],
) -> list[list[float]]:
    resp = await client.embeddings.create(model=model, input=texts)
    return [list(d.embedding) for d in resp.data]


async def _embed_async(
    hypotheses: list[dict],
    model: str = "text-embedding-3-small",
    batch_size: int = 96,
) -> list[Embedded]:
    _ensure_tic_on_path()
    try:
        from openai import AsyncOpenAI  # type: ignore
    except Exception as e:
        raise EmbeddingsUnavailableError(f"openai sdk not available: {e}")
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise EmbeddingsUnavailableError("OPENAI_API_KEY not set")
    client = AsyncOpenAI(api_key=key)
    out: list[Embedded] = []
    for i in range(0, len(hypotheses), batch_size):
        batch = hypotheses[i:i + batch_size]
        texts = [h["text"] for h in batch]
        try:
            vecs = await _embed_batch(client, model, texts)
        except Exception as e:
            raise EmbeddingsUnavailableError(
                f"embeddings batch {i}-{i + len(batch)} failed: {e}"
            )
        for h, v in zip(batch, vecs):
            out.append(Embedded(
                hypothesis_id=h["id"],
                text=h["text"],
                vector=v,
                entity=h.get("entity"),
                lens=h.get("lens"),
            ))
    return out


def embed_hypotheses(
    hypotheses: list[dict],
    model: str = "text-embedding-3-small",
) -> list[Embedded]:
    """Embed a list of hypotheses. Each dict must have keys
    `id` (str), `text` (str); optional: `entity`, `lens`.
    """
    if not hypotheses:
        return []
    try:
        asyncio.get_running_loop()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(
                asyncio.run, _embed_async(hypotheses, model=model),
            ).result()
    except RuntimeError:
        return asyncio.run(_embed_async(hypotheses, model=model))
