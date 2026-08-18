"""Is replay non-deterministic, and if so is it the SET or just the ORDER?"""
import sys; sys.path.insert(0, ".")
from recant.store import BeliefStore, connect

S = "det_probe"
Q = "should I approve a large refund for this customer?"
with connect() as conn:
    cur = conn.cursor()
    cur.execute("DELETE FROM beliefs WHERE subject_id=%s", (S,))
    st = BeliefStore(conn)
    # deliberately near-identical content -> near-tied distances
    for i in range(12):
        st.assert_belief(S, f"belief number {i}: customer has escalation flag {i} set", "bench")

    live = st.retrieve(S, Q, k=5)
    print("live retrieval:")
    for b in live.beliefs:
        print(f"   #{b.rank} d={b.distance:.9f} {b.belief_id[:8]}")

    print("\nfive replays at the SAME anchor:")
    sets, orders = set(), set()
    for i in range(5):
        r = st.retrieve_as_of(S, Q, live.hlc, k=5)
        ids = [b.belief_id for b in r.beliefs]
        sets.add(frozenset(ids)); orders.add(tuple(ids))
        print(f"   replay {i}: {[x[:8] for x in ids]}")

    print(f"\n  distinct SETS returned   = {len(sets)}   (1 means the set is stable)")
    print(f"  distinct ORDERS returned = {len(orders)}   (1 means order is stable too)")
    live_ids = [b.belief_id for b in live.beliefs]
    print(f"  live set == replay set   = {frozenset(live_ids) in sets}")

    print("\n  distances (are they tied?):")
    cur.execute("""SELECT count(DISTINCT round(d::numeric, 9)), count(*) FROM (
                     SELECT embedding <=> (SELECT embedding FROM beliefs WHERE subject_id=%s LIMIT 1) AS d
                     FROM beliefs WHERE subject_id=%s) t""", (S, S))
    distinct, total = cur.fetchone()
    print(f"    {distinct} distinct distances across {total} beliefs")
    cur.execute("DELETE FROM beliefs WHERE subject_id=%s", (S,))
