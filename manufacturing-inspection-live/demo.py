from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from psycopg.rows import dict_row

SQL = Path(__file__).with_name("assessment.sql").read_text().strip()
START = datetime(2026, 9, 15, 8, tzinfo=timezone.utc)
FAULT_START = START + timedelta(hours=1)
FAULT_END = START + timedelta(hours=3)
PIXELS = [198, 201, 195, 204, 200, 206, 201, 199, 204, 196, 203, 200,
          195, 202, 198, 205, 201, 197, 201, 204, 195, 199, 202, 200]


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


def rows(conn, sql, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def has_table(conn, table):
    return bool(rows(conn, """SELECT table_name FROM information_schema.tables
                             WHERE table_schema = 'public' AND table_name = %s""", (table,)))


def basis(conn):
    return rows(conn, "SELECT MAX(system_time) AS basis FROM xt.txs")[0]["basis"]


def as_of(sql, timestamp):
    return f"SETTING DEFAULT SYSTEM_TIME AS OF TIMESTAMP '{timestamp.astimezone(timezone.utc).isoformat()}'\n{sql}"


def run_record(conn, run_id):
    found = rows(conn, "SELECT * FROM demo_run WHERE _id = %s", (run_id,)) if has_table(conn, "demo_run") else []
    if not found:
        raise NotFound("Shift not found. Start a new shift.")
    return found[0]


def optional_rows(conn, table, run_id):
    if not has_table(conn, table):
        return []
    return rows(conn, f"SELECT * FROM {table} WHERE run_id = %s ORDER BY _id", (run_id,))


def assess(conn, run_id, timestamp):
    return rows(conn, as_of(SQL, timestamp), (run_id,))


def write_inspection(cur, run_id, sequence):
    cur.execute("""INSERT INTO inspection
                   (_id, run_id, part, camera_id, product_id, inspected_at, pixel_width)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (f"{run_id}/{sequence + 1}", run_id, f"P-{sequence + 1:03}",
                 run_id + "/camera", run_id + "/product",
                 START + timedelta(minutes=sequence * 10), PIXELS[sequence % len(PIXELS)]))


def finish_inspections(conn, run_id):
    timestamp = basis(conn)
    decisions = {p["_id"] for p in optional_rows(conn, "inspection_decision", run_id)}
    pending = [p for p in assess(conn, run_id, timestamp) if p["inspection_id"] not in decisions]
    if not pending:
        return
    with conn.transaction(), conn.cursor() as cur:
        for part in pending:
            cur.execute("""INSERT INTO inspection_decision
                           (_id, run_id, width_mm, assessment, released, basis, recorded_at)
                           VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)""",
                        (part["inspection_id"], run_id, part["width_mm"], part["assessment"],
                         part["assessment"] == "pass", timestamp))


def create_run(conn):
    run_id = uuid4().hex
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("INSERT INTO demo_run (_id, next_sequence) VALUES (%s, 18)", (run_id,))
        cur.execute("""INSERT INTO calibration (_id, _valid_from, mm_per_pixel)
                       VALUES (%s, %s, 0.050)""", (run_id + "/camera", START))
        cur.execute("""INSERT INTO product_spec (_id, _valid_from, min_mm, max_mm)
                       VALUES (%s, %s, 9.80, 10.20)""", (run_id + "/product", START))
        for sequence in range(18):
            write_inspection(cur, run_id, sequence)
    finish_inspections(conn, run_id)
    return run_id


def tick(conn, run_id, expected_sequence):
    run = run_record(conn, run_id)
    finish_inspections(conn, run_id)
    sequence = run["next_sequence"]
    if expected_sequence != sequence:
        return
    with conn.transaction(), conn.cursor() as cur:
        write_inspection(cur, run_id, sequence)
        cur.execute("UPDATE demo_run SET next_sequence = %s WHERE _id = %s", (sequence + 1, run_id))
    finish_inspections(conn, run_id)


def incident_record(conn, run_id):
    found = optional_rows(conn, "incident", run_id)
    return found[0] if found else None


def report(conn, run_id):
    run_record(conn, run_id)
    finish_inspections(conn, run_id)
    if incident_record(conn, run_id):
        return
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("""INSERT INTO incident
                       (_id, run_id, stage, affected_from, affected_to, corrected_factor, recorded_at)
                       VALUES (%s, %s, 'reported', %s, %s, 0.051, CURRENT_TIMESTAMP)""",
                    (run_id, run_id, FAULT_START, FAULT_END))


def reassess(conn, run_id):
    incident = incident_record(conn, run_id)
    if not incident:
        raise Conflict("Report a calibration problem first.")
    if incident["stage"] != "reported":
        return
    finish_inspections(conn, run_id)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("""INSERT INTO calibration (_id, _valid_from, _valid_to, mm_per_pixel)
                       VALUES (%s, %s, %s, %s)""",
                    (run_id + "/camera", incident["affected_from"], incident["affected_to"],
                     incident["corrected_factor"]))
        cur.execute("UPDATE incident SET stage = 'assessed' WHERE _id = %s", (run_id,))


def contain(conn, run_id):
    incident = incident_record(conn, run_id)
    if not incident or incident["stage"] == "reported":
        raise Conflict("Reassess the candidate parts before placing releases on hold.")
    if incident["stage"] == "contained":
        return
    snapshot = state(conn, run_id)
    with conn.transaction(), conn.cursor() as cur:
        for part in snapshot["candidates"]:
            if part["group"] == "review":
                cur.execute("""INSERT INTO stock_hold
                               (_id, run_id, incident_id, basis, reason, recorded_at)
                               VALUES (%s, %s, %s, %s, 'Calibration reassessment', CURRENT_TIMESTAMP)""",
                            (part["inspection_id"], run_id, run_id, snapshot["basis"]))
        cur.execute("UPDATE incident SET stage = 'contained' WHERE _id = %s", (run_id,))


def state(conn, run_id):
    run = run_record(conn, run_id)
    timestamp = basis(conn)
    incident = incident_record(conn, run_id)
    decisions = {p["_id"]: p for p in optional_rows(conn, "inspection_decision", run_id)}
    holds = {p["_id"]: p for p in optional_rows(conn, "stock_hold", run_id)}
    parts = assess(conn, run_id, timestamp)
    candidates = []
    for part in parts:
        decision = decisions.get(part["inspection_id"])
        hold = holds.get(part["inspection_id"])
        part["decision"] = decision
        part["hold"] = hold
        part["disposition"] = "quality_hold" if hold else "released" if decision and decision["released"] else "held"
        if incident and incident["affected_from"] <= part["inspected_at"] < incident["affected_to"]:
            group = "candidate"
            if incident["stage"] != "reported" and decision:
                if decision["released"] and part["assessment"] != "pass":
                    group = "review"
                elif not decision["released"] and part["assessment"] == "pass":
                    group = "now_passes"
                else:
                    group = "unchanged"
            part["group"] = group
            candidates.append(part)
    return {
        "run_id": run_id, "next_sequence": run["next_sequence"], "basis": timestamp,
        "incident": incident, "candidates": candidates, "recent": parts[-8:],
        "control": next((p for p in parts if p["part"] == "P-019"), None),
        "counts": {
            "inspected": len(parts),
            "released": sum(p["disposition"] == "released" for p in parts),
            "held": sum(p["disposition"] != "released" for p in parts),
            "review": sum(p.get("group") == "review" for p in candidates),
            "now_passes": sum(p.get("group") == "now_passes" for p in candidates),
            "unchanged": sum(p.get("group") == "unchanged" for p in candidates),
            "quality_holds": len(holds),
        },
        "sql": as_of(SQL, timestamp),
    }


def replay(conn, run_id, part_number):
    run_record(conn, run_id)
    inspection_id = f"{run_id}/{part_number}"
    found = rows(conn, "SELECT * FROM inspection_decision WHERE _id = %s", (inspection_id,)) if has_table(conn, "inspection_decision") else []
    if not found:
        raise NotFound("No recorded decision for this part.")
    decision = found[0]
    sql = SQL.replace("WHERE i.run_id = %s", "WHERE i.run_id = %s AND i._id = %s")
    original_sql = as_of(sql, decision["basis"])
    original = rows(conn, original_sql, (run_id, inspection_id))[0]
    current = rows(conn, as_of(sql, basis(conn)), (run_id, inspection_id))[0]
    return {"original": original, "current": current, "decision": decision, "sql": original_sql,
            "parameters": [run_id, inspection_id]}
