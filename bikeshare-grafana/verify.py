# /// script
# dependencies = ["psycopg[binary]>=3.1"]
# ///
"""Smoke-test the compat surface this demo depends on.

Four groups:

1. The introspection queries Grafana's PostgreSQL plugin issues on its own —
   version detection, TimescaleDB detection, and the table/column discovery
   queries behind the visual query builder. No dashboard panel exercises these,
   so nothing else catches a pg_catalog regression.
2. Every panel query in both dashboards, with template variables and Grafana's
   macros substituted.
3. The correction: assertions about the *content* of the second temporal axis,
   not just that its queries run. Row counts can't tell you whether a backdated
   correction actually produced two different beliefs about the same instant.
4. KNOWN_GAPS — queries that currently *fail* on the pinned XTDB. Listed so the
   gaps are visible rather than folklore, and so we hear about it when one
   starts working.

Checks carry a `min_rows`, because several of these fail by returning nothing
rather than by erroring: an empty column list means Grafana's builder has no
columns to offer, which is a failure that a bare "did it throw?" check waves
through.

Run after `docker compose up -d && uv run seed.py`.
Exits non-zero on any unexpected result, so it works as a CI gate.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import psycopg

DSN = os.environ.get("XTDB_DSN", "host=localhost port=5442 user=xtdb dbname=xtdb")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:3002")
GRAFANA_AUTH = os.environ.get("GRAFANA_AUTH", "admin:admin")

# Grafana resolves search_path into schema names with this subquery. It needs
# string_to_array, array_lower/array_upper, two-arg generate_series, and an
# array-valued function in FROM position whose alias binds as a column.
SCHEMA_CONSTRAINT = """
quote_ident(table_schema) IN (
  SELECT CASE WHEN trim(s[i]) = '"$user"' THEN user ELSE trim(s[i]) END
  FROM generate_series(
         array_lower(string_to_array(current_setting('search_path'), ','), 1),
         array_upper(string_to_array(current_setting('search_path'), ','), 1)
       ) AS i,
       string_to_array(current_setting('search_path'), ',') s
)
"""

# (name, sql, min_rows)
INTROSPECTION = [
    ("version detection",
     "SELECT current_setting('server_version_num')::int/100 AS version", 1),
    ("timescaledb detection (expects empty)",
     "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'", 0),
    ("comment-only query (plugin ping)", "-- ping", 0),
    # Grafana's real table-discovery query is in KNOWN_GAPS: the schema constraint
    # doesn't plan. This is the same query with the constraint removed, so a failure
    # here means the catalog itself has regressed rather than the search_path lookup.
    ("table discovery (no schema constraint)",
     """
     SELECT quote_ident(table_schema) || '.' || quote_ident(table_name) AS "table"
     FROM information_schema.tables
     WHERE quote_ident(table_schema) NOT IN (
       'information_schema', 'pg_catalog',
       '_timescaledb_cache', '_timescaledb_catalog',
       '_timescaledb_internal', '_timescaledb_config',
       'timescaledb_information', 'timescaledb_experimental'
     )
     ORDER BY 1
     """, 2),
    # Without the schema constraint the same query finds all 10 columns, so this
    # isolates the constraint rather than the catalog.
    ("column discovery (no schema constraint)",
     """
     SELECT quote_ident(column_name) AS "column", data_type AS "type"
     FROM information_schema.columns
     WHERE quote_ident(table_name) = 'stations'
     """, 5),
]

MACROS = [
    ("$__timeFilter expansion",
     "SELECT COUNT(*) FROM rides"
     " WHERE started_at BETWEEN NOW() - INTERVAL '7' DAY AND NOW()", 1),
    # $__timeGroup emits GROUP BY over the repeated expression, which works.
    ("$__timeGroup expansion (expression repeated)",
     "SELECT floor(extract(epoch from started_at)/300)*300 AS t, COUNT(*) FROM rides"
     " GROUP BY floor(extract(epoch from started_at)/300)*300 ORDER BY t LIMIT 5", 1),
]

# Verified failing on XTDB 2.2.0-rc0. Each is reduced to the smallest repro.
KNOWN_GAPS = [
    ("array-valued function in FROM binds no column",
     "SELECT s FROM string_to_array('a,b', ',') s",
     "Postgres binds the alias as the function's result column; XTDB accepts the"
     " relation but 'Column not found: s'. Blocks Grafana's schema-constraint"
     " subquery, so column discovery returns 0 rows instead of 10."),
    ("GROUP BY by ordinal",
     "SELECT user_type, COUNT(*) FROM rides GROUP BY 1",
     "'Missing grouping columns'. ORDER BY 1 works; GROUP BY 1 does not."
     " Repeating the full expression is the workaround."),
    ("table discovery (Grafana's actual query)",
     f"""
     SELECT CASE WHEN {SCHEMA_CONSTRAINT}
              THEN quote_ident(table_name)
              ELSE quote_ident(table_schema) || '.' || quote_ident(table_name)
            END AS "table"
     FROM information_schema.tables
     WHERE quote_ident(table_schema) NOT IN (
       'information_schema', 'pg_catalog',
       '_timescaledb_cache', '_timescaledb_catalog',
       '_timescaledb_internal', '_timescaledb_config',
       'timescaledb_information', 'timescaledb_experimental'
     )
     ORDER BY CASE WHEN {SCHEMA_CONSTRAINT} THEN 0 ELSE 1 END, 1
     """,
     "Fails to plan with 'Column not found: s' / 'Column not found: i' — the"
     " FROM-alias gap again, so the query builder can't even list tables. Panels"
     " with hand-written SQL are unaffected, which is why both dashboards work."),
    ("column discovery (Grafana's actual query)",
     f"""
     SELECT quote_ident(column_name) AS "column", data_type AS "type"
     FROM information_schema.columns
     WHERE CASE WHEN array_length(parse_ident('stations'), 1) = 2
              THEN quote_ident(table_schema) = (parse_ident('stations'))[1]
                AND quote_ident(table_name) = (parse_ident('stations'))[2]
              ELSE quote_ident(table_name) = 'stations'
                AND {SCHEMA_CONSTRAINT}
            END
     """,
     "Returns 0 rows rather than erroring, downstream of the FROM-alias gap."),
    ("subquery as a temporal AS OF bound",
     "SELECT capacity FROM stations FOR SYSTEM_TIME AS OF"
     " (SELECT MIN(_system_from) FROM stations FOR ALL SYSTEM_TIME)",
     "'Subqueries are not allowed in this context'. Would let a panel name a"
     " knowledge basis by derivation ('as first recorded') instead of pinning a"
     " literal timestamp. The dashboard works around it with a Grafana query"
     " variable that resolves the timestamp client-side."),
]

# The dashboard derives its knowledge bases from the distinct _system_from values
# on stations, so 'as first recorded' is only meaningful if the seed wrote every
# station version in ONE transaction. If a future seed batches them, this count
# grows and the basis dropdown quietly starts offering half-written states.
EXPECTED_BASES = 2


def expand_grafana_macros(sql: str) -> str:
    """Stand in for the expansion Grafana's backend does before sending SQL.

    Only the macros these dashboards use. We care that the expanded SQL runs,
    not what it returns, so the window is arbitrary.
    """
    return re.sub(
        r"\$__timeFilter\(\s*([\w.]+)\s*\)",
        r"\1 BETWEEN NOW() - INTERVAL '30' DAY AND NOW()",
        sql,
    )


def resolve_variables(conn, dash) -> dict[str, str]:
    """Pick one value per template variable, the way Grafana picks a default.

    Custom variables carry their options inline. Query variables don't — Grafana
    runs their SQL and reads the `__value` column, so we do too. That means a
    broken variable query fails here rather than showing up as an empty dropdown
    in the browser.
    """
    subs = {}
    for v in dash.get("templating", {}).get("list", []):
        if v.get("type") == "query":
            raw = v.get("query")
            raw = raw.get("rawSql") if isinstance(raw, dict) else raw
            with conn.cursor() as cur:
                cur.execute(raw)
                cols = [d.name for d in cur.description]
                rows = cur.fetchall()
            if not rows:
                raise SystemExit(f"variable ${v['name']} resolved to no rows")
            col = cols.index("__value") if "__value" in cols else 0
            subs[v["name"]] = rows[0][col]
        else:
            subs[v["name"]] = (v.get("options") or [{}])[0].get("value", "0")
    return subs


def panel_queries(conn) -> list[tuple[str, str, int]]:
    """Every rawSql in both dashboards, variables and macros substituted."""
    out = []
    for path in sorted(Path("grafana/dashboards").glob("*.json")):
        dash = json.loads(path.read_text())
        subs = resolve_variables(conn, dash)
        for panel in dash.get("panels", []):
            for target in panel.get("targets", []):
                raw = target.get("rawSql")
                if not raw:
                    continue
                sql = expand_grafana_macros(raw)
                for name, value in subs.items():
                    sql = sql.replace(f"${{{name}}}", str(value))
                if leftover := re.findall(r"\$(?:__)?\{?\w+\}?", sql):
                    raise SystemExit(
                        f"unsubstituted variable in {path.name} /"
                        f" {panel.get('title')}: {leftover}"
                    )
                # Every panel is expected to render something; a panel that
                # silently returns nothing is a broken panel.
                out.append((f"{path.stem} / {panel.get('title')}", sql, 1))
    return out


def grafana_query(sql: str) -> tuple[list, str]:
    """Run SQL through Grafana's datasource proxy — the path the dashboards use.

    Returns (columns, error). Raises ConnectionError if Grafana isn't there at all.
    """
    body = json.dumps({
        "queries": [{"refId": "A", "format": "table",
                     "datasource": {"type": "postgres", "uid": "DS_XTDB"},
                     "rawSql": sql}],
        "from": "now-30d", "to": "now",
    }).encode()
    token = base64.b64encode(GRAFANA_AUTH.encode()).decode()
    req = urllib.request.Request(
        f"{GRAFANA_URL}/api/ds/query", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Basic {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read())
    except urllib.error.URLError as exc:
        raise ConnectionError(str(exc.reason)) from exc
    result = payload.get("results", {}).get("A", {})
    if err := result.get("error"):
        return [], " ".join(err.split())
    frame = (result.get("frames") or [{}])[0]
    return frame.get("data", {}).get("values", []), ""


def check_grafana(conn) -> list[str]:
    """Cross-check that Grafana and this script are looking at the same database.

    Everything else here connects to XTDB directly on the host port. Grafana
    doesn't — it reaches XTDB over the compose network. When those two resolve to
    different nodes (anything already bound to host 5432 does it, and silently)
    every other check passes against a database no dashboard can read.

    This is also the only place a malformed template variable surfaces without
    opening a browser: Grafana wants a variable's `query` as a raw SQL string, and
    hands back a 500 for the object form the panel editor uses.
    """
    fails = []

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM stations")
        direct = cur.fetchone()[0]

    try:
        values, err = grafana_query("SELECT COUNT(*) AS n FROM stations")
    except ConnectionError as exc:
        print(f"  SKIPPED no Grafana at {GRAFANA_URL} ({exc}) — nothing checks that"
              f" the dashboards can see what {DSN.split()[1]} can")
        return []

    if err:
        return [f"Grafana cannot query stations at all: {err}"]
    through = values[0][0]
    if through != direct:
        fails.append(f"split brain: {direct} stations over {DSN.split()[1]},"
                     f" {through} through Grafana — check nothing else owns the host"
                     f" port XTDB_DSN points at")
    else:
        print(f"  same node: {direct} stations both directly and through Grafana")

    for path in sorted(Path("grafana/dashboards").glob("*.json")):
        dash = json.loads(path.read_text())
        for v in dash.get("templating", {}).get("list", []):
            if v.get("type") != "query":
                continue
            if not isinstance(v.get("query"), str):
                fails.append(f"{path.name}: variable ${v['name']} has a non-string"
                             f" query; Grafana answers 500 for the object form")
                continue
            values, err = grafana_query(v["query"])
            if err:
                fails.append(f"{path.name}: variable ${v['name']} failed"
                             f" through Grafana: {err}")
            elif not values or not values[0]:
                fails.append(f"{path.name}: variable ${v['name']} resolved to an"
                             f" empty dropdown through Grafana")
            else:
                print(f"  variable ${v['name']} -> {[c[0] for c in values]}")

    return fails


def check_correction(conn) -> list[str]:
    """Assert the backdated correction produced a genuine second axis.

    Four quadrants, one station, two knowledge bases:

                              valid time inside      valid time before
                              the corrected window   the backdate point
      as first recorded       old capacity           unchanged
      current knowledge       new capacity           unchanged

    The bottom-left cell is the only one that differs, and it is the whole point
    of the demo: same instant of the world, two different records of it. If a
    seed change flattens that, every 'as of' panel still runs and still returns
    rows, so nothing else here would notice.
    """
    fails = []

    def q(sql):
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()

    bases = [r[0] for r in q(
        "SELECT DISTINCT CAST(_system_from AS VARCHAR) AS basis FROM stations"
        " FOR ALL SYSTEM_TIME FOR ALL VALID_TIME ORDER BY 1"
    )]
    print(f"  knowledge bases on stations: {len(bases)}")
    if len(bases) != EXPECTED_BASES:
        return [f"expected {EXPECTED_BASES} knowledge bases on stations,"
                f" found {len(bases)}: {bases}"]
    first, current = bases

    # Everything the correction transaction wrote.
    corrected = q(f"""
        SELECT name, capacity, CAST(_valid_from AS VARCHAR) AS vf
        FROM stations FOR ALL SYSTEM_TIME FOR ALL VALID_TIME
        WHERE _system_from > TIMESTAMP '{first}'
    """)
    names = {r[0] for r in corrected}
    if len(names) != 1:
        fails.append(f"expected exactly one corrected station, found {sorted(names)}")
        return fails
    station = names.pop()
    print(f"  corrected station: {station}")

    def capacity_at(valid_sql: str, basis: str):
        rows = q(f"""
            SELECT capacity FROM stations
            FOR VALID_TIME AS OF {valid_sql}
            FOR SYSTEM_TIME AS OF TIMESTAMP '{basis}'
            WHERE name = '{station}'
        """)
        return rows[0][0] if rows else None

    def fleet_at(valid_sql: str, basis: str):
        return q(f"""
            SELECT SUM(capacity) AS total FROM stations
            FOR VALID_TIME AS OF {valid_sql}
            FOR SYSTEM_TIME AS OF TIMESTAMP '{basis}'
        """)[0][0]

    inside, outside = "NOW()", "(NOW() - INTERVAL '25' DAY)"

    was, is_now = capacity_at(inside, first), capacity_at(inside, current)
    if was is None or is_now is None:
        fails.append(f"{station} missing at one basis: first={was} current={is_now}")
    elif was == is_now:
        fails.append(f"no restatement inside the corrected window:"
                     f" {station} reads {was} at both bases")
    else:
        print(f"  inside the window:  {was} as first recorded -> {is_now} now")

    before_was, before_now = capacity_at(outside, first), capacity_at(outside, current)
    if before_was != before_now:
        fails.append(f"correction leaked outside its backdated window:"
                     f" {station} at -25d reads {before_was} then {before_now}")
    else:
        print(f"  before the window:  {before_was} at both bases (correction is bounded)")

    fleet_was, fleet_now = fleet_at(inside, first), fleet_at(inside, current)
    if fleet_was == fleet_now:
        fails.append("fleet capacity is identical across both bases — the"
                     " correction doesn't reach the aggregate panels")
    else:
        print(f"  fleet capacity:     {fleet_was} as first recorded"
              f" -> {fleet_now} now ({fleet_now - fleet_was:+d})")

    if fleet_at(outside, first) != fleet_at(outside, current):
        fails.append("fleet capacity differs across bases at -25d, outside the"
                     " corrected window")

    return fails


def run(conn, sql: str) -> tuple[bool, int, str]:
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall() if cur.description else []
        return True, len(rows), ""
    except Exception as exc:
        return False, 0, str(exc).splitlines()[0]


def main() -> int:
    failures = []
    with psycopg.connect(DSN, autocommit=True) as conn:
        checks = (
            [("introspection", *c) for c in INTROSPECTION]
            + [("macro", *c) for c in MACROS]
            + [("panel", *c) for c in panel_queries(conn)]
        )

        for group, name, sql, min_rows in checks:
            ok, n, err = run(conn, sql)
            if not ok:
                failures.append(name)
                print(f"  FAIL [{group}] {name}: {err}", file=sys.stderr)
            elif n < min_rows:
                failures.append(name)
                print(f"  FAIL [{group}] {name}: {n} rows, expected >= {min_rows}",
                      file=sys.stderr)
            else:
                print(f"  ok   [{group}] {name} -> {n} row(s)")

        print("\nThrough Grafana:")
        grafana_fails = check_grafana(conn)
        for f in grafana_fails:
            print(f"  FAIL [grafana] {f}", file=sys.stderr)
        failures.extend(grafana_fails)

        print("\nThe correction (second temporal axis):")
        correction_fails = check_correction(conn)
        for f in correction_fails:
            print(f"  FAIL [correction] {f}", file=sys.stderr)
        failures.extend(correction_fails)

        print("\nKnown gaps on the pinned XTDB:")
        fixed = []
        for name, sql, note in KNOWN_GAPS:
            ok, n, err = run(conn, sql)
            # The column-discovery gap fails by emptiness, the others by error.
            still_broken = (not ok) or n == 0
            if still_broken:
                print(f"  xfail  {name}\n         {note}")
            else:
                fixed.append(name)
                print(f"  FIXED  {name} -> {n} row(s) — drop it from KNOWN_GAPS"
                      f" and revisit the README")

    total = len(checks)
    query_failures = len(failures) - len(correction_fails) - len(grafana_fails)
    print(f"\n{total - query_failures}/{total} query checks passed,"
          f" grafana {'ok' if not grafana_fails else f'FAILED ({len(grafana_fails)})'},"
          f" correction {'ok' if not correction_fails else f'FAILED ({len(correction_fails)})'},"
          f" {len(KNOWN_GAPS) - len(fixed)} known gaps still open")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
