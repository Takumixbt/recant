"""Make the vector index usable: fold liveness into the index prefix."""
import os, sys; sys.path.insert(0, ".")
import psycopg
from dotenv import load_dotenv
from recant.embed import LocalEmbedder, to_pgvector
load_dotenv()

with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
    cur = conn.cursor()
    cur.execute("SET CLUSTER SETTING feature.vector_index.enabled = true")
    cur.execute("SET sql_safe_updates = false")

    print("--- add a STORED computed liveness column ---")
    try:
        cur.execute("""ALTER TABLE beliefs ADD COLUMN IF NOT EXISTS live BOOL NOT NULL
                       AS (quarantined_at IS NULL AND valid_to IS NULL) STORED""")
        print("  ok")
    except Exception as e:
        print("  FAIL:", str(e).splitlines()[0][:140]); sys.exit(1)

    print("--- create vector index with (subject_id, live) as prefix ---")
    try:
        cur.execute("""CREATE VECTOR INDEX IF NOT EXISTS beliefs_live_embedding
                       ON beliefs (subject_id, live, embedding vector_cosine_ops)""")
        print("  ok")
    except Exception as e:
        print("  FAIL:", str(e).splitlines()[0][:180]); sys.exit(1)

    e = LocalEmbedder(); v = to_pgvector(e.embed("should I approve a large refund?"))
    S = "cust_00000"
    sql = ("SELECT id FROM beliefs WHERE subject_id=%s AND live "
           "ORDER BY embedding <=> %s::VECTOR(1024), id LIMIT 5")
    cur.execute("EXPLAIN " + sql, (S, v))
    rows = [r[0] for r in cur.fetchall()]
    used = any("beliefs_live_embedding" in r or "vector" in r.lower() for r in rows)
    print(f"\n--- plan uses vector index = {used} ---")
    for r in rows[:14]:
        if r.strip(): print("   ", r)
