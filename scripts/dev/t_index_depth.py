"""At what memory depth does the optimizer choose the vector index?"""
import os, sys, time; sys.path.insert(0, ".")
import psycopg
from dotenv import load_dotenv
from recant.embed import LocalEmbedder, to_pgvector
load_dotenv()
S = "depth_probe"
e = LocalEmbedder()
q = to_pgvector(e.embed("should I approve a large refund for this customer?"))

def plan_uses_index(cur):
    cur.execute("EXPLAIN SELECT id FROM beliefs WHERE subject_id=%s AND live=true "
                "ORDER BY embedding <=> %s::VECTOR(1024) LIMIT 5", (S, q))
    blob = " ".join(r[0] for r in cur.fetchall()).lower()
    return ("beliefs_live_embedding" in blob or "beliefs_subject_embedding" in blob), blob

with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
    cur = conn.cursor()
    cur.execute("DELETE FROM beliefs WHERE subject_id=%s", (S,))
    cur.execute("SET CLUSTER SETTING feature.vector_index.enabled = true")
    total = 0
    for target in (200, 1000, 3000):
        batch = target - total
        print(f"\ninserting {batch} beliefs (subject depth -> {target}) ...")
        t0 = time.perf_counter()
        rows, params = [], []
        for i in range(total, target):
            rows.append("(%s,%s,'fact','seed',0.5,%s::VECTOR(1024),0)")
            params += [S, f"memory item {i}: interaction note number {i} about billing",
                       to_pgvector(e.embed(f"memory item {i}: interaction note number {i} about billing"))]
            if len(rows) == 100:
                cur.execute("INSERT INTO beliefs (subject_id,content,kind,source,trust,embedding,created_hlc) "
                            "VALUES " + ",".join(rows), params)
                rows, params = [], []
        if rows:
            cur.execute("INSERT INTO beliefs (subject_id,content,kind,source,trust,embedding,created_hlc) "
                        "VALUES " + ",".join(rows), params)
        total = target
        cur.execute("ANALYZE beliefs")
        used, blob = plan_uses_index(cur)
        print(f"  depth={target:<6} inserted in {time.perf_counter()-t0:.0f}s   vector index used = {used}")
        if not used:
            key = [x for x in ("full scan","index join","top-k","scan") if x in blob]
            print(f"     plan mentions: {key}")
        if used:
            break
    cur.execute("DELETE FROM beliefs WHERE subject_id=%s", (S,))
    print("\ncleaned up")
