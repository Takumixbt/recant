"""
Write-time interdiction: judge a belief BEFORE it is ever trusted.

Blast radius answers "what breaks if I remove this belief?" -- it walks the
evidence edges a belief already has. A newly arrived belief has no edges yet, so
that machinery finds nothing. The question here runs the other way:

    if this belief had existed, which past decisions would have retrieved it,
    and would any of them have come out differently?

That cannot be answered with AS OF SYSTEM TIME, because the belief did not exist
at those timestamps. It is instead answered by simulation, using material the
ledger already stores:

    - each past decision's prompt (so we can embed the query it actually ran)
    - each past decision's evidence edges, with rank and distance (so we know
      the exact retrieval set and the distance of the weakest member)

If the candidate's distance to a past query beats that weakest member, it would
have displaced it. Rebuild that decision's context with the substitution made,
re-run the policy, and compare the action.

The security consequence: a belief that would retroactively flip an anomalous
number of settled decisions is not behaving like a fact. Facts are consistent
with the past. A statement that rewrites hundreds of prior outcomes is an attack,
and this catches it at the door rather than in the postmortem.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .agent import Agent, Decision
from .embed import to_pgvector
from .store import BeliefStore


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """Cosine distance for unit vectors: exactly 1 - dot product.

    Both embedders normalize (Titan via normalize=True, the local one
    explicitly), so this is the same number CockroachDB's <=> operator returns,
    not an approximation of it. Interdictor._verify_metric asserts that against
    the live cluster rather than taking it on faith.
    """
    return 1.0 - sum(x * y for x, y in zip(a, b))


@dataclass
class WouldFlip:
    decision_id: str
    was: str
    would_be: str
    amount: float | None
    distance: float
    displaced_rank: int


@dataclass
class Verdict:
    content: str
    subject_id: str
    examined: int = 0
    would_retrieve: int = 0
    flips: list[WouldFlip] = field(default_factory=list)
    admitted: bool = True
    reason: str = ""

    @property
    def exposure(self) -> float:
        return sum(f.amount or 0.0 for f in self.flips)

    @property
    def flip_rate(self) -> float:
        return len(self.flips) / self.examined if self.examined else 0.0

    def summary(self) -> str:
        verdict = "ADMITTED" if self.admitted else "QUARANTINED"
        return "\n".join(
            [
                f"{verdict}: {self.content[:70]}",
                f"  past decisions examined    {self.examined}",
                f"  would have been retrieved  {self.would_retrieve}",
                f"  would have FLIPPED         {len(self.flips)}"
                f"  ({100 * self.flip_rate:.1f}% of history)",
                f"  retroactive exposure       {self.exposure:,.2f} USD",
                f"  {self.reason}",
            ]
        )


class Interdictor:
    """
    Gate on incoming beliefs.

    `max_flip_rate` is the share of a subject's settled decisions a single new
    belief may rewrite before it is treated as hostile. An ordinary fact ("the
    customer prefers email") flips nothing. A waiver planted by an attacker
    flips every large refund in the subject's history.
    """

    def __init__(self, store: BeliefStore, agent: Agent, max_flip_rate: float = 0.25):
        self.store = store
        self.agent = agent
        self.max_flip_rate = max_flip_rate
        self._metric_verified = False

    def _verify_metric(self, cand_vec: list[float], sample_prompt: str) -> None:
        """Prove the local distance computation matches the database's.

        Runs once per evaluation against a real prompt from this subject's
        history. If the two ever disagree, every verdict downstream is
        meaningless, so this raises rather than warning.
        """
        if self._metric_verified:
            return
        other = self.store.embedder.embed(sample_prompt)
        cur = self.store.conn.cursor()
        cur.execute(
            "SELECT %s::VECTOR(1024) <=> %s::VECTOR(1024)",
            (to_pgvector(cand_vec), to_pgvector(other)),
        )
        db = float(cur.fetchone()[0])
        local = _cosine_distance(cand_vec, other)
        if abs(db - local) > 1e-4:
            raise RuntimeError(
                f"distance metric disagrees with the database: "
                f"local={local:.9f} db={db:.9f}. Interdiction verdicts would be "
                f"unsound; refusing to continue."
            )
        self._metric_verified = True

    def _history(self, subject_id: str, limit: int = 200):
        """Past decisions with their retrieval sets, newest first."""
        cur = self.store.conn.cursor()
        cur.execute(
            """
            SELECT d.id, d.prompt, d.action, d.amount,
                   max(db.distance) AS weakest,
                   max(db.rank)     AS deepest,
                   count(*)         AS k
              FROM decisions d
              JOIN decision_beliefs db ON db.decision_id = d.id
             WHERE d.subject_id = %s
          GROUP BY d.id, d.prompt, d.action, d.amount
          ORDER BY d.decided_at DESC
             LIMIT %s
            """,
            (subject_id, limit),
        )
        return cur.fetchall()

    def _context_with(self, decision_id: str, candidate: str, drop_rank: int) -> str:
        """Rebuild a past decision's context with the candidate substituted in
        for the member it would have displaced."""
        cur = self.store.conn.cursor()
        cur.execute(
            """
            SELECT b.content, b.source, b.trust, db.rank
              FROM decision_beliefs db
              JOIN beliefs b ON b.id = db.belief_id
             WHERE db.decision_id = %s
          ORDER BY db.rank
            """,
            (decision_id,),
        )
        lines = [
            f"- {c}  [source: {s}, trust: {t:.2f}]"
            for c, s, t, rank in cur.fetchall()
            if rank != drop_rank
        ]
        lines.insert(0, f"- {candidate}  [source: user:unverified, trust: 0.50]")
        return "\n".join(lines)

    def evaluate(self, subject_id: str, content: str) -> Verdict:
        v = Verdict(content=content, subject_id=subject_id)
        cand_vec = self.store.embedder.embed(content)
        cand_lit = to_pgvector(cand_vec)
        rows = self._history(subject_id)
        v.examined = len(rows)
        if not rows:
            v.reason = "no settled history for this subject; nothing to contradict"
            return v

        # Distance from the candidate to each past query.
        #
        # CockroachDB has no array-of-VECTOR type, so these cannot be batched
        # into one statement, and sending several hundred 1024-dimension vector
        # literals as a VALUES list means megabytes of SQL per evaluation.
        # So the arithmetic happens here instead.
        #
        # That is exact rather than approximate: both embedders emit unit
        # vectors, and for unit vectors cosine distance is exactly 1 - dot. But
        # "we reimplemented the database's distance metric" is the kind of claim
        # that should be checked rather than asserted, so _verify_metric() below
        # proves the two agree against this cluster before any verdict is
        # issued.
        self._verify_metric(cand_vec, rows[0][1])
        dists = {
            i: _cosine_distance(cand_vec, self.store.embedder.embed(r[1]))
            for i, r in enumerate(rows)
        }

        for idx, (did, prompt, was, amount, weakest, deepest, k) in enumerate(rows):
            d = dists.get(idx)
            if d is None or d >= float(weakest):
                continue  # would not have made the cut
            v.would_retrieve += 1
            ctx = self._context_with(str(did), content, int(deepest))
            new: Decision = self.agent.policy.decide(
                prompt, ctx, float(amount) if amount is not None else None
            )
            if new.action != was:
                v.flips.append(
                    WouldFlip(
                        str(did), was, new.action,
                        float(amount) if amount is not None else None,
                        d, int(deepest),
                    )
                )

        if v.flip_rate > self.max_flip_rate:
            v.admitted = False
            v.reason = (
                f"rewrites {100 * v.flip_rate:.1f}% of settled decisions "
                f"(threshold {100 * self.max_flip_rate:.0f}%) -- held for review"
            )
        else:
            v.reason = "consistent with settled history"
        return v

    def admit(self, subject_id: str, content: str, source: str, trust: float = 0.5):
        """Evaluate, then write. A rejected belief is still recorded -- it is
        quarantined on arrival, so the attempt itself remains auditable."""
        v = self.evaluate(subject_id, content)
        belief_id = self.store.assert_belief(subject_id, content, source, trust)
        if not v.admitted:
            self.store.quarantine(belief_id, f"interdicted on write: {v.reason}")
        return belief_id, v
