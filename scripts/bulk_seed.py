"""
Bulk-seed deep agent memory.

The point of depth: a vector index earns its keep at thousands of vectors per
prefix, not at five. Seeded shallow, CockroachDB's optimizer correctly ignores
the index and scans -- which makes the whole "distributed vector indexing" claim
hollow. Seeded at realistic depth (a customer with years of accumulated notes),
the index is chosen and retrieval stays flat as memory grows.

Writes go out in multi-row batches across parallel workers. A single-row insert
per belief spends its life waiting on us-east-1.

Usage: python scripts/bulk_seed.py [subjects] [beliefs_each] [workers]
"""

from __future__ import annotations

import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, ".")

from recant.blast import close_pool, pool
from recant.embed import get_embedder, to_pgvector

SUBJECTS = int(sys.argv[1]) if len(sys.argv) > 1 else 40
DEPTH = int(sys.argv[2]) if len(sys.argv) > 2 else 1200
WORKERS = int(sys.argv[3]) if len(sys.argv) > 3 else 12
BATCH = 200

TEMPLATES = [
    "interaction note {i}: customer asked about billing cycle {n}",
    "interaction note {i}: shipment {n} delivered without incident",
    "interaction note {i}: customer updated payment method",
    "interaction note {i}: support ticket {n} resolved on first contact",
    "interaction note {i}: customer browsed plan tier {tier}",
    "interaction note {i}: invoice {n} paid on time",
    "interaction note {i}: customer declined an upsell offer",
    "interaction note {i}: address verification completed for order {n}",
    "interaction note {i}: refund of {n} USD issued and settled",
    "interaction note {i}: customer contacted support about delivery timing",
]
TIERS = ["standard", "priority", "enterprise"]

POLICY = "refunds above 500 USD require a manual hold"


def seed_subject(args) -> int:
    subject, depth = args
    emb = get_embedder()
    rng = random.Random(hash(subject) & 0xFFFFFF)
    p = pool()
    written = 0

    with p.connection() as conn:
        cur = conn.cursor()
        rows: list[str] = []
        params: list = []

        def flush():
            nonlocal rows, params, written
            if not rows:
                return
            cur.execute(
                "INSERT INTO beliefs (subject_id, content, kind, source, trust, "
                "embedding, created_hlc) VALUES " + ",".join(rows),
                params,
            )
            written += len(rows)
            rows, params = [], []

        # the governing policy always sits in memory
        for content, source, trust in [(POLICY, "policy", 0.99)]:
            rows.append("(%s,%s,'policy','policy',%s,%s::VECTOR(1024),0)")
            params += [subject, content, trust, to_pgvector(emb.embed(content))]

        for i in range(depth):
            content = TEMPLATES[i % len(TEMPLATES)].format(
                i=i, n=rng.randint(1, 9999), tier=rng.choice(TIERS)
            )
            rows.append("(%s,%s,'fact',%s,%s,%s::VECTOR(1024),0)")
            params += [
                subject, content, rng.choice(["import", "agent"]),
                round(rng.uniform(0.5, 0.95), 2), to_pgvector(emb.embed(content)),
            ]
            if len(rows) >= BATCH:
                flush()
        flush()
    return written


def main():
    subjects = [f"deep_{i:04d}" for i in range(SUBJECTS)]
    print(f"{SUBJECTS} subjects x {DEPTH} beliefs = {SUBJECTS * DEPTH:,} vectors, "
          f"{WORKERS} workers")

    p = pool()
    with p.connection() as conn:
        cur = conn.cursor()
        cur.execute("SET CLUSTER SETTING feature.vector_index.enabled = true")
        cur.execute("DELETE FROM beliefs WHERE subject_id LIKE 'deep_%'")

    t0 = time.perf_counter()
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for n in ex.map(seed_subject, [(s, DEPTH) for s in subjects]):
            done += n
            el = time.perf_counter() - t0
            print(f"  {done:,} / {SUBJECTS * (DEPTH + 1):,} beliefs   "
                  f"{el:.0f}s   {done / max(el, 1e-9):,.0f}/s")

    with p.connection() as conn:
        cur = conn.cursor()
        cur.execute("ANALYZE beliefs")
        cur.execute("SELECT count(*) FROM beliefs")
        total = cur.fetchone()[0]
    print(f"\ndone: {total:,} beliefs in {time.perf_counter() - t0:.0f}s")
    close_pool()


if __name__ == "__main__":
    main()
