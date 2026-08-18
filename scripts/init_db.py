"""Apply db/schema.sql, statement by statement, and report what landed."""
import os
import re
import sys

import psycopg
from dotenv import load_dotenv

load_dotenv()

sql = open("db/schema.sql").read()
# strip comments so the naive splitter can't trip on a ';' inside one
sql = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
statements = [s.strip() for s in sql.split(";") if s.strip()]

with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, connect_timeout=20) as conn:
    cur = conn.cursor()
    cur.execute("SET CLUSTER SETTING feature.vector_index.enabled = true")
    cur.execute("SET statement_timeout = '120s'")

    for st in statements:
        label = " ".join(st.split())[:68]
        try:
            cur.execute(st)
            print(f"  ok    {label}")
        except Exception as e:
            print(f"  FAIL  {label}")
            print(f"        {str(e).strip().splitlines()[0][:150]}")
            sys.exit(1)

    print("\n--- tables ---")
    cur.execute("SELECT table_name FROM [SHOW TABLES] ORDER BY table_name")
    for (t,) in cur.fetchall():
        cur.execute(f"SELECT count(*) FROM {t}")
        print(f"  {t:<20} rows={cur.fetchone()[0]}")

    cur.execute("SELECT index_name FROM [SHOW INDEXES FROM beliefs] WHERE index_name LIKE '%embedding%'")
    idx = [r[0] for r in cur.fetchall()]
    print(f"\n  vector index present: {idx or 'NONE'}")
