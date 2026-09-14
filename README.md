# XTDB demos

Runnable demos of [XTDB](https://xtdb.com), one per directory. Each is
self-contained, brings up its own XTDB via Docker, and is **pinned to a released
version** so it keeps working after `main` moves on.

| Demo | What it shows |
| --- | --- |
| [`bikeshare-grafana`](bikeshare-grafana) | Grafana's stock PostgreSQL data source against XTDB, plus a dashboard that rewinds to a point in the past |
| [`fraud-detection`](fraud-detection) | Point-in-time fraud features, historical training, and replaying a score after a late chargeback |

## Conventions

Each demo directory has:

- a `README.md` with the run steps and the version it is pinned to;
- a `docker-compose.yml` that starts everything it needs, including XTDB itself;
- an honest "known rough edges" section.

A demo that needs a locally-built XTDB, or a dev REPL, does not belong here — that
is a branch in [`xtdb/xtdb`](https://github.com/xtdb/xtdb). The dividing line is
whether someone who has never cloned XTDB can run it.

## Related

- [`xtdb/driver-examples`](https://github.com/xtdb/driver-examples) — minimal
  connection examples per language.
