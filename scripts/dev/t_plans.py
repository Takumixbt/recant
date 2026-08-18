"""Do the live and historical retrieval paths use the SAME access method?"""
import sys; sys.path.insert(0, ".")
from recant.store import BeliefStore, connect
from recant.embed import to_pgvector

S = "plan_probe"
Q = "should I approve a large refund for this customer?"
with connect() as conn:
    cur = conn.cursor()
    cur.execute("DELETE FROM beliefs WHERE subject_id=%s", (S,))
    st = BeliefStore(conn)
    for i in range(40):
        st.assert_belief(S, f"belief {i}: customer has escalation flag {i} set", "bench")
    v = to_pgvector(st.embedder.embed(Q))
    cur.execute("SELECT cluster_logical_timestamp()")
    hlc = cur.fetchone()[0]

    def plan(label, sql, params):
        cur.execute("EXPLAIN " + sql, params)
        rows = [r[0] for r in cur.fetchall()]
        uses = any("vector" in r.lower() or "beliefs_subject_embedding" in r for r in rows)
        print(f"\n{label}: uses vector index = {uses}")
        for r in rows[:12]:
            if r.strip():
                print("   ", r)

    live_sql = ("SELECT id FROM beliefs WHERE subject_id=%s AND quarantined_at IS NULL "
                "AND valid_to IS NULL ORDER BY embedding <=> %s::VECTOR(1024) LIMIT 5")
    hist_sql = (f"SELECT id FROM beliefs AS OF SYSTEM TIME {hlc} WHERE subject_id=%s "
                "AND quarantined_at IS NULL AND valid_to IS NULL "
                "ORDER BY embedding <=> %s::VECTOR(1024) LIMIT 5")
    plan("LIVE", live_sql, (S, v))
    plan("HISTORICAL (AS OF SYSTEM TIME)", hist_sql, (S, v))

    print("\n--- do they return the same rows? ---")
    cur.execute(live_sql, (S, v)); a = [str(r[0]) for r in cur.fetchall()]
    cur.execute(hist_sql, (S, v)); b = [str(r[0]) for r in cur.fetchall()]
    print("   live      ", [x[:8] for x in a])
    print("   historical", [x[:8] for x in b])
    print("   identical =", a == b)
    cur.execute("DELETE FROM beliefs WHERE subject_id=%s", (S,))
