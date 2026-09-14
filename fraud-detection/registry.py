"""Feature registry — the heart of the 'a feature is a query' thesis.

Each feature is a standalone SQL query computing one value for a (account, T)
context. compute() returns BOTH the exact SQL it ran and the resulting value, so
the UI can show the query beside its output everywhere — training, serving, audit.

All queries are point-in-time by construction: `FOR VALID_TIME AS OF T` gives the
account's history as the world was at T, and the trailing edge is bounded on txn_ts,
the transaction's own event time. Pass system_time to reconstruct what the features
looked like as-of a past *wall-clock* instant — that's the audit path.

The trailing bound reads txn_ts rather than _valid_from deliberately: valid-time
answers 'when was this fact true', which is only incidentally the event instant, and
model.py's training windows order on txn_ts. The two paths have to agree on what the
event time *is* or the batch and online features silently drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def ts_lit(t: datetime) -> str:
    utc = t.astimezone(timezone.utc)
    return "TIMESTAMP '" + utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ") + "'"


# A sample z-score is meaningless with only a handful of near-identical priors: a
# ~£0.50 std makes an ordinary amount look like a 12-sigma event. Require a minimum
# number of observations before trusting it, then clip the magnitude so neither a
# tiny-variance window nor a genuine outlier can produce an absurd score.
MIN_OBS_30D = 5
Z_CLIP = 8.0


def zscore(amount, mean, std, n) -> float:
    if n is None or n < MIN_OBS_30D or mean is None or std in (None, 0):
        return 0.0
    z = (float(amount) - float(mean)) / float(std)
    return max(-Z_CLIP, min(Z_CLIP, z))


@dataclass
class Ctx:
    account_id: str
    t: datetime                 # decision instant (sim/valid time)
    amount: float               # the transaction being scored
    country: str
    system_time: datetime | None = None   # audit: read as-of this wall-clock


# --- per-feature SQL builders ------------------------------------------------
# Each returns a SELECT projecting a single column `v`.

# The point-in-time cut is `FOR VALID_TIME AS OF T` — the temporal index prunes away
# everything the account didn't know at T ("its history as the world was at T"). The
# window itself is then bounded on the event time: `txn_ts < T` excludes the scored
# event itself, `txn_ts >= T - w` is the trailing edge.

def _amount_zscore(c: Ctx) -> str:
    return f"""SELECT CASE
         WHEN COUNT(amount) < {MIN_OBS_30D}
           OR STDDEV_SAMP(amount) IS NULL OR STDDEV_SAMP(amount) = 0 THEN 0.0
         ELSE LEAST({Z_CLIP}, GREATEST(-{Z_CLIP},
                ({c.amount} - AVG(amount)) / STDDEV_SAMP(amount)))
       END AS v
FROM txn FOR VALID_TIME AS OF {ts_lit(c.t)}
WHERE account_id = '{c.account_id}'
  AND txn_ts <  {ts_lit(c.t)}
  AND txn_ts >= {ts_lit(c.t)} - INTERVAL 'P30D'"""


def _txn_count_24h(c: Ctx) -> str:
    return f"""SELECT COUNT(*) AS v
FROM txn FOR VALID_TIME AS OF {ts_lit(c.t)}
WHERE account_id = '{c.account_id}'
  AND txn_ts <  {ts_lit(c.t)}
  AND txn_ts >= {ts_lit(c.t)} - INTERVAL 'PT24H'"""


def _foreign(c: Ctx) -> str:
    return f"""SELECT CASE WHEN home_country <> '{c.country}' THEN 1 ELSE 0 END AS v
FROM account FOR VALID_TIME AS OF {ts_lit(c.t)}
WHERE _id = '{c.account_id}'"""


def _prior_confirmed_fraud(c: Ctx) -> str:
    # bounded to a trailing 90d window: recent prior fraud is the signal, and it
    # caps the scan so the feature scales regardless of the account's total history
    return f"""SELECT COUNT(*) AS v
FROM txn FOR VALID_TIME AS OF {ts_lit(c.t)} AS t
JOIN label AS l ON l._id = t._id
WHERE t.account_id = '{c.account_id}'
  AND t.txn_ts <  {ts_lit(c.t)}
  AND t.txn_ts >= {ts_lit(c.t)} - INTERVAL 'P90D'
  AND l.is_fraud = true"""


@dataclass
class Feature:
    name: str
    desc: str
    build: object          # Ctx -> sql
    kind: str = "query"    # "query" | "input"


FEATURES = [
    Feature("amount_zscore",
            "How unusual this amount is vs the account's own trailing-30d spend.",
            build=_amount_zscore),
    Feature("txn_count_24h",
            "Velocity: how many transactions the account made in the prior 24h.",
            build=_txn_count_24h),
    Feature("foreign",
            "1 if the transaction country differs from the account's home country as-of T.",
            build=_foreign),
    Feature("prior_confirmed_fraud",
            "Count of the account's prior transactions confirmed fraud in the last "
            "90 days (rises when a late chargeback lands — the audit driver).",
            build=_prior_confirmed_fraud),
]

# Vector order the model expects.
FEATURE_ORDER = [f.name for f in FEATURES]


def _with_basis(sql: str, c: Ctx) -> str:
    if c.system_time is None:
        return sql
    return f"SETTING DEFAULT SYSTEM_TIME AS OF {ts_lit(c.system_time)}\n{sql}"


def _serve_sql(c: Ctx) -> str:
    """One query over the account's history returning all trailing-window aggregates
    for the scored (hypothetical) transaction at instant T. Point-in-time by
    construction: `FOR VALID_TIME AS OF T` yields the account's history as the world
    was at T (temporal-index pruned), and the scored txn isn't in the table."""
    T = ts_lit(c.t)
    return f"""SELECT
  COALESCE(SUM(CASE WHEN t.txn_ts >= {T} - INTERVAL 'PT24H' THEN 1 ELSE 0 END),0) AS txn_count_24h,
  AVG(CASE WHEN t.txn_ts >= {T} - INTERVAL 'P30D' THEN t.amount END)                AS mean_30d,
  STDDEV_SAMP(CASE WHEN t.txn_ts >= {T} - INTERVAL 'P30D' THEN t.amount END)        AS std_30d,
  COUNT(CASE WHEN t.txn_ts >= {T} - INTERVAL 'P30D' THEN t.amount END)              AS n_30d,
  COALESCE(SUM(CASE WHEN t.txn_ts >= {T} - INTERVAL 'P90D' AND l.is_fraud THEN 1 ELSE 0 END),0) AS prior_confirmed_fraud
FROM txn FOR VALID_TIME AS OF {T} AS t
JOIN label AS l ON l._id = t._id
WHERE t.account_id = '{c.account_id}' AND t.txn_ts < {T}"""


# The batch ('training') form of each feature — the shape used to build the *whole*
# dataset in one pass over txn (see model._extract_sql). Shown beside the serving form
# so the two query shapes for the same feature — one point lookup per account, one pass
# over every anchor — sit side by side.
#
# Deliberately not window functions. Aggregate RANGE frames over an interval are
# SQL:2011 but unreleased in XTDB (xtdb/xtdb#5809), and requiring a locally-built image
# is the biggest obstacle to anyone running this. A band self-join says the same thing
# and runs on a stock published node.
TRAIN_FRAGMENTS = {
    "amount_zscore":
        "(t.amount - AVG(p.amount)) / STDDEV_SAMP(p.amount)"
        "\n                                           -- p = the strictly-earlier 30d band",
    "txn_count_24h":
        "COUNT(CASE WHEN p.txn_ts >= t.txn_ts - INTERVAL 'PT24H' THEN 1 END)",
    "foreign":
        "CASE WHEN a.home_country <> t.country THEN 1 ELSE 0 END",
    "prior_confirmed_fraud":
        "COUNT(s._id)\n-- fraud_status s: s._valid_time CONTAINS r.txn_ts AND s.is_fraud",
}

# How the batch form bounds each trailing window: a band join from the anchor `t` to its
# own account's prior transactions `p`, keyed on txn_ts (the event instant) so the band
# is an event-time window — the batch analogue of the serving path's trailing bound
# under FOR VALID_TIME AS OF T. Strictly earlier, so an anchor is never part of the
# baseline it is scored against, which is what `txn_ts < T` does on the serving side.
WINDOW_DEFS = (
    "-- p = the trailing band for anchor t:\n"
    "--   LEFT JOIN txn p ON p.account_id = t.account_id\n"
    "--    AND p.txn_ts <  t.txn_ts                     -- strictly earlier: never itself\n"
    "--    AND p.txn_ts >= t.txn_ts - INTERVAL 'P30D'   -- widest window; 24h narrowed via CASE"
)


def home_country(conn, c: Ctx) -> str | None:
    sql = _with_basis(
        f"SELECT home_country AS v FROM account FOR VALID_TIME AS OF {ts_lit(c.t)} "
        f"WHERE _id = '{c.account_id}'", c)
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def serve(conn, c: Ctx) -> tuple[dict, str]:
    """Compute the full feature vector for a hypothetical transaction in one query
    (plus a home-country lookup for `foreign`). Returns (values, combined_sql)."""
    sql = _with_basis(_serve_sql(c), c)
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        row = dict(zip(cols, cur.fetchone()))
    z = zscore(c.amount, row["mean_30d"], row["std_30d"], row["n_30d"])
    hc = home_country(conn, c)
    vals = {
        "amount_zscore": float(z),
        "txn_count_24h": float(row["txn_count_24h"] or 0),
        "foreign": 1.0 if hc is not None and hc != c.country else 0.0,
        "prior_confirmed_fraud": float(row["prior_confirmed_fraud"] or 0),
    }
    return vals, sql


def compute_one(conn, feat: Feature, c: Ctx):
    """Return (sql, value) for one feature. sql is None for pure inputs."""
    if feat.kind == "input":
        return None, float(c.amount)
    sql = _with_basis(feat.build(c), c)
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return sql, (row[0] if row and row[0] is not None else 0)


def vector(conn, c: Ctx) -> dict:
    """All features as {name: value} in FEATURE_ORDER — the model input row."""
    out = {}
    for feat in FEATURES:
        _, v = compute_one(conn, feat, c)
        out[feat.name] = float(v)
    return out
