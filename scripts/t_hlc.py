"""Isolate why the write transaction cannot commit."""
import os, sys
sys.path.insert(0, ".")
import psycopg
from dotenv import load_dotenv
from recant.embed import LocalEmbedder, to_pgvector
load_dotenv()
S = "hlc_probe"
e = LocalEmbedder(); v = to_pgvector(e.embed("probe text"))

with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
    cur = conn.cursor()
    cur.execute("DELETE FROM beliefs WHERE subject_id=%s", (S,))

    def attempt(label, use_hlc):
        try:
            with conn.transaction():
                if use_hlc:
                    cur.execute("SELECT cluster_logical_timestamp()")
                    hlc = cur.fetchone()[0]
                else:
                    hlc = 0
                cur.execute(
                    "INSERT INTO beliefs (subject_id,content,source,embedding,created_hlc) "
                    "VALUES (%s,%s,%s,%s::VECTOR(1024),%s) RETURNING id",
                    (S, "probe", "test", v, hlc))
                cur.fetchone()
            print(f"  {label:<46} OK")
            return True
        except Exception as ex:
            print(f"  {label:<46} {type(ex).__name__}")
            return False

    print("--- insert with vs without cluster_logical_timestamp() in txn ---")
    for i in range(3):
        attempt(f"[{i}] WITH cluster_logical_timestamp()", True)
    for i in range(3):
        attempt(f"[{i}] WITHOUT (hlc precomputed outside txn)", False)

    print("\n--- is crdb_internal_mvcc_timestamp readable? ---")
    try:
        cur.execute("SELECT id, crdb_internal_mvcc_timestamp FROM beliefs "
                    "WHERE subject_id=%s LIMIT 2", (S,))
        for r in cur.fetchall():
            print(f"  {str(r[0])[:8]}  mvcc={r[1]}")
        print("  -> true commit HLC is available per row, no manual capture needed")
    except Exception as ex:
        print("  unavailable:", str(ex)[:110])

    print("\n--- read-only txn with cluster_logical_timestamp() (the anchor path) ---")
    for i in range(3):
        try:
            with conn.transaction():
                cur.execute("SELECT cluster_logical_timestamp()")
                h = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM beliefs WHERE subject_id=%s", (S,))
                cur.fetchone()
            print(f"  [{i}] read-only anchor capture           OK  hlc={h}")
        except Exception as ex:
            print(f"  [{i}] read-only anchor capture           {type(ex).__name__}")

    cur.execute("DELETE FROM beliefs WHERE subject_id=%s", (S,))
