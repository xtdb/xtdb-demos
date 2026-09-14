"""JSON API for the React app — thin wrappers over registry.py / model.py.

    uvicorn api:app --port 8000 --reload

No business logic lives here: features come from the registry (SQL + value),
decisions from the model. The one addition is a `badge` per feature naming which
XTDB capability it leans on, so the UI can disclose XT in relation to the system.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import TypedDict

import model as M
import registry as R
from queries import connect, sys_tx_retry
from sim_control import controller as SIM

UTC = timezone.utc
THRESHOLD = 0.5

app = FastAPI(title="XTDB fraud feature store")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

# Which XTDB capability each feature's query demonstrates.
BADGES = {
    "amount_zscore": "valid-time window · 30d trailing",
    "txn_count_24h": "valid-time window · 24h trailing",
    "foreign": "FOR VALID_TIME AS OF · account as-of T",
    "prior_confirmed_fraud": "labels as-of · bitemporal",
}


def _iso(dt):
    return dt.isoformat() if dt is not None else None


def _parse(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _decision(pipe, vals):
    if pipe is None:
        return None
    e = M.explain(pipe, vals)
    return {
        "prob": e["prob"],
        "verdict": "fraud" if e["prob"] >= THRESHOLD else "legit",
        "intercept": e["intercept"],
        "contributions": [{"name": k, "value": v} for k, v in e["contributions"].items()],
    }


# --- endpoints ---------------------------------------------------------------

@app.get("/api/status")
def status():
    with connect() as c, c.cursor() as cur:
        cur.execute("SELECT MAX(sim_now) FROM sim_clock")
        sn = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM txn")
        n_txn = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM label WHERE is_fraud = true")
        n_fraud = cur.fetchone()[0]
        _, meta = M.load_latest(c)
    return {
        "sim_now": _iso(sn), "n_txn": n_txn, "n_fraud": n_fraud,
        "model": ({"version": meta["version"], "auc": meta["auc"],
                   "n_train": meta["n_train"]} if meta else None),
    }


@app.get("/api/accounts")
def accounts():
    with connect() as c, c.cursor() as cur:
        cur.execute("SELECT _id FROM account ORDER BY _id")
        return [r[0] for r in cur.fetchall()]


# --- sim control: drive the live data feed from the web app --------------------

class SimReq(BaseModel):
    speed: float | None = None


@app.get("/api/sim")
def sim_status():
    return SIM.status()


@app.post("/api/sim/start")
def sim_start(req: SimReq):
    return SIM.start(req.speed)


@app.post("/api/sim/stop")
def sim_stop():
    return SIM.stop()


@app.post("/api/sim/speed")
def sim_speed(req: SimReq):
    if req.speed is not None:
        SIM.set_speed(req.speed)
    return SIM.status()


# --- the data-central canvas: feed + basis-aware data view ---------------------

@app.get("/api/bounds")
def bounds():
    """Cursor ranges for the basis control: valid-time spans the simulated world
    (history start -> sim-now); system-time spans wall-clock knowledge."""
    with connect() as c, c.cursor() as cur:
        cur.execute("SELECT MIN(_valid_from) FROM txn")
        vt_min = cur.fetchone()[0]
        cur.execute("SELECT MAX(sim_now) FROM sim_clock")
        sim_now = cur.fetchone()[0]
        cur.execute("SELECT MIN(_system_from), MAX(_system_from) FROM txn FOR ALL SYSTEM_TIME")
        st_min, st_max = cur.fetchone()
    return {"valid_min": _iso(vt_min), "valid_now": _iso(sim_now),
            "system_min": _iso(st_min), "system_now": _iso(st_max)}


def corrections_sql(limit: int = 40) -> str:
    """Transactions whose label was first recorded legit and later recorded fraud — a
    real chargeback, seen purely on the system-time axis.

    One grouped pass over every label version, deliberately not a correlated `EXISTS`
    per candidate row. The `EXISTS` form re-scanned all versions for each candidate and
    cost ~28s on the default seed where this costs ~0.3s, for identical output.

    `first_seen < confirmed_at` is the whole definition of a correction: we recorded
    legit before we recorded fraud. A transaction booked as fraud from the outset has no
    legit version and drops out.
    """
    return f"""WITH corrected AS (
                 SELECT _id,
                        MIN(CASE WHEN is_fraud THEN _system_from END)     AS confirmed_at,
                        MIN(CASE WHEN NOT is_fraud THEN _system_from END) AS first_seen,
                        MIN(CASE WHEN is_fraud THEN _valid_from END)      AS event_at
                 FROM label FOR ALL SYSTEM_TIME
                 GROUP BY _id)
               SELECT c._id, t.account_id, t.amount, t.country, c.event_at, c.confirmed_at
               FROM corrected c
               JOIN txn t ON t._id = c._id
               WHERE c.first_seen < c.confirmed_at
               ORDER BY c.confirmed_at DESC LIMIT {limit}"""


@app.get("/api/feed")
def feed(limit: int = 40):
    """Recent activity as the database learned it (ORDER BY _system_from) — no event
    bus, just a query over the system-time axis. Each item carries happened_at
    (valid) vs learned_at (system); a correction is a label that flipped legit->fraud
    at a later system-time than it was first recorded (a real chargeback)."""
    txn_sql = f"""SELECT _id, account_id, amount, country, _valid_from, _system_from
                  FROM txn ORDER BY _system_from DESC LIMIT {limit}"""
    corr_sql = corrections_sql(limit)
    items = []
    with connect() as c, c.cursor() as cur:
        cur.execute(txn_sql)
        for _id, acc, amt, ctry, vf, sf in cur.fetchall():
            items.append({"id": _id, "account": acc, "amount": float(amt), "country": ctry,
                          "kind": "txn", "happened_at": _iso(vf), "learned_at": _iso(sf)})
        cur.execute(corr_sql)
        for _id, acc, amt, ctry, vf, sf in cur.fetchall():
            items.append({"id": _id, "account": acc, "amount": float(amt), "country": ctry,
                          "kind": "correction", "happened_at": _iso(vf), "learned_at": _iso(sf)})
    items.sort(key=lambda x: x["learned_at"], reverse=True)
    return {"items": items[:limit]}


# Live per-transaction model score, cached by txn id (a txn's arrival-time score is
# fixed). Cleared when the model version changes.
_score_cache: dict = {}
_score_ver = None
_score_lock = threading.Lock()

# Background scorer — queue of (id, account, happened_at, amount, country) waiting to score.
# Several worker threads drain it concurrently; each R.serve is a ~600ms DB round-trip,
# so parallelism (not batching) is the throughput win — XTDB serves the queries in parallel.
_SCORER_WORKERS = 6
_score_queue: list = []
_scorer_started = False
# Watermark: transactions already present when we start watching are "historical" and
# are NOT live-scored — only genuinely new (streamed-in) transactions get scored live.
_score_baseline = None


def _ensure_scorer():
    """Lazily start the pool of background scorer threads (once per process lifetime)."""
    global _scorer_started
    if _scorer_started:
        return
    with _score_lock:
        if _scorer_started:
            return
        _scorer_started = True
        for k in range(_SCORER_WORKERS):
            threading.Thread(target=_scorer_loop, daemon=True, name=f"bg-scorer-{k}").start()


def _scorer_loop():
    """One worker: pop a row and score it. Workers run in parallel, so throughput is
    ~_SCORER_WORKERS × single-row rate (each R.serve is an independent DB round-trip)."""
    global _score_cache, _score_ver, _score_queue
    while True:
        time.sleep(0.05)   # tight loop — the DB latency is the real throttle
        with _score_lock:
            if not _score_queue:
                continue
            # Take one row at a time; R.serve is ~600ms so batchsize > 1
            # just delays the first visible score update.
            item = _score_queue.pop(0)

        row_id, account, happened_at, amount, country = item
        try:
            with connect() as c:
                pipe, meta = M.load_latest(c)
                ver = meta["version"] if meta else None
                with _score_lock:
                    if ver != _score_ver:
                        _score_cache, _score_ver = {}, ver
                    if row_id in _score_cache:
                        continue
                if pipe is None:
                    continue
                ctx = R.Ctx(account, _parse(happened_at), amount, country)
                vals, _ = R.serve(c, ctx)
                p = M.explain(pipe, vals)["prob"]
                with _score_lock:
                    _score_cache[row_id] = p
        except Exception:
            # Don't crash the thread — scoring will retry on next tick.
            pass


# Prescore: batch-score the recent historical window once per model version, via the
# efficient window-function extraction, so every row shows a verdict (not a blank —).
_prescore_ver = None
_prescore_running = False


def _ensure_prescore(ver):
    global _prescore_running
    if ver is None or _prescore_ver == ver or _prescore_running:
        return
    with _score_lock:
        if _prescore_ver == ver or _prescore_running:
            return
        _prescore_running = True
    threading.Thread(target=_prescore_loop, args=(ver,), daemon=True).start()


def _prescore_loop(ver):
    """Score the recent live window (the historical rows on screen) in one pass and
    merge into _score_cache. Runs once per model version, on a background thread."""
    global _prescore_ver, _prescore_running, _score_cache
    try:
        with connect() as c:
            pipe, meta = M.load_latest(c)
            if pipe is None or (meta and meta["version"]) != ver:
                return
            since = M.sim_now(c) - timedelta(days=120)
        scores = M.score_recent(pipe, since)   # {txn_id: p} via the extraction
        with _score_lock:
            if _score_ver == ver:              # model hasn't changed under us
                _score_cache.update(scores)
                _prescore_ver = ver
    except Exception:
        pass
    finally:
        _prescore_running = False


def _score_rows(c, rows, st, live):
    """Attach the model's live verdict `p` to each row (null until scored).

    Historical rows are filled by the background prescore (one window-function pass);
    genuinely-new rows (above the watermark) are additionally enqueued to the per-row
    scorer for the immediate land→score transition. Pinned-basis views aren't scored."""
    global _score_cache, _score_ver, _score_queue, _score_baseline
    pipe, meta = M.load_latest(c)
    ver = meta["version"] if meta else None
    with _score_lock:
        if ver != _score_ver:
            _score_cache, _score_ver = {}, ver

    if not live:
        for r in rows:
            r["p"] = None
        return

    _ensure_scorer()
    _ensure_prescore(ver)          # background-fill the historical window's scores
    with _score_lock:
        if _score_baseline is None:
            with c.cursor() as cur:
                cur.execute("SELECT MAX(_id) FROM txn")
                _score_baseline = cur.fetchone()[0] or ""
        baseline = _score_baseline
        cache = dict(_score_cache)

    to_enqueue = []
    for r in rows:
        r["p"] = cache.get(r["id"])
        # brand-new rows get the slow per-row scorer for the live transition;
        # historical rows are covered by the prescore, so don't enqueue them.
        if r["p"] is None and r["id"] > baseline:
            to_enqueue.append((r["id"], r["account"], r["happened_at"], r["amount"], r["country"]))
    if to_enqueue:
        with _score_lock:
            queued_ids = {item[0] for item in _score_queue}
            for item in to_enqueue:
                if item[0] not in queued_ids:
                    _score_queue.append(item)
            if len(_score_queue) > 20:
                _score_queue = _score_queue[-20:]


@app.get("/api/data")
def data(valid_time: str | None = None, system_time: str | None = None,
         limit: int = 40, before: str | None = None):
    """The data itself, re-read at a bitemporal basis, with the model's live verdict
    per row. `label` is what's *recorded* (chargeback-confirmed); `p` is what the
    *model predicts* now. Pulling system-time back drops corrections not yet learned.

    `before` enables paging: when present, returns rows with _valid_from < before
    (no lower bound — the caller drives the window by moving `before` backwards).
    The live head (before absent) is bounded to a 1-day window for cheapness."""
    with connect() as c:
        vt = _parse(valid_time) if valid_time else M.sim_now(c)
        st = _parse(system_time) if system_time else None
        V = R.ts_lit(vt)

        if before is not None:
            # Paged view: caller supplies an explicit upper bound; no lower bound so
            # each page is a contiguous ~limit-row window working backwards.
            B = R.ts_lit(_parse(before))
            where = f"t._valid_from < {B}"
        else:
            # Live head: bounded to the last day so the temporal sort is cheap.
            where = f"t._valid_from < {V} AND t._valid_from >= {V} - INTERVAL 'P1D'"

        # Bounded to a recent valid-time window and no `FOR VALID_TIME AS OF`: valid_to
        # is always open, so `_valid_from < V` already gives the world as-of V, and the
        # window keeps the newest-N sort cheap (the temporal sort is the cost). The
        # system-time SETTING is still applied when a basis is pinned (the rewind).
        # 3-state per-row recorded status: fraud (chargeback settled → is_fraud=true),
        # pending (a chargeback is in flight for THIS txn but not settled), else legit.
        # The pending join is what distinguishes "known fraud" from "suspected, not yet
        # confirmed" — an already-settled fraud has no pending_chargeback row.
        sql = f"""SELECT t._id, t.account_id, t.amount, t.country, t._valid_from, t._system_from,
                    CASE WHEN l.is_fraud THEN 'fraud'
                         WHEN pc._id IS NOT NULL THEN 'pending'
                         ELSE 'legit' END AS label
                  FROM txn t
                  JOIN label l ON l._id = t._id
                  LEFT JOIN pending_chargeback pc ON pc._id = t._id
                  WHERE {where}
                  ORDER BY t._valid_from DESC LIMIT {limit}"""
        if st is not None:
            sql = f"SETTING DEFAULT SYSTEM_TIME AS OF {R.ts_lit(st)}\n{sql}"
        with c.cursor() as cur:
            try:
                cur.execute(sql)
            except Exception:  # pending_chargeback may not exist yet (no fraud recorded)
                cur.execute(sql.replace("LEFT JOIN pending_chargeback pc ON pc._id = t._id", "")
                               .replace("WHEN pc._id IS NOT NULL THEN 'pending'\n                         ", ""))
            rows = [{"id": r[0], "account": r[1], "amount": float(r[2]), "country": r[3],
                     "happened_at": _iso(r[4]), "recorded_at": _iso(r[5]), "label": r[6]}
                    for r in cur.fetchall()]
        _score_rows(c, rows, st, live=(valid_time is None and system_time is None and before is None))
    return {"valid_time": _iso(vt), "system_time": _iso(st), "rows": rows, "sql": sql}


class TrainingDataset(TypedDict):
    knowledge_basis: str
    settled_before: str
    total_rows: int
    total_fraud: int
    displayed_rows: int
    rows: list[dict]
    differing: int
    sample_sql: str


@app.get("/api/training")
def training(txn_id: str, system_time: str, limit: int = 60) -> TrainingDataset:
    knowledge_basis = _parse(system_time)
    with connect() as c:
        settled_before = knowledge_basis - M.training_outcome_horizon()
        with c.cursor() as cur:
            cur.execute(M._basis_sql(
                f"SELECT account_id FROM txn WHERE _id = '{txn_id}'", knowledge_basis))
            selected = cur.fetchone()
        if selected is None:
            raise HTTPException(status_code=404, detail="selected transaction is not visible at this knowledge basis")
        with c.cursor() as cur:
            cur.execute(M._basis_sql(
                f"""SELECT COUNT(*), SUM(CASE WHEN l.is_fraud THEN 1 ELSE 0 END)
                    FROM txn t JOIN label l ON l._id = t._id
                    WHERE t._valid_from <= {R.ts_lit(settled_before)}""",
                knowledge_basis))
            total_rows, total_fraud = cur.fetchone()
        df = M.sample_training(limit=limit, resolved_before=settled_before,
                               system_time=knowledge_basis)
    rows = [{"txn_id": str(r.id), "txn_ts": _iso(r.txn_ts), "account_id": r.account_id,
             "amount": float(r.amount), "country": r.country, "label": bool(r.label),
             "as_known_then": int(r.prior_confirmed_fraud_as_known_then),
             "with_hindsight": int(r.prior_confirmed_fraud_with_hindsight)}
            for r in df.itertuples()]
    differing = sum(r["as_known_then"] != r["with_hindsight"] for r in rows)
    return {"knowledge_basis": _iso(knowledge_basis), "settled_before": _iso(settled_before),
            "total_rows": int(total_rows), "total_fraud": int(total_fraud or 0),
            "displayed_rows": len(rows), "rows": rows, "differing": differing,
            "sample_sql": M._sample_sql(limit=limit, resolved_before=settled_before,
                                 system_time=knowledge_basis)}


class ScoreReq(BaseModel):
    account_id: str
    amount: float
    country: str
    t: str | None = None            # decision instant (sim/valid time); default sim-now
    system_time: str | None = None  # audit: read as-of this wall-clock


@app.post("/api/score")
def score(req: ScoreReq):
    with connect() as c:
        start = time.perf_counter()
        t = _parse(req.t) if req.t else M.sim_now(c)
        sysT = _parse(req.system_time) if req.system_time else None
        ctx = R.Ctx(req.account_id, t, req.amount, req.country, system_time=sysT)
        vals, combined_sql = R.serve(c, ctx)   # one query for the three windows
        feats = []
        for f in R.FEATURES:
            badge = BADGES.get(f.name, "")
            if sysT:
                badge += " · SYSTEM_TIME AS OF"
            feats.append({
                "name": f.name, "desc": f.desc, "value": vals[f.name], "badge": badge,
                # serving shape: the real per-account point-lookup that ran live
                "sql_serve": R._with_basis(f.build(ctx), ctx),
                # training shape: the feature's window-function form (dataset pass)
                "sql_train": R.TRAIN_FRAGMENTS.get(f.name, ""),
            })
        train_sql = R._with_basis(M._extract_sql(), ctx) if sysT else M._extract_sql()
        pipe, _ = M.load_latest(c)
        decision = _decision(pipe, vals)
        elapsed_ms = (time.perf_counter() - start) * 1000
    return {"t": _iso(t), "features": feats, "decision": decision,
            "elapsed_ms": elapsed_ms,
            "serve_sql": combined_sql,          # one query, all windows, one account
            "train_sql": train_sql,             # window pass over every anchor
            "window_defs": R.WINDOW_DEFS}


class HistoricalTrainingPreview(TypedDict):
    row_count: int
    auc: float | None
    elapsed_ms: float
    live_model_unchanged: bool
    training_sql: str


@app.post("/api/training/preview")
def train_preview(system_time: str) -> HistoricalTrainingPreview:
    knowledge_basis = _parse(system_time)
    with connect() as c:
        out = M.train_window(c, persist=False, system_time=knowledge_basis)
    return {"row_count": out["n"], "auc": out["auc"],
            "elapsed_ms": out["extract_ms"] + (out["fit_ms"] or 0),
            "live_model_unchanged": True,
            "training_sql": M._extract_sql(system_time=knowledge_basis)}


class TrainReq(BaseModel):
    limit: int | None = None   # None = full 1M (window-function pass, ~seconds)


@app.post("/api/train")
def train(req: TrainReq):
    """Train over a window-function pass and register the model (serving picks it
    up). Returns the latency breakdown + the extraction SQL for disclosure."""
    with connect() as c:
        out = M.train_window(c, req.limit, persist=True)
    out["sql"] = M._extract_sql(req.limit)
    out["total_ms"] = out["extract_ms"] + (out["fit_ms"] or 0)
    out["rows_per_s"] = out["n"] / (out["total_ms"] / 1000) if out["total_ms"] else 0
    return out


class SweepReq(BaseModel):
    sizes: list[int] = [1_000, 10_000, 100_000]


@app.post("/api/train/sweep")
def train_sweep(req: SweepReq):
    """Time extraction+fit across window sizes (no persist) — the scaling curve."""
    res = []
    with connect() as c:
        for k in req.sizes:
            out = M.train_window(c, k, persist=False)
            res.append({"limit": k, "n": out["n"], "n_fraud": out["n_fraud"],
                        "extract_ms": out["extract_ms"], "fit_ms": out["fit_ms"],
                        "auc": out["auc"]})
    return res


@app.get("/api/training/query")
def training_query():
    """The extraction query itself (no execution) so the Training tab can show both
    reads: the event-time window pass, and the bitemporal prior_confirmed_fraud join
    (the `_system_from <= r._valid_from` cut is the as-of on known-time)."""
    return {"sql": M._extract_sql(), "window_defs": R.WINDOW_DEFS,
            "pcf_sql": M._pcf_sql().strip(), "pcf_leaky_sql": M._pcf_leaky_sql().strip()}


@app.get("/api/training/leakage")
def training_leakage(limit: int = 8):
    """Leak-free vs leaky prior_confirmed_fraud over the resolved training set: how many
    rows the naive (mature-label, no system-time cut) query overcounts, with examples."""
    with connect() as c:
        return M.leakage_sample(c, limit)


@app.get("/api/audit/candidates")
def audit_candidates(limit: int = 40):
    sql = f"""SELECT pc._id AS fraud_id, pc.account_id, pc.txn_ts AS fraud_ts,
                     b._id AS later_id, b.txn_ts AS later_ts, b.amount, b.country
              FROM pending_chargeback pc
              JOIN txn b ON b.account_id = pc.account_id AND b.txn_ts > pc.txn_ts
              ORDER BY b.txn_ts DESC LIMIT {limit}"""
    with connect() as c, c.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["fraud_ts"] = _iso(d["fraud_ts"])
            d["later_ts"] = _iso(d["later_ts"])
            d["amount"] = float(d["amount"])
            out.append(d)
    return out


@app.get("/api/audit/pending-accounts")
def pending_accounts():
    """Accounts with an unconfirmed prior fraud — the exhibit marks these so a
    click lands on an actionable transaction rather than a random one."""
    with connect() as c, c.cursor() as cur:
        try:
            cur.execute("SELECT DISTINCT account_id FROM pending_chargeback")
            return {"accounts": [r[0] for r in cur.fetchall()]}
        except Exception:
            return {"accounts": []}


@app.get("/api/audit/account-history")
def account_history(account_id: str, limit: int = 30):
    """The account's recent transactions with their current fraud state, so the
    correction lens can show the chargebacks sitting in the real history rather than a
    detached list. `learned_at` (label._system_from) vs `ts` (_valid_from) is the
    bitemporal signal: a confirmed fraud was true when it happened but recorded later."""
    base = f"""SELECT t._id AS id, t._valid_from AS ts, t.amount, t.country,
                      l.is_fraud AS is_fraud, l._system_from AS learned_at, {{pending}}
               FROM txn t
               JOIN label l ON l._id = t._id
               {{pcjoin}}
               WHERE t.account_id = '{account_id}'
               ORDER BY t._valid_from DESC LIMIT {limit}"""
    full = base.format(pending="pc._id AS pending_id",
                        pcjoin="LEFT JOIN pending_chargeback pc ON pc._id = t._id")
    with connect() as c:
        with c.cursor() as cur:
            try:
                cur.execute(full)
            except Exception:  # pending_chargeback may not exist yet — degrade to txn+label
                cur.execute(base.format(pending="NULL AS pending_id", pcjoin=""))
            cols = [d.name for d in cur.description]
            raw = [dict(zip(cols, r)) for r in cur.fetchall()]
        # score the whole account in one window-function pass, so every row shows a
        # verdict immediately (and reflects corrections once confirmed) — far cheaper
        # than a per-row point lookup for each transaction.
        try:
            pipe, _ = M.load_latest(c)
            scores = M.score_account(pipe, account_id, M.sim_now(c) - timedelta(days=120),
                                     knowledge="current")
        except Exception:
            scores = {}
    rows = [{"id": d["id"], "ts": _iso(d["ts"]), "amount": float(d["amount"]),
             "country": d["country"],
             "state": "fraud" if d["is_fraud"] else ("pending" if d["pending_id"] is not None else "legit"),
             "learned_at": _iso(d["learned_at"]) if d["learned_at"] else None,
             "p": scores.get(d["id"])} for d in raw]
    return {"account_id": account_id, "rows": rows}


def _pending_burst(cur, account_id: str, before: datetime):
    """The unconfirmed frauds on this account within the 90d window before `before`
    — the burst a chargeback investigation confirms together."""
    cur.execute(f"""SELECT _id, txn_ts FROM pending_chargeback
                    WHERE account_id = '{account_id}'
                      AND txn_ts <  {R.ts_lit(before)}
                      AND txn_ts >= {R.ts_lit(before)} - INTERVAL 'P90D'
                    ORDER BY txn_ts""")
    return cur.fetchall()


def _impact(c, pipe, model_version: str, account_id: str, horizon: datetime):
    """Non-mutating preview: among the account's recent real transactions, the one
    whose score would move most if its pending prior-fraud burst were confirmed.
    Returns None when the account has no pending burst or no visible effect.

    One window-function pass gives every candidate's feature vector; the 'after' is a
    pure in-memory tweak (add the pending frauds to prior_confirmed_fraud), so this is
    a single query rather than one point lookup per candidate."""
    with c.cursor() as cur:
        pend = _pending_burst(cur, account_id, horizon)
    if not pend:
        return None
    pend = [(fid, fts if fts.tzinfo else fts.replace(tzinfo=UTC)) for fid, fts in pend]
    with c.cursor() as cur:
        cur.execute(f"""SELECT _id, _valid_from, amount, country
                        FROM txn WHERE account_id = '{account_id}'
                          AND _valid_from > {R.ts_lit(horizon - timedelta(days=120))}
                        ORDER BY _valid_from DESC LIMIT 4""")
        candidates = cur.fetchall()
    best = None
    for txn_id, ts, amount, country in candidates:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        n_ts = sum(1 for _, fts in pend if fts < ts)
        if n_ts == 0:
            continue
        vals, _ = R.serve(c, R.Ctx(account_id, ts, float(amount), country))
        before = M.explain(pipe, vals)["prob"]
        v2 = dict(vals); v2["prior_confirmed_fraud"] = vals["prior_confirmed_fraud"] + n_ts
        after = M.explain(pipe, v2)["prob"]
        if best is None or (after - before) > best["delta"]:
            best = {"account_id": account_id, "txn_id": txn_id, "valid_time": _iso(ts),
                    "amount": float(amount), "country": country,
                    "before": before, "after": after, "delta": after - before,
                    "n": n_ts, "n_account": len(pend), "model_version": model_version}
    return best


@app.get("/api/audit/best-subject")
def best_subject(account_id: str):
    """The transaction on this account a pending-fraud correction would move most."""
    with connect() as c:
        pipe, meta = M.load_latest(c)
        best = _impact(c, pipe, meta["version"], account_id, M.sim_now(c))
    return {"subject": best}


@app.get("/api/audit/subject-impact")
def subject_impact(account_id: str, txn_id: str, ts: str, amount: float, country: str):
    """Impact of confirming the account's prior pending chargebacks on *this specific*
    transaction (the one clicked). before → after against the same model; the earlier
    pending frauds raise its prior_confirmed_fraud."""
    when = _parse(ts)
    with connect() as c:
        pipe, meta = M.load_latest(c)
        with c.cursor() as cur:
            all_pend = _account_pending(cur, account_id)
        all_pend = [(fid, fts if fts.tzinfo else fts.replace(tzinfo=UTC)) for fid, fts in all_pend]
        # only frauds strictly before this txn move its score (prior_confirmed_fraud excludes self)
        n_ts = sum(1 for fid, fts in all_pend if fts < when and fid != txn_id)
        self_pending = any(fid == txn_id for fid, _ in all_pend)   # this txn is itself charged back
        vals, _ = R.serve(c, R.Ctx(account_id, when, float(amount), country))
        before = M.explain(pipe, vals)["prob"]
        v2 = dict(vals); v2["prior_confirmed_fraud"] = vals["prior_confirmed_fraud"] + n_ts
        after = M.explain(pipe, v2)["prob"]
    return {"subject": {"account_id": account_id, "txn_id": txn_id, "valid_time": _iso(when),
                        "amount": float(amount), "country": country,
                        "before": before, "after": after, "delta": after - before,
                        "n": n_ts, "n_account": len(all_pend), "self_pending": self_pending,
                        "model_version": meta["version"]}}


_impactful_cache: dict = {"at": 0.0, "data": None}
_impactful_computing = False


def _impactful_fallback():
    """Find one real correction case on a cold cache so the demo is never empty.

    The pending-chargeback lookup is cheap; only accounts with real pending facts are
    considered. Ranking the full set remains background work.
    """
    try:
        with connect() as c:
            pipe, meta = M.load_latest(c)
            horizon = M.sim_now(c)
            with c.cursor() as cur:
                cur.execute("SELECT DISTINCT account_id FROM pending_chargeback LIMIT 8")
                accounts = [r[0] for r in cur.fetchall()]
            best = None
            for account_id in accounts:
                case = _impact(c, pipe, meta["version"], account_id, horizon)
                if case and (best is None or case["delta"] > best["delta"]):
                    best = case
            return [best] if best is not None else []
    except Exception:
        return []


def _schedule_case_ranking():
    threading.Thread(target=_impactful_refresh, daemon=True).start()


def _impactful_refresh():
    """Recompute the ranked impactful cases (scores many transactions — seconds).
    Runs on a background thread so it never blocks a request / first render."""
    global _impactful_computing
    with _score_lock:
        if _impactful_computing:
            return
        _impactful_computing = True
    try:
        cases = []
        with connect() as c:
            pipe, meta = M.load_latest(c)
            horizon = M.sim_now(c)
            with c.cursor() as cur:
                cur.execute("SELECT DISTINCT account_id FROM pending_chargeback")
                accts = [r[0] for r in cur.fetchall()]
            for acc in accts:
                b = _impact(c, pipe, meta["version"], acc, horizon)
                if b and b["delta"] > 0.02:
                    cases.append(b)
        cases.sort(key=lambda x: -x["delta"])
        _impactful_cache.update(at=time.time(), data=cases)
    except Exception:
        pass
    finally:
        _impactful_computing = False


@app.get("/api/audit/impactful")
def impactful(limit: int = 12):
    """Pending-fraud cases ranked by how much confirming them would move a real score
    — marks actionable accounts + the fallback list. Returns the cached result
    immediately (empty on a cold start); refreshes in the background when stale, so it
    is never on the render path."""
    now = time.time()
    cached = _impactful_cache["data"]
    if cached is None:
        cached = _impactful_fallback()
        if cached:
            _impactful_cache.update(at=now, data=cached)
    stale = _impactful_cache["data"] is None or now - _impactful_cache["at"] > 45
    if stale and not _impactful_computing:
        _schedule_case_ranking()
    return {"cases": (cached or [])[:limit]}


class ConfirmReq(BaseModel):
    account_id: str
    later_ts: str
    amount: float
    country: str
    model_version: str


class ConfirmResponse(TypedDict):
    before: float
    after: float
    reproduced: float
    system_time: str
    model_version: str
    repro_sql: str
    n_confirmed: int
    n_prior: int
    pcf_before: float
    pcf_after: float
    pcf_reproduced: float


def _account_pending(cur, account_id: str):
    """Every unconfirmed chargeback on the account (any time) — the whole set an
    investigation confirms, independent of which transaction is being examined."""
    cur.execute(f"""SELECT _id, txn_ts FROM pending_chargeback
                    WHERE account_id = '{account_id}' ORDER BY txn_ts""")
    return cur.fetchall()


@app.post("/api/audit/confirm")
def audit_confirm(req: ConfirmReq) -> ConfirmResponse:
    """Confirm ALL of the account's pending chargebacks (the real, account-level action)
    and measure the effect on the examined transaction: before → after → reproduced
    (SYSTEM_TIME AS OF before the confirm). Same model, only the data moved.

    The examined transaction's score moves only by the chargebacks that predate it — the
    `prior_confirmed_fraud` window is strictly-before, so later frauds can't leak into an
    earlier decision. `n_confirmed` is the whole set; `n_prior` is the subset affecting
    this transaction."""
    with connect() as c:
        later = _parse(req.later_ts)
        ctxB = R.Ctx(req.account_id, later, req.amount, req.country)
        pipe, meta = M.load_version(c, req.model_version)
        vecB = R.vector(c, ctxB)
        before = M.explain(pipe, vecB)
        with c.cursor() as cur:
            pend = _account_pending(cur, req.account_id)
        if not pend:
            # Nothing in flight, so nothing to confirm and nothing to reproduce. Reachable
            # from a stale ranking, and worth answering honestly rather than rewinding to a
            # basis derived from an empty write, which reads as a score that moved on its own.
            pf = [f for f in R.FEATURES if f.name == "prior_confirmed_fraud"][0]
            return {"before": before["prob"], "after": before["prob"],
                    "reproduced": before["prob"], "system_time": _iso(later),
                    "model_version": meta["version"],
                    "repro_sql": R._with_basis(pf.build(ctxB), ctxB),
                    "n_confirmed": 0, "n_prior": 0,
                    "pcf_before": vecB["prior_confirmed_fraud"],
                    "pcf_after": vecB["prior_confirmed_fraud"],
                    "pcf_reproduced": vecB["prior_confirmed_fraud"]}
        # confirm at system-time = sim-now (when we learn it now); the fraud was true
        # from its event, so valid-time stays the original txn time.
        def _confirm(cur, confirmed_at):
            cur.executemany(
                "INSERT INTO label (_id, _valid_from, is_fraud) VALUES (%s, %s, true)",
                [(fid, fts) for fid, fts in pend],
            )
            cur.executemany(
                "INSERT INTO fraud_status (_id, _valid_from, is_fraud) VALUES (%s, %s, true)",
                [(fid, confirmed_at) for fid, _ in pend],
            )
            cur.executemany(
                "DELETE FROM pending_chargeback WHERE _id = %s",
                [(fid,) for fid, _ in pend],
            )
        confirmed_at, _ = sys_tx_retry(c, lambda: M.sim_now(c), _confirm)
        n_prior = sum(1 for _, fts in pend
                      if (fts if fts.tzinfo else fts.replace(tzinfo=UTC)) < later)
        # Re-read rather than add n_prior to the vector we already have. The arithmetic
        # would give the same answer more cheaply, but the point being demonstrated is
        # that the store returns it, so the store has to be asked.
        vecA = R.vector(c, ctxB)
        after = M.explain(pipe, vecA)
        # And ask a third time with system-time wound back to just before the confirm.
        # This is the claim of the whole exercise: same model, same query, a basis that
        # predates what we just learned, and the original decision comes back.
        as_of = confirmed_at - timedelta(microseconds=1)
        ctx_repro = R.Ctx(req.account_id, later, req.amount, req.country, system_time=as_of)
        vecR = R.vector(c, ctx_repro)
        repro = M.explain(pipe, vecR)
        pf = [f for f in R.FEATURES if f.name == "prior_confirmed_fraud"][0]
        repro_sql = R._with_basis(pf.build(ctx_repro), ctx_repro)
    return {"before": before["prob"], "after": after["prob"],
            "reproduced": repro["prob"], "system_time": _iso(as_of),
            "model_version": meta["version"], "repro_sql": repro_sql,
            "n_confirmed": len(pend), "n_prior": n_prior,
            "pcf_before": vecB["prior_confirmed_fraud"], "pcf_after": vecA["prior_confirmed_fraud"],
            "pcf_reproduced": vecR["prior_confirmed_fraud"]}
