# recant

**Before you change what an AI believes, this shows you every past decision that
would have come out differently.**

Built for the [CockroachDB × AWS Hackathon: Build with Agentic
Memory](https://cockroachdb-ai.devpost.com/).

**Live demo:** http://ec2-100-52-174-127.compute-1.amazonaws.com

---

## The problem

AI agents remember things about you between conversations — "this account is
verified", "refunds over $500 need a hold" — and those memories decide what the
agent does. Three things are broken about that everywhere:

1. **You can't see them.** When an agent does something wrong, nobody can say
   which stored memory caused it.
2. **Nobody checks them.** A memory is written once and silently steers every
   later decision. Anyone who talks to the agent can plant one.
3. **You can't safely fix them.** Delete a bad memory and you have no idea what
   else you just changed.

recant makes agent memory auditable: every decision records the exact database
timestamp its retrieval read at, so any past decision can be reconstructed
exactly, re-run against a changed past, or evaluated in bulk.

---

## What it does

| Capability | What it answers |
|---|---|
| **Exact replay** | What did the agent actually see when it decided this? |
| **Counterfactual** | Would it have decided differently without this one memory? |
| **Blast radius** | What does changing this memory do to *everything already decided*? |
| **Write-time interdiction** | Should this new memory be trusted at all? |

---

## Measured results

All figures below come from runs against a live CockroachDB Cloud cluster with
**18,021 beliefs and 1,080 decisions**. Reproduce them with the scripts named.

### Blast radius — `python scripts/verify_story.py`

| | decisions touched | flipped | exposure |
|---|---|---|---|
| One planted belief | 40 | **40 (100%)** | $46,160 |
| Six-belief campaign | 240 of 1,080 | **240 (100%)** | **$286,120** |
| Legitimate policy belief *(control)* | 120 | **0 (0%)** | $0 |

The control matters more than the headline. A real belief gets retrieved and
changes nothing; a poisoned one flips everything it touches. That separation is
a property of the data, not a tuned threshold.

### Write-time interdiction — `python scripts/t_gate.py`

```
THE ATTACK   REJECTED   retrieved by 40/40 past decisions, flips 52.5%, $19,640
control 1    admitted   would never have been retrieved
control 2    admitted   would never have been retrieved
control 3    admitted   would never have been retrieved
```

### Falsification benchmark — `python bench/falsification.py`

The common way to build agent memory writes the row now and embeds it
asynchronously, then reconstructs replay by filtering on wall-clock time. In the
window between those two events a belief exists but is not retrievable, so a
decision made then genuinely did not see it. The audit runs later, once the
pipeline has caught up, and the wall-clock filter now includes it.

```
common architecture (async embedding + wall-clock replay)
  decisions audited          15
  replay was WRONG            8   (53%)
  fabricated evidence items   8

recant (transactional embedding + MVCC replay)
  replay was WRONG            0   (0%)
  fabricated evidence items   0
```

**That architecture fabricates evidence in 53% of audits** — it names specific
beliefs and asserts the agent used them. recant cannot exhibit this: the
embedding commits in the same transaction as the belief, so the window does not
exist.

---

## Why CockroachDB

Not incidental. Each of these is load-bearing:

- **`AS OF SYSTEM TIME`** — replay is not a log we wrote, it is the same read
  served again at the timestamp the agent read at.
- **Distributed vector indexing** — verified in use (`lookup join` on
  `beliefs_live_embedding`). Measured crossover: at 200 beliefs per subject the
  optimizer correctly *scans*; at 1,000+ it chooses the index.
- **Transactional vector writes** — belief and embedding commit together, which
  is precisely what the falsification benchmark shows the alternative getting
  wrong.
- **Serializable isolation** — concurrent agents writing shared memory get
  retries, not lost updates. `retry_txn()` in `recant/store.py`.

### Three findings worth reading

**1. The replay window is not bounded by the TTL you set.** `AS OF SYSTEM TIME`
must also read table *descriptors* at the target timestamp, and those live in
system ranges pinned at `gc.ttlseconds=4500`. A non-system tenant cannot raise
them. A table set to 24 hours still fails replay after ~75 minutes. recant falls
back to the append-only log, which works because the schema never deletes and
embeddings are immutable — *a belief was live at T iff it was asserted at or
before T and not quarantined at or before T*. Both paths are verified to return
identical results (`scripts/t_paths_agree.py`).

**2. Cosine ties were breaking replay.** Without a total order, tied distances
were resolved by scan order, so replay could disagree with the retrieval it was
reproducing. The `, id` tiebreak in `recant/store.py` is load-bearing.

**3. Pooling matters more than parallelism.** Opening a TLS connection per
worker per belief made a 24-worker fan-out *slower* than an 8-worker one
(650s vs 264s). Pooling brought it to 126s.

---

## Architecture

```
   attacker / user / import
             │
             ▼
   ┌──────────────────┐   rejected if it would rewrite
   │  interdiction    │──▶ too much settled history
   └────────┬─────────┘
            ▼
   ┌──────────────────────────────────────────┐
   │  CockroachDB                             │
   │    beliefs   (bitemporal + VECTOR(1024)) │
   │    decisions (anchored to read HLC)      │
   │    decision_beliefs (evidence edges)     │
   │    memory_events    (append-only log)    │
   └────────┬─────────────────────────────────┘
            │ AS OF SYSTEM TIME  /  log replay
            ▼
   ┌──────────────────┐
   │  replay engine   │──▶ exact replay · counterfactual · blast radius
   └──────────────────┘
```

---

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env            # add your CockroachDB connection string
python scripts/init_db.py       # schema + vector index
python scripts/demo_seed.py     # deep memory, decisions, and the attack
python -m uvicorn app.api:app --port 8100
```

Then open <http://127.0.0.1:8100>.

Verify the claims:

```bash
python bench/falsification.py   # 53% vs 0%
python scripts/verify_story.py  # blast radius + control
python scripts/t_gate.py        # interdiction
python scripts/t_paths_agree.py # MVCC and log replay agree
```

---

## Honest limitations

- **Bedrock is not in the live path.** This AWS account's Bedrock quota never
  opened (`ThrottlingException: Too many tokens per day` on a brand-new
  account), so embeddings run on `BAAI/bge-small-en-v1.5` locally and the agent
  policy is deterministic rather than an LLM. Both are behind provider
  interfaces — `EMBED_PROVIDER=bedrock` and `MODEL_PROVIDER=bedrock` switch them
  with no other change, and `scripts/bedrock_check.py` verifies access.
- **Embeddings are 384-dimensional, zero-padded to 1024.** Padding changes
  neither dot products nor norms, so distances are identical to native 384 while
  the schema stays compatible with Titan's native 1024.
- **Latency figures are from the free serverless tier** and reflect its
  throttling, not the design. Blast radius runs at ~4.5 replays/sec there.
- **Multi-region data domiciling and crypto-shredded erasure are designed, not
  built.** The schema carries `subject_keys` for the latter. Neither is claimed
  as working.
- **The agent policy is a deterministic rule**, not a model. It is genuinely
  fooled by the poisoned belief, which is the property the demo needs, but it is
  not an LLM.

---

## License

Apache 2.0.
