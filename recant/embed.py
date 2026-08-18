"""
Embedding providers.

Bedrock Titan is the intended provider. This account's Bedrock quota is not yet
open, so a deterministic local provider stands in during development. Both emit
1024-dimensional unit vectors, which is what the VECTOR(1024) column and the
cosine index expect, so nothing downstream can tell them apart structurally.

Swap by setting EMBED_PROVIDER=bedrock in .env once quota frees.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Protocol

DIMS = 1024


class Embedder(Protocol):
    name: str

    def embed(self, text: str) -> list[float]: ...


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


class LocalEmbedder:
    """
    Deterministic hashed bag-of-words. Not semantically strong, but it is stable,
    free, offline, and genuinely separates different text -- enough to exercise
    retrieval, replay, and blast radius end to end.
    """

    name = "local-hash-v1"

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * DIMS
        tokens = [t for t in text.lower().replace("/", " ").split() if t]
        for tok in tokens:
            # two hashes per token keeps collisions from cancelling signal
            for salt in (b"a", b"b"):
                h = hashlib.blake2b(tok.encode() + salt, digest_size=8).digest()
                idx = int.from_bytes(h[:4], "big") % DIMS
                sign = 1.0 if h[4] & 1 else -1.0
                vec[idx] += sign
        return _unit(vec) if any(vec) else [1.0] + [0.0] * (DIMS - 1)


class BedrockEmbedder:
    """Amazon Titan Text Embeddings V2, 1024 dims, normalized server-side."""

    name = "amazon.titan-embed-text-v2:0"

    def __init__(self, region: str | None = None, model_id: str | None = None):
        import boto3  # noqa: PLC0415

        self.model_id = model_id or os.environ.get(
            "BEDROCK_EMBED_MODEL", "amazon.titan-embed-text-v2:0"
        )
        self._rt = boto3.client(
            "bedrock-runtime", region_name=region or os.environ.get("AWS_REGION", "us-east-1")
        )

    def embed(self, text: str) -> list[float]:
        r = self._rt.invoke_model(
            modelId=self.model_id,
            body=json.dumps({"inputText": text, "dimensions": DIMS, "normalize": True}),
            accept="application/json",
            contentType="application/json",
        )
        return json.loads(r["body"].read())["embedding"]


def get_embedder() -> Embedder:
    provider = os.environ.get("EMBED_PROVIDER", "local").strip().lower()
    if provider == "bedrock":
        return BedrockEmbedder()
    return LocalEmbedder()


def to_pgvector(vec: list[float]) -> str:
    """CockroachDB takes vector literals as '[a,b,c]' text, cast to VECTOR(n)."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
