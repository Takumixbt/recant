"""
The belief store and the retrieval path.

The most important line in this file captures cluster_logical_timestamp() inside
the same transaction as the vector search. That decimal is the seed for every
replay, counterfactual, and blast-radius query the rest of the system performs.
Without it, the past is unreachable.
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass
from decimal import Decimal

import socket
from urllib.parse import urlparse

import psycopg
from dotenv import load_dotenv
from psycopg import errors as pg_errors

from .embed import get_embedder, to_pgvector

load_dotenv()


@dataclass(frozen=True)
class Retrieved:
    belief_id: str
    content: str
    source: str
    trust: float
    rank: int
    distance: float


@dataclass(frozen=True)
class Retrieval:
    """What the agent saw, and the exact instant it saw it."""

    hlc: Decimal
    beliefs: tuple[Retrieved, ...]

    def as_context(self) -> str:
        if not self.beliefs:
            return "(no stored beliefs about this subject)"
        return "\n".join(
            f"- {b.content}  [source: {b.source}, trust: {b.trust:.2f}]" for b in self.beliefs
        )


def retry_txn(conn: psycopg.Connection, body, attempts: int = 10):
    """
    Run `body` in a transaction, retrying on serialization failures.

    CockroachDB runs SERIALIZABLE by default, so a contended transaction is told
    to restart rather than being allowed to commit a lost update. Retrying is the
    correct response, not a workaround: it is the mechanism by which two agents
    writing the same subject's beliefs concurrently cannot clobber each other.
    Under read-committed, that same workload corrupts silently.
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            with conn.transaction():
                return body()
        except pg_errors.SerializationFailure as e:
            last = e
            time.sleep(min(0.05 * (2**i), 2.0) * (0.5 + random.random()))
    raise last  # type: ignore[misc]


def warm_dns(attempts: int = 8) -> None:
    """Resolve the cluster host before connecting, retrying transient failures.

    This machine intermittently fails to resolve the CockroachDB Cloud hostname
    (getaddrinfo 11001/11002). It is a local resolver problem, not a cluster
    problem, but it kills a long parallel job just as dead -- so every entry
    point warms the name first rather than discovering it mid-fan-out.
    """
    host = urlparse(os.environ["DATABASE_URL"]).hostname
    if not host:
        return
    last: Exception | None = None
    for i in range(attempts):
        try:
            socket.getaddrinfo(host, None)
            return
        except socket.gaierror as e:
            last = e
            time.sleep(min(0.5 * (2**i), 8.0))
    raise RuntimeError(f"could not resolve {host} after {attempts} attempts") from last


def connect(attempts: int = 5) -> psycopg.Connection:
    warm_dns()
    last: Exception | None = None
    for i in range(attempts):
        try:
            return psycopg.connect(
                os.environ["DATABASE_URL"], autocommit=True, connect_timeout=20
            )
        except psycopg.OperationalError as e:
            last = e
            time.sleep(min(0.5 * (2**i), 8.0))
            warm_dns()
    raise last  # type: ignore[misc]


def _to_retrieval(hlc: Decimal, rows) -> Retrieval:
    return Retrieval(
        hlc=hlc,
        beliefs=tuple(
            Retrieved(str(r[0]), r[1], r[2], float(r[3]), i, float(r[4]))
            for i, r in enumerate(rows)
        ),
    )


class BeliefStore:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn
        self.embedder = get_embedder()
        self._hlc_hint: Decimal | None = None

    def amortize_clock(self) -> None:
        """Cache one clock read for a batch of writes. Only affects the log's
        ordering key; the authoritative per-row time is still MVCC."""
        self._hlc_hint = self.now_hlc()

    def now_hlc(self) -> Decimal:
        """Current cluster HLC, read outside any write transaction."""
        cur = self.conn.cursor()
        cur.execute("SELECT cluster_logical_timestamp()")
        return cur.fetchone()[0]

    def commit_hlc(self, belief_id: str) -> Decimal:
        """The row's authoritative commit timestamp, straight from MVCC.

        This is the timestamp the storage engine actually assigned, not one the
        application guessed -- which is why replay anchored to it is exact.
        """
        cur = self.conn.cursor()
        cur.execute(
            "SELECT crdb_internal_mvcc_timestamp FROM beliefs WHERE id = %s", (belief_id,)
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"no such belief: {belief_id}")
        return row[0]

    # --- writing ---------------------------------------------------------

    def assert_belief(
        self,
        subject_id: str,
        content: str,
        source: str,
        trust: float = 0.5,
        kind: str = "fact",
        valid_from: str | None = None,
    ) -> str:
        """
        Write a belief and its embedding in ONE transaction.

        The vector is searchable the instant this commits: there is no window in
        which the row exists but the index does not. A pgvector stack with a
        separate embedding pipeline cannot make that promise, which is exactly
        why its historical replays are unsound.
        """
        vec = to_pgvector(self.embedder.embed(content))
        # Read the clock OUTSIDE the write. Calling cluster_logical_timestamp()
        # inside a write pins that transaction's commit timestamp, so any push
        # (vector-index maintenance reliably causes one) forces an unrecoverable
        # restart. The row's authoritative commit time is
        # crdb_internal_mvcc_timestamp anyway -- supplied by the storage engine,
        # not by us -- so this value is only a log ordering key. Callers seeding
        # in bulk can amortize one clock read across many writes.
        hlc = self._hlc_hint if self._hlc_hint is not None else self.now_hlc()
        payload = json.dumps({"content": content, "source": source, "trust": trust})

        # One statement, one round trip. A data-modifying CTE keeps the belief
        # and its log entry atomic without paying four network hops to us-east-1.
        def _body():
            cur = self.conn.cursor()
            cur.execute(
                """
                WITH ins AS (
                    INSERT INTO beliefs
                        (subject_id, content, kind, source, trust, embedding,
                         valid_from, created_hlc)
                    VALUES (%s, %s, %s, %s, %s, %s::VECTOR(1024),
                            COALESCE(%s::TIMESTAMPTZ, now()), %s)
                    RETURNING id, subject_id
                ), ev AS (
                    INSERT INTO memory_events (hlc, op, belief_id, subject_id, payload)
                    SELECT %s, 'assert', id, subject_id, %s::JSONB FROM ins
                    RETURNING 1
                )
                SELECT id FROM ins
                """,
                (subject_id, content, kind, source, trust, vec, valid_from, hlc,
                 hlc, payload),
            )
            return cur.fetchone()[0]

        return str(retry_txn(self.conn, _body))

    def quarantine(self, belief_id: str, reason: str) -> None:
        """Close a belief's validity interval. Never deletes: the audit trail and
        every edge to past decisions survive."""

        hlc = self.now_hlc()

        def _body():
            cur = self.conn.cursor()
            cur.execute(
                """
                UPDATE beliefs
                   SET quarantined_at = now(), quarantine_reason = %s,
                       valid_to = COALESCE(valid_to, now())
                 WHERE id = %s
             RETURNING subject_id
                """,
                (reason, belief_id),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"no such belief: {belief_id}")
            cur.execute(
                """
                INSERT INTO memory_events (hlc, op, belief_id, subject_id, payload)
                VALUES (%s, 'quarantine', %s, %s, %s)
                """,
                (hlc, belief_id, row[0], json.dumps({"reason": reason})),
            )

        retry_txn(self.conn, _body)

    # --- reading ---------------------------------------------------------

    def retrieve(self, subject_id: str, query: str, k: int = 5) -> Retrieval:
        """Live retrieval. Captures the read HLC and the ranked result set in one
        statement, so the two can never disagree.

        The `, id` tiebreak is load-bearing, not cosmetic. Cosine distance ties
        are common among similar beliefs, and without a total order the winner
        of a tie is decided by scan order -- which made replay disagree with the
        retrieval it was supposed to reproduce. A deterministic total order is
        what lets "replay is exact" be a guarantee instead of a tendency.
        """
        vec = to_pgvector(self.embedder.embed(query))
        # One statement, so the anchor and the result set are read at the same
        # timestamp by construction -- they cannot drift apart, and it costs one
        # round trip instead of four. The LEFT JOIN guarantees the anchor comes
        # back even when the subject has no beliefs yet.
        cur = self.conn.cursor()
        cur.execute(
            """
            WITH anchor AS (SELECT cluster_logical_timestamp() AS hlc),
                 hits AS (
                    SELECT id, content, source, trust,
                           embedding <=> %s::VECTOR(1024) AS dist
                      FROM beliefs
                     WHERE subject_id = %s
                       AND quarantined_at IS NULL
                       AND valid_to IS NULL
                  ORDER BY embedding <=> %s::VECTOR(1024), id
                     LIMIT %s
                 )
            SELECT a.hlc, h.id, h.content, h.source, h.trust, h.dist
              FROM anchor a LEFT JOIN hits h ON true
          ORDER BY h.dist, h.id
            """,
            (vec, subject_id, vec, k),
        )
        raw = cur.fetchall()
        hlc = raw[0][0]
        rows = [r[1:] for r in raw if r[1] is not None]
        return _to_retrieval(hlc, rows)

    def retrieve_as_of(
        self,
        subject_id: str,
        query: str,
        hlc: Decimal,
        k: int = 5,
        exclude: frozenset[str] = frozenset(),
    ) -> Retrieval:
        """
        Reconstruct a past retrieval exactly, optionally minus some beliefs.

        AS OF SYSTEM TIME demands a constant expression, so the HLC is inlined
        rather than bound. It is a Decimal this system produced, never user
        input, so there is nothing to inject.

        `exclude` is what turns a replay into a counterfactual: same instant,
        same index, same ranking, one belief removed.
        """
        if not isinstance(hlc, Decimal):
            raise TypeError("hlc must be a Decimal produced by this system")
        vec = to_pgvector(self.embedder.embed(query))

        clause, params = "", [vec, subject_id]
        if exclude:
            clause = "AND id NOT IN (" + ",".join(["%s"] * len(exclude)) + ")"
            params += list(exclude)
        params += [vec, k]

        cur = self.conn.cursor()
        cur.execute(
            f"""
            SELECT id, content, source, trust, embedding <=> %s::VECTOR(1024) AS dist
              FROM beliefs
              AS OF SYSTEM TIME {hlc}
             WHERE subject_id = %s
               AND quarantined_at IS NULL
               AND valid_to IS NULL
               {clause}
          ORDER BY embedding <=> %s::VECTOR(1024), id
             LIMIT %s
            """,
            params,
        )
        return _to_retrieval(hlc, cur.fetchall())
