import os, psycopg
from dotenv import load_dotenv
load_dotenv()
with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
    cur = c.cursor()
    for t in ("beliefs", "decisions", "decision_beliefs"):
        cur.execute(f"SELECT count(*) FROM {t}")
        print(f"  {t:<18} {cur.fetchone()[0]:,}")
    cur.execute("SELECT action, count(*) FROM decisions GROUP BY action ORDER BY 2 DESC")
    for a, n in cur.fetchall():
        print(f"  {a:<18} {n:,}")
    cur.execute("SELECT count(DISTINCT subject_id) FROM beliefs")
    print(f"  subjects seeded    {cur.fetchone()[0]:,} / 120")
