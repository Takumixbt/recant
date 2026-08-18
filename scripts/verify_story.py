"""Go/no-go: does the demo's central claim hold on real embeddings?"""
import sys; sys.path.insert(0,'.')
from recant import blast
from recant.store import connect

poison = open("poison_id.txt").read().strip()
with connect() as c:
    cur = c.cursor()
    cur.execute("SELECT subject_id FROM beliefs WHERE id=%s", (poison,))
    subj = cur.fetchone()[0]
    cur.execute("SELECT id FROM beliefs WHERE source='user:attacker'")
    campaign = [str(r[0]) for r in cur.fetchall()]
    cur.execute("SELECT id FROM beliefs WHERE kind='policy' AND subject_id=%s LIMIT 1", (subj,))
    policy = str(cur.fetchone()[0])
    cur.execute("""SELECT db.rank, db.distance, b.source, left(b.content,58)
                     FROM decision_beliefs db JOIN beliefs b ON b.id=db.belief_id
                    WHERE db.decision_id = (SELECT id FROM decisions WHERE subject_id=%s
                                            ORDER BY decided_at DESC LIMIT 1)
                    ORDER BY db.rank""", (subj,))
    print("=== what a post-attack decision actually retrieved ===")
    for r,d,s,t in cur.fetchall():
        mark = "  <-- POISON" if s=="user:attacker" else ""
        print(f"  #{r} d={float(d):.4f} [{s}] {t}{mark}")

print("\n=== A. one planted belief ==="); a = blast.compute(poison, workers=12); print(a.summary())
print("\n=== B. the campaign ==="); b = blast.compute(campaign, workers=16); print(b.summary())
print("\n=== C. control: the legitimate refund policy ==="); c_ = blast.compute(policy, workers=12); print(c_.summary())
fr = lambda r: 100.0*len(r.flips)/max(r.replayed,1)
print("\n=== SEPARATION ===")
print(f"  poisoned belief   flips {fr(a):5.1f}% of what it touches")
print(f"  poison campaign   flips {fr(b):5.1f}% of what it touches")
print(f"  legitimate belief flips {fr(c_):5.1f}% of what it touches")
blast.close_pool()
