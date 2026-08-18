"""
Blast radius: what does changing a belief do to everything already decided?

The naive implementation replays the entire decision history against the mutated
memory. That is both ruinously expensive and wrong-headed: a decision that never
retrieved the belief cannot possibly change because of it.

So the engine works in two stages.

  Stage 1 (cheap, pure SQL): use the reverse index on decision_beliefs.belief_id
  to find exactly the decisions whose retrieval touched these beliefs. A thousand
  decisions collapse to the few hundred that could possibly flip.

  Stage 2 (parallel, historical): replay only those, each at its own recorded
  HLC. Every read is AS OF SYSTEM TIME at a timestamp in the past, and
  CockroachDB serves sufficiently old reads from the closest replica rather than
  the leaseholder -- so the fan-out spreads across the cluster instead of
  stampeding one node. That is what makes the flagship feature viable at all.

Connections come from a shared pool. This is not incidental: on a first pass the
engine opened a fresh TLS connection per worker per belief, and connection setup
dominated the measurement so thoroughly that adding workers made it slower.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from dotenv import load_dotenv
from psycopg_pool import ConnectionPool

from .agent import Agent
from .store import BeliefStore

load_dotenv()

_POOL: ConnectionPool | None = None

# Sized once for the process, never by whichever caller happens to be first.
# Getting this wrong is subtle: a first call asking for 8 workers used to pin the
# pool at 8, and a later 24-worker fan-out then deadlocked waiting for
# connections that could never exist.
POOL_MAX = int(os.environ.get("RECANT_POOL_MAX", "16"))


def pool() -> ConnectionPool:
    """One process-wide pool. Reused across every replay so TLS setup is paid
    once per connection rather than once per unit of work."""
    global _POOL
    if _POOL is None:
        _POOL = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=4,
            max_size=POOL_MAX,
            timeout=120,
            kwargs={"autocommit": True},
            open=True,
        )
        _POOL.wait(timeout=90)
    return _POOL


def close_pool() -> None:
    global _POOL
    if _POOL is not None:
        _POOL.close()
        _POOL = None


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
    belief_ids: list[str]
    candidates: int = 0       # decisions that actually retrieved these beliefs
    total_decisions: int = 0  # decisions in the whole ledger
    replayed: int = 0
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
        return xs[min(int(round(p / 100.0 * (len(xs) - 1))), len(xs) - 1)]

    @property
    def throughput(self) -> float:
        return self.replayed / (self.wall_ms / 1000.0) if self.wall_ms else 0.0

    def summary(self) -> str:
        label = (
            self.belief_ids[0][:8]
            if len(self.belief_ids) == 1
            else f"{len(self.belief_ids)} beliefs"
        )
        pct = 100.0 * self.candidates / max(self.total_decisions, 1)
        lines = [
            f"blast radius of {label}",
            f"  ledger            {self.total_decisions:,} decisions",
            f"  touched           {self.candidates:,}  ({pct:.1f}% of ledger)",
            f"  replayed          {self.replayed:,}",
            f"  FLIPPED           {len(self.flips):,}",
            f"  exposure          {self.exposure:,.2f} USD",
            f"  wall              {self.wall_ms:,.0f} ms",
            f"  throughput        {self.throughput:,.1f} replays/s",
            f"  p50 / p95 / p99   {self.percentile(50):,.0f} / "
            f"{self.percentile(95):,.0f} / {self.percentile(99):,.0f} ms",
        ]
        if self.errors:
            lines.append(f"  errors            {len(self.errors)}")
        return "\n".join(lines)


def compute(belief_ids: str | list[str], workers: int = 16, limit: int | None = None) -> BlastRadius:
    """
    Blast radius of quarantining one belief or a whole set of them.

    Passing a set is the right shape for an attack campaign: the question is not
    "what does each planted belief do alone" but "what happens if we pull all of
    them", and the answer is one fan-out rather than N.
    """
    ids = [belief_ids] if isinstance(belief_ids, str) else list(belief_ids)
    result = BlastRadius(belief_ids=ids)
    p = pool()

    with p.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM decisions")
        result.total_decisions = cur.fetchone()[0]
        # Stage 1: reverse index lookup. No model calls, no history scan.
        cur.execute(
            """
            SELECT DISTINCT d.id, d.subject_id, d.action, d.amount, d.prompt, d.read_hlc
              FROM decision_beliefs db
              JOIN decisions d ON d.id = db.decision_id
             WHERE db.belief_id = ANY(%s)
            """,
            (ids,),
        )
        meta = {
            str(r[0]): (str(r[1]), r[2], float(r[3]) if r[3] is not None else None, r[4], r[5])
            for r in cur.fetchall()
        }

    cands = list(meta)
    if limit is not None:
        cands = cands[:limit]
    result.candidates = len(meta)
    if not cands:
        return result

    exclude = frozenset(ids)

    def work(chunk: list[str]):
        flips, errs, lats = [], [], []
        with p.connection() as c:
            agent = Agent(BeliefStore(c))
            for did in chunk:
                subject, was, amount, prompt, read_hlc = meta[did]
                t0 = time.perf_counter()
                try:
                    new, _ = agent.replay(
                        did, exclude=exclude, meta=(subject, prompt, amount, read_hlc)
                    )
                except Exception as e:  # e.g. a replay past the GC window
                    errs.append(f"{did[:8]}: {str(e).splitlines()[0][:90]}")
                    continue
                ms = (time.perf_counter() - t0) * 1000.0
                lats.append(ms)
                if new.action != was:
                    flips.append(Flip(did, subject, was, new.action, amount, prompt, ms))
        return flips, errs, lats

    # Never spin up more workers than there is work for them to do, and never
    # more than the pool can hand out -- oversubscribing it only creates
    # threads that block waiting for a connection.
    nw = max(1, min(workers, len(cands), POOL_MAX))
    chunks: list[list[str]] = [[] for _ in range(nw)]
    for i, did in enumerate(cands):
        chunks[i % nw].append(did)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=nw) as ex:
        for flips, errs, lats in ex.map(work, chunks):
            result.flips.extend(flips)
            result.errors.extend(errs)
            result.latencies_ms.extend(lats)
    result.wall_ms = (time.perf_counter() - t0) * 1000.0
    result.replayed = len(result.latencies_ms)
    result.flips.sort(key=lambda f: (f.amount or 0.0), reverse=True)
    return result
