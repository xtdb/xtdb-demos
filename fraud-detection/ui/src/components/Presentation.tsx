import { useEffect, useState, type ReactNode } from 'react'

type Slide = { eyebrow?: string; title: string; body: ReactNode }

// Point-in-time note (used across slides): bullet lists read as slide points.
const Points = ({ items }: { items: ReactNode[] }) => (
  <ul className="space-y-3">
    {items.map((it, k) => (
      <li key={k} className="flex gap-3">
        <span className="text-sky-400 mt-1 shrink-0">·</span>
        <span>{it}</span>
      </li>
    ))}
  </ul>
)

const SLIDES: Slide[] = [
  {
    eyebrow: 'XTDB · feature store',
    title: 'Fraud detection with no second store',
    body: (
      <div className="space-y-6">
        <p className="text-neutral-300">
          A feature store's hard problem isn't <em>storing</em> features. It's retrieving them
          <strong className="text-neutral-100"> as they were known at a past instant</strong>. Get it wrong and the
          future leaks into training: great offline, rotten in production.
        </p>
        <p className="text-neutral-400">
          The usual answer is two stores, offline for training and online for serving, plus a great deal of effort to
          keep them in sync. Here it's <strong className="text-neutral-100">one bitemporal store, plain SQL</strong>.
        </p>
        <Points
          items={[
            <>A <strong className="text-neutral-100">feature is a query</strong>: computed on read, point-in-time correct by construction.</>,
            <>One store for <strong className="text-neutral-100">training and serving</strong>. Different query shapes over the same history.</>,
            <>Late corrections place themselves in <strong className="text-neutral-100">valid-time</strong>; <strong className="text-neutral-100">system-time</strong> preserves the data basis for replay.</>,
          ]}
        />
      </div>
    ),
  },
  {
    eyebrow: 'serving',
    title: 'A feature is a query',
    body: (
      <div className="space-y-6">
        <p className="text-neutral-300">
          There's no feature pipeline to run ahead of time, no online store to look up. The <strong className="text-neutral-100">features
          are computed on read</strong>, from the same transaction history, by the same feature definitions that built
          the training set.
        </p>
        <Points
          items={[
            <>Every <span className="font-mono text-neutral-300">p(fraud)</span> starts with a query over the account's history <strong className="text-neutral-100">as it stood at the instant of the transaction</strong>.</>,
            <>Trailing-window features (24h count, 30d z-score, 90d prior fraud) are just aggregates with a valid-time bound.</>,
            <>Click a row and open the <span className="font-mono text-neutral-300">Query</span> tab to read the exact SQL that supplied its features.</>,
          ]}
        />
      </div>
    ),
  },
  {
    eyebrow: 'correction',
    title: 'A correction is just an insert',
    body: (
      <div className="space-y-6">
        <p className="text-neutral-300">
          A chargeback confirms a transaction was fraud, often weeks after it happened. In most feature stores that
          means a backfill job to recompute the affected features. Here the correction is{' '}
          <strong className="text-neutral-100">a single insert</strong> into the label table, placed in valid-time at
          the moment the fraud occurred. Because features are queries, the account's history simply reads differently
          from then on.
        </p>
        <Points
          items={[
            <>Rows on an <span className="text-amber-400">⚠</span> account have an unconfirmed chargeback in flight. Click one, open <span className="font-mono text-neutral-300">Correction</span>, and confirm it.</>,
            <>Run <span className="font-mono text-neutral-300">p(fraud)</span> again over the account's history: confirming raises <span className="font-mono text-neutral-300">prior_confirmed_fraud</span>, and the <strong className="text-neutral-100">same model</strong> re-scores its next borderline transaction upward. No retrain, no feature backfill.</>,
            <>The fact lands in <strong className="text-neutral-100">valid-time</strong> (when the fraud happened) but is recorded at a new <strong className="text-neutral-100">system-time</strong> (when we learned it). Both axes are kept.</>,
          ]}
        />
      </div>
    ),
  },
  {
    eyebrow: 'reproducibility',
    title: 'Reproduce a decision, exactly',
    body: (
      <div className="space-y-6">
        <p className="text-neutral-300">
          Reproducing a decision means restoring both inputs: <strong className="text-neutral-100">the data basis and
          the model version</strong>. System-time makes the data basis addressable; the model registry makes the scoring
          function addressable.
        </p>
        <Points
          items={[
            <>In <span className="font-mono text-neutral-300">Correction</span>, the <span className="text-emerald-400">reproduced</span> score rewinds <span className="font-mono text-neutral-300">SYSTEM_TIME AS OF</span> and pins the recorded model version.</>,
            <>The demo replays the decision shown before confirmation: same temporal data basis, same immutable model artifact, same score.</>,
            <>In production, recording those two identifiers with each decision turns audit into replay rather than archaeology.</>,
          ]}
        />
      </div>
    ),
  },
  {
    eyebrow: 'training',
    title: 'Training is an as-of join',
    body: (
      <div className="space-y-6">
        <p className="text-neutral-300">
          To train, attach each past outcome to the features available at its own decision time. Apply that temporal
          discipline across every historical row and the future cannot leak into the past.
        </p>
        <div className="border-l-2 border-sky-500 pl-4 text-neutral-100 italic">
          A model must be trained on exactly the information it will have at the moment it predicts, no more.
        </div>
        <Points
          items={[
            <>Serving is a point lookup; training is one pass over the whole log. Two query shapes over one bitemporal history, rather than two stores to keep in step.</>,
            <>The trailing window is bounded by <strong className="text-neutral-100">event time</strong>; each row counts only facts known by its own <strong className="text-neutral-100">system-time</strong>.</>,
            <>In <span className="font-mono text-neutral-300">Training</span>, compare <strong className="text-neutral-100">as known then</strong> with <strong className="text-neutral-100">with hindsight</strong>. The latter includes chargebacks that had not settled when the decision was made.</>,
          ]}
        />
      </div>
    ),
  },
]

export function Presentation() {
  const [i, setI] = useState(0)
  const s = SLIDES[i]
  const last = SLIDES.length - 1

  // Arrow keys page the deck while it's on screen (this component only mounts when
  // the presentation is showing). Ignore keys typed into form fields.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t && /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName)) return
      if (e.key === 'ArrowRight') setI((n) => Math.min(SLIDES.length - 1, n + 1))
      else if (e.key === 'ArrowLeft') setI((n) => Math.max(0, n - 1))
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  const navBtn =
    'px-2 leading-none text-neutral-400 hover:text-neutral-100 disabled:opacity-25 disabled:hover:text-neutral-400'
  return (
    <div className="relative h-full flex flex-col overflow-hidden">
      <div className="flex-1 min-h-0 overflow-auto px-10 py-8 flex flex-col justify-center">
        <div className="w-full max-w-2xl">
          {s.eyebrow && (
            <div className="text-[11px] uppercase tracking-[0.2em] text-sky-400 mb-4">{s.eyebrow}</div>
          )}
          <h1 className="text-3xl font-semibold leading-tight tracking-tight text-neutral-50 mb-6">{s.title}</h1>
          <div className="text-[15px] leading-relaxed">{s.body}</div>
        </div>
      </div>
      <div className="absolute bottom-3 right-3 flex items-center gap-0.5 rounded-full border border-neutral-800 bg-neutral-900/70 backdrop-blur px-1 py-1 text-sm">
        <button onClick={() => setI((n) => Math.max(0, n - 1))} disabled={i === 0} className={navBtn}>‹</button>
        <span className="px-1 text-xs text-neutral-500 font-mono tabular-nums">{i + 1}/{SLIDES.length}</span>
        <button onClick={() => setI((n) => Math.min(last, n + 1))} disabled={i === last} className={navBtn}>›</button>
      </div>
    </div>
  )
}
