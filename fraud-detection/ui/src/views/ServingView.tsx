import { useEffect, useState } from 'react'
import { getAccounts, postScore } from '../api'
import type { ScoreResp } from '../api'
import { ScoreDetail } from '../components/ScoreDetail'

const COUNTRIES = ['GB', 'US', 'FR', 'DE', 'IE', 'NG', 'RU', 'CN', 'BR']

export function ServingView() {
  const [accts, setAccts] = useState<string[]>([])
  const [acc, setAcc] = useState('')
  const [amount, setAmount] = useState(600)
  const [country, setCountry] = useState('RU')
  const [detail, setDetail] = useState<ScoreResp | null>(null)

  useEffect(() => {
    getAccounts().then((a) => { setAccts(a); setAcc(a[0] ?? '') })
  }, [])

  const score = () => {
    setDetail(null)
    postScore({ account_id: acc, amount, country }).then(setDetail)
  }

  const field = 'mt-1 bg-neutral-900 border border-neutral-800 rounded px-2 py-1.5 text-sm'
  return (
    <div>
      <p className="text-neutral-400 text-sm mb-4">
        Compose a transaction; the same feature queries run at <code className="text-neutral-200">T = now</code>.
        No online store — the serving vector comes from the same queries as training.
      </p>
      <div className="flex items-end gap-3 mb-6">
        <label className="text-sm text-neutral-400">account
          <br /><select value={acc} onChange={(e) => setAcc(e.target.value)} className={`${field} font-mono`}>
            {accts.map((a) => <option key={a}>{a}</option>)}
          </select>
        </label>
        <label className="text-sm text-neutral-400">amount £
          <br /><input type="number" value={amount} onChange={(e) => setAmount(+e.target.value)} className={`${field} w-28`} />
        </label>
        <label className="text-sm text-neutral-400">country
          <br /><select value={country} onChange={(e) => setCountry(e.target.value)} className={field}>
            {COUNTRIES.map((c) => <option key={c}>{c}</option>)}
          </select>
        </label>
        <button onClick={score} className="px-4 py-2 rounded-md bg-sky-600 hover:bg-sky-500 text-white text-sm">
          score at T = now
        </button>
      </div>
      {detail && <ScoreDetail data={detail} />}
    </div>
  )
}
