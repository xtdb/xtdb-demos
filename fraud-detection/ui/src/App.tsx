import { useCallback, useState } from 'react'
import { StatusBar } from './components/StatusBar'
import { SimControl } from './components/SimControl'
import { Presentation } from './components/Presentation'
import { DataView } from './components/DataView'
import { AuditView } from './views/AuditView'
import { TrainView } from './views/TrainView'
import { QueryView } from './views/QueryView'
import type { Basis, Selection } from './api'

type LensId = 'query' | 'correction' | 'training'
const LENSES: { id: LensId; label: string }[] = [
  { id: 'query', label: 'Query' },
  { id: 'correction', label: 'Correction' },
  { id: 'training', label: 'Training' },
]

// The exhibit stays live (per-lens basis lives inside the lenses).
const LIVE: Basis = { valid_time: null, system_time: null }

export default function App() {
  const [selection, setSelection] = useState<Selection | null>(null)
  const [lens, setLens] = useState<LensId>('query')
  const [showPres, setShowPres] = useState(false)
  // bumped when a correction is confirmed, so the main table re-scores that account
  const [correction, setCorrection] = useState<{ account_id: string; nonce: number } | null>(null)

  // clicking a live row just updates the selection; the active lens is sticky, so the
  // panel you picked stays put as you click through transactions.
  const onSelect = useCallback((s: Selection) => setSelection(s), [])

  return (
    <div className="h-screen flex flex-col">
      <header className="border-b border-neutral-800 flex items-center justify-between gap-6 shrink-0 h-14 pr-6 overflow-hidden">
        <div className="flex items-center gap-4 h-full">
          {/* angled white block with the XTDB logo — echoes the docs nav */}
          <div className="h-full bg-white flex items-center pl-6 pr-9 -ml-4 -skew-x-12">
            <img src="/xtdb-logo.svg" alt="XTDB" className="h-8 skew-x-12" />
          </div>
          <span className="font-semibold text-lg tracking-tight text-neutral-100">Fraud Feature Store</span>
        </div>
        <div className="flex items-center gap-4">
          <button
            onClick={() => setShowPres((v) => !v)}
            className={`text-xs px-2.5 py-1 rounded-md font-medium text-white ${
              showPres ? 'bg-sky-600/80 hover:bg-sky-500' : 'bg-neutral-700/80 hover:bg-neutral-600'}`}
          >
            {showPres ? '❚❚ present' : '▶ present'}
          </button>
          <SimControl />
          <StatusBar />
        </div>
      </header>

      <main className="flex-1 min-h-0 p-6">
        <div className={`grid grid-cols-1 gap-4 h-full ${
          showPres ? 'lg:grid-cols-[1.2fr_1.4fr_1fr]' : 'lg:grid-cols-[1.4fr_1fr] max-w-7xl mx-auto'}`}>
          {/* the presentation pane (guided narration over the live exhibit) */}
          {showPres && (
            <section className="min-h-0 min-w-0 h-full">
              <Presentation />
            </section>
          )}

          {/* the exhibit: one live dataset */}
          <section className="flex flex-col min-h-0 min-w-0">
            <DataView basis={LIVE} onSelect={onSelect} selectedId={selection?.txn_id} correction={correction} />
          </section>

          {/* the lens dock */}
          <aside className="flex flex-col min-h-0 min-w-0 border border-neutral-800 rounded-lg">
            <div className="flex gap-1 p-2 border-b border-neutral-800 shrink-0">
              {LENSES.map((l) => (
                <button
                  key={l.id}
                  onClick={() => setLens(l.id)}
                  className={`px-3 py-1.5 rounded-md text-sm ${lens === l.id ? 'bg-neutral-800 text-white' : 'text-neutral-400 hover:text-neutral-200'}`}
                >
                  {l.label}
                </button>
              ))}
            </div>
            <div className="flex-1 min-h-0 overflow-auto p-4">
              {lens === 'query' && <QueryView seed={selection} />}
              {lens === 'correction' && (
                <AuditView
                  seed={selection}
                  onCorrected={(account_id) => setCorrection({ account_id, nonce: Date.now() })}
                />
              )}
              {lens === 'training' && <TrainView selection={selection} />}
            </div>
          </aside>
        </div>
      </main>
    </div>
  )
}
