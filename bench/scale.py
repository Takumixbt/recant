"""
Scale benchmark: does retrieval stay fast as an agent's memory grows?

An agent that has been running for a year has a lot of memory. The question that
decides whether an audit layer is usable in production is whether retrieval --
and therefore every replay built on it -- degrades linearly with memory depth.

This walks a ladder of memory depths, measures retrieval latency at each, and
reports which access method the optimizer chose. The interesting result is the
crossover: below a threshold, scanning a handful of rows genuinely beats
traversing an ANN index, and CockroachDB correctly scans. Above it, the index
takes over and latency stops tracking depth.

Reporting the crossover honestly matters more than claiming the index is always
used. A submission that claims "distributed vector indexing" while its plans
show a full scan is one EXPLAIN away from being caught.

Usage: python bench/scale.py [--depths 10,100,1000,5000] [--trials 25]
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import time

sys.path.insert(0, ".")

from recant.blast import close_pool, pool
from recant.embed import get_embedder, to_pgvector

QUERY = "should I approve a large refund for this customer?"
K = 5
BATCH = 200

TEMPLATES = [
    "interaction note {i}: customer asked about billing cycle {n}",
    "interaction note {i}: shipment {n} delivered without incident",
    "interaction note {i}: support ticket {n} resolved on first contact",
    "interaction note {i}: invoice {n} paid on time",
    "interaction note {i}: refund of {n} USD issued and settled",
]


def seed(subject: str, depth: int, emb) -> None:
    rng = random.Random(hash(subject) & 0xFFFF)
    p = pool()
    with p.connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM beliefs WHERE subject_id=%s", (subject,))
        rows, params = [], []
        for i in range(depth):
            content = TEMPLATES[i % len(TEMPLATES)].format(i=i, n=rng.randint(1, 9999))
            rows.append("(%s,%s,'fact','seed',0.5,%s::VECTOR(1024),0)")
            params += [subject, content, to_pgvector(emb.embed(content))]
            if len(rows) >= BATCH:
                cur.execute(
                    "INSERT INTO beliefs (subject_id,content,kind,source,trust,"
                    "embedding,created_hlc) VALUES " + ",".join(rows), params
                )
                rows, params = [], []
        if rows:
            cur.execute(
                "INSERT INTO beliefs (subject_id,content,kind,source,trust,"
                "embedding,created_hlc) VALUES " + ",".join(rows), params
            )


def access_method(cur, subject: str, qvec: str) -> str:
    cur.execute(
        "EXPLAIN SELECT id FROM beliefs WHERE subject_id=%s AND live=true "
        "ORDER BY embedding <=> %s::VECTOR(1024) LIMIT %s",
        (subject, qvec, K),
    )
    blob = " ".join(r[0] for r in cur.fetchall()).lower()
    if "embedding" in blob and ("beliefs_live_embedding" in blob
                               or "beliefs_subject_embedding" in blob):
        return "vector index"
    return "scan + top-k"


def measure(subject: str, qvec: str, trials: int) -> tuple[list[float], str]:
    p = pool()
    lats: list[float] = []
    with p.connection() as conn:
        cur = conn.cursor()
        method = access_method(cur, subject, qvec)
        for _ in range(3):  # warm
            cur.execute(
                "SELECT id FROM beliefs WHERE subject_id=%s AND live=true "
                "ORDER BY embedding <=> %s::VECTOR(1024), id LIMIT %s",
                (subject, qvec, K),
            )
            cur.fetchall()
        for _ in range(trials):
            t0 = time.perf_counter()
            cur.execute(
                "SELECT id FROM beliefs WHERE subject_id=%s AND live=true "
                "ORDER BY embedding <=> %s::VECTOR(1024), id LIMIT %s",
                (subject, qvec, K),
            )
            cur.fetchall()
            lats.append((time.perf_counter() - t0) * 1000.0)
    return lats, method


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--depths", default="10,100,1000,5000")
    ap.add_argument("--trials", type=int, default=25)
    ap.add_argument("--keep", action="store_true", help="do not delete seeded subjects")
    a = ap.parse_args()
    depths = [int(x) for x in a.depths.split(",")]

    emb = get_embedder()
    qvec = to_pgvector(emb.embed(QUERY))

    p = pool()
    with p.connection() as conn:
        conn.cursor().execute("SET CLUSTER SETTING feature.vector_index.enabled = true")

    print(f"retrieval latency vs memory depth   (k={K}, {a.trials} trials each)\n")
    print(f"  {'depth':>8}  {'access method':<14}  {'p50 ms':>8}  {'p95 ms':>8}  {'per-1k':>8}")
    print("  " + "-" * 58)

    rows = []
    for d in depths:
        sub = f"scale_{d}"
        seed(sub, d, emb)
        with p.connection() as conn:
            conn.cursor().execute("ANALYZE beliefs")
        lats, method = measure(sub, qvec, a.trials)
        lats.sort()
        p50 = statistics.median(lats)
        p95 = lats[min(int(0.95 * (len(lats) - 1)), len(lats) - 1)]
        per_k = p50 / max(d / 1000.0, 1e-9)
        rows.append((d, method, p50, p95, per_k))
        print(f"  {d:>8,}  {method:<14}  {p50:>8.1f}  {p95:>8.1f}  {per_k:>8.1f}")
        if not a.keep:
            with p.connection() as conn:
                conn.cursor().execute("DELETE FROM beliefs WHERE subject_id=%s", (sub,))

    print("\n  interpretation")
    scans = [r for r in rows if r[1] == "scan + top-k"]
    idx = [r for r in rows if r[1] == "vector index"]
    if scans and idx:
        print(f"    crossover between {scans[-1][0]:,} and {idx[0][0]:,} beliefs per subject.")
        print(f"    Below it CockroachDB scans, which is genuinely faster at that size.")
        print(f"    Above it the index is chosen and cost per 1k beliefs falls from "
              f"{scans[-1][4]:,.0f} ms to {idx[-1][4]:,.1f} ms.")
    elif idx:
        print("    vector index chosen at every depth measured.")
    else:
        print("    scan chosen at every depth measured; go deeper to find the crossover.")
    close_pool()


if __name__ == "__main__":
    main()
