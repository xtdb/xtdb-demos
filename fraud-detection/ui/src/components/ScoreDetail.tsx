import { useState } from 'react'
import type { ScoreResp, Shape } from '../api'
import { QueryReveal } from './QueryReveal'
import { Decision } from './Decision'
import { Sql } from './Sql'

const SHAPES: { id: Shape; label: string; hint: string }[] = [
  { id: 'serve', label: 'Serving', hint: 'online — one account, point lookup as-of T' },
  { id: 'train', label: 'Training', hint: 'batch, one pass over every transaction' },
]

function ShapeToggle({ shape, onChange }: { shape: Shape; onChange: (s: Shape) => void }) {
  return (
    <div className="inline-flex rounded-md border border-neutral-800 overflow-hidden text-xs">
      {SHAPES.map((s) => (
        <button
          key={s.id}
          onClick={() => onChange(s.id)}
          className={`px-2.5 py-1 ${shape === s.id ? 'bg-neutral-800 text-white' : 'text-neutral-400 hover:text-neutral-200'}`}
        >
          {s.label}
        </button>
      ))}
    </div>
  )
}

function FullQuery({ data, shape }: { data: ScoreResp; shape: Shape }) {
  const [open, setOpen] = useState(false)
  const sql = shape === 'train' ? `${data.window_defs}\n\n${data.train_sql}` : data.serve_sql
  const label = shape === 'train' ? 'the whole extraction query (all anchors)' : 'the one combined serving query'
  return (
    <div className="mt-2">
      <button onClick={() => setOpen((o) => !o)} className="text-xs text-sky-400 hover:underline">
        {open ? 'hide' : 'show'} {label}
      </button>
      {open && <Sql code={sql} className="mt-2 rounded border border-neutral-800" />}
    </div>
  )
}

export function ScoreDetail({ data, vertical = false }: { data: ScoreResp; vertical?: boolean }) {
  const [shape, setShape] = useState<Shape>('serve')
  const hint = SHAPES.find((s) => s.id === shape)!.hint
  return (
    <div className={vertical ? 'space-y-6' : 'grid grid-cols-1 lg:grid-cols-2 gap-6'}>
      <div>
        <div className="flex items-center justify-between gap-3 mb-2">
          <h3 className="text-xs uppercase tracking-wide text-neutral-500">
            features · each one is a query
          </h3>
          <ShapeToggle shape={shape} onChange={setShape} />
        </div>
        <p className="text-[11px] text-neutral-500 mb-2">
          same feature, same store — {hint}. The value shown is the live serving computation.
        </p>
        <div className="space-y-2">
          {data.features.map((f) => (
            <QueryReveal key={f.name} feature={f} shape={shape} />
          ))}
        </div>
        <FullQuery data={data} shape={shape} />
      </div>
      <div>
        <h3 className="text-xs uppercase tracking-wide text-neutral-500 mb-2">decision</h3>
        <Decision decision={data.decision} />
        <p className="mt-3 text-[11px] text-neutral-500">
          features computed on-read + scored in{' '}
          <span className="font-mono text-neutral-300">{data.elapsed_ms.toFixed(0)} ms</span>
          {' '}— no online store
        </p>
      </div>
    </div>
  )
}
