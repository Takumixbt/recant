-- RECANT :: bitemporal belief store with a decision ledger anchored to MVCC.
--
-- Two independent time axes:
--   valid time       (valid_from / valid_to)  when the fact held in the world
--   transaction time (MVCC, via AS OF SYSTEM TIME) when the system believed it
--
-- The second axis costs us nothing to maintain: CockroachDB already keeps it.
-- That is the whole reason this design is possible here and nowhere else.

CREATE TABLE IF NOT EXISTS beliefs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_id        STRING NOT NULL,           -- whose belief this is
    content           STRING NOT NULL,
    kind              STRING NOT NULL DEFAULT 'fact',
    source            STRING NOT NULL,           -- agent | user:<id> | import
    trust             FLOAT NOT NULL DEFAULT 0.5,
    embedding         VECTOR(1024),

    -- valid time. Contradiction closes the interval; nothing is ever deleted.
    valid_from        TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to          TIMESTAMPTZ,               -- NULL = still believed

    -- quarantine is a soft close: the belief stops being retrievable but its
    -- row, its edges, and its history all survive for audit.
    quarantined_at    TIMESTAMPTZ,
    quarantine_reason STRING,

    created_hlc       DECIMAL NOT NULL,          -- set explicitly on insert
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Prefix column first: retrieval is always scoped to one subject, and a vector
-- index is only used when its prefix columns are constrained with = or IN.
CREATE VECTOR INDEX IF NOT EXISTS beliefs_subject_embedding
    ON beliefs (subject_id, embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS beliefs_subject_live
    ON beliefs (subject_id, valid_to) STORING (content, source, trust);


-- Every decision records the exact HLC its retrieval read at. That single
-- decimal is what makes the past reconstructible.
CREATE TABLE IF NOT EXISTS decisions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_id  STRING NOT NULL,
    agent       STRING NOT NULL DEFAULT 'support-agent',
    prompt      STRING NOT NULL,
    action      STRING NOT NULL,       -- e.g. approve_refund | decline | escalate
    amount      DECIMAL,               -- exposure, for ranking blast radius
    rationale   STRING,
    model       STRING,
    read_hlc    DECIMAL NOT NULL,      -- <<< the anchor
    decided_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS decisions_subject_time
    ON decisions (subject_id, decided_at DESC);


-- Which beliefs each decision actually retrieved, and where they ranked.
-- The reverse index is what makes blast radius cheap: finding every decision
-- touched by one belief is an index lookup, not a scan over history.
CREATE TABLE IF NOT EXISTS decision_beliefs (
    decision_id UUID NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    belief_id   UUID NOT NULL,
    rank        INT NOT NULL,
    distance    FLOAT NOT NULL,
    PRIMARY KEY (decision_id, belief_id)
);

CREATE INDEX IF NOT EXISTS decision_beliefs_by_belief
    ON decision_beliefs (belief_id);


-- Append-only log. MVCC gives us perfect replay inside the GC window
-- (gc.ttlseconds, currently 24h); this log is how replay reaches past it.
CREATE TABLE IF NOT EXISTS memory_events (
    seq        INT8 PRIMARY KEY DEFAULT unique_rowid(),
    hlc        DECIMAL NOT NULL,
    op         STRING NOT NULL,        -- assert | quarantine | erase
    belief_id  UUID NOT NULL,
    subject_id STRING NOT NULL,
    payload    JSONB,
    at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS memory_events_subject ON memory_events (subject_id, seq);


-- Crypto-shredding. Erasure destroys the key, so even MVCC history holds only
-- ciphertext. What survives is the belief's existence, its timestamps, and its
-- edges to decisions -- provable influence without readable content.
CREATE TABLE IF NOT EXISTS subject_keys (
    subject_id   STRING PRIMARY KEY,
    key_material BYTES,                -- NULL once erased
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    erased_at    TIMESTAMPTZ
);
