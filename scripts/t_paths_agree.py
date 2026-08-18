"""The two replay paths must agree wherever both are available.

MVCC replay is authoritative but GC-bounded. The log path is unbounded. If they
disagree, the unbounded one is quietly lying about history, so this compares
them head to head on fresh anchors where MVCC still has the data.
"""
import sys; sys.path.insert(0,'.')
from decimal import Decimal
from recant.store import BeliefStore, connect

S = "agree_probe"
Q = "should I approve a large refund for this customer?"
with connect() as c:
    cur = c.cursor()
    cur.execute("DELETE FROM memory_events WHERE subject_id=%s", (S,))
    cur.execute("DELETE FROM beliefs WHERE subject_id=%s", (S,))
    st = BeliefStore(c)

    ids = []
    for i in range(10):
        ids.append(st.assert_belief(S, f"belief {i}: customer note about refund policy {i}",
                                    "seed", 0.5 + i * 0.02))
    st.quarantine(ids[3], "test quarantine")

    anchors = []
    for i in range(4):
        st.assert_belief(S, f"later belief {i}: additional note {i}", "seed", 0.7)
        r = st.retrieve(S, Q, k=5)
        anchors.append((r.hlc, [b.belief_id for b in r.beliefs]))

    agree = disagree = 0
    for hlc, live_ids in anchors:
        mvcc = st.retrieve_as_of(S, Q, Decimal(hlc), k=5)
        logp = st._retrieve_via_log(S, __import__("recant.embed", fromlist=["x"]).to_pgvector(
            st.embedder.embed(Q)), Decimal(hlc), 5, frozenset())
        a = [b.belief_id for b in mvcc.beliefs]
        b_ = [b.belief_id for b in logp.beliefs]
        same = a == b_
        agree += same; disagree += (not same)
        print(f"  anchor {str(hlc)[:14]}  mvcc={len(a)} log={len(b_)}  identical={same}")
        if not same:
            print(f"     mvcc {[x[:8] for x in a]}")
            print(f"     log  {[x[:8] for x in b_]}")

    print(f"\n  agree {agree} / {agree+disagree}")
    print("  " + ("PASS: the unbounded path tells the same truth as MVCC"
                  if disagree == 0 else "FAIL: paths disagree"))
    cur.execute("DELETE FROM memory_events WHERE subject_id=%s", (S,))
    cur.execute("DELETE FROM beliefs WHERE subject_id=%s", (S,))
