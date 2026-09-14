import { useCallback, useEffect, useRef, useState } from 'react'
import { getAccountHistory, getBestSubject, getImpactful, getPendingAccounts, getSubjectImpact, postConfirm } from '../api'
import type { AcctTxn, ConfirmResp, ImpactCase, Selection } from '../api'
import { Sql } from '../components/Sql'

const clock = (iso: string) => new Date(iso).toISOString().slice(0, 16).replace('T', ' ')
const verdict = (p: number) => (p >= 0.5 ? 'fraud' : 'legit')

function StateTag({ state }: { state: AcctTxn['state'] }) {
  if (state === 'fraud')
    return <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-950 text-red-300 border border-red-900">charged back</span>
  if (state === 'pending')
    return <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-950/60 text-amber-300 border border-amber-900/70">⚠ pending</span>
  return <span className="text-[10px] px-1.5 py-0.5 text-neutral-600">legit</span>
}

function ScoreBadge({ p }: { p: number | null }) {
  if (p == null) return <span className="w-14 text-right font-mono text-neutral-700">—</span>
  return (
    <span className={`w-14 text-right font-mono ${p >= 0.5 ? 'text-red-400' : 'text-neutral-400'}`}>
      {p >= 0.5 ? '● ' : ''}{p.toFixed(3)}
    </span>
  )
}

function History({ account_id, subjectId, refreshKey, onPick }: {
  account_id: string; subjectId?: string; refreshKey: string; onPick?: (r: AcctTxn) => void
}) {
  const [rows, setRows] = useState<AcctTxn[]>([])
  const [changed, setChanged] = useState<Set<string>>(new Set())    // rows whose score just moved
  const prev = useRef<Record<string, number | null>>({})

  useEffect(() => {
    let on = true
    getAccountHistory(account_id, 20).then((h) => {
      if (!on) return
      const moved = new Set<string>()
      for (const r of h.rows) {
        const was = prev.current[r.id]
        if (was != null && r.p != null && Math.abs(r.p - was) > 5e-4) moved.add(r.id)
      }
      prev.current = Object.fromEntries(h.rows.map((r) => [r.id, r.p]))
      setRows(h.rows)
      setChanged(moved)
      if (moved.size) window.setTimeout(() => { if (on) setChanged(new Set()) }, 2500)
    }).catch(() => {})
    return () => { on = false }
    // refreshKey bumps on each confirm so the scores re-read the corrected data
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [account_id, subjectId, refreshKey])

  return (
    <div className="mt-6 pt-4 border-t border-neutral-800">
      <div className="text-[11px] uppercase tracking-wide text-neutral-500 mb-2">
        account history · <span className="font-mono text-neutral-300">{account_id}</span>
      </div>
      <div className="space-y-1">
        {rows.map((r) => (
          <div key={r.id}
            onClick={() => onPick?.(r)}
            className={`flex items-center gap-2 px-2.5 py-1.5 rounded-md border text-xs transition-colors duration-700 ${
              onPick ? 'cursor-pointer hover:border-neutral-700' : ''} ${
              changed.has(r.id) ? 'border-amber-700 bg-amber-500/20'
                : r.id === subjectId ? 'border-sky-700 bg-sky-950/50' : 'border-neutral-900'}`}>
            <span className="font-mono text-neutral-500 w-28 shrink-0">{clock(r.ts)}</span>
            <span className="font-mono text-right w-14 shrink-0">£{r.amount.toFixed(0)}</span>
            <span className="w-7 text-neutral-400 shrink-0">{r.country}</span>
            <StateTag state={r.state} />
            <span className="flex-1" />
            <ScoreBadge p={r.p} />
          </div>
        ))}
      </div>
    </div>
  )
}

function Score({ label, p, tone }: { label: string; p: number; tone: 'base' | 'up' | 'repro' }) {
  const color = tone === 'up' ? 'text-red-400' : tone === 'repro' ? 'text-emerald-400' : 'text-neutral-200'
  return (
    <div className="border border-neutral-800 rounded-lg p-3 flex-1 text-center">
      <div className="text-[10px] uppercase tracking-wide text-neutral-500">{label}</div>
      <div className={`text-2xl font-mono ${color}`}>{p.toFixed(3)}</div>
      <div className={`text-xs ${p >= 0.5 ? 'text-red-400' : 'text-neutral-500'}`}>{verdict(p)}</div>
    </div>
  )
}

export function AuditView({ seed, onCorrected }: { seed?: Selection | null; onCorrected?: (account_id: string) => void }) {
  const [subject, setSubject] = useState<ImpactCase | null>(null)
  const [res, setRes] = useState<ConfirmResp | null>(null)
  const [busy, setBusy] = useState(false)
  const [impactful, setImpactful] = useState<ImpactCase[]>([])

  const loadImpactful = () => getImpactful(12).then(async (r) => {
    if (r.cases.length) { setImpactful(r.cases); return }
    const { accounts } = await getPendingAccounts()
    if (!accounts[0]) { setImpactful([]); return }
    const fallback = await getBestSubject(accounts[0])
    setImpactful(fallback.subject ? [fallback.subject] : [])
  }).catch(() => {})
  useEffect(() => { loadImpactful() }, [])

  // Focus a specific transaction (the one clicked, in the table or the account history)
  // as the subject — its own score, and how confirming this account's prior chargebacks
  // would move it.
  const focus = useCallback((sel: { account_id: string; txn_id: string; valid_time: string; amount: number; country: string }) => {
    setRes(null)
    getSubjectImpact(sel).then((r) => setSubject(r.subject)).catch(() => setSubject(null))
  }, [])

  useEffect(() => {
    setRes(null); setSubject(null)
    if (seed) focus(seed)
  }, [seed, focus])

  const confirm = async () => {
    if (!subject) return
    setBusy(true)
    try {
      setRes(await postConfirm({
        account_id: subject.account_id, later_ts: subject.valid_time,
        amount: subject.amount, country: subject.country, model_version: subject.model_version,
      }))
      loadImpactful()
      onCorrected?.(subject.account_id)   // nudge the main table to re-score this account
    } finally { setBusy(false) }
  }

  const pick = (c: ImpactCase) => { setRes(null); setSubject(c) }

  const Ranked = ({ title }: { title: string }) => (
    <div className="mt-6 pt-4 border-t border-neutral-800">
      <div className="text-[11px] uppercase tracking-wide text-neutral-500 mb-2">{title}</div>
      {impactful.length === 0 ? (
        <div className="text-neutral-600 text-sm">finding an actionable correction…</div>
      ) : (
        <div className="space-y-1">
          {impactful.map((c) => (
            <button key={c.txn_id} onClick={() => pick(c)}
              className="w-full flex items-center justify-between gap-3 px-3 py-1.5 rounded-md border border-neutral-800 hover:bg-neutral-900 text-left text-sm">
              <span className="font-mono text-xs">{c.account_id} · £{c.amount.toFixed(0)} {c.country}</span>
              <span className="font-mono text-xs">
                <span className="text-neutral-400">{c.before.toFixed(2)}</span>
                <span className="text-neutral-600"> → </span>
                <span className="text-red-400">{c.after.toFixed(2)}</span>
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  )

  if (!subject) {
    return (
      <div>
        <p className="text-neutral-400 text-sm">
          Click a transaction on an <span className="text-amber-400">⚠</span> account — one that follows a late
          chargeback — to see the correction, then how <code className="text-neutral-200">SYSTEM_TIME AS OF</code>
          {' '}and the pinned model version reproduce the before score.
        </p>
        <Ranked title="high-impact cases" />
      </div>
    )
  }

  const s = subject
  const pickRow = (r: AcctTxn) =>
    focus({ account_id: s.account_id, txn_id: r.id, valid_time: r.ts, amount: r.amount, country: r.country })
  return (
    <div className="space-y-4">
      {!res && (
        s.n_account === 0 ? (
          <div className="flex items-center gap-3">
            <Score label="scored now" p={s.before} tone="base" />
            <div className="flex-1 text-sm text-neutral-500">
              No chargebacks in flight on this account, so there's nothing to confirm. Pick a transaction on an{' '}
              <span className="text-amber-300">⚠</span> account below to see a correction move its score.
            </div>
          </div>
        ) : (
          <div className="flex items-center gap-3">
            <Score label="scored now · fraud pending" p={s.before} tone="base" />
            <div className="flex-1 text-sm text-neutral-400">
              {s.n === 0 ? (
                s.self_pending ? (
                  <>
                    This transaction is itself a suspected fraud, charged back late. Confirming records it as fraud, but
                    its own score won't move: <code className="text-neutral-200">prior_confirmed_fraud</code> counts frauds
                    strictly <em>before</em> a transaction, so it never counts itself. It does raise the count for the
                    account's <em>later</em> transactions.
                  </>
                ) : (
                  <>
                    The <span className="text-amber-300">{s.n_account} chargeback{s.n_account > 1 ? 's' : ''}</span> in flight
                    on this account occur <em>after</em> this transaction, so its score won't change. Confirming still
                    records them, and moves later transactions.
                  </>
                )
              ) : s.after - s.before > 0.02 ? (
                <>
                  <span className="text-amber-300">{s.n_account} chargeback{s.n_account > 1 ? 's' : ''}</span> in flight
                  on this account. Confirming records them all; the <span className="text-neutral-200">{s.n}</span> before
                  this transaction raise <code className="text-neutral-200">prior_confirmed_fraud</code>, moving its score
                  to ≈<span className="font-mono">{s.after.toFixed(3)}</span>.
                </>
              ) : (
                <>
                  <span className="text-amber-300">{s.n_account} chargeback{s.n_account > 1 ? 's' : ''}</span> in flight,
                  but this transaction is already clearly classified, so confirming barely moves it. Pick a{' '}
                  <em>borderline</em> transaction below to see a correction land.
                </>
              )}
              <button onClick={confirm} disabled={busy}
                className="mt-2 block px-3 py-2 rounded-md bg-sky-600 hover:bg-sky-500 disabled:opacity-50 text-white text-sm">
                {busy ? 'confirming…' : `Confirm chargeback${s.n_account > 1 ? 's' : ''} (${s.n_account})`}
              </button>
            </div>
          </div>
        )
      )}

      {res && (
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <Score label="before · fraud pending" p={res.before} tone="base" />
            <div className="text-neutral-600">→</div>
            <Score label="after · chargeback confirmed" p={res.after} tone="up" />
            <div className="text-neutral-600">↻</div>
            <Score label={`reproduced · ${res.model_version} · AS OF ${res.system_time.slice(11, 19)}`} p={res.reproduced} tone="repro" />
          </div>
          <p className="text-sm text-neutral-400">
            Confirmed <span className="text-neutral-200">{res.n_confirmed}</span> chargeback{res.n_confirmed !== 1 ? 's' : ''} on
            this account.{' '}
            {res.n_confirmed !== res.n_prior ? (
              <>This transaction sees only the <span className="text-neutral-200">{res.n_prior}</span> that predate it —{' '}
              <code className="text-neutral-200">prior_confirmed_fraud</code> is strictly-before, so the later{' '}
              {res.n_confirmed - res.n_prior} can't leak into an earlier decision. </>
            ) : (
              <>All of them predate this transaction. </>
            )}
            Its <code className="text-neutral-200">prior_confirmed_fraud</code> went{' '}
            <span className="font-mono">{res.pcf_before.toFixed(0)} → {res.pcf_after.toFixed(0)}</span>, so the{' '}
            <strong>same model</strong> now scores it{' '}
            <span className="font-mono text-red-400">{res.after.toFixed(3)}</span> (was{' '}
            <span className="font-mono">{res.before.toFixed(3)}</span>).{' '}
            {Math.abs(res.reproduced - res.before) < 1e-9 ? (
              <>Rewinding <code className="text-neutral-200">SYSTEM_TIME AS OF</code> before the chargeback and loading model{' '}
              <span className="font-mono text-neutral-200">{res.model_version}</span> reproduces{' '}
              <span className="font-mono text-emerald-400">{res.reproduced.toFixed(3)}</span> — <strong>exactly</strong> the
              same model and data basis.</>
            ) : (
              <>Rewound score: <span className="font-mono text-emerald-400">{res.reproduced.toFixed(3)}</span>.</>
            )}
          </p>
          <div>
            <div className="text-[11px] uppercase tracking-wide text-neutral-500 mb-1">the rewind — note the system-time basis</div>
            <Sql code={res.repro_sql} className="rounded border border-neutral-800" />
          </div>
        </div>
      )}

      <History account_id={s.account_id} subjectId={s.txn_id} refreshKey={res ? res.system_time : 'base'} onPick={pickRow} />
    </div>
  )
}
