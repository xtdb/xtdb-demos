export type Status = {
  sim_now: string | null
  n_txn: number
  n_fraud: number
  model: { version: string; auc: number; n_train: number } | null
}
export type Feature = { name: string; desc: string; sql_serve: string; sql_train: string; value: number; badge: string }
export type Contribution = { name: string; value: number }
export type Decision = { prob: number; verdict: string; intercept: number; contributions: Contribution[] } | null
export type Shape = 'serve' | 'train'
export type ScoreResp = {
  t: string; features: Feature[]; decision: Decision; elapsed_ms: number
  serve_sql: string; train_sql: string; window_defs: string
}
export type TrainResult = {
  n: number; n_fraud: number; extract_ms: number; fit_ms: number | null
  total_ms: number; rows_per_s: number; auc: number | null; version: string | null; sql: string
}
export type SweepRow = { limit: number; n: number; n_fraud: number; extract_ms: number; fit_ms: number | null; auc: number | null }
export type AuditCand = {
  fraud_id: string; account_id: string; fraud_ts: string
  later_id: string; later_ts: string; amount: number; country: string
}
export type ConfirmResp = {
  before: number; after: number; reproduced: number
  system_time: string; model_version: string; repro_sql: string; n_confirmed: number; n_prior: number
  pcf_before: number; pcf_after: number
}

export type AcctTxn = {
  id: string; ts: string; amount: number; country: string
  state: 'legit' | 'pending' | 'fraud'; learned_at: string | null; p: number | null
}
export type AcctHistory = { account_id: string; rows: AcctTxn[] }

async function j<T>(r: Response): Promise<T> {
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}
const post = (url: string, body: unknown) =>
  fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })

export const getStatus = () => fetch('/api/status').then(j<Status>)
export const getTrainingQuery = () => fetch('/api/training/query').then(j<{ sql: string; window_defs: string; pcf_sql: string; pcf_leaky_sql: string }>)
export type Leakage = {
  rows: number; differing: number; overcount: number
  examples: { txn_id: string; account_id: string; valid_time: string | null; leak_free: number; leaky: number }[]
}
export const getTrainingLeakage = (limit = 8) => fetch(`/api/training/leakage?limit=${limit}`).then(j<Leakage>)
export const getAccounts = () => fetch('/api/accounts').then(j<string[]>)
export const postScore = (b: { account_id: string; amount: number; country: string; t?: string; system_time?: string }) =>
  post('/api/score', b).then(j<ScoreResp>)
export const getAccountHistory = (account_id: string, limit = 30) =>
  fetch(`/api/audit/account-history?account_id=${encodeURIComponent(account_id)}&limit=${limit}`).then(j<AcctHistory>)
export const getAuditCandidates = () => fetch('/api/audit/candidates').then(j<AuditCand[]>)
export const postConfirm = (b: { account_id: string; later_ts: string; amount: number; country: string; model_version: string }) =>
  post('/api/audit/confirm', b).then(j<ConfirmResp>)
export const postTrain = (b: { limit: number | null }) => post('/api/train', b).then(j<TrainResult>)
export const postSweep = (b: { sizes: number[] }) => post('/api/train/sweep', b).then(j<SweepRow[]>)

// --- data-central canvas ---
export type Bounds = { valid_min: string; valid_now: string; system_min: string; system_now: string }
export type FeedItem = {
  id: string; account: string; amount: number; country: string
  kind: 'txn' | 'correction'; happened_at: string; learned_at: string
}
export type DataRow = {
  id: string; account: string; amount: number; country: string; happened_at: string; recorded_at: string
  label: string        // recorded — chargeback-confirmed is_fraud
  p: number | null     // model — predicted probability of fraud (null until scored)
}
export type DataResp = { valid_time: string; system_time: string | null; rows: DataRow[]; sql: string }
export type Basis = { valid_time: string | null; system_time: string | null }
// A row picked from the live exhibit, handed off to seed a lens.
export type Selection = {
  account_id: string; txn_id: string; valid_time: string; system_time: string; label: string
  amount: number; country: string
}
export type TrainingDataset = {
  knowledge_basis: string; settled_before: string; total_rows: number; total_fraud: number
  displayed_rows: number; differing: number; sample_sql: string
  rows: { txn_id: string; txn_ts: string; account_id: string; amount: number; country: string; label: boolean
    as_known_then: number; with_hindsight: number }[]
}
export const getTrainingDataset = (s: Selection, limit = 60) => {
  const p = new URLSearchParams({ txn_id: s.txn_id, system_time: s.system_time, limit: String(limit) })
  return fetch(`/api/training?${p}`).then(j<TrainingDataset>)
}
export type HistoricalTrainingPreview = {
  row_count: number; auc: number | null; elapsed_ms: number; live_model_unchanged: boolean; training_sql: string
}
export const postTrainingPreview = (systemTime: string) => {
  const p = new URLSearchParams({ system_time: systemTime })
  return fetch(`/api/training/preview?${p}`, { method: 'POST' }).then(j<HistoricalTrainingPreview>)
}
// A pending-fraud correction previewed against a real transaction on the account.
export type ImpactCase = {
  account_id: string; txn_id: string; valid_time: string; amount: number; country: string
  before: number; after: number; delta: number; n: number; n_account: number; model_version: string
  self_pending?: boolean   // the examined transaction is itself a pending chargeback
}
export const getPendingAccounts = () =>
  fetch('/api/audit/pending-accounts').then(j<{ accounts: string[] }>)
export const getBestSubject = (account_id: string) =>
  fetch(`/api/audit/best-subject?account_id=${encodeURIComponent(account_id)}`).then(j<{ subject: ImpactCase | null }>)
export const getSubjectImpact = (s: { account_id: string; txn_id: string; valid_time: string; amount: number; country: string }) => {
  const p = new URLSearchParams({
    account_id: s.account_id, txn_id: s.txn_id, ts: s.valid_time,
    amount: String(s.amount), country: s.country,
  })
  return fetch(`/api/audit/subject-impact?${p}`).then(j<{ subject: ImpactCase }>)
}
export const getImpactful = (limit = 12) =>
  fetch(`/api/audit/impactful?limit=${limit}`).then(j<{ cases: ImpactCase[] }>)

export type SimStatus = {
  running: boolean; speed: number; sim_now: string | null
  made: number; landed: number; ticks: number
}
export const getSim = () => fetch('/api/sim').then(j<SimStatus>)
export const startSim = (speed?: number) => post('/api/sim/start', { speed }).then(j<SimStatus>)
export const stopSim = () => post('/api/sim/stop', {}).then(j<SimStatus>)
export const setSimSpeed = (speed: number) => post('/api/sim/speed', { speed }).then(j<SimStatus>)

export const getBounds = () => fetch('/api/bounds').then(j<Bounds>)
export const getFeed = (limit = 40) => fetch(`/api/feed?limit=${limit}`).then(j<{ items: FeedItem[] }>)
export const getData = (b: Basis & { limit?: number; before?: string | null }) => {
  const p = new URLSearchParams()
  if (b.valid_time) p.set('valid_time', b.valid_time)
  if (b.system_time) p.set('system_time', b.system_time)
  if (b.limit) p.set('limit', String(b.limit))
  if (b.before) p.set('before', b.before)
  return fetch(`/api/data?${p}`).then(j<DataResp>)
}
