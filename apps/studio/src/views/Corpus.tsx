import { useEffect, useMemo, useState } from 'react'
import { api, type CorpusItem } from '../api'

/* The labelled corpus, published.
 *
 * A benchmark whose test set is private is a marketing number. Everything the headline claim
 * rests on is here, item by item, with the score the endpointer assigns and the rule that
 * produced it — including the items it gets wrong.
 */
export function Corpus() {
  const [items, setItems] = useState<CorpusItem[]>([])
  const [filter, setFilter] = useState<'all' | 'complete' | 'incomplete' | 'disagree'>('all')

  useEffect(() => {
    api.corpus().then((c) => setItems(c.items)).catch(() => undefined)
  }, [])

  const shown = useMemo(
    () =>
      items.filter((item) => {
        const agrees = item.completion_score >= 0.5 === item.complete
        if (filter === 'complete') return item.complete
        if (filter === 'incomplete') return !item.complete
        if (filter === 'disagree') return !agrees
        return true
      }),
    [items, filter],
  )

  const disagreements = items.filter((i) => (i.completion_score >= 0.5) !== i.complete).length

  return (
    <div className="page">
      <div className="page-head">
        <div className="eyebrow">The corpus</div>
        <h1>The test set, in full</h1>
        <p>
          Hand-written from the failure cases that actually occur on a phone line: numbers read
          aloud, dangling prepositions, fillers, short confirmations, thinking pauses. It is
          small and it says so — the claim is that the methodology is right and the cases are the
          real ones, not that {items.length} items settle anything.
        </p>
      </div>

      <div className="grid grid-4" style={{ marginBottom: 18 }}>
        <Stat label="Labelled turns" value={String(items.length)} sub="every one published"
              accent="var(--latency)" />
        <Stat label="Complete" value={String(items.filter((i) => i.complete).length)}
              sub="the agent should respond fast" accent="var(--good)" />
        <Stat label="Unfinished" value={String(items.filter((i) => !i.complete).length)}
              sub="the agent must hold" accent="var(--cost)" />
        <Stat label="Scorer disagrees" value={String(disagreements)}
              sub="shown rather than hidden"
              accent={disagreements ? 'var(--bad)' : 'var(--good)'} />
      </div>

      <div className="panel">
        <div className="control-row" style={{ marginBottom: 16 }}>
          {(['all', 'complete', 'incomplete', 'disagree'] as const).map((key) => (
            <button key={key} className="ghost" data-active={filter === key} onClick={() => setFilter(key)}>
              {key === 'disagree' ? 'where the scorer disagrees' : key}
            </button>
          ))}
        </div>
        <div className="scroll-x">
          <table>
            <thead>
              <tr>
                <th>id</th>
                <th>transcript at the pause</th>
                <th>ground truth</th>
                <th style={{ textAlign: 'right' }}>score</th>
                <th>why</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((item) => {
                const agrees = item.completion_score >= 0.5 === item.complete
                return (
                  <tr key={item.id}>
                    <td className="num" style={{ color: 'var(--text-faint)' }}>{item.id}</td>
                    <td>&ldquo;{item.transcript}&rdquo;</td>
                    <td>
                      <span className="chip" data-tone={item.complete ? 'good' : 'cost'}>
                        {item.complete ? 'finished' : 'still talking'}
                      </span>
                    </td>
                    <td className="n" style={{ color: agrees ? 'var(--text)' : 'var(--bad)' }}>
                      {item.completion_score.toFixed(2)}
                    </td>
                    <td style={{ color: 'var(--text-faint)', fontSize: 12.5 }}>{item.reason}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

function Stat({ label, value, sub, accent }: { label: string; value: string; sub: string; accent: string }) {
  return (
    <div className="stat" style={{ ['--accent' as string]: accent }}>
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={{ color: accent }}>{value}</div>
      <div className="stat-sub">{sub}</div>
    </div>
  )
}
