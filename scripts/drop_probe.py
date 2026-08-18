import os, psycopg
from dotenv import load_dotenv
load_dotenv()
with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
    cur = c.cursor()
    cur.execute("SELECT table_name FROM [SHOW TABLES] WHERE table_name LIKE 'probe_%'")
    for (t,) in cur.fetchall():
        cur.execute(f"DROP TABLE IF EXISTS {t}")
        print("dropped", t)
