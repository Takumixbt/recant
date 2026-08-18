"""
Embedding providers.

Bedrock Titan is the intended provider. This account's Bedrock quota has not
opened, so a real local model stands in -- and "real" is the operative word.

An earlier version used a hashed bag-of-words as the stand-in. It was fast,
deterministic, and completely useless: it scored a poisoned belief about waiving
refund holds as LESS similar to "should I approve a refund" than unrelated
chatter about email preferences. Retrieval only appeared to work because every
subject held exactly k beliefs, so the top-k returned everything regardless of
relevance. Any demo built on it would have been theatre.

The stand-in is now BAAI/bge-small-en-v1.5 via fastembed (ONNX, no PyTorch),
which produces genuine semantic distances.

All providers emit 1024-dimension unit vectors so the VECTOR(1024) column and
the cosine index are provider-agnostic. bge-small is natively 384-dimensional
and is zero-padded to 1024. That is not a fudge: appending zeros changes neither
the dot product between two vectors nor either vector's norm, so every cosine
distance is bit-for-bit what it would be at 384. It buys schema stability across
a provider swap for free.

Select with EMBED_PROVIDER=bedrock|local (default local).
"""

from __future__ import annotations

import json
import math
import os
from typing import Iterable, Protocol

DIMS = 1024
LOCAL_MODEL = os.environ.get("LOCAL_EMBED_MODEL", "BAAI/bge-small-en-v1.5")


class Embedder(Protocol):
    name: str

    def embed(self, text: str) -> list[float]: ...

    def embed_many(self, texts: list[str]) -> list[list[float]]: ...


def _pad(v: Iterable[float]) -> list[float]:
    out = list(v)
    if len(out) > DIMS:
        raise ValueError(f"embedding of {len(out)} dims exceeds VECTOR({DIMS})")
    return out + [0.0] * (DIMS - len(out))


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


class LocalEmbedder:
    """BAAI/bge-small-en-v1.5 through fastembed. Real semantics, CPU, offline."""

    name = f"local:{LOCAL_MODEL}"
    _model = None  # loaded once per process; init is slow, inference is not

    def _get(self):
        if LocalEmbedder._model is None:
            from fastembed import TextEmbedding  # noqa: PLC0415

            LocalEmbedder._model = TextEmbedding(model_name=LOCAL_MODEL)
        return LocalEmbedder._model

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vecs = list(self._get().embed(texts))
        return [_pad(_unit([float(x) for x in v])) for v in vecs]


class BedrockEmbedder:
    """Amazon Titan Text Embeddings V2, natively 1024 dims and normalized."""

    def __init__(self, region: str | None = None, model_id: str | None = None):
        import boto3  # noqa: PLC0415

        self.name = model_id or os.environ.get(
            "BEDROCK_EMBED_MODEL", "amazon.titan-embed-text-v2:0"
        )
        self._rt = boto3.client(
            "bedrock-runtime", region_name=region or os.environ.get("AWS_REGION", "us-east-1")
        )

    def embed(self, text: str) -> list[float]:
        r = self._rt.invoke_model(
            modelId=self.name,
            body=json.dumps({"inputText": text, "dimensions": DIMS, "normalize": True}),
            accept="application/json",
            contentType="application/json",
        )
        return json.loads(r["body"].read())["embedding"]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        # Titan has no batch endpoint on invoke_model; the caller parallelizes.
        return [self.embed(t) for t in texts]


_CACHED: Embedder | None = None


def get_embedder() -> Embedder:
    """One embedder per process. The local model costs ~80s to initialize, so
    constructing a fresh one per request would dominate every code path."""
    global _CACHED
    if _CACHED is None:
        provider = os.environ.get("EMBED_PROVIDER", "local").strip().lower()
        _CACHED = BedrockEmbedder() if provider == "bedrock" else LocalEmbedder()
    return _CACHED


def to_pgvector(vec: list[float]) -> str:
    """CockroachDB takes vector literals as '[a,b,c]' text, cast to VECTOR(n)."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
