import sys, time; sys.path.insert(0, ".")
from recant import blast
from recant.store import connect
poison = open("poison_id.txt").read().strip()
with connect() as c:
    cur = c.cursor()
    cur.execute("SELECT id FROM beliefs WHERE source='user:attacker' ORDER BY created_at")
    campaign = [str(r[0]) for r in cur.fetchall()]
print("single belief, 16 workers:")
r = blast.compute(poison, workers=16)
print(r.summary())
print("\nfull campaign, 24 workers:")
t0=time.perf_counter(); c_,rp,fl,ex,lats = 0,0,0,0.0,[]
for b in campaign:
    x = blast.compute(b, workers=24)
    c_+=x.candidates; rp+=x.replayed; fl+=len(x.flips); ex+=x.exposure; lats+=x.latencies_ms
w=(time.perf_counter()-t0)*1000; lats.sort()
p=lambda q: lats[min(int(round(q/100*(len(lats)-1))),len(lats)-1)] if lats else 0
print(f"  touched {c_}  replayed {rp}  FLIPPED {fl}  exposure {ex:,.0f} USD")
print(f"  wall {w:,.0f} ms   p50/p95/p99 {p(50):,.1f} / {p(95):,.1f} / {p(99):,.1f} ms")
print(f"  throughput {rp/(w/1000):.1f} replays/s")
