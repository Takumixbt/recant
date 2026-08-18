"""Replay must work both inside and beyond the MVCC garbage-collection window."""
import sys; sys.path.insert(0,'.')
from decimal import Decimal
from recant.store import BeliefStore, connect

with connect() as c:
    cur = c.cursor()
    cur.execute("""SELECT d.id, d.subject_id, d.prompt, d.read_hlc, d.action
                     FROM decisions d ORDER BY d.decided_at ASC LIMIT 1""")
    old = cur.fetchone()
    cur.execute("""SELECT d.id, d.subject_id, d.prompt, d.read_hlc, d.action
                     FROM decisions d ORDER BY d.decided_at DESC LIMIT 1""")
    new = cur.fetchone()
    cur.execute("SELECT cluster_logical_timestamp()")
    now = cur.fetchone()[0]

    st = BeliefStore(c)
    for label, row in (("OLDEST decision (beyond GC window)", old),
                       ("NEWEST decision (inside GC window)", new)):
        did, subj, prompt, hlc, action = row
        age_min = float(now - hlc) / 1e9 / 60
        print(f"\n{label}")
        print(f"  anchor age  {age_min:,.0f} min")
        try:
            r = st.retrieve_as_of(subj, prompt, Decimal(hlc), k=5)
            print(f"  reconstructed {len(r.beliefs)} beliefs")
            for b in r.beliefs[:3]:
                print(f"     #{b.rank} d={b.distance:.4f} {b.content[:50]}")
        except Exception as e:
            print("  FAILED:", str(e).splitlines()[0][:120])
