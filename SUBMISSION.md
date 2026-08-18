# Devpost submission — copy/paste fields

**Repo:** https://github.com/Takumixbt/recant

---

## Project name

recant

## Tagline (one line)

Before you change what an AI believes, see every past decision that would have
come out differently.

---

## Inspiration

Agents remember things about you between conversations, and those memories decide
what they do. Nobody can see them, nobody checks them, and nobody knows what
breaks if you change one. We ship code through review, tests, and rollback. We
ship *beliefs* into production agents with none of that.

## What it does

recant makes agent memory auditable. Every decision records the exact database
timestamp its memory lookup read at, which makes four things possible:

- **Exact replay** — reconstruct precisely what the agent saw when it decided.
- **Counterfactual** — remove one memory, re-run that decision against the same
  instant in the past, see whether it flips.
- **Blast radius** — the question nobody can answer today: what does changing
  this memory do to *everything already decided*?
- **Write-time interdiction** — refuse a memory that would retroactively rewrite
  too much settled history.

## How we built it

CockroachDB stores beliefs bitemporally with their embeddings in the same
transaction, and every decision is anchored to the cluster HLC its retrieval read
at. Replay uses `AS OF SYSTEM TIME` to serve that same read again. Blast radius
resolves candidate decisions through a reverse index on the evidence edges, then
replays only those in parallel, each at its own point in history.

## Challenges we ran into

**The audit trail that lies.** The common architecture — write the row, embed
asynchronously, reconstruct replay by wall-clock time — fabricates evidence.
Between the row landing and the embedding landing, a belief exists but is not
retrievable, so a decision made then did not see it. The audit runs later, the
pipeline has caught up, and the wall-clock filter now includes it. We benchmarked
it: **53% of its audit records describe a retrieval that never happened.**

**The replay window is not the one you configure.** `AS OF SYSTEM TIME` must also
read table descriptors at the target timestamp, and those live in system ranges
pinned at 4500 seconds that a non-system tenant cannot raise. A table set to 24
hours still fails replay after ~75 minutes. We added an append-only log fallback
and verified both paths return identical results.

**The demo failed its first honest test.** With real embeddings and 1,200
beliefs per subject, our original poisoned belief ranked ~12th and was never
retrieved — so blast radius was correctly zero. A real memory-poisoning attack
doesn't plant a generic lie; it plants one shaped like the governing policy so it
surfaces beside the real one. Rewriting it moved the poison from rank 12 to
rank 1.

## Accomplishments we're proud of

Measured on 18,021 beliefs and 1,080 decisions: a poisoned belief flips **100%**
of the decisions it touches ($286,120 exposure across the campaign), while a
legitimate policy belief touches 120 decisions and flips **0%**. That separation
is a property of the data, not a threshold we tuned.

## What we learned

Below ~1,000 beliefs per subject CockroachDB correctly *ignores* the vector index
and scans, because scanning is genuinely faster at that size. Claiming a feature
your query plan doesn't use is one `EXPLAIN` away from being caught.

## What's next

Multi-region data domiciling (EU subjects' beliefs pinned to Frankfurt while the
agent runs globally) and crypto-shredded erasure — destroy the per-subject key so
a belief's existence and influence remain provable while its content becomes
unrecoverable. Both are designed; neither is built.

---

## CockroachDB features used

- **Distributed vector indexing** — `VECTOR(1024)` with a
  `(subject_id, live, embedding vector_cosine_ops)` index, verified in use
  (`lookup join` on `beliefs_live_embedding`).
- **`AS OF SYSTEM TIME`** — the entire replay and counterfactual engine.
- **Serializable transactions** — concurrent memory writes get retries, not lost
  updates.
- **`crdb_internal_mvcc_timestamp`** — authoritative per-row commit time from the
  storage engine.
- **CockroachDB Cloud MCP Server** — connected for investigation queries.

## AWS services used

- **Amazon Bedrock** — Titan Text Embeddings V2 and Claude, integrated behind a
  provider interface (`recant/embed.py`, `recant/agent.py`). This account's
  Bedrock quota did not open during the hackathon, so the live path runs local
  models; `scripts/bedrock_check.py` verifies access and
  `EMBED_PROVIDER=bedrock` switches over.
- **AWS Lambda / S3** — deployment target for the console.

---

## Try it

```bash
git clone https://github.com/Takumixbt/recant && cd recant
pip install -r requirements.txt && cp .env.example .env   # add your connection string
python scripts/init_db.py && python scripts/demo_seed.py
python -m uvicorn app.api:app --port 8100
```

Verify every claim in the README:

```bash
python bench/falsification.py    # 53% vs 0%
python scripts/verify_story.py   # blast radius + control
python scripts/t_gate.py         # interdiction
```
