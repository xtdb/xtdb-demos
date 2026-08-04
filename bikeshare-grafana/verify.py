# /// script
# dependencies = ["psycopg[binary]>=3.1"]
# ///
"""Smoke-test the compat surface this demo depends on.

Three groups:

1. The introspection queries Grafana's PostgreSQL plugin issues on its own —
   version detection, TimescaleDB detection, and the table/column discovery
   queries behind the visual query builder. No dashboard panel exercises these,
   so nothing else catches a pg_catalog regression.
2. Every panel query in both dashboards, with template variables and Grafana's
   macros substituted.
3. KNOWN_GAPS — queries that currently *fail* on the pinned XTDB. Listed so the
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

import json
import os
import re
import sys
from pathlib import Path

import psycopg

DSN = os.environ.get("XTDB_DSN", "host=localhost port=5432 user=xtdb dbname=xtdb")

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
    ("table discovery",
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
]


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


def panel_queries() -> list[tuple[str, str, int]]:
    """Every rawSql in both dashboards, variables and macros substituted."""
    out = []
    for path in sorted(Path("grafana/dashboards").glob("*.json")):
        dash = json.loads(path.read_text())
        subs = {
            v["name"]: (v.get("options") or [{}])[0].get("value", "0")
            for v in dash.get("templating", {}).get("list", [])
        }
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


def run(conn, sql: str) -> tuple[bool, int, str]:
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall() if cur.description else []
        return True, len(rows), ""
    except Exception as exc:
        return False, 0, str(exc).splitlines()[0]


def main() -> int:
    checks = (
        [("introspection", *c) for c in INTROSPECTION]
        + [("macro", *c) for c in MACROS]
        + [("panel", *c) for c in panel_queries()]
    )

    failures = []
    with psycopg.connect(DSN, autocommit=True) as conn:
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
    print(f"\n{total - len(failures)}/{total} checks passed,"
          f" {len(KNOWN_GAPS) - len(fixed)} known gaps still open")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
