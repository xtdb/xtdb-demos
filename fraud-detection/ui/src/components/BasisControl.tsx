import { useEffect, useState } from 'react'
import { getBounds } from '../api'
import type { Bounds, Basis } from '../api'

const ms = (iso: string) => new Date(iso).getTime()
const iso = (m: number) => new Date(m).toISOString()
const fmt = (m: number) => new Date(m).toISOString().slice(0, 16).replace('T', ' ')

type Cursor = { pinned: boolean; value: number }

function CursorRow({
  label, hint, axis, min, max, cursor, set,
}: {
  label: string; hint: string; axis: string
  min: number; max: number; cursor: Cursor
  set: (c: Cursor) => void
}) {
  const live = !cursor.pinned
  return (
    <div className="flex items-center gap-3 py-1.5">
      <div className="w-28 shrink-0">
        <div className="text-xs font-medium text-neutral-200">{label}</div>
        <div className="text-[10px] text-neutral-500">{hint}</div>
      </div>
      <button
        onClick={() => set({ pinned: live, value: live ? max : cursor.value })}
        className={`text-[11px] px-2 py-1 rounded-md border shrink-0 w-16 ${
          live ? 'border-emerald-800 bg-emerald-950/60 text-emerald-300'
               : 'border-amber-800 bg-amber-950/60 text-amber-300'}`}
      >
        {live ? '● live' : '❚❚ pinned'}
      </button>
      <input
        type="range" min={min} max={max} value={cursor.value} disabled={live}
        onChange={(e) => set({ pinned: true, value: +e.target.value })}
        className="flex-1 accent-sky-500 disabled:opacity-40"
      />
      <div className="w-40 shrink-0 text-right">
        <div className="font-mono text-xs text-neutral-200">{live ? 'now' : fmt(cursor.value)}</div>
        <div className="text-[10px] text-neutral-500">{axis}</div>
      </div>
    </div>
  )
}

export function BasisControl({ onChange }: { onChange: (b: Basis) => void }) {
  const [b, setB] = useState<Bounds | null>(null)
  const [vt, setVt] = useState<Cursor>({ pinned: false, value: 0 })
  const [st, setSt] = useState<Cursor>({ pinned: false, value: 0 })

  useEffect(() => {
    getBounds().then((bb) => {
      setB(bb)
      setVt({ pinned: false, value: ms(bb.valid_now) })
      setSt({ pinned: false, value: ms(bb.system_now) })
    })
  }, [])

  useEffect(() => {
    onChange({
      valid_time: vt.pinned ? iso(vt.value) : null,
      system_time: st.pinned ? iso(st.value) : null,
    })
  }, [vt, st, onChange])

  if (!b) return <div className="text-neutral-500 text-sm p-3">loading basis…</div>

  const anyPinned = vt.pinned || st.pinned
  return (
    <div className="border border-neutral-800 rounded-lg p-3 bg-neutral-950">
      <div className="flex items-center justify-between mb-1">
        <h2 className="text-xs uppercase tracking-wide text-neutral-500">bitemporal basis</h2>
        {anyPinned ? (
          <span className="text-[11px] text-amber-300">
            {vt.pinned && 'viewing the past'}{vt.pinned && st.pinned && ' · '}
            {st.pinned && 'through past knowledge'}
          </span>
        ) : (
          <span className="text-[11px] text-emerald-400">tracking live</span>
        )}
      </div>
      <CursorRow
        label="valid-time" hint="what was true" axis="in the simulated world"
        min={ms(b.valid_min)} max={ms(b.valid_now)} cursor={vt} set={setVt}
      />
      <CursorRow
        label="system-time" hint="what we knew" axis="wall-clock knowledge"
        min={ms(b.system_min)} max={ms(b.system_now)} cursor={st} set={setSt}
      />
    </div>
  )
}
