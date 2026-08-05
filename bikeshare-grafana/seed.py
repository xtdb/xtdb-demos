# /// script
# dependencies = ["psycopg[binary]>=3.1"]
# ///
"""Seed a small but realistic bike-share dataset into XTDB.

Stations:   ~24 across 4 neighborhoods, with capacity + lat/lon, written as
            bitemporal versions so FOR VALID_TIME AS OF has something to rewind.
Rides:      ~12000 over the last 30 days, with rush-hour weekday patterns,
            weekend leisure mix, and electric/classic bike split.
Correction: one station's capacity was recorded wrong from the start, then fixed
            in a later transaction and backdated. That gives the dataset a second
            temporal axis: valid time says what was true, system time says what we
            had recorded at the time. History alone only needs the first.

Point it elsewhere with XTDB_DSN, e.g. at a dev REPL on port 5440.
"""

from __future__ import annotations

import os
import random
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg import sql

DSN = os.environ.get("XTDB_DSN", "host=localhost port=5442 user=xtdb dbname=xtdb")
SEED = 42
NOW = datetime.now(tz=timezone.utc).replace(microsecond=0)
DAYS_BACK = 30
TARGET_RIDES = 12000
# The audit that found the mis-recorded capacity. Inside the shortest few 'As of'
# options so the dashboard shows the correction at 0/1/3/7 days back and not at
# 14/21/30 — the dropdown then demonstrates a bounded restatement, not a global one.
CORRECTION_BACKDATED_DAYS = 12
CORRECTION_UNDERCOUNT = 15

NEIGHBORHOODS = [
    ("Midtown",    40.755, -73.985),
    ("Downtown",   40.715, -74.005),
    ("Brooklyn",   40.690, -73.985),
    ("UpperWest",  40.785, -73.975),
]

STATION_NAMES = [
    "Park & 5th", "Library Plaza", "Grand Central", "Union Square",
    "City Hall", "Battery Park", "Wall St", "Stone St",
    "Bedford Ave", "Atlantic Ave", "Prospect Pk W", "DUMBO",
    "Lincoln Ctr", "Columbus Cir", "Broadway 86", "West 79",
    "Madison Park", "Bryant Park", "Times Sq", "Penn Stn",
    "Chambers St", "Fulton St", "Bowery", "East River",
]


def jitter(coord: float, scale: float = 0.008) -> float:
    return coord + random.uniform(-scale, scale)


def station_rows():
    rng = random.Random(SEED)
    rows = []
    per_hood = len(STATION_NAMES) // len(NEIGHBORHOODS)
    for hi, (hood, hlat, hlon) in enumerate(NEIGHBORHOODS):
        names = STATION_NAMES[hi * per_hood:(hi + 1) * per_hood]
        for i, name in enumerate(names, start=1):
            sid = hi * 100 + i
            rows.append((
                sid,
                f"{hood} - {name}",
                hood,
                rng.choice([15, 20, 25, 30, 35, 40]),
                jitter(hlat, 0.012),
                jitter(hlon, 0.012),
            ))
    return rows


def hour_weight(ts: datetime) -> float:
    """Probability multiplier for a ride starting at this hour.
    Weekdays peak at 8-9am and 5-6pm; weekends are flatter with afternoon peak.
    """
    h = ts.hour
    if ts.weekday() < 5:  # weekday
        if 7 <= h <= 9:   return 3.5
        if 16 <= h <= 19: return 4.0
        if 10 <= h <= 15: return 1.2
        if 20 <= h <= 22: return 0.9
        return 0.7         # overnight floor so no hour is empty
    else:                 # weekend
        if 11 <= h <= 17: return 2.5
        if 9 <= h <= 22:  return 1.4
        return 0.8


def ride_rows(stations):
    rng = random.Random(SEED + 1)
    by_id = {s[0]: s for s in stations}
    sids = list(by_id.keys())

    # Pre-build per-hour weights so we can sample timestamps according to demand.
    candidates: list[tuple[datetime, float]] = []
    cursor = NOW - timedelta(days=DAYS_BACK)
    while cursor < NOW:
        candidates.append((cursor, hour_weight(cursor)))
        cursor += timedelta(minutes=10)
    weights = [w for _, w in candidates]
    times = [t for t, _ in candidates]

    rows = []
    for _ in range(TARGET_RIDES):
        slot = rng.choices(times, weights=weights, k=1)[0]
        started = slot + timedelta(seconds=rng.randint(0, 599))
        bike = rng.choices(["classic", "electric"], weights=[0.65, 0.35])[0]
        # electric trips are shorter on average
        base = 12 * 60 if bike == "classic" else 8 * 60
        duration = max(60, int(rng.gauss(base, base * 0.5)))
        ended = started + timedelta(seconds=duration)
        if ended > NOW:
            continue
        start_id = rng.choice(sids)
        # 80% of trips end within the same neighborhood
        if rng.random() < 0.8:
            hood = by_id[start_id][2]
            same_hood = [s for s in sids if by_id[s][2] == hood and s != start_id]
            end_id = rng.choice(same_hood) if same_hood else rng.choice(sids)
        else:
            end_id = rng.choice([s for s in sids if s != start_id])
        # weekday rush-hour skews toward members
        is_rush = started.weekday() < 5 and (7 <= started.hour <= 9 or 16 <= started.hour <= 19)
        user_type = "member" if rng.random() < (0.85 if is_rush else 0.55) else "casual"
        rows.append((
            str(uuid.uuid4()),
            started,
            ended,
            start_id,
            end_id,
            user_type,
            bike,
            duration,
        ))
    return rows


def station_history(stations):
    """Return (versions, static_ids).

    versions is a list of (station_row, valid_from, valid_to). Most stations get a
    single open-ended version covering the whole window; a handful demonstrate
    capacity expansions, mid-window closures, and new openings.

    static_ids are the stations with exactly one version. The correction targets one
    of those, so its two beliefs aren't tangled up with a valid-time change as well.
    """
    rng = random.Random(SEED + 2)
    window_start = NOW - timedelta(days=DAYS_BACK)
    versions: list[tuple[tuple, datetime, datetime | None]] = []

    # pick 3 expansions, 2 closures, 2 late openings
    by_id = {s[0]: s for s in stations}
    sids = list(by_id.keys())
    rng.shuffle(sids)
    expand_ids = sids[:3]
    close_ids  = sids[3:5]
    new_ids    = sids[5:7]
    static_ids = set(sids[7:])

    for sid in static_ids:
        versions.append((by_id[sid], window_start, None))

    for sid in expand_ids:
        s = by_id[sid]
        change = window_start + timedelta(days=rng.randint(8, 22))
        old_cap = s[3]
        new_cap = old_cap + rng.choice([10, 15, 20])
        versions.append(((s[0], s[1], s[2], old_cap, s[4], s[5]), window_start, change))
        versions.append(((s[0], s[1], s[2], new_cap, s[4], s[5]), change, None))

    for sid in close_ids:
        s = by_id[sid]
        close_at = window_start + timedelta(days=rng.randint(15, 25))
        versions.append((s, window_start, close_at))

    for sid in new_ids:
        s = by_id[sid]
        open_at = window_start + timedelta(days=rng.randint(5, 15))
        versions.append((s, open_at, None))

    return versions, static_ids


def correction_target(stations, static_ids):
    """Pick the station whose capacity was mis-recorded, and by how much."""
    rng = random.Random(SEED + 3)
    sid = rng.choice(sorted(static_ids))
    s = next(x for x in stations if x[0] == sid)
    return sid, s[1], s[3], s[3] + CORRECTION_UNDERCOUNT


def main():
    stations = station_rows()
    rides = ride_rows(stations)
    history, static_ids = station_history(stations)
    print(f"Generated {len(stations)} stations, {len(history)} station versions, {len(rides)} rides")

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            # XTDB has no CREATE TABLE, so tables exist only once written to, and
            # ERASE against a table that isn't there is a planning error rather
            # than a no-op. Re-runnable means checking first.
            cur.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = 'public' AND table_name IN ('stations', 'rides')"
            )
            existing = {row[0] for row in cur.fetchall()}
            for table in ("stations", "rides"):
                if table in existing:
                    cur.execute(
                        sql.SQL("ERASE FROM {} WHERE _id IS NOT NULL").format(sql.Identifier(table))
                    )
                    print(f"  cleared existing {table}")

        # XTDB pgwire rejects untyped DML params; embed values as SQL literals instead.
        def insert_rows(table: str, columns: list[str], rows: list[tuple], batch: int = 500):
            cols_sql = sql.SQL(",").join(map(sql.Identifier, columns))
            for i in range(0, len(rows), batch):
                values_sql = sql.SQL(",").join(
                    sql.SQL("({})").format(sql.SQL(",").join(sql.Literal(v) for v in row))
                    for row in rows[i:i + batch]
                )
                stmt = sql.SQL("INSERT INTO {} ({}) VALUES {}").format(
                    sql.Identifier(table), cols_sql, values_sql
                )
                with conn.cursor() as cur:
                    cur.execute(stmt)
                print(f"  {table}: inserted {min(i + batch, len(rows))}/{len(rows)}")

        # Insert each station version with its valid_from/valid_to so FOR VALID_TIME AS OF works.
        station_versions = [
            (sid, name, neigh, cap, lat, lon, vf, vt)
            for ((sid, name, neigh, cap, lat, lon), vf, vt) in history
        ]
        # One statement, deliberately: the dashboard's 'as first recorded' basis is
        # the earliest _system_from on stations, so splitting these across
        # transactions would make that basis a half-written table. verify.py asserts
        # stations carries exactly two distinct _system_from values.
        insert_rows("stations",
                    ["_id", "name", "neighborhood", "capacity", "lat", "lon",
                     "_valid_from", "_valid_to"],
                    station_versions, batch=len(station_versions))
        insert_rows("rides",
                    ["_id", "started_at", "ended_at", "start_station_id", "end_station_id",
                     "user_type", "bike_type", "duration_seconds"],
                    rides)

        # The correction, in its own transaction so it lands at a later system time
        # than the original recording. FOR PORTION OF VALID_TIME confines it to the
        # window the audit actually covers, so earlier valid time keeps the old value
        # under both bases — a restatement, not a rewrite.
        sid, name, old_cap, new_cap = correction_target(stations, static_ids)
        audit_date = NOW - timedelta(days=CORRECTION_BACKDATED_DAYS)
        with conn.cursor() as cur:
            # Spelled `TIMESTAMP '...'` rather than passing a datetime: psycopg
            # renders those as `'...'::timestamptz`, and XTDB's parser rejects a cast
            # in a temporal bound even though it accepts one in VALUES.
            cur.execute(
                sql.SQL(
                    "UPDATE stations FOR PORTION OF VALID_TIME FROM TIMESTAMP {audit}"
                    " TO NULL SET capacity = {cap} WHERE _id = {sid}"
                ).format(audit=sql.Literal(audit_date.strftime("%Y-%m-%dT%H:%M:%SZ")),
                         cap=sql.Literal(new_cap),
                         sid=sql.Literal(sid))
            )
        print(f"  correction: {name} capacity {old_cap} -> {new_cap},"
              f" backdated to {audit_date.date()} ({CORRECTION_BACKDATED_DAYS}d ago)")

    print("done")


if __name__ == "__main__":
    main()
