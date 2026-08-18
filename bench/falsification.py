"""
Falsification benchmark: does the audit trail actually tell the truth?

Every "agent memory + audit log" product claims it can show you what the agent
knew. This benchmark tries to break that claim on two architectures and reports
which one survives.

THE STACK UNDER TEST (how nearly everyone builds this)
    A relational row is written when a belief arrives. Its embedding is produced
    by a separate pipeline -- an async worker, a batch job, a queue consumer --
    and lands some time later. This is standard practice, because embedding is
    slow and nobody wants it on the write path.

    Replay is reconstructed by filtering on wall-clock time:
        WHERE created_at <= <the decision's timestamp>

THE BUG THIS CREATES
    Between a belief's row landing and its embedding landing, the belief exists
    but is NOT retrievable. A decision made in that window genuinely did not see
    it.

    The audit happens later -- days later, when someone asks why a refund was
    approved. By then the pipeline has caught up and every embedding is present.
    The wall-clock filter says the row existed at decision time, so the
    reconstruction includes it. The audit trail now reports a retrieval that
    never happened, and reports it with total confidence, because nothing
    recorded that the embedding was missing.

    That is worse than having no audit trail. An investigator draws conclusions
    about the agent's reasoning from evidence the agent never had.

    TIMING IS THE WHOLE POINT. Replay during the lag window and the bug hides.
    Replay after the pipeline settles -- which is when real audits happen -- and
    it appears. An earlier version of this benchmark measured the wrong moment
    and reported the broken architecture as sound.

THE STACK BEING VALIDATED (recant)
    The belief and its embedding commit in ONE transaction, so the window does
    not exist. Replay is anchored to the MVCC timestamp the retrieval actually
    read at, so the reconstruction is not inferred from wall-clock ordering --
    it is the same read, served again.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field

sys.path.insert(0, ".")

from recant.embed import get_embedder, to_pgvector
from recant.store import BeliefStore, connect

SUBJECT = "bench_subject"
QUERY = "should I approve a large refund for this customer?"
K = 5
LAG_SECONDS = 2.0   # how far the async embedding pipeline trails the row write
DECISIONS = 15      # decisions made while the pipeline is catching up

NAIVE_SCHEMA = """
CREATE TABLE naive_beliefs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_id  STRING NOT NULL,
    content     STRING NOT NULL,
    embedding   VECTOR(1024),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedded_at TIMESTAMPTZ
);
CREATE TABLE naive_decisions (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decided_at TIMESTAMPTZ NOT NULL,
    served     STRING[] NOT NULL
);
"""


@dataclass
class Result:
    stack: str
    trials: int = 0
    faithful: int = 0
    wrong: int = 0
    ghosts: int = 0            # beliefs a replay claimed were seen but weren't
    examples: list[str] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return 100.0 * self.wrong / self.trials if self.trials else 0.0


def setup(conn):
    cur = conn.cursor()
    # Drop rather than DELETE: an earlier revision of this benchmark created
    # these tables with a different shape, and IF NOT EXISTS would silently keep
    # the stale one.
    cur.execute("DROP TABLE IF EXISTS naive_decisions")
    cur.execute("DROP TABLE IF EXISTS naive_beliefs")
    for st in [s.strip() for s in NAIVE_SCHEMA.split(";") if s.strip()]:
        cur.execute(st)
    cur.execute(
        "DELETE FROM decision_beliefs WHERE decision_id IN "
        "(SELECT id FROM decisions WHERE subject_id = %s)", (SUBJECT,)
    )
    for t in ("decisions", "memory_events", "beliefs"):
        cur.execute(f"DELETE FROM {t} WHERE subject_id = %s", (SUBJECT,))


def main():
    emb = get_embedder()
    qvec = to_pgvector(emb.embed(QUERY))
    naive = Result("common architecture (async embedding + wall-clock replay)")
    recant = Result("recant (transactional embedding + MVCC replay)")

    with connect() as conn:
        setup(conn)
        cur = conn.cursor()
        store = BeliefStore(conn)

        print(f"embedder={emb.name}  pipeline lag={LAG_SECONDS}s  "
              f"decisions during lag={DECISIONS}\n")
        print("PHASE 1: beliefs arrive; embeddings trail behind; agent decides")

        pending: list[tuple[float, str, str]] = []
        recant_anchors: list = []

        for i in range(DECISIONS):
            content = f"belief {i}: customer has escalation flag {i} set"
            vec = to_pgvector(emb.embed(content))

            # common architecture: row lands now, embedding is queued
            cur.execute(
                "INSERT INTO naive_beliefs (subject_id, content) VALUES (%s,%s) RETURNING id",
                (SUBJECT, content),
            )
            pending.append((time.monotonic() + LAG_SECONDS, str(cur.fetchone()[0]), vec))

            # the pipeline drains whatever is due
            for item in list(pending):
                if time.monotonic() >= item[0]:
                    cur.execute(
                        "UPDATE naive_beliefs SET embedding=%s::VECTOR(1024), "
                        "embedded_at=now() WHERE id=%s", (item[2], item[1])
                    )
                    pending.remove(item)

            # the agent decides: only embedded rows are findable
            cur.execute(
                """
                SELECT id, now() FROM naive_beliefs
                 WHERE subject_id=%s AND embedding IS NOT NULL
              ORDER BY embedding <=> %s::VECTOR(1024), id LIMIT %s
                """,
                (SUBJECT, qvec, K),
            )
            rows = cur.fetchall()
            if rows:
                served, at = [str(r[0]) for r in rows], rows[0][1]
            else:
                cur.execute("SELECT now()")
                served, at = [], cur.fetchone()[0]
            cur.execute(
                "INSERT INTO naive_decisions (decided_at, served) VALUES (%s,%s)", (at, served)
            )

            # recant: belief and embedding commit together
            store.assert_belief(SUBJECT, content, "bench", 0.5)
            live = store.retrieve(SUBJECT, QUERY, k=K)
            recant_anchors.append((live.hlc, [b.belief_id for b in live.beliefs]))
            time.sleep(0.15)

        print(f"  {DECISIONS} decisions recorded on each stack")

        print("\nPHASE 2: the embedding pipeline finishes draining")
        for due, bid, v in pending:
            cur.execute(
                "UPDATE naive_beliefs SET embedding=%s::VECTOR(1024), embedded_at=now() "
                "WHERE id=%s", (v, bid)
            )
        cur.execute("SELECT count(*) FROM naive_beliefs WHERE embedding IS NULL")
        print(f"  beliefs still un-embedded: {cur.fetchone()[0]}  (pipeline is caught up)")

        print("\nPHASE 3: the audit runs -- days later, in practice")
        cur.execute("SELECT decided_at, served FROM naive_decisions ORDER BY decided_at")
        for at, served in cur.fetchall():
            cur.execute(
                """
                SELECT id FROM naive_beliefs
                 WHERE subject_id=%s AND created_at <= %s AND embedding IS NOT NULL
              ORDER BY embedding <=> %s::VECTOR(1024), id LIMIT %s
                """,
                (SUBJECT, at, qvec, K),
            )
            replayed = [str(r[0]) for r in cur.fetchall()]
            naive.trials += 1
            if replayed == list(served):
                naive.faithful += 1
            else:
                naive.wrong += 1
                g = set(replayed) - set(served)
                naive.ghosts += len(g)
                if g and len(naive.examples) < 3:
                    naive.examples.append(
                        f"replay asserts {len(g)} belief(s) informed this decision "
                        f"that the agent could not see (e.g. {sorted(g)[0][:8]})"
                    )

        for hlc, served in recant_anchors:
            back = store.retrieve_as_of(SUBJECT, QUERY, hlc, k=K)
            recant.trials += 1
            if [b.belief_id for b in back.beliefs] == served:
                recant.faithful += 1
            else:
                recant.wrong += 1
                recant.ghosts += len(
                    {b.belief_id for b in back.beliefs} - set(served)
                )

    print("\n" + "=" * 72)
    print("  FALSIFICATION BENCHMARK")
    print("=" * 72)
    for r in (naive, recant):
        print(f"\n  {r.stack}")
        print(f"    decisions audited            {r.trials}")
        print(f"    replay matched reality       {r.faithful}")
        print(f"    replay was WRONG             {r.wrong}   ({r.rate:.0f}%)")
        print(f"    fabricated evidence items    {r.ghosts}")
        for e in r.examples:
            print(f"      - {e}")

    print("\n" + "=" * 72)
    if naive.wrong > 0 and recant.wrong == 0:
        print("  RESULT: the common architecture fabricates evidence.")
        print(f"  {naive.rate:.0f}% of its audit records describe a retrieval that never")
        print(f"  happened, inventing {naive.ghosts} pieces of evidence the agent never saw.")
        print("  recant cannot exhibit this failure. The embedding commits with the")
        print("  belief, so the window the bug lives in does not exist, and replay is")
        print("  the original read re-served rather than a guess reconstructed from")
        print("  wall-clock ordering.")
    elif recant.wrong:
        print("  UNEXPECTED: recant mismatched. Investigate before submitting.")
    else:
        print(f"  Both faithful. Raise LAG_SECONDS (now {LAG_SECONDS}s) to widen the")
        print("  window the common architecture fails in.")
    print("=" * 72)


if __name__ == "__main__":
    main()
