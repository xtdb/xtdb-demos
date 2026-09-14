import { useEffect, useState } from 'react'
import { getTrainingDataset, postTrainingPreview } from '../api'
import type { HistoricalTrainingPreview, Selection, TrainingDataset } from '../api'
import { Sql } from '../components/Sql'

const clock = (iso: string) => new Date(iso).toISOString().slice(0, 16).replace('T', ' ')

export function TrainView({ selection }: { selection: Selection | null }) {
  const [data, setData] = useState<TrainingDataset | null>(null)
  const [showSampleSql, setShowSampleSql] = useState(false)
  const [showTrainingSql, setShowTrainingSql] = useState(false)
  const [error, setError] = useState('')
  const [training, setTraining] = useState(false)
  const [result, setResult] = useState<HistoricalTrainingPreview | null>(null)

  useEffect(() => {
    setData(null)
    setError('')
    setResult(null)
    if (!selection) return
    getTrainingDataset(selection).then(setData).catch((e: Error) => setError(e.message))
  }, [selection])

  if (!selection) {
    return <div className="text-neutral-500 text-sm border border-dashed border-neutral-800 rounded-lg p-8 text-center">Select a transaction to set the training dataset's knowledge cutoff.</div>
  }
  if (error) return <div className="text-red-400 text-sm">{error}</div>
  if (!data) return <div className="text-neutral-500 text-sm">building dataset…</div>

  const train = async () => {
    setTraining(true)
    setResult(null)
    setError('')
    try { setResult(await postTrainingPreview(data.knowledge_basis)) }
    catch (e) { setError((e as Error).message) }
    finally { setTraining(false) }
  }

  return (
    <div>
      <div className="flex items-start justify-between gap-3 mb-4">
        <div>
          <h3 className="text-xs uppercase tracking-wide text-neutral-500">training dataset</h3>
          <div className="text-sm text-neutral-300 mt-1">as known <span className="font-mono">{clock(data.knowledge_basis)}</span></div>
          <div className="text-xs text-neutral-500">settled outcomes through {clock(data.settled_before)}</div>
        </div>
        <button onClick={() => setShowSampleSql((v) => !v)} className="text-xs text-sky-400 hover:underline shrink-0">
          {showSampleSql ? 'hide sample SQL' : 'show sample SQL'}
        </button>
      </div>

      {showSampleSql && <Sql code={data.sample_sql} className="rounded border border-neutral-800 mb-4 max-h-72" />}

      <div className="mb-4 flex items-center gap-3">
        <button onClick={train} disabled={training}
          className="px-3 py-2 rounded-md bg-sky-600 hover:bg-sky-500 disabled:opacity-50 text-sm text-white">
          {training ? 'training…' : 'train at this point'}
        </button>
        <span className="text-xs text-neutral-500">preview only · live model unchanged</span>
      </div>
      {result && (
        <div className="mb-4 border border-neutral-800 rounded-lg p-3 text-sm">
          <div className="flex gap-5 items-center">
            <span><span className="font-mono text-neutral-100">{result.row_count.toLocaleString()}</span> rows</span>
            <span>AUC <span className="font-mono text-emerald-400">{result.auc?.toFixed(4) ?? '—'}</span></span>
            <span><span className="font-mono text-neutral-100">{(result.elapsed_ms / 1000).toFixed(1)}s</span></span>
            <span className="text-neutral-500">live model unchanged</span>
            <button onClick={() => setShowTrainingSql((v) => !v)} className="ml-auto text-xs text-sky-400 hover:underline">
              {showTrainingSql ? 'hide training SQL' : 'show training SQL'}
            </button>
          </div>
          {showTrainingSql && <Sql code={result.training_sql} className="rounded border border-neutral-800 mt-3 max-h-72" />}
        </div>
      )}

      <div className="flex gap-5 text-sm mb-3">
        <span><span className="font-mono text-neutral-100">{data.total_rows.toLocaleString()}</span> rows</span>
        <span><span className="font-mono text-neutral-100">{data.total_fraud.toLocaleString()}</span> fraud</span>
      </div>

      <div className="text-[11px] text-neutral-400 mb-3">
        <span className="text-neutral-200">Prior confirmed fraud:</span> at the transaction's decision time versus by the selected knowledge time.
      </div>
      <div className="text-[11px] uppercase tracking-wide text-neutral-500 mb-1">
        newest {data.displayed_rows} rows · {data.differing} changed by later chargebacks
      </div>
      <div className="border border-neutral-800 rounded-lg overflow-hidden">
        <table className="w-full text-xs">
          <thead className="bg-neutral-900 text-neutral-500 uppercase">
            <tr>
              <th className="text-left px-2 py-2">time</th>
              <th className="text-left px-2 py-2">account</th>
              <th className="text-right px-2 py-2">amount</th>
              <th className="text-left px-2 py-2">outcome</th>
              <th className="text-right px-2 py-2">at decision</th>
              <th className="text-right px-2 py-2">by selected time</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {data.rows.map((r) => {
              const changed = r.as_known_then !== r.with_hindsight
              return (
                <tr key={r.txn_id} className={`border-t border-neutral-900 ${changed ? 'bg-amber-950/20' : ''}`}>
                  <td className="px-2 py-1.5 text-neutral-400">{clock(r.txn_ts).slice(5)}</td>
                  <td className="px-2 py-1.5">{r.account_id}</td>
                  <td className="px-2 py-1.5 text-right">£{r.amount.toFixed(0)}</td>
                  <td className={`px-2 py-1.5 ${r.label ? 'text-red-400' : 'text-neutral-500'}`}>{r.label ? 'fraud' : 'legit'}</td>
                  <td className="px-2 py-1.5 text-right text-emerald-400">{r.as_known_then}</td>
                  <td className={`px-2 py-1.5 text-right ${changed ? 'text-amber-300' : 'text-neutral-500'}`}>{r.with_hindsight}{changed ? ' ⚠' : ''}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
