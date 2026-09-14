import { useEffect, useState } from 'react'
import { postScore } from '../api'
import type { ScoreResp, Selection } from '../api'
import { ScoreDetail } from '../components/ScoreDetail'

// The queries behind one row's score. Scored at the row's own valid instant, so the
// SQL shown is exactly what ran (or would run) to produce that transaction's p(fraud).
export function QueryView({ seed }: { seed: Selection | null }) {
  const [detail, setDetail] = useState<ScoreResp | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!seed) { setDetail(null); return }
    let on = true
    setLoading(true)
    postScore({ account_id: seed.account_id, amount: seed.amount, country: seed.country, t: seed.valid_time })
      .then((d) => { if (on) setDetail(d) })
      .catch(() => { if (on) setDetail(null) })
      .finally(() => { if (on) setLoading(false) })
    return () => { on = false }
  }, [seed?.txn_id, seed?.valid_time])

  if (!seed) {
    return <p className="text-sm text-neutral-500">Click a transaction to see the queries that produced its score.</p>
  }

  return (
    <div className="space-y-4">
      <div className="text-sm text-neutral-400">
        <span className="font-mono text-neutral-200">{seed.account_id}</span> · £{seed.amount.toFixed(0)} · {seed.country}
        <span className="text-neutral-600"> · at </span>
        <span className="font-mono text-neutral-300">{seed.valid_time.slice(0, 16).replace('T', ' ')}</span>
      </div>
      <p className="text-[11px] text-neutral-500">
        Each feature is a query over this account's history as it stood at the transaction's instant.
        Toggle serving vs training: the same definition, one as a point lookup per account, one as a single pass over every transaction.
      </p>
      {loading && !detail && <p className="text-sm text-neutral-500 animate-pulse">running the feature queries…</p>}
      {detail && <ScoreDetail data={detail} vertical />}
    </div>
  )
}
