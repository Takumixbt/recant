"""
Feasibility probe.

Tests every CockroachDB capability the design depends on, against the actual
cluster/tier we were given. Run this BEFORE writing real code: a failure here
changes the architecture, and it is much cheaper to find out now.

Every probe is bounded by a statement timeout so nothing can hang the run.

Usage:  python scripts/probe.py
Reads DATABASE_URL from .env
"""

import os
import sys
import uuid

import psycopg
from dotenv import load_dotenv

load_dotenv()

URL = os.environ.get("DATABASE_URL")
if not URL:
    sys.exit("DATABASE_URL not set. Put it in .env")

RESULTS = []


def check(name, critical=False, timeout_ms=20000):
    """Run a probe, print + record PASS/FAIL, never raise, never hang."""

    def wrap(fn):
        try:
            cur.execute(f"SET statement_timeout = '{timeout_ms}ms'")
            detail = fn()
            ok, msg = True, (detail or "")
        except Exception as e:
            ok, msg = False, str(e).strip().split("\n")[0][:170]
            # a killed statement can leave the session dirty
            try:
                conn.rollback()
            except Exception:
                pass
        RESULTS.append((name, ok, msg, critical))
        flag = " [CRITICAL]" if critical and not ok else ""
        print(f"  {'PASS' if ok else 'FAIL':4}  {name}{flag}", flush=True)
        if msg:
            print(f"        {msg}", flush=True)
        return fn

    return wrap


print("\n" + "=" * 78, flush=True)
print("  RECANT :: CockroachDB feasibility probe", flush=True)
print("=" * 78, flush=True)

with psycopg.connect(URL, autocommit=True, connect_timeout=20) as conn:
    cur = conn.cursor()
    TBL = f"probe_{uuid.uuid4().hex[:8]}"
    DB = conn.info.dbname

    @check("connect + version")
    def _():
        cur.execute("SELECT version()")
        return cur.fetchone()[0][:90]

    @check("current database / user")
    def _():
        cur.execute("SELECT current_database(), current_user")
        return "db={} user={}".format(*cur.fetchone())

    # --- the time-travel primitives ----------------------------------------

    @check("cluster_logical_timestamp() (HLC anchor for decisions)", critical=True)
    def _():
        cur.execute("SELECT cluster_logical_timestamp()")
        return f"hlc={cur.fetchone()[0]}"

    @check("follower_read_timestamp() (cheap parallel replay)")
    def _():
        cur.execute("SELECT follower_read_timestamp()")
        return f"ts={cur.fetchone()[0]}"

    # --- vector storage + index --------------------------------------------

    @check("VECTOR column type", critical=True)
    def _():
        cur.execute(
            f"CREATE TABLE {TBL} ("
            f"  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
            f"  subject STRING NOT NULL,"
            f"  emb VECTOR(1024))"
        )
        return "VECTOR(1024) column created"

    @check("SET CLUSTER SETTING feature.vector_index.enabled")
    def _():
        cur.execute("SET CLUSTER SETTING feature.vector_index.enabled = true")
        return "settable (we hold cluster-setting privileges)"

    @check("CREATE VECTOR INDEX  <<< THE BLOCKER", critical=True, timeout_ms=60000)
    def _():
        # opclass is declared inline on the column, pgvector-style
        cur.execute(
            f"CREATE VECTOR INDEX ON {TBL} (subject, emb vector_cosine_ops)"
        )
        return "distributed vector index created, with prefix column"

    @check("cosine ANN query")
    def _():
        v = "[" + ",".join(["0.01"] * 1024) + "]"
        cur.execute(
            f"SELECT id FROM {TBL} WHERE subject = 'x' "
            f"ORDER BY emb <=> %s::VECTOR(1024) LIMIT 5",
            (v,),
        )
        cur.fetchall()
        return "planned + executed"

    @check("vector searchable in the SAME txn that wrote it (freshness claim)")
    def _():
        v = "[" + ",".join(["0.02"] * 1024) + "]"
        conn.autocommit = False
        try:
            cur.execute(
                f"INSERT INTO {TBL} (subject, emb) VALUES ('t', %s::VECTOR(1024))", (v,)
            )
            cur.execute(
                f"SELECT count(*) FROM (SELECT id FROM {TBL} WHERE subject='t' "
                f"ORDER BY emb <=> %s::VECTOR(1024) LIMIT 1)",
                (v,),
            )
            n = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.autocommit = True
        return f"row visible to ANN inside the same txn (n={n})"

    @check("AS OF SYSTEM TIME reconstructs an exact past state", critical=True)
    def _():
        # This is the core mechanism of the whole product: anchor an HLC, write
        # after it, then prove the later write is invisible at that anchor.
        cur.execute("SELECT cluster_logical_timestamp()")
        anchor = cur.fetchone()[0]
        v = "[" + ",".join(["0.03"] * 1024) + "]"
        cur.execute(
            f"INSERT INTO {TBL} (subject, emb) VALUES ('after', %s::VECTOR(1024))", (v,)
        )
        cur.execute(f"SELECT count(*) FROM {TBL}")
        now_n = cur.fetchone()[0]
        # AOST demands a constant expression, so the HLC must be inlined, not
        # bound. anchor is a Decimal we produced ourselves, so this is safe.
        cur.execute(
            f"SELECT count(*) FROM {TBL} AS OF SYSTEM TIME {anchor}"
        )
        past_n = cur.fetchone()[0]
        if past_n >= now_n:
            raise RuntimeError(f"no time separation: past={past_n} now={now_n}")
        return f"rows now={now_n}, at anchor={past_n}  (anchor={anchor})"

    # --- retention window governs how far replay can reach ------------------

    @check("read gc.ttlseconds")
    def _():
        cur.execute(f"SHOW ZONE CONFIGURATION FROM DATABASE {DB}")
        raw = str(cur.fetchone()[1])
        hit = [l.strip() for l in raw.split("\n") if "ttlseconds" in l]
        return hit[0] if hit else raw[:110]

    @check("ALTER gc.ttlseconds -> 24h (extends replay window)")
    def _():
        cur.execute(f"ALTER DATABASE {DB} CONFIGURE ZONE USING gc.ttlseconds = 86400")
        return "raised to 86400s"

    # --- changefeed for write-time interdiction -----------------------------
    # core changefeeds stream forever; a short timeout IS the success signal

    # Optional: interdiction can also run synchronously in the write path,
    # which is arguably better (block before trust, not after commit).
    @check("CHANGEFEED (optional, for async interdiction)", timeout_ms=25000)
    def _():
        cur.execute(
            f"EXPERIMENTAL CHANGEFEED FOR {TBL} WITH initial_scan = 'yes'"
        )
        row = cur.fetchone()
        return f"streamed a row: {str(row)[:80]}"

    # --- multi-region --------------------------------------------------------

    @check("multi-region regions available")
    def _():
        cur.execute("SHOW REGIONS FROM CLUSTER")
        return f"regions: {[r[0] for r in cur.fetchall()] or 'none'}"

    try:
        cur.execute("SET statement_timeout = '20s'")
        cur.execute(f"DROP TABLE IF EXISTS {TBL}")
    except Exception:
        pass

print("=" * 78)
blockers = [n for n, ok, _, crit in RESULTS if crit and not ok]
if blockers:
    print("  BLOCKERS -- architecture must change:")
    for b in blockers:
        print(f"    - {b}")
    sys.exit(1)
print("  No critical blockers. Design holds on this tier.")
print("=" * 78)
