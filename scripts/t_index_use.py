"""Which query shape actually gets the vector index?"""
import os, sys; sys.path.insert(0, ".")
import psycopg
from dotenv import load_dotenv
from recant.embed import LocalEmbedder, to_pgvector
load_dotenv()
e = LocalEmbedder(); v = to_pgvector(e.embed("should I approve a large refund?"))
S = "cust_00000"

VARIANTS = {
 "bare: subject only, no filters":
   ("SELECT id FROM beliefs WHERE subject_id=%s ORDER BY embedding <=> %s::VECTOR(1024) LIMIT 5", (S, v)),
 "subject + live=true":
   ("SELECT id FROM beliefs WHERE subject_id=%s AND live=true ORDER BY embedding <=> %s::VECTOR(1024) LIMIT 5", (S, v)),
 "subject + live=true + id tiebreak":
   ("SELECT id FROM beliefs WHERE subject_id=%s AND live=true ORDER BY embedding <=> %s::VECTOR(1024), id LIMIT 5", (S, v)),
 "subject + IS NULL filters (current code)":
   ("SELECT id FROM beliefs WHERE subject_id=%s AND quarantined_at IS NULL AND valid_to IS NULL ORDER BY embedding <=> %s::VECTOR(1024) LIMIT 5", (S, v)),
}
with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM beliefs")
    print(f"beliefs in table: {cur.fetchone()[0]:,}")
    cur.execute("SELECT count(*) FROM beliefs WHERE subject_id=%s", (S,))
    print(f"beliefs for {S}: {cur.fetchone()[0]}\n")
    for label, (sql, params) in VARIANTS.items():
        cur.execute("EXPLAIN " + sql, params)
        rows = [r[0] for r in cur.fetchall()]
        blob = " ".join(rows).lower()
        used = "beliefs_live_embedding" in blob or "beliefs_subject_embedding" in blob
        scan = [r.strip() for r in rows if "table:" in r or "vector" in r.lower()]
        print(f"  vector index used = {str(used):<5}  {label}")
        for s in scan[:2]:
            print(f"        {s}")
