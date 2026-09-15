# Agents

Read README.md for setup, the data model, and tests.
Use `uv` for Python and `pnpm` in `ui/`.
Keep SQL comprehensible and preserve the UI's access to the queries behind its results.

- Keep queries compatible with the pinned XTDB image; use self-joins rather than window frames in `model._window_sql`.
- Use `txn_ts` for event-time windows and exclude the transaction being scored from its own history.
- `label` valid time starts when a classification became available; keep the latest interval open-ended.
- Train with `as_known_then` from label history; use current labels for targets and hindsight, preserving the system-time basis across every table.
- Keep training and serving feature semantics aligned.
- Test SQL behaviour against the separate playground, never the demo database.
- Start the playground before running the suite and report any skipped tests.
- Use GET healthchecks (`wget -q -O /dev/null`); the node rejects HEAD requests.

Use conventional commits and sentence-per-line Markdown.
