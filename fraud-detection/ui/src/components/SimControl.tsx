import { useEffect, useState } from 'react'
import { getSim, startSim, stopSim } from '../api'
import type { SimStatus } from '../api'

export function SimControl() {
  const [s, setS] = useState<SimStatus | null>(null)

  useEffect(() => {
    const tick = () => getSim().then(setS).catch(() => {})
    tick()
    const t = window.setInterval(tick, 2000)
    return () => window.clearInterval(t)
  }, [])

  const running = s?.running ?? false

  return (
    <div className="flex items-center gap-2">
      <button
        onClick={async () => setS(running ? await stopSim() : await startSim())}
        className={`text-xs px-2.5 py-1 rounded-md font-medium ${
          running ? 'bg-red-600/80 hover:bg-red-500 text-white' : 'bg-emerald-600/80 hover:bg-emerald-500 text-white'}`}
      >
        {running ? '❚❚ sim' : '▶ sim'}
      </button>
    </div>
  )
}
