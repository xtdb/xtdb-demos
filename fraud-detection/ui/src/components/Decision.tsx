import type { Decision as D } from '../api'

export function Decision({ decision }: { decision: D }) {
  if (!decision) return <div className="text-neutral-500 text-sm">no model yet</div>
  const sum = decision.contributions.reduce((a, c) => a + c.value, 0)
  const logit = decision.intercept + sum
  const rows = [{ name: 'intercept', value: decision.intercept }, ...decision.contributions]
  const max = Math.max(...rows.map((r) => Math.abs(r.value)), 0.001)
  return (
    <div>
      <div className="flex items-baseline gap-3 mb-4">
        <span className={`text-2xl font-bold ${decision.verdict === 'fraud' ? 'text-red-400' : 'text-emerald-400'}`}>
          {decision.verdict.toUpperCase()}
        </span>
        <span className="text-neutral-400 font-mono">p = {decision.prob.toFixed(3)}</span>
      </div>
      <div className="space-y-1.5">
        {rows.map((r) => (
          <div key={r.name} className="flex items-center gap-2 text-xs">
            <span className={`w-44 text-right font-mono truncate ${r.name === 'intercept' ? 'text-neutral-500 italic' : 'text-neutral-400'}`}>
              {r.name}
            </span>
            <div className="flex-1 relative h-4 bg-neutral-900 rounded">
              <div className="absolute left-1/2 top-0 bottom-0 w-px bg-neutral-700" />
              <div
                className={`absolute top-0 bottom-0 rounded ${r.value >= 0 ? 'bg-red-500/60' : 'bg-sky-500/60'}`}
                style={
                  r.value >= 0
                    ? { left: '50%', width: `${(Math.abs(r.value) / max) * 50}%` }
                    : { right: '50%', width: `${(Math.abs(r.value) / max) * 50}%` }
                }
              />
            </div>
            <span className="w-12 font-mono text-neutral-300 text-right">
              {r.value >= 0 ? '+' : ''}
              {r.value.toFixed(2)}
            </span>
          </div>
        ))}
      </div>
      <div className="mt-3 pt-2 border-t border-neutral-800 flex items-center justify-between text-xs font-mono text-neutral-400">
        <span>intercept + Σ contributions</span>
        <span>= {logit.toFixed(2)} → p {decision.prob.toFixed(3)}</span>
      </div>
      <p className="mt-2 text-[11px] text-neutral-500 leading-relaxed">
        Contributions are coefficient × <em>standardised</em> feature value (log-odds), so they differ from the
        raw values on the left — a feature at 0 still contributes when 0 is below the population mean.
      </p>
    </div>
  )
}
