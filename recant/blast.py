"""
Blast radius: what does changing a belief do to everything already decided?

The naive implementation replays the entire decision history against the
mutated memory. That is both ruinously expensive and wrong-headed: a decision
that never retrieved the belief cannot possibly change because of it.

So the engine works in two stages.

  Stage 1 (cheap, pure SQL): use the reverse index on decision_beliefs.belief_id
  to find exactly the decisions whose retrieval touched this belief. Twelve
  thousand decisions collapse to the few hundred that could possibly flip.

  Stage 2 (parallel, historical): replay only those, each at its own recorded
  HLC. Every read is AS OF SYSTEM TIME at a timestamp well in the past, and
  CockroachDB serves sufficiently old reads from the closest replica rather than
  the leaseholder -- so the fan-out spreads across the cluster instead of
  stampeding one node. This is why the flagship feature is viable at all.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .agent import Agent
from .store import BeliefStore, connect


@dataclass
class Flip:
    decision_id: str
    subject_id: str
    was: str
    now: str
    amount: float | None
    prompt: str
    latency_ms: float


@dataclass
class BlastRadius:
    belief_id: str
    candidates: int          # decisions that actually retrieved this belief
    total_decisions: int     # decisions in the whole ledger
    replayed: int
    flips: list[Flip] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    wall_ms: float = 0.0
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def exposure(self) -> float:
        return sum(f.amount or 0.0 for f in self.flips)

    def percentile(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        xs = sorted(self.latencies_ms)
        i = min(int(round(p / 100.0 * (len(xs) - 1))), len(xs) - 1)
        return xs[i]

    def summary(self) -> str:
        lines = [
            f"belief {self.belief_id[:8]}",
            f"  ledger            {self.total_decisions} decisions",
            f"  touched by belief {self.candidates}  "
            f"({100.0 * self.candidates / max(self.total_decisions, 1):.1f}% of ledger)",
            f"  replayed          {self.replayed}",
            f"  FLIPPED           {len(self.flips)}",
            f"  exposure          {self.exposure:,.2f} USD",
            f"  wall              {self.wall_ms:,.0f} ms",
            f"  replay p50/p99    {self.percentile(50):,.1f} / {self.percentile(99):,.1f} ms",
        ]
        if self.errors:
            lines.append(f"  errors            {len(self.errors)}")
        return "\n".join(lines)


def candidates_for(conn, belief_id: str) -> list[str]:
    """Stage 1. Pure index lookup on decision_beliefs.belief_id -- no model calls,
    no history scan."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT d.id
          FROM decision_beliefs db
          JOIN decisions d ON d.id = db.decision_id
         WHERE db.belief_id = %s
      ORDER BY d.decided_at DESC
        """,
        (belief_id,),
    )
    return [str(r[0]) for r in cur.fetchall()]


def total_decisions(conn) -> int:
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM decisions")
    return cur.fetchone()[0]


def compute(belief_id: str, workers: int = 8, limit: int | None = None) -> BlastRadius:
    """
    Compute the blast radius of removing one belief.

    Each worker holds its own connection: the replays are independent historical
    reads, so they parallelize cleanly.
    """
    with connect() as conn:
        cands = candidates_for(conn, belief_id)
        total = total_decisions(conn)
        cur = conn.cursor()
        cur.execute(
            "SELECT id, subject_id, action, amount, prompt FROM decisions "
            "WHERE id = ANY(%s)",
            (cands,),
        )
        meta = {
            str(r[0]): (str(r[1]), r[2], float(r[3]) if r[3] is not None else None, r[4])
            for r in cur.fetchall()
        }

    if limit is not None:
        cands = cands[:limit]

    result = BlastRadius(
        belief_id=belief_id, candidates=len(cands), total_decisions=total, replayed=0
    )
    if not cands:
        return result

    exclude = frozenset({belief_id})

    def work(chunk: list[str]) -> tuple[list[Flip], list[str], list[float]]:
        flips, errs, lats = [], [], []
        with connect() as c:
            agent = Agent(BeliefStore(c))
            for did in chunk:
                t0 = time.perf_counter()
                try:
                    new, _ = agent.replay(did, exclude=exclude)
                except Exception as e:  # a replay past the GC window, etc.
                    errs.append(f"{did[:8]}: {str(e).splitlines()[0][:90]}")
                    continue
                ms = (time.perf_counter() - t0) * 1000.0
                lats.append(ms)
                subject, was, amount, prompt = meta[did]
                if new.action != was:
                    flips.append(Flip(did, subject, was, new.action, amount, prompt, ms))
        return flips, errs, lats

    chunks: list[list[str]] = [[] for _ in range(workers)]
    for i, did in enumerate(cands):
        chunks[i % workers].append(did)
    chunks = [c for c in chunks if c]

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
        for flips, errs, lats in pool.map(work, chunks):
            result.flips.extend(flips)
            result.errors.extend(errs)
            result.latencies_ms.extend(lats)
    result.wall_ms = (time.perf_counter() - t0) * 1000.0
    result.replayed = len(result.latencies_ms)
    result.flips.sort(key=lambda f: (f.amount or 0.0), reverse=True)
    return result
