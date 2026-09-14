import { useEffect, useState } from 'react'
import { getStatus } from '../api'
import type { Status } from '../api'

export function StatusBar() {
  const [s, setS] = useState<Status | null>(null)
  useEffect(() => {
    const f = () => getStatus().then(setS).catch(() => {})
    f()
    const id = setInterval(f, 3000)
    return () => clearInterval(id)
  }, [])
  if (!s) return <span className="text-xs text-neutral-500">connecting…</span>
  return (
    <div className="flex items-center gap-4 text-xs text-neutral-400">
      <span>sim <span className="text-neutral-200 font-mono">{s.sim_now?.slice(0, 16).replace('T', ' ')}</span></span>
      <span>txns <span className="text-neutral-200 font-mono">{s.n_txn.toLocaleString()}</span></span>
      <span>fraud <span className="text-neutral-200 font-mono">{s.n_fraud.toLocaleString()}</span></span>
      {s.model && (
        <span>model <span className="text-emerald-400 font-mono">AUC {s.model.auc.toFixed(3)}</span></span>
      )}
    </div>
  )
}
