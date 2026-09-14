import { useEffect, useRef, useState } from 'react'
import { getBestSubject, getData, getImpactful, getPendingAccounts, getSim, postScore } from '../api'
import type { Basis, DataRow, ImpactCase, Selection } from '../api'
import { Sql } from './Sql'

const clock = (iso: string) => new Date(iso).toISOString().slice(0, 16).replace('T', ' ')

type Flash = 'new' | 'corrected'
// A re-score triggered by a correction: the old displayed score and the fresh one.
type ScoreFx = { from: number | null; to: number }

export function DataView({
  basis, onSelect, selectedId, correction,
}: {
  basis: Basis
  onSelect?: (s: Selection) => void
  selectedId?: string | null
  correction?: { account_id: string; nonce: number } | null
}) {
  const [rows, setRows] = useState<DataRow[]>([])
  const [sql, setSql] = useState('')
  const [showSql, setShowSql] = useState(false)
  const [flash, setFlash] = useState<Record<string, Flash>>({})
  const [pending, setPending] = useState<Set<string>>(new Set())  // accounts where a correction visibly moves a score
  const [simOn, setSimOn] = useState(false)
  const [spotlight, setSpotlight] = useState<ImpactCase | null>(null)
  // Corrections re-score the affected account's rows client-side (the backend cache
  // holds the pre-correction score). `overrides` win over the polled `p`; `scoreFx`
  // drives the transient old→new reveal; `rescoring` marks rows mid-recompute.
  const [overrides, setOverrides] = useState<Record<string, number>>({})
  const [scoreFx, setScoreFx] = useState<Record<string, ScoreFx>>({})
  const [rescoring, setRescoring] = useState<Set<string>>(new Set())
  // Pagination: stack of `before` cursors (ISO timestamps) — each entry is the
  // oldest `happened_at` from the page that pushed it. Empty = live head (page 1).
  const [pageStack, setPageStack] = useState<string[]>([])
  const prev = useRef<Map<string, string>>(new Map())   // id -> label, for diffing
  const live = !basis.valid_time && !basis.system_time

  // The `before` cursor for the current page: top of stack, or undefined (live head).
  const before = pageStack.length > 0 ? pageStack[pageStack.length - 1] : undefined
  // We're on the live head iff the basis is live AND we have no page cursor.
  const isLiveHead = live && before === undefined

  // Reset page stack whenever the basis changes so we start at the head.
  const prevBasis = useRef<Basis>({ valid_time: null, system_time: null })
  useEffect(() => {
    if (
      prevBasis.current.valid_time !== basis.valid_time ||
      prevBasis.current.system_time !== basis.system_time
    ) {
      prevBasis.current = basis
      setPageStack([])
    }
  }, [basis.valid_time, basis.system_time])

  useEffect(() => {
    const tick = () => getImpactful(30).then((r) => setPending(new Set(r.cases.map((x) => x.account_id)))).catch(() => {})
    tick()
    const t = window.setInterval(tick, 20000)
    return () => window.clearInterval(t)
  }, [])

  useEffect(() => {
    const tick = () => getSim().then((s) => setSimOn(s.running)).catch(() => {})
    tick()
    const t = window.setInterval(tick, 2000)
    return () => window.clearInterval(t)
  }, [])

  // A correction landed: re-score that account's visible rows against the corrected
  // data and reveal the move (old → new). Only rows after the confirmed fraud shift.
  useEffect(() => {
    if (!correction) return
    const acct = correction.account_id
    const targets = rows.filter((r) => r.account === acct)
    if (targets.length === 0) return
    setRescoring((s) => new Set([...s, ...targets.map((r) => r.id)]))
    targets.forEach((r) => {
      const from = (overrides[r.id] ?? r.p) ?? null
      postScore({ account_id: acct, amount: r.amount, country: r.country, t: r.happened_at })
        .then((d) => {
          const to = d.decision?.prob ?? 0
          setOverrides((o) => ({ ...o, [r.id]: to }))
          if (from == null || Math.abs(to - from) > 5e-4) {
            setScoreFx((s) => ({ ...s, [r.id]: { from, to } }))
            window.setTimeout(() => setScoreFx((s) => { const { [r.id]: _drop, ...rest } = s; return rest }), 5000)
          }
        })
        .catch(() => {})
        .finally(() => setRescoring((s) => { const n = new Set(s); n.delete(r.id); return n }))
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [correction?.nonce])

  useEffect(() => {
    prev.current = new Map()          // fresh baseline whenever the basis or page changes
    setFlash({})
    let on = true

    const load = async () => {
      const r = await getData({ ...basis, limit: 50, before }).catch(() => null)
      if (!on || !r) return
      const seen = prev.current
      const firstLoad = seen.size === 0
      const f: Record<string, Flash> = {}
      // Only flash new/corrected rows when we're on the live head.
      if (isLiveHead) {
        for (const row of r.rows) {
          const prevLabel = seen.get(row.id)
          if (prevLabel === undefined) { if (!firstLoad) f[row.id] = 'new' }
          else if (prevLabel !== row.label && row.label === 'fraud') f[row.id] = 'corrected'
        }
      }
      prev.current = new Map(r.rows.map((x) => [x.id, x.label]))
      setRows(r.rows)
      setSql(r.sql)
      const ids = Object.keys(f)
      if (ids.length) {
        setFlash((cur) => ({ ...cur, ...f }))
        ids.forEach((id) =>
          setTimeout(() => setFlash((c) => { const { [id]: _drop, ...rest } = c; return rest }), 1400))
      }
    }

    load()
    // Only poll on the live head — older pages are static snapshots.
    const t = isLiveHead ? window.setInterval(load, 800) : null
    return () => { on = false; if (t) window.clearInterval(t) }
  }, [basis.valid_time, basis.system_time, before, isLiveHead])

  useEffect(() => {
    if (!onSelect || rows.length === 0) return
    getImpactful(1)
      .then(({ cases }) => cases[0] ? { subject: cases[0] } : getPendingAccounts()
        .then(({ accounts }) => accounts[0] ? getBestSubject(accounts[0]) : { subject: null }))
      .then((result) => setSpotlight(result.subject))
      .catch(() => {})
  }, [rows.length, onSelect])

  const goOlder = () => {
    if (rows.length === 0) return
    // Use the oldest visible row's timestamp as the next `before` cursor.
    const oldest = rows[rows.length - 1].happened_at
    setPageStack((s) => [...s, oldest])
  }

  const goNewer = () => {
    setPageStack((s) => s.slice(0, -1))
  }

  const goLive = () => setPageStack([])

  const pageNum = pageStack.length + 1   // page 1 = live head

  return (
    <div className="border border-neutral-800 rounded-lg overflow-hidden flex flex-col h-full">
      <div className="flex items-center justify-between px-3 py-2 border-b border-neutral-800 bg-neutral-900">
        <h2 className="text-xs uppercase tracking-wide text-neutral-500">
          transactions{' '}
          {isLiveHead
            ? (simOn
                ? <span className="text-emerald-400 normal-case">· live</span>
                : <span className="text-neutral-500 normal-case">· idle</span>)
            : live
              ? <span className="text-amber-300 normal-case">· paused · page {pageNum}</span>
              : <span className="text-amber-300 normal-case">· at pinned basis</span>}
        </h2>
        <button onClick={() => setShowSql((s) => !s)} className="text-[11px] text-sky-400 hover:underline">
          {showSql ? 'hide' : 'show'} query
        </button>
      </div>
      {showSql && sql && <Sql code={sql} className="border-b border-neutral-800" />}
      {spotlight && !rows.some((r) => r.account === spotlight.account_id) && (
        <button
          onClick={() => onSelect?.({ ...spotlight, system_time: spotlight.valid_time, label: 'pending' })}
          className="px-3 py-2 text-left text-xs border-b border-amber-900/60 bg-amber-950/30 hover:bg-amber-950/50"
        >
          <span className="text-amber-300">⚠ correction ready</span>
          <span className="text-neutral-400"> · {spotlight.account_id} · score {spotlight.before.toFixed(2)} → {spotlight.after.toFixed(2)} · open case</span>
        </button>
      )}
      {!isLiveHead && live && (
        <div className="px-3 py-1.5 text-[11px] text-amber-300/70 border-b border-neutral-800 bg-neutral-950 flex items-center gap-2">
          <span>viewing older transactions — live updates paused</span>
          <button onClick={goLive} className="underline hover:text-amber-300">back to live</button>
        </div>
      )}
      <div className="overflow-auto flex-1">
        <table className="w-full text-sm">
          <thead className="bg-neutral-950 text-neutral-500 text-xs uppercase sticky top-0">
            <tr>
              <th className="text-left px-3 py-2">time</th>
              <th className="text-left px-3 py-2">account</th>
              <th className="text-right px-3 py-2">amount</th>
              <th className="text-left px-3 py-2">ctry</th>
              <th className="text-left px-3 py-2" title="the model's live fraud probability for this transaction">model p(fraud)</th>
              <th className="text-left px-3 py-2" title="the recorded outcome — a chargeback confirms fraud, often days after the transaction">recorded</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const fl = flash[r.id]
              const fx = scoreFx[r.id]
              const selected = selectedId === r.id
              const bg = fx ? 'bg-amber-500/25'
                : selected ? 'bg-sky-950/60'
                : fl === 'new' ? 'bg-emerald-500/25'
                : fl === 'corrected' ? 'bg-amber-500/30' : ''
              return (
                <tr
                  key={r.id}
                  onClick={() => onSelect?.({ account_id: r.account, txn_id: r.id, valid_time: r.happened_at, system_time: r.recorded_at, label: r.label, amount: r.amount, country: r.country })}
                  className={`border-t border-neutral-900 transition-colors duration-1000 ${bg} ${onSelect ? 'cursor-pointer hover:bg-neutral-900' : ''}`}
                >
                  <td className="px-3 py-1.5 font-mono text-xs text-neutral-400">{clock(r.happened_at)}</td>
                  <td className="px-3 py-1.5 font-mono text-xs">
                    {pending.has(r.account) && <span title="unconfirmed prior fraud — click to correct" className="text-amber-400 mr-1">⚠</span>}
                    {r.account}
                  </td>
                  <td className="px-3 py-1.5 text-right font-mono">£{r.amount.toFixed(0)}</td>
                  <td className="px-3 py-1.5">{r.country}</td>
                  <td className="px-3 py-1.5 font-mono text-xs">
                    {(() => {
                      const shown = overrides[r.id] ?? r.p
                      const scoreEl = (p: number) => p >= 0.5
                        ? <span className="text-red-400">● fraud {p.toFixed(2)}</span>
                        : <span className="text-neutral-500">{p.toFixed(2)}</span>
                      if (shown == null)
                        return isLiveHead && simOn
                          ? <span className="text-neutral-600 animate-pulse">scoring…</span>
                          : <span className="text-neutral-700">—</span>
                      if (rescoring.has(r.id))
                        return <span className="opacity-60 animate-pulse">{scoreEl(shown)}</span>
                      if (fx)
                        return (
                          <span className="inline-flex items-center gap-1">
                            <span className="text-neutral-600 line-through">{fx.from == null ? '—' : fx.from.toFixed(2)}</span>
                            <span className="text-amber-400">→</span>
                            {scoreEl(fx.to)}
                          </span>
                        )
                      return scoreEl(shown)
                    })()}
                  </td>
                  <td className="px-3 py-1.5 text-xs">
                    {r.label === 'fraud'
                      ? <span className="text-red-400">fraud</span>
                      : r.label === 'pending'
                        ? <span className="text-amber-400/70" title="chargeback in flight — not yet settled">pending</span>
                        : <span className="text-neutral-600">legit</span>}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {/* Pager — only shown in live mode (paging doesn't apply to pinned basis) */}
      {live && (
        <div className="flex items-center justify-between px-3 py-2 border-t border-neutral-800 bg-neutral-900 text-xs text-neutral-500">
          <button
            onClick={goNewer}
            disabled={pageStack.length === 0}
            className="px-2 py-0.5 rounded border border-neutral-700 hover:border-neutral-500 hover:text-neutral-300 disabled:opacity-30 disabled:cursor-not-allowed"
          >
            ▶ newer
          </button>
          <span className="text-neutral-600">
            {isLiveHead ? 'live head' : `page ${pageNum}`}
          </span>
          <button
            onClick={goOlder}
            disabled={rows.length === 0}
            className="px-2 py-0.5 rounded border border-neutral-700 hover:border-neutral-500 hover:text-neutral-300 disabled:opacity-30 disabled:cursor-not-allowed"
          >
            ◀ older
          </button>
        </div>
      )}
    </div>
  )
}
