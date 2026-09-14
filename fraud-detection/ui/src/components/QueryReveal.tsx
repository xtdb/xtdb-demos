import { useState } from 'react'
import type { Feature, Shape } from '../api'
import { Sql } from './Sql'

const fmt = (v: number) => (Number.isInteger(v) ? String(v) : v.toFixed(3))

const SHAPE_TAG: Record<Shape, string> = {
  serve: 'point lookup · one account',
  train: 'one pass · every transaction',
}

export function QueryReveal({ feature, shape }: { feature: Feature; shape: Shape }) {
  const [open, setOpen] = useState(false)
  const sql = shape === 'train' ? feature.sql_train : feature.sql_serve
  return (
    <div className="border border-neutral-800 rounded-lg overflow-hidden">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between gap-3 px-3 py-2 hover:bg-neutral-900 text-left"
      >
        <span className="flex items-center gap-2 min-w-0">
          <span className="font-mono text-sm text-neutral-200">{feature.name}</span>
          <span className="text-[10px] px-2 py-0.5 rounded-full bg-sky-950 text-sky-300 border border-sky-900 whitespace-nowrap">
            {feature.badge}
          </span>
        </span>
        <span className="flex items-center gap-3 shrink-0">
          <span className="font-mono text-sm text-white">{fmt(feature.value)}</span>
          <span className="text-neutral-500 text-xs">{open ? 'hide' : 'query ▸'}</span>
        </span>
      </button>
      {open && sql && (
        <div className="border-t border-neutral-800">
          <div className="px-3 pt-2 text-[10px] uppercase tracking-wide text-neutral-500">{SHAPE_TAG[shape]}</div>
          <Sql code={sql} />
        </div>
      )}
    </div>
  )
}
