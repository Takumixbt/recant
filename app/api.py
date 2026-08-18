"""
Console backend.

Every endpoint here is a thin wrapper over the engine -- deliberately. The point
of the demo is that the capabilities are real properties of the data layer, not
features of a web app. If an endpoint did any interesting work of its own, the
claim would be weaker.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recant import blast
from recant.agent import Agent
from recant.interdict import Interdictor
from recant.store import BeliefStore

app = FastAPI(title="recant", docs_url="/api/docs")
STATIC = Path(__file__).parent / "static"


def _store():
    conn = blast.pool().getconn()
    return conn, BeliefStore(conn)


class Ask(BaseModel):
    subject_id: str
    prompt: str
    amount: float | None = None


class Assert(BaseModel):
    subject_id: str
    content: str
    source: str = "user:unverified"
    trust: float = 0.5


@app.get("/api/health")
def health():
    p = blast.pool()
    with p.connection() as c:
        cur = c.cursor()
        cur.execute("SELECT version(), cluster_logical_timestamp()")
        v, hlc = cur.fetchone()
    return {"ok": True, "server": v.split(",")[0], "hlc": str(hlc)}


@app.get("/api/subjects")
def subjects(limit: int = 40):
    with blast.pool().connection() as c:
        cur = c.cursor()
        cur.execute(
            """
            SELECT b.subject_id, count(*) AS beliefs,
                   coalesce(max(d.n), 0) AS decisions
              FROM beliefs b
              LEFT JOIN (SELECT subject_id, count(*) n FROM decisions GROUP BY subject_id) d
                     ON d.subject_id = b.subject_id
             GROUP BY b.subject_id
             ORDER BY decisions DESC, beliefs DESC
             LIMIT %s
            """,
            (limit,),
        )
        return [
            {"subject_id": r[0], "beliefs": r[1], "decisions": r[2]} for r in cur.fetchall()
        ]


@app.get("/api/memory/{subject_id}")
def memory(subject_id: str, limit: int = 60):
    """The belief timeline, newest first, with each row's true MVCC commit time."""
    with blast.pool().connection() as c:
        cur = c.cursor()
        cur.execute(
            """
            SELECT id, content, source, trust, valid_from, valid_to,
                   quarantined_at, quarantine_reason, crdb_internal_mvcc_timestamp
              FROM beliefs
             WHERE subject_id = %s
             ORDER BY crdb_internal_mvcc_timestamp DESC
             LIMIT %s
            """,
            (subject_id, limit),
        )
        return [
            {
                "id": str(r[0]), "content": r[1], "source": r[2], "trust": float(r[3]),
                "valid_from": r[4].isoformat() if r[4] else None,
                "valid_to": r[5].isoformat() if r[5] else None,
                "quarantined_at": r[6].isoformat() if r[6] else None,
                "quarantine_reason": r[7],
                "commit_hlc": str(r[8]),
            }
            for r in cur.fetchall()
        ]


@app.get("/api/decisions/{subject_id}")
def decisions(subject_id: str, limit: int = 40):
    with blast.pool().connection() as c:
        cur = c.cursor()
        cur.execute(
            """
            SELECT id, prompt, action, amount, rationale, model, read_hlc, decided_at
              FROM decisions WHERE subject_id = %s
             ORDER BY decided_at DESC LIMIT %s
            """,
            (subject_id, limit),
        )
        return [
            {
                "id": str(r[0]), "prompt": r[1], "action": r[2],
                "amount": float(r[3]) if r[3] is not None else None,
                "rationale": r[4], "model": r[5], "read_hlc": str(r[6]),
                "decided_at": r[7].isoformat(),
            }
            for r in cur.fetchall()
        ]


@app.get("/api/decision/{decision_id}")
def decision_detail(decision_id: str):
    """The decision plus the exact evidence it retrieved, in rank order."""
    with blast.pool().connection() as c:
        cur = c.cursor()
        cur.execute(
            "SELECT id, subject_id, prompt, action, amount, rationale, model, "
            "read_hlc, decided_at FROM decisions WHERE id = %s",
            (decision_id,),
        )
        d = cur.fetchone()
        if d is None:
            raise HTTPException(404, "no such decision")
        cur.execute(
            """
            SELECT b.id, b.content, b.source, b.trust, db.rank, db.distance,
                   b.quarantined_at IS NOT NULL AS quarantined
              FROM decision_beliefs db JOIN beliefs b ON b.id = db.belief_id
             WHERE db.decision_id = %s ORDER BY db.rank
            """,
            (decision_id,),
        )
        evidence = [
            {"id": str(r[0]), "content": r[1], "source": r[2], "trust": float(r[3]),
             "rank": r[4], "distance": float(r[5]), "quarantined": r[6]}
            for r in cur.fetchall()
        ]
    return {
        "id": str(d[0]), "subject_id": d[1], "prompt": d[2], "action": d[3],
        "amount": float(d[4]) if d[4] is not None else None, "rationale": d[5],
        "model": d[6], "read_hlc": str(d[7]), "decided_at": d[8].isoformat(),
        "evidence": evidence,
    }


@app.post("/api/ask")
def ask(body: Ask):
    conn = blast.pool().getconn()
    try:
        store = BeliefStore(conn)
        did, dec, retrieval = Agent(store).decide_and_record(
            body.subject_id, body.prompt, amount=body.amount
        )
        return {
            "decision_id": did, "action": dec.action, "rationale": dec.rationale,
            "model": dec.model, "read_hlc": str(retrieval.hlc),
            "evidence": [
                {"id": b.belief_id, "content": b.content, "source": b.source,
                 "rank": b.rank, "distance": b.distance}
                for b in retrieval.beliefs
            ],
        }
    finally:
        blast.pool().putconn(conn)


@app.post("/api/assert")
def assert_belief(body: Assert):
    """Write a belief THROUGH the interdiction gate.

    The gate runs before the belief is trusted, which is the whole point: a
    statement that would rewrite a large share of settled history is refused at
    the door rather than discovered in a postmortem.
    """
    conn = blast.pool().getconn()
    try:
        store = BeliefStore(conn)
        gate = Interdictor(store, Agent(store))
        belief_id, verdict = gate.admit(
            body.subject_id, body.content, body.source, body.trust
        )
        return {
            "belief_id": belief_id,
            "admitted": verdict.admitted,
            "reason": verdict.reason,
            "examined": verdict.examined,
            "would_retrieve": verdict.would_retrieve,
            "would_flip": len(verdict.flips),
            "flip_rate": round(100 * verdict.flip_rate, 1),
            "exposure": verdict.exposure,
        }
    finally:
        blast.pool().putconn(conn)


@app.get("/api/replay/{decision_id}")
def replay(decision_id: str, exclude: str = ""):
    """Re-run a decision at its own recorded instant.

    With no exclusions this must reproduce the original action exactly. With a
    belief excluded it becomes the counterfactual.
    """
    drop = frozenset(x for x in exclude.split(",") if x)
    conn = blast.pool().getconn()
    try:
        agent = Agent(BeliefStore(conn))
        dec, retrieval = agent.replay(decision_id, exclude=drop)
        cur = conn.cursor()
        cur.execute("SELECT action, rationale FROM decisions WHERE id=%s", (decision_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "no such decision")
        return {
            "original": {"action": row[0], "rationale": row[1]},
            "replayed": {"action": dec.action, "rationale": dec.rationale},
            "flipped": dec.action != row[0],
            "excluded": sorted(drop),
            "anchor_hlc": str(retrieval.hlc),
            "evidence": [
                {"id": b.belief_id, "content": b.content, "rank": b.rank,
                 "distance": b.distance}
                for b in retrieval.beliefs
            ],
        }
    finally:
        blast.pool().putconn(conn)


@app.get("/api/blast/{belief_id}")
def blast_radius(belief_id: str, workers: int = 12):
    r = blast.compute([x for x in belief_id.split(",") if x], workers=workers)
    return {
        "belief_ids": r.belief_ids,
        "total_decisions": r.total_decisions,
        "touched": r.candidates,
        "replayed": r.replayed,
        "flipped": len(r.flips),
        "exposure": r.exposure,
        "wall_ms": round(r.wall_ms),
        "throughput": round(r.throughput, 1),
        "p50_ms": round(r.percentile(50)),
        "p95_ms": round(r.percentile(95)),
        "p99_ms": round(r.percentile(99)),
        "flips": [
            {"decision_id": f.decision_id, "subject_id": f.subject_id,
             "was": f.was, "now": f.now, "amount": f.amount, "prompt": f.prompt}
            for f in r.flips[:50]
        ],
    }


@app.post("/api/quarantine/{belief_id}")
def quarantine(belief_id: str, reason: str = "quarantined from console"):
    conn = blast.pool().getconn()
    try:
        BeliefStore(conn).quarantine(belief_id, reason)
        return {"ok": True, "belief_id": belief_id, "reason": reason}
    finally:
        blast.pool().putconn(conn)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
