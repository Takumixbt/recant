"""Measure blast radius against the seeded ledger."""
import sys; sys.path.insert(0, ".")
from recant import blast
from recant.store import connect

poison = open("poison_id.txt").read().strip()
with connect() as c:
    cur = c.cursor()
    cur.execute("SELECT subject_id FROM beliefs WHERE id=%s", (poison,))
    subj = cur.fetchone()[0]
    cur.execute("SELECT id FROM beliefs WHERE source='user:attacker'")
    campaign = [str(r[0]) for r in cur.fetchall()]
    cur.execute("SELECT id FROM beliefs WHERE content LIKE 'refunds above 500%%' "
                "AND subject_id=%s LIMIT 1", (subj,))
    policy = str(cur.fetchone()[0])

print("="*64); print("A. ONE planted belief"); print("="*64)
a = blast.compute(poison, workers=8); print(a.summary())
for f in a.flips[:4]:
    print(f"    {f.decision_id[:8]}  {f.was} -> {f.now}  {f.amount:,.0f} USD")

print("\n"+"="*64); print(f"B. THE WHOLE CAMPAIGN ({len(campaign)} planted beliefs, one fan-out)"); print("="*64)
b = blast.compute(campaign, workers=24); print(b.summary())

print("\n"+"="*64); print("C. CONTROL: a legitimate policy belief"); print("="*64)
c_ = blast.compute(policy, workers=8); print(c_.summary())

print("\n"+"="*64); print("SEPARATION"); print("="*64)
fr = lambda r: 100.0*len(r.flips)/max(r.replayed,1)
print(f"  poisoned belief   flips {fr(a):.0f}% of what it touches")
print(f"  poison campaign   flips {fr(b):.0f}% of what it touches")
print(f"  legitimate belief flips {fr(c_):.0f}% of what it touches")
blast.close_pool()
