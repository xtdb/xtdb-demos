# Bikeshare on Grafana

Grafana's **stock PostgreSQL data source** pointed at XTDB. No plugin, no fork.

Two dashboards:

- `bikeshare` — an ordinary operational dashboard. Stat tiles, time series, donuts,
  a geomap, a table. The point is that it is unremarkable: this is what any Postgres
  data source gets you.
- `bikeshare-asof` — the same data, with an **As of** dropdown that rewinds every
  panel to what the database held at a chosen point in the past, via
  `FOR VALID_TIME AS OF`. That one is not available anywhere else.

![As-of dashboard](images/bikeshare-asof-dashboard.png)

## Run it

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
docker compose up -d          # XTDB on :5432, Grafana on :3002
uv run seed.py                # ~24 stations, ~12k rides, 30 days of history
open http://localhost:3002
```

Grafana is provisioned with the data source and both dashboards already loaded,
and anonymous admin access is on, so there is no login step.

Tear down with `docker compose down -v`.

## What the as-of dashboard does

Every panel takes the dashboard's `as_of` variable and interpolates it into the
query's valid-time clause:

```sql
SELECT name, capacity
FROM stations FOR VALID_TIME AS OF (NOW() - INTERVAL '${days_back}' DAY)
ORDER BY capacity DESC
```

The seed script writes each station as a series of versions with explicit
`_valid_from` / `_valid_to` bounds — capacity expansions, a couple of mid-window
closures, a couple of late openings. Moving the dropdown from *Now* to *14 days
ago* changes the fleet capacity total, drops the stations that had not opened yet,
and restores the ones since closed.

The "Capacity history" panel selects the raw versions, so you can see the
bitemporal rows the other panels are resolving against.

## Versions

Pinned to **XTDB 2.2.0-rc0**, the first release carrying the catalog functions
Grafana's plugin needs (`quote_ident`, `parse_ident`, `string_to_array`,
`array_lower`, `array_length`, two-arg `generate_series`,
`current_setting('search_path')`, and a `pg_extension` stub). Earlier releases
connect but introspect nothing.

Grafana is pinned to 12.2.

## Verify it

```bash
uv run verify.py
```

Replays Grafana's introspection queries and every dashboard panel against the
running node, asserting row counts rather than just "did it throw" — several of
these fail by returning nothing. It also re-checks the known gaps below, and says
so if one has started working.

## Known gaps

Verified against 2.2.0-rc0 by `verify.py`. All three are XTDB-side.

**An array-valued function in `FROM` binds no column.**

```sql
SELECT s FROM string_to_array('a,b', ',') s   -- Column not found: s
```

Postgres binds the alias as the function's result column. XTDB accepts the
relation but won't resolve `s`. Grafana's search-path subquery depends on exactly
this, so its column-discovery query returns **0 rows** where the same query
without the schema constraint finds all 10. `unnest()` works, so there is a
rewrite available.

**`GROUP BY` by ordinal is unsupported.**

```sql
SELECT user_type, COUNT(*) FROM rides GROUP BY 1   -- Missing grouping columns
```

`ORDER BY 1` works; `GROUP BY 1` doesn't. Repeat the full expression instead —
which is what the `$__timeGroup` panels here do.

**The image's Docker healthcheck can never pass.** `HEALTHCHECK` uses `wget
--spider`, i.e. HEAD, and `/healthz/alive` answers 405, so every container
reports `unhealthy` forever and `depends_on: condition: service_healthy`
deadlocks against the official image. This compose file overrides it with a GET.

## Demo rough edges

- It travels **valid time only** — history, not corrections. No panel contrasts
  "what was true then" with "what we believed then" (system time). That second
  axis is the part nothing else can do, and it is the most valuable thing to add.
- Data is synthetic, from a fixed seed. Realistic in shape, invented in substance.
- Categorical colours aren't stable across panels: the `user type` and `bike type`
  donuts reuse the same green/yellow pair for different categories.
- Panel queries quote their aliases (`AS "time"`, `AS "hour"`) to sidestep
  reserved-keyword handling.
- Whether Grafana's *visual query builder* populates its dropdowns is untested.
  The column-discovery gap above suggests it won't, but that was measured by
  replaying SQL, not by driving the UI.
