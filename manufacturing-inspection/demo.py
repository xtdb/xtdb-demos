from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from psycopg.rows import dict_row

ASSESSMENT_SQL = Path(__file__).with_name("assessment.sql").read_text().strip()
START = datetime(2026, 9, 15, 8, tzinfo=timezone.utc)
CORRECTION_START = START + timedelta(hours=1)
CORRECTION_END = START + timedelta(hours=3)
PIXELS = [198, 201, 195, 204, 200, 206, 201, 199, 204, 196, 203, 200,
          195, 202, 198, 205, 201, 197, 201, 204, 195, 199, 202, 200]


class StageConflict(Exception):
    pass


class RunNotFound(Exception):
    pass


def timestamp_sql(value):
    return "TIMESTAMP '" + value.astimezone(timezone.utc).isoformat() + "'"


def at_basis(sql, basis):
    return f"SETTING DEFAULT SYSTEM_TIME AS OF {timestamp_sql(basis)}\n{sql}"


def rows(conn, sql, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def latest_basis(conn):
    return rows(conn, "SELECT MAX(system_time) AS basis FROM xt.txs")[0]["basis"]


def has_table(conn, table):
    return bool(rows(conn, """SELECT table_name FROM information_schema.tables
                             WHERE table_schema = 'public' AND table_name = %s""", (table,)))


def get_run(conn, run_id):
    if not has_table(conn, 'demo_run'):
        raise RunNotFound(run_id)
    found = rows(conn, "SELECT * FROM demo_run WHERE _id = %s", (run_id,))
    if not found:
        raise RunNotFound(run_id)
    return found[0]


def assess(conn, run_id, basis):
    return rows(conn, at_basis(ASSESSMENT_SQL, basis), (run_id,))


def create_run(conn):
    run_id = uuid4().hex
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("INSERT INTO demo_run (_id, stage) VALUES (%s, 'inspected')", (run_id,))
        cur.execute("""INSERT INTO calibration (_id, _valid_from, mm_per_pixel)
                       VALUES (%s, %s, 0.050)""", (run_id + "/camera", START))
        cur.execute("""INSERT INTO product_spec (_id, _valid_from, min_mm, max_mm)
                       VALUES (%s, %s, 9.80, 10.20)""", (run_id + "/product", START))
        for index, pixels in enumerate(PIXELS):
            cur.execute("""INSERT INTO inspection
                           (_id, run_id, part, camera_id, product_id, inspected_at, pixel_width)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (f"{run_id}/{index + 1}", run_id, f"P-{index + 1:03}",
                         run_id + "/camera", run_id + "/product",
                         START + timedelta(minutes=index * 10), pixels))
    return run_id


def release_batch(conn, run_id):
    run = get_run(conn, run_id)
    if run["stage"] != "inspected":
        raise StageConflict("This batch has already been released.")
    basis = latest_basis(conn)
    assessed = assess(conn, run_id, basis)
    with conn.transaction(), conn.cursor() as cur:
        for part in assessed:
            if part["assessment"] == "pass":
                cur.execute("""INSERT INTO release
                               (_id, run_id, width_mm, basis, released_at)
                               VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)""",
                            (part["inspection_id"], run_id, part["width_mm"], basis))
        cur.execute("UPDATE demo_run SET stage = 'released', release_basis = %s WHERE _id = %s",
                    (basis, run_id))


def correct_calibration(conn, run_id):
    run = get_run(conn, run_id)
    if run["stage"] != "released":
        raise StageConflict("Release the batch before applying its one calibration correction.")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("""INSERT INTO calibration (_id, _valid_from, _valid_to, mm_per_pixel)
                       VALUES (%s, %s, %s, 0.051)""",
                    (run_id + "/camera", CORRECTION_START, CORRECTION_END))
        cur.execute("UPDATE demo_run SET stage = 'corrected' WHERE _id = %s", (run_id,))


def state(conn, run_id):
    run = get_run(conn, run_id)
    basis = latest_basis(conn)
    current = assess(conn, run_id, basis)
    original_basis = run.get("release_basis")
    original = assess(conn, run_id, original_basis) if original_basis else []
    releases = rows(conn, "SELECT * FROM release WHERE run_id = %s ORDER BY _id", (run_id,)) if has_table(conn, 'release') else []
    released_ids = {r["_id"] for r in releases}
    timeline_sql = """SELECT _valid_from AS valid_from, _valid_to AS valid_to,
                            mm_per_pixel FROM calibration FOR ALL VALID_TIME
                     WHERE _id = %s ORDER BY _valid_from"""
    timeline = rows(conn, at_basis(timeline_sql, basis), (run_id + "/camera",))
    original_timeline = rows(conn, at_basis(timeline_sql, original_basis), (run_id + "/camera",)) if original_basis else []
    return {
        "run_id": run_id, "stage": run["stage"], "basis": basis,
        "release_basis": original_basis, "current": current, "original": original,
        "releases": releases, "timeline": timeline, "original_timeline": original_timeline,
        "review_ids": [p["inspection_id"] for p in current
                       if p["inspection_id"] in released_ids and p["assessment"] != "pass"],
        "sql": at_basis(ASSESSMENT_SQL, basis),
        "original_sql": at_basis(ASSESSMENT_SQL, original_basis) if original_basis else None,
        "correction_start": CORRECTION_START, "correction_end": CORRECTION_END,
        "batch_start": START, "batch_end": START + timedelta(hours=4),
    }
