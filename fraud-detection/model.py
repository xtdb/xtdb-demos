"""Model layer — periodic logistic-regression retrain from the live store.

Training extraction uses two SQL queries: event-time features and prior-fraud status. We only train on *resolved* labels — transactions older than the chargeback
window — honouring 'recent events aren't labelable yet'. The fitted pipeline is
persisted to disk; a model_registry table in XTDB records each version.

LR (not trees) on purpose: the decision decomposes into per-feature
contributions, which the serving/audit views show beside each feature's query.
"""

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import joblib
import numpy as np
import pandas as pd
import pyarrow as pa
from adbc_driver_flightsql import dbapi as flight_sql
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from queries import connect, sys_tx_retry
from registry import FEATURE_ORDER, MIN_OBS_30D, Z_CLIP
from registry import ts_lit as R_ts_lit

UTC = timezone.utc
MODELS_DIR = Path(__file__).parent / "models"


def training_outcome_horizon() -> timedelta:
    from sim import CHARGEBACK_DELAY
    return CHARGEBACK_DELAY

FLIGHT_SQL_URI = os.environ.get("XTDB_FLIGHT_URI", "grpc://localhost:9834")
# Arrow batches for a full 1M extraction blow past the default gRPC message cap.
MAX_MSG_SIZE = str(512 * 1024 * 1024)


def _trailing(iv: str) -> str:
    # The trailing edge of a window, as a predicate on the band-joined prior row `p`.
    # Inclusive, so a transaction exactly one interval back still counts. The leading
    # edge is `p.txn_ts < t.txn_ts` on the join itself: STRICTLY earlier, so an anchor is
    # never part of the baseline it is measured against, which is what the serving
    # queries have always done (`txn_ts < :t` in registry.py).
    #
    # Keyed on txn_ts, the transaction's own event time. Deliberately not _valid_from:
    # valid-time answers 'when was this fact true', which is only incidentally the event
    # instant today and stops being it the moment a txn gets a lifecycle.
    return f"p.txn_ts >= t.txn_ts - INTERVAL '{iv}'"


def _anchor_conds(col: str, since, account, resolved_before) -> str:
    conds = []
    if since:
        conds.append(f"{col}.txn_ts >= {R_ts_lit(since)}")
    if account:
        conds.append(f"{col}.account_id = '{account}'")
    # training only: exclude rows whose fraud outcome isn't settled yet (younger than the
    # max chargeback delay), so the mature target label is actually final for every row.
    if resolved_before:
        conds.append(f"{col}.txn_ts <= {R_ts_lit(resolved_before)}")
    return conds


def _basis_sql(sql: str, system_time: datetime | None) -> str:
    if system_time is None:
        return sql
    return f"SETTING DEFAULT SYSTEM_TIME AS OF {R_ts_lit(system_time)}\n{sql}"


def _window_sql(limit=None, since=None, account=None, resolved_before=None,
                system_time: datetime | None = None) -> str:
    """Event-time features + the mature target label, in one window pass. These three
    features come off the transaction's own fixed attributes (amount, velocity, origin),
    known the instant it happens — ordinary trailing windows over txn_ts, leak-free
    against future *events*. `label` is the mature (latest-system-time) outcome: the
    target we're learning, so we deliberately want hindsight there.

    The account join reads the version of the account in force at the transaction's own
    event time; `CONTAINS` is half-open, so a transaction landing exactly on a version
    boundary joins to the newer version only, and the row can't fan out."""
    conds = _anchor_conds("t", since, account, resolved_before)
    where = ("\nWHERE " + "\n  AND ".join(conds)) if conds else ""
    order_limit = f"\nORDER BY t.txn_ts DESC LIMIT {limit}" if limit else ""
    # LEFT, not INNER: an account's first transaction has no prior band at all, and it is
    # still a training row. Its aggregates come back NULL / 0, which registry.zscore reads
    # as "no baseline" and scores 0.0 — the same answer the serving path gives.
    # The anchor filters belong in WHERE, not in the LEFT JOIN's ON: conditions on `t`
    # inside an outer join's ON don't filter the preserved side.
    return _basis_sql(f"""
SELECT t._id AS id, t.account_id AS account_id, t.txn_ts AS txn_ts,
       t.amount AS amount, t.country AS country,
       CASE WHEN l.is_fraud THEN 1 ELSE 0 END AS label,
       CASE WHEN a.home_country <> t.country THEN 1 ELSE 0 END AS foreign,
       COUNT(CASE WHEN {_trailing('PT24H')} THEN 1 END)  AS txn_count_24h,
       AVG(p.amount)                                     AS mean_30d,
       STDDEV_SAMP(p.amount)                             AS std_30d,
       COUNT(p.amount)                                   AS n_30d
FROM txn t
JOIN account FOR ALL VALID_TIME AS a
  ON a._id = t.account_id AND a._valid_time CONTAINS t.txn_ts
JOIN label l ON l._id = t._id
LEFT JOIN txn p
  ON p.account_id = t.account_id
 AND p.txn_ts < t.txn_ts
 AND {_trailing('P30D')}{where}
GROUP BY t._id, t.account_id, t.txn_ts, t.amount, t.country,
         l.is_fraud, a.home_country{order_limit}
""", system_time)


def _require_fraud_status(cur, system_time: datetime | None = None):
    """Older demo volumes must not silently train with missing status histories."""
    cur.execute("""SELECT table_name FROM information_schema.tables
                   WHERE table_schema = 'public' AND table_name IN ('txn', 'fraud_status')""")
    tables = {row[0] for row in cur.fetchall()}
    if 'txn' not in tables:
        return
    sql = "SELECT _id FROM txn LIMIT 1"
    if 'fraud_status' in tables:
        sql = """
SELECT t._id FROM txn t
LEFT JOIN fraud_status FOR ALL VALID_TIME AS s ON s._id = t._id
WHERE s._id IS NULL
LIMIT 1
"""
    cur.execute(_basis_sql(sql, system_time))
    if cur.fetchone() is not None:
        raise RuntimeError(
            "This dataset has incomplete fraud_status history and needs a fresh seed. "
            "See README.md: restarting the API does not upgrade an existing demo volume."
        )


def _fraud_joins(anchor: str) -> str:
    """Share the status-interval join between the extract and its displayed sample."""
    return f"""LEFT JOIN txn f
  ON f.account_id = {anchor}.account_id
 AND f.txn_ts < {anchor}.txn_ts
 AND f.txn_ts >= {anchor}.txn_ts - INTERVAL 'P90D'
LEFT JOIN fraud_status FOR ALL VALID_TIME AS s
  ON s._id = f._id
 AND s._valid_time CONTAINS {anchor}.txn_ts
 AND s.is_fraud
LEFT JOIN label l ON l._id = f._id AND l.is_fraud"""


def _pcf_sql(limit=None, since=None, account=None, resolved_before=None,
             system_time: datetime | None = None) -> str:
    """Count prior fraud at each decision, alongside outcomes at the selected basis."""
    conds = _anchor_conds("r", since, account, resolved_before)
    where = ("\nWHERE " + " AND ".join(conds)) if conds else ""
    order_limit = f"\nORDER BY r.txn_ts DESC LIMIT {limit}" if limit else ""
    return _basis_sql(f"""
SELECT r._id AS id,
       COUNT(s._id) AS prior_confirmed_fraud_as_known_then,
       COUNT(l._id) AS prior_confirmed_fraud_with_hindsight,
       r.txn_ts AS anchor_time
FROM txn r
{_fraud_joins('r')}{where}
GROUP BY r._id, r.txn_ts{order_limit}
""", system_time)


def _pcf_leaky_sql(limit=None, since=None, account=None, resolved_before=None,
                   system_time: datetime | None = None) -> str:
    """Count current outcomes without checking status at each decision.

    This credits earlier decisions with confirmations that arrived later."""
    conds = _anchor_conds("r", since, account, resolved_before)
    where = ("\n  WHERE " + " AND ".join(conds)) if conds else ""
    order_limit = f"\nORDER BY r._id LIMIT {limit}" if limit else ""
    return _basis_sql(f"""
SELECT r._id AS id, COUNT(cf.fid) AS pcf_leaky
FROM txn r
LEFT JOIN (
  SELECT f._id AS fid, f.account_id AS acct, f.txn_ts AS fevent
  FROM txn f
  JOIN (SELECT _id FROM label WHERE is_fraud = true) lf ON lf._id = f._id
) cf
  ON cf.acct = r.account_id
 AND cf.fevent <  r.txn_ts
 AND cf.fevent >= r.txn_ts - INTERVAL 'P90D'{where}
GROUP BY r._id{order_limit}
""", system_time)


def _sample_sql(limit: int, resolved_before: datetime,
                system_time: datetime | None = None) -> str:
    """Bound the displayed anchors before joining their prior fraud history."""
    return _basis_sql(f"""
WITH anchors AS (
  SELECT t._id AS id, t.account_id, t.txn_ts,
         t.amount, t.country, CASE WHEN l.is_fraud THEN 1 ELSE 0 END AS label
  FROM txn t
  JOIN label l ON l._id = t._id
  WHERE t.txn_ts <= {R_ts_lit(resolved_before)}
  ORDER BY t.txn_ts DESC
  LIMIT {limit}
)
SELECT a.id, a.txn_ts, a.account_id, a.amount, a.country, a.label,
       COUNT(s._id) AS prior_confirmed_fraud_as_known_then,
       COUNT(l._id) AS prior_confirmed_fraud_with_hindsight
FROM anchors a
{_fraud_joins('a')}
GROUP BY a.id, a.txn_ts, a.account_id, a.amount, a.country, a.label
ORDER BY a.txn_ts DESC
""", system_time)


def sample_training(limit: int, resolved_before: datetime,
                    system_time: datetime | None = None) -> pd.DataFrame:
    fconn = _flight_connect()
    try:
        cur = fconn.cursor()
        try:
            _require_fraud_status(cur, system_time)
            cur.execute(_sample_sql(limit, resolved_before, system_time))
            return _fetch_df(cur)
        finally:
            cur.close()
    finally:
        fconn.close()


def _extract_sql(limit=None, since=None, account=None, resolved_before=None,
                 system_time: datetime | None = None) -> str:
    """The extraction, as one string, for disclosure in the UI. It's really two reads:
    the event-time window pass, and the bitemporal prior_confirmed_fraud join."""
    return (f"-- event-time features and settled outcomes\n"
            f"{_window_sql(limit, since, account, resolved_before, system_time).strip()}\n\n"
            f"-- prior fraud: as known at each decision, and with knowledge available at the selected basis\n"
            f"{_pcf_sql(limit, since, account, resolved_before, system_time).strip()}")


def leakage_sample(conn, limit: int = 8) -> dict:
    """Compare leak-free vs leaky prior_confirmed_fraud over the resolved training set:
    totals plus a few example transactions the naive query overcounts (it credits the
    account with frauds whose chargebacks hadn't landed yet at that transaction's time).
    The leaky count uses current outcomes instead of the status at each decision."""
    resolved = sim_now(conn) - training_outcome_horizon()
    fconn = _flight_connect()
    try:
        cur = fconn.cursor()
        try:
            _require_fraud_status(cur)
            cur.execute(_pcf_sql(resolved_before=resolved))
            free = _fetch_df(cur)
            cur.execute(_pcf_leaky_sql(resolved_before=resolved))
            leaky = _fetch_df(cur)
        finally:
            cur.close()
    finally:
        fconn.close()
    m = free.merge(leaky, on="id", how="outer").fillna(0)
    m["over"] = m["pcf_leaky"] - m["prior_confirmed_fraud_as_known_then"]
    total, differing = len(m), int((m["over"] != 0).sum())
    overcount = int(m["over"].sum())
    top = m[m["over"] > 0].sort_values("over", ascending=False).head(limit)
    examples: list[dict] = []
    if not top.empty:
        ids = "', '".join(top["id"].tolist())
        with conn.cursor() as cur:
            cur.execute(f"SELECT _id, account_id, _valid_from FROM txn WHERE _id IN ('{ids}')")
            meta = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        for _, r in top.iterrows():
            acc, vt = meta.get(r["id"], ("?", None))
            examples.append({"txn_id": r["id"], "account_id": acc,
                             "valid_time": vt.isoformat() if vt else None,
                             "leak_free": int(r["prior_confirmed_fraud_as_known_then"]), "leaky": int(r["pcf_leaky"])})
    return {"rows": total, "differing": differing, "overcount": overcount, "examples": examples}


def sim_now(conn, system_time: datetime | None = None) -> datetime:
    with conn.cursor() as cur:
        cur.execute(_basis_sql("SELECT MAX(sim_now) FROM sim_clock", system_time))
        return cur.fetchone()[0]


def _fetch_df(cur) -> pd.DataFrame:
    """Arrow -> pandas, normalising 'Z'-zoned timestamps to 'UTC'.

    A user-written timestamptz column (txn_ts) comes back with its zone as the offset it
    was written with, 'Z'. That's a legal Arrow zone string but pytz can't resolve it, so
    the default conversion raises UnknownTimeZoneError. XT's own temporal columns come
    back as 'UTC' and convert fine, which is why this only bites columns we wrote."""
    tbl = cur.fetch_arrow_table()
    fields = [f.with_type(pa.timestamp(f.type.unit, "UTC"))
              if pa.types.is_timestamp(f.type) and f.type.tz == "Z" else f
              for f in tbl.schema]
    return tbl.cast(pa.schema(fields)).to_pandas()


def _flight_connect():
    return flight_sql.connect(
        FLIGHT_SQL_URI, autocommit=True,
        db_kwargs={"adbc.flight.sql.client_option.with_max_msg_size": MAX_MSG_SIZE})


Knowledge = Literal["as_of_decision", "current"]


def extract(limit: int | None = None, since: datetime | None = None,
            account: str | None = None, resolved_before: datetime | None = None,
            system_time: datetime | None = None,
            knowledge: Knowledge = "as_of_decision") -> pd.DataFrame:
    """Feature extraction over Arrow Flight SQL. Two reads: the event-time window pass
    and the bitemporal prior_confirmed_fraud join, merged on txn id. amount_zscore is
    derived in pandas from the trailing-30d mean/std the window query returns.

    `knowledge` picks which of the two prior_confirmed_fraud counts feeds the model.
    Training must use `as_of_decision` (what each decision could see at its own instant)
    or it leaks. A correction/serving view must use `current`, because a chargeback
    confirmed now is learned *after* every historical decision instant and so can never
    change an as-of-decision count — the scores would look frozen."""
    fconn = _flight_connect()
    try:
        cur = fconn.cursor()
        try:
            _require_fraud_status(cur, system_time)
            cur.execute(_window_sql(limit, since, account, resolved_before, system_time))
            df = _fetch_df(cur)
            cur.execute(_pcf_sql(limit, since, account, resolved_before, system_time))
            pcf = _fetch_df(cur)
        finally:
            cur.close()
    finally:
        fconn.close()
    df = df.merge(pcf.drop(columns=["anchor_time"], errors="ignore"), on="id", how="left")
    df["prior_confirmed_fraud_as_known_then"] = df["prior_confirmed_fraud_as_known_then"].fillna(0).astype(float)
    df["prior_confirmed_fraud_with_hindsight"] = df["prior_confirmed_fraud_with_hindsight"].fillna(0).astype(float)
    df["prior_confirmed_fraud"] = (df["prior_confirmed_fraud_as_known_then"]
                                   if knowledge == "as_of_decision"
                                   else df["prior_confirmed_fraud_with_hindsight"])
    std = df["std_30d"].to_numpy(dtype=float)
    mean = df["mean_30d"].to_numpy(dtype=float)
    amount = df["amount"].to_numpy(dtype=float)
    n = df["n_30d"].to_numpy(dtype=float)
    # guard low-sample instability + clip, matching registry.zscore
    bad = np.isnan(std) | (std == 0.0) | (n < MIN_OBS_30D)
    z = np.where(bad, 0.0, (amount - mean) / np.where(std == 0.0, 1.0, std))
    df["amount_zscore"] = np.clip(np.nan_to_num(z), -Z_CLIP, Z_CLIP)
    df["label"] = df["label"].astype(int)
    return df


def _fit(df: pd.DataFrame) -> tuple[Pipeline, float]:
    X = df[FEATURE_ORDER].astype(float)
    y = df.label.astype(int)
    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.3,
                                              random_state=0, stratify=y)
    pipe = Pipeline([("scaler", StandardScaler()),
                     ("lr", LogisticRegression(max_iter=1000, class_weight="balanced"))])
    pipe.fit(X_tr, y_tr)
    auc = float(roc_auc_score(y_te, pipe.predict_proba(X_te)[:, 1]))
    return pipe, auc


def _persist(conn, pipe: Pipeline, auc: float, n: int, n_fraud: int) -> str:
    MODELS_DIR.mkdir(exist_ok=True)
    version = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")[:-3]
    path = MODELS_DIR / f"model_{version}.joblib"
    joblib.dump(pipe, path)
    sys_tx_retry(conn, lambda: sim_now(conn), lambda cur, st: cur.execute(
        """INSERT INTO model_registry
             (_id, _valid_from, trained_at_wall, trained_at_sim, auc, n_train, n_fraud, path)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (version, st, datetime.now(UTC), st, auc, n, n_fraud, str(path))))
    return version


def train(conn, limit: int | None = None) -> dict | None:
    # only train on settled outcomes: exclude txns younger than the max chargeback delay
    df = extract(limit, resolved_before=sim_now(conn) - training_outcome_horizon())
    if df.empty or int(df.label.sum()) < 5:
        return None
    pipe, auc = _fit(df)
    n, n_fraud = len(df), int(df.label.sum())
    version = _persist(conn, pipe, auc, n, n_fraud)
    return dict(version=version, trained_at_wall=datetime.now(UTC),
                trained_at_sim=sim_now(conn), auc=auc, n_train=n, n_fraud=n_fraud)


def train_window(conn, limit: int | None = None, persist: bool = True,
                 system_time: datetime | None = None) -> dict:
    """Extract (window-function pass over Arrow Flight SQL) then fit, timed
    separately. The point: the fit is negligible; the extraction — XTDB computing
    point-in-time features for every row — is where the time goes, and it's seconds
    even on the full 1M. persist=True registers the model so serving uses it."""
    if system_time is not None and persist:
        raise ValueError("training at a past knowledge basis cannot replace the live model")
    t0 = time.perf_counter()
    basis_now = system_time if system_time is not None else sim_now(conn)
    df = extract(limit, resolved_before=basis_now - training_outcome_horizon(),
                 system_time=system_time)
    extract_ms = (time.perf_counter() - t0) * 1000
    n, n_fraud = len(df), int(df.label.sum())
    out = dict(n=n, n_fraud=n_fraud, extract_ms=extract_ms, fit_ms=None,
               auc=None, version=None)
    if n_fraud >= 2 and n - n_fraud >= 2:
        t1 = time.perf_counter()
        pipe, auc = _fit(df)
        out["fit_ms"] = (time.perf_counter() - t1) * 1000
        out["auc"] = auc
        if persist:
            out["version"] = _persist(conn, pipe, auc, n, n_fraud)
    return out


def scored_frame(conn, pipe, limit: int) -> pd.DataFrame:
    """Recent extracted rows with the model's score — the training-browse view."""
    df = extract(limit)
    if df.empty:
        return df
    df["p"] = (pipe.predict_proba(df[FEATURE_ORDER].astype(float))[:, 1]
               if pipe is not None else 0.0)
    return df


def extract_account(account_id: str, since: datetime,
                    knowledge: Knowledge = "as_of_decision") -> pd.DataFrame:
    """All of one account's transactions since `since`, with point-in-time features,
    in a single window-function pass — the audit/correction path."""
    return extract(since=since, account=account_id, knowledge=knowledge)


def score_account(pipe, account_id: str, since: datetime,
                  knowledge: Knowledge = "as_of_decision") -> dict:
    """{txn_id: p} for every one of the account's transactions since `since`, scored in
    one pass. Reads the label at the current basis, so it reflects any corrections."""
    if pipe is None:
        return {}
    df = extract_account(account_id, since, knowledge=knowledge)
    if df.empty:
        return {}
    p = pipe.predict_proba(df[FEATURE_ORDER].astype(float))[:, 1]
    return {str(i): float(x) for i, x in zip(df["id"], p)}


def score_recent(pipe, since: datetime) -> dict:
    """Batch-score every transaction since `since` in one window-function pass — the
    efficient 'feature = query, scored in one pass' path — and return {txn_id: p}.
    Used to pre-score the historical live window so every row shows a verdict."""
    if pipe is None:
        return {}
    df = extract(since=since)
    if df.empty:
        return {}
    p = pipe.predict_proba(df[FEATURE_ORDER].astype(float))[:, 1]
    return {str(i): float(x) for i, x in zip(df["id"], p)}


# The registry lookup is cheap; the joblib.load isn't. Paths are unique per model
# version, so caching by path gives a natural invalidation on retrain (and lets the
# parallel background scorers skip the disk read on every row).
_pipe_cache: dict = {}


_MODEL_COLUMNS = "_id, trained_at_wall, trained_at_sim, auc, n_train, n_fraud, path"
_MODEL_KEYS = ["version", "trained_at_wall", "trained_at_sim", "auc", "n_train", "n_fraud", "path"]


def _load_model_row(row):
    if not row:
        return None, None
    meta = dict(zip(_MODEL_KEYS, row))
    path = meta["path"]
    pipe = _pipe_cache.get(path)
    if pipe is None:
        pipe = joblib.load(path)
        _pipe_cache[path] = pipe
    return pipe, meta


def load_latest(conn):
    """Return (pipeline, meta) for the newest model, or (None, None)."""
    with conn.cursor() as cur:
        try:
            cur.execute(f"SELECT {_MODEL_COLUMNS} FROM model_registry "
                        "ORDER BY trained_at_wall DESC LIMIT 1")
        except Exception:
            return None, None
        return _load_model_row(cur.fetchone())


def load_version(conn, version: str):
    """Load an immutable model artifact by its registry version."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_MODEL_COLUMNS} FROM model_registry WHERE _id = %s", (version,))
        pipe, meta = _load_model_row(cur.fetchone())
    if pipe is None:
        raise ValueError(f"unknown model version: {version}")
    return pipe, meta


def explain(pipe, x: dict) -> dict:
    """Decompose the decision: per-feature log-odds contributions + prob."""
    scaler, lr = pipe.named_steps["scaler"], pipe.named_steps["lr"]
    contribs, logit = {}, float(lr.intercept_[0])
    for i, name in enumerate(FEATURE_ORDER):
        z = (float(x[name]) - scaler.mean_[i]) / scaler.scale_[i]
        c = float(lr.coef_[0][i]) * z
        contribs[name] = c
        logit += c
    prob = 1.0 / (1.0 + math.exp(-logit))
    return dict(contributions=contribs, intercept=float(lr.intercept_[0]),
                logit=logit, prob=prob)


if __name__ == "__main__":
    conn = connect()
    t0 = time.perf_counter()
    meta = train(conn)
    if meta:
        meta["total_ms"] = (time.perf_counter() - t0) * 1000
    if meta is None:
        print("not enough resolved fraud to train yet")
    else:
        print(f"trained {meta['version']}  auc={meta['auc']:.4f}  "
              f"n={meta['n_train']} (fraud={meta['n_fraud']})")
        pipe, m = load_latest(conn)
        # smoke: explain a synthetic high-risk txn
        x = dict(amount_zscore=6.0, txn_count_24h=3, foreign=1, prior_confirmed_fraud=1)
        e = explain(pipe, x)
        print(f"sample high-risk score p={e['prob']:.3f}")
        for k, v in sorted(e["contributions"].items(), key=lambda kv: -abs(kv[1])):
            print(f"  {k:22} {v:+.2f}")
