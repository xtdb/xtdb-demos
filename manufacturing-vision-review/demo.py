from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from psycopg.rows import dict_row

SQL = Path(__file__).with_name('assessment.sql').read_text().strip()
START = datetime(2026, 9, 29, 8, tzinfo=timezone.utc)
SCENARIO = [
    ('clean', False, .04), ('clean', False, .08), ('crack', True, .97),
    ('clean', False, .03), ('hairline', False, .12), ('scratch', True, .88),
    ('clean', False, .06), ('clean', False, .09), ('crack', True, .96),
    ('clean', False, .05), ('clean', False, .07), ('dust', True, .82),
]


class NotFound(Exception):
    pass


def rows(conn, sql, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def exists(conn, table):
    return bool(rows(conn, "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name=%s", (table,)))


def basis(conn):
    return rows(conn, 'SELECT MAX(system_time) AS basis FROM xt.txs')[0]['basis']


def as_of(sql, timestamp):
    return f"SETTING DEFAULT SYSTEM_TIME AS OF TIMESTAMP '{timestamp.astimezone(timezone.utc).isoformat()}'\n{sql}"


def get_run(conn, run_id):
    found = rows(conn, 'SELECT * FROM vision_run WHERE _id=%s', (run_id,)) if exists(conn, 'vision_run') else []
    if not found:
        raise NotFound('Batch not found. Start a new batch.')
    return found[0]


def get_part(conn, run_id, number):
    get_run(conn, run_id)
    found = rows(conn, 'SELECT * FROM part WHERE _id=%s AND run_id=%s', (f'{run_id}/{number}', run_id))
    if not found:
        raise NotFound('Part not found in this batch.')
    return found[0]


def write_review(cur, part, defective, source):
    cur.execute('''INSERT INTO review (_id, run_id, _valid_from, defective, source, recorded_at)
                   VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)''',
                (part['_id'], part['run_id'], part['produced_at'], defective, source))


def create_run(conn):
    run_id = uuid4().hex
    with conn.transaction(), conn.cursor() as cur:
        cur.execute('INSERT INTO vision_run (_id) VALUES (%s)', (run_id,))
        for index, (appearance, prediction, score) in enumerate(SCENARIO, start=1):
            part_id = f'{run_id}/{index}'
            produced = START + timedelta(minutes=2 * (index - 1))
            cur.execute('''INSERT INTO part (_id, run_id, number, appearance, produced_at)
                           VALUES (%s, %s, %s, %s, %s)''', (part_id, run_id, index, appearance, produced))
            cur.execute('''INSERT INTO prediction (_id, run_id, defective, defect_score, model_version, line_action)
                           VALUES (%s, %s, %s, %s, 'vision-v1', %s)''',
                        (part_id, run_id, prediction, score, 'held' if prediction else 'released'))
            if index <= 4:
                write_review(cur, {'_id':part_id, 'run_id':run_id, 'produced_at':produced},
                             appearance == 'crack', 'Earlier inspection')
    save_report(conn, run_id, 'Before your reviews')
    return run_id


def read_parts(conn, run_id, timestamp):
    return rows(conn, as_of(SQL, timestamp), (run_id,))


def metrics(parts):
    reviewed = [p for p in parts if p['reviewed_defective'] is not None]
    tp = sum(p['predicted_defective'] and p['reviewed_defective'] for p in reviewed)
    tn = sum(not p['predicted_defective'] and not p['reviewed_defective'] for p in reviewed)
    fp = sum(p['predicted_defective'] and not p['reviewed_defective'] for p in reviewed)
    fn = sum(not p['predicted_defective'] and p['reviewed_defective'] for p in reviewed)
    return {'total':len(parts), 'reviewed':len(reviewed), 'unreviewed':len(parts)-len(reviewed),
            'defects':tp+fn, 'acceptable':tn+fp, 'true_positive':tp, 'true_negative':tn,
            'false_positive':fp, 'false_negative':fn,
            'agreement_pct':round(100*(tp+tn)/len(reviewed), 1) if reviewed else None,
            'defect_pct':round(100*(tp+fn)/len(reviewed), 1) if reviewed else None,
            'model_rejected':sum(p['predicted_defective'] for p in parts)}


def review_part(conn, run_id, number, defective):
    part = get_part(conn, run_id, number)
    with conn.transaction(), conn.cursor() as cur:
        write_review(cur, part, defective, 'Your inspection')


def complete_reviews(conn, run_id):
    get_run(conn, run_id)
    remaining = [p for p in read_parts(conn, run_id, basis(conn)) if p['reviewed_defective'] is None]
    with conn.transaction(), conn.cursor() as cur:
        for part in remaining:
            write_review(cur, {'_id':part['part_id'], 'run_id':run_id, 'produced_at':part['produced_at']},
                         part['appearance'] in ('crack', 'hairline'), 'Scripted inspection')


def save_report(conn, run_id, title='Saved review report'):
    get_run(conn, run_id)
    timestamp = basis(conn)
    report_id = uuid4().hex
    with conn.transaction(), conn.cursor() as cur:
        cur.execute('''INSERT INTO report (_id, run_id, title, basis, recorded_at)
                       VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)''', (report_id, run_id, title, timestamp))
    return report_id


def state(conn, run_id):
    get_run(conn, run_id)
    timestamp = basis(conn)
    parts = read_parts(conn, run_id, timestamp)
    reports = rows(conn, 'SELECT * FROM report WHERE run_id=%s ORDER BY recorded_at', (run_id,))
    return {'run_id':run_id, 'basis':timestamp, 'parts':parts, 'metrics':metrics(parts), 'reports':reports,
            'sql':as_of(SQL, timestamp)}


def replay(conn, run_id, report_id):
    get_run(conn, run_id)
    found = rows(conn, 'SELECT * FROM report WHERE _id=%s AND run_id=%s', (report_id, run_id))
    if not found:
        raise NotFound('Report not found in this batch.')
    report = found[0]
    parts = read_parts(conn, run_id, report['basis'])
    return {'report':report, 'parts':parts, 'metrics':metrics(parts), 'sql':as_of(SQL, report['basis'])}
