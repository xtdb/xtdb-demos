"""Connection helper shared across the demo.

XTDB's pgwire rejects parameters sent with an unknown type OID, so we register a
StrDumper for str; each caller opens its own short-lived autocommit connection.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

import psycopg
from psycopg.types.string import StrDumper

# Overridden to `xtdb` (the service name) when running under compose.
DSN = os.environ.get("XTDB_DSN", "postgresql://xtdb@localhost:5444/xtdb")


def connect() -> psycopg.Connection:
    conn = psycopg.connect(DSN, autocommit=True)
    conn.adapters.register_dumper(str, StrDumper)  # XTDB rejects unknown-OID params
    return conn


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@contextmanager
def sys_tx(conn, system_time: datetime):
    """A write transaction stamped with an explicit system-time (when the DB *learns*
    these facts). The whole node lives on the sim clock — system-time must only ever
    move forward — so every writer stamps its sim-now here."""
    cur = conn.cursor()
    try:
        cur.execute(f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{iso(system_time)}')")
        yield cur
        cur.execute("COMMIT")
    except Exception:
        try:
            cur.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        cur.close()


def sys_tx_retry(conn, now_fn, run, tries: int = 6):
    """Run write ops at system-time = now_fn(), retrying if a concurrent writer (the live
    sim advancing its clock) has moved system-time past ours. Returns the system-time used
    and run(cursor, system_time)'s result."""
    for _ in range(tries):
        st = now_fn()
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(system_time) FROM xt.txs")
            latest = cur.fetchone()[0]
        # A stopped sim can reuse its last commit time; the published node requires
        # a later instant for the next write to become visible.
        if latest is not None:
            st = max(st, latest + timedelta(microseconds=1))
        try:
            with sys_tx(conn, st) as cur:
                return st, run(cur, st)
        except Exception as e:
            if "system-time" in str(e).lower():
                time.sleep(0.03)
                continue
            raise
    raise RuntimeError("system-time contended; retries exhausted")


if __name__ == "__main__":
    # Headless smoke test of the query layer (no UI): connect, then compute one
    # point-in-time feature vector via the registry. `python queries.py` should
    # print a plausible vector if node + schema + feature SQL are all healthy.
    import model
    import registry

    with connect() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM txn")
        n = cur.fetchone()[0]
        cur.execute("SELECT _id FROM account ORDER BY _id LIMIT 1")
        acc = cur.fetchone()[0]
        t = model.sim_now(c)
        vals, _ = registry.serve(c, registry.Ctx(acc, t, 100.0, "RU"))
    print(f"txns={n:,}  sim_now={t}")
    print(f"features for {acc}: {vals}")
