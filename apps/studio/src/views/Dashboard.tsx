import { useEffect, useState } from 'react'
import type { ViewProps } from '../App'
import { api, type CallRow, type Overview } from '../api'
import { Icon } from '../components/Icon'

/* What an operator looks at first.
 *
 * Every number here answers a question someone actually asks about a contact centre: how much is
 * the agent handling on its own, how often does it hand over, how fast does it reply, and what
 * happened on the last few calls. A dashboard of things that are merely countable is how you get
 * a screen nobody opens twice.
 */
export function Dashboard({ navigate, ready }: ViewProps) {
  const [data, setData] = useState<Overview | null>(null)
  const [recent, setRecent] = useState<CallRow[]>([])

  useEffect(() => {
    const load = async () => {
      try {
        const [overview, { calls }] = await Promise.all([api.overview(), api.calls({ limit: 8 })])
        setData(overview)
        setRecent(calls)
      } catch {
        /* the sidebar reports the gateway being unreachable */
      }
    }
    void load()
    const timer = window.setInterval(load, 8000)
    return () => window.clearInterval(timer)
  }, [])

  if (!data) {
    return (
      <div className="page">
        <div className="grid g4">
          {Array.from({ length: 4 }, (_, i) => <div key={i} className="skeleton" style={{ height: 88 }} />)}
        </div>
      </div>
    )
  }

  const answered = data.calls || 0

  return (
    <div className="page">
      <div className="head">
        <h1>Dashboard</h1>
        <p>Live state of the contact centre, refreshed every eight seconds.</p>
      </div>

      <div className="grid g4" style={{ marginBottom: 14 }}>
        <Metric
          k="Calls handled"
          v={String(answered)}
          s={data.live ? `${data.live} in progress now` : 'none in progress'}
          tone="var(--accent)"
        />
        <Metric
          k="Handled without a human"
          v={`${Math.round(data.containment * 100)}%`}
          s={`${data.escalated} passed to a person`}
          tone={data.containment >= 0.7 ? 'var(--good)' : 'var(--cost)'}
        />
        <Metric
          k="Median reply"
          v={`${Math.round(data.median_turn_ms)}ms`}
          s={`p90 ${Math.round(data.p90_turn_ms)}ms`}
          tone="var(--agent)"
        />
        <Metric
          k="Knowledge"
          v={String(data.documents)}
          s={`${data.agents} agent${data.agents === 1 ? '' : 's'} configured`}
          tone="var(--info)"
        />
      </div>

      <div className="grid g2" style={{ alignItems: 'start' }}>
        <div className="card">
          <h2 className="card-h">Calls per day</h2>
          <p className="card-sub">Filled portion is what the agent resolved on its own.</p>
          {data.by_day.length === 0 ? (
            <div className="empty" style={{ padding: 30 }}>No calls yet. Start one from <b>Live call</b>.</div>
          ) : (
            <DayChart days={data.by_day} />
          )}
        </div>

        <div className="card">
          <h2 className="card-h">How callers sounded</h2>
          <p className="card-sub">
            Read from the caller's own words, not the agent's — the agent is unfailingly polite by
            construction, so including it would score every call positive.
          </p>
          <Sentiment counts={data.sentiment} total={answered} />

          <div style={{ marginTop: 18 }}>
            <div className="stat-line"><span>Average call length</span><span>{(data.avg_duration_ms / 1000).toFixed(1)}s</span></div>
            <div className="stat-line"><span>Handed to a person</span><span>{Math.round(data.escalation_rate * 100)}%</span></div>
            <div className="stat-line"><span>Abandoned</span><span>{data.abandoned}</span></div>
          </div>
        </div>
      </div>

      <div className="card card-pad-0" style={{ marginTop: 14 }}>
        <div className="row-between" style={{ padding: '14px 16px 12px' }}>
          <div>
            <h2 className="card-h">Recent calls</h2>
            <p className="card-sub" style={{ margin: 0 }}>Click one to read the transcript and its timings.</p>
          </div>
          <button className="btn btn-ghost btn-sm" onClick={() => navigate({ view: 'calls' })}>
            All calls <Icon name="chevron" size={13} />
          </button>
        </div>

        {recent.length === 0 ? (
          <div className="empty">
            <h3>No calls yet</h3>
            <button className="btn btn-primary" style={{ marginTop: 10 }} onClick={() => navigate({ view: 'live' })} disabled={!ready}>
              <Icon name="phone" /> Make the first call
            </button>
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table>
              <thead>
                <tr>
                  <th>What they wanted</th>
                  <th>Agent</th>
                  <th>Outcome</th>
                  <th>Mood</th>
                  <th style={{ textAlign: 'right' }}>Turns</th>
                  <th style={{ textAlign: 'right' }}>Length</th>
                </tr>
              </thead>
              <tbody>
                {recent.map((c) => (
                  <tr key={c.id} data-clickable onClick={() => navigate({ view: 'calls', callId: c.id })}>
                    <td style={{ maxWidth: 320 }}>{c.summary || <span style={{ color: 'var(--text-3)' }}>no speech</span>}</td>
                    <td style={{ color: 'var(--text-2)' }}>{c.agent_name}</td>
                    <td><Outcome value={c.outcome} escalated={!!c.escalated} /></td>
                    <td><Mood value={c.sentiment} /></td>
                    <td className="n">{c.turn_count}</td>
                    <td className="n">{(c.duration_ms / 1000).toFixed(1)}s</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

function Metric({ k, v, s, tone }: { k: string; v: string; s: string; tone: string }) {
  return (
    <div className="metric" style={{ ['--tone' as string]: tone }}>
      <div className="metric-k">{k}</div>
      <div className="metric-v">{v}</div>
      <div className="metric-s">{s}</div>
    </div>
  )
}

function DayChart({ days }: { days: { day: string; calls: number; resolved: number }[] }) {
  const peak = Math.max(1, ...days.map((d) => d.calls))
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', gap: 6, height: 132 }}>
      {days.map((d) => (
        // The column needs an explicit full height: the bar inside sizes itself as a percentage,
        // and a percentage of an auto height is zero, which renders every bar as a flat line.
        <div key={d.day} style={{ flex: 1, height: '100%', display: 'flex', flexDirection: 'column', justifyContent: 'flex-end', alignItems: 'center', gap: 6 }}>
          <div
            title={`${d.day}: ${d.calls} calls, ${d.resolved} resolved`}
            style={{
              width: '100%',
              height: `${(d.calls / peak) * 100}%`,
              minHeight: 3,
              background: 'var(--bg-3)',
              borderRadius: '4px 4px 0 0',
              display: 'flex',
              flexDirection: 'column',
              justifyContent: 'flex-end',
              overflow: 'hidden',
            }}
          >
            <div style={{ height: `${d.calls ? (d.resolved / d.calls) * 100 : 0}%`, background: 'var(--accent)' }} />
          </div>
          <div style={{ fontSize: 9.5, color: 'var(--text-3)', fontFamily: 'var(--mono)' }}>
            {d.day.slice(8)}
          </div>
        </div>
      ))}
    </div>
  )
}

function Sentiment({ counts, total }: { counts: Record<string, number>; total: number }) {
  const rows: [string, string, string][] = [
    ['positive', 'Positive', 'var(--good)'],
    ['neutral', 'Neutral', 'var(--text-dim)'],
    ['negative', 'Negative', 'var(--bad)'],
  ]
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 9 }}>
      {rows.map(([key, label, colour]) => {
        const n = counts[key] ?? 0
        const pct = total ? (n / total) * 100 : 0
        return (
          <div key={key}>
            <div className="row-between" style={{ fontSize: 12, marginBottom: 4 }}>
              <span style={{ color: 'var(--text-2)' }}>{label}</span>
              <span className="num" style={{ color: colour }}>{n}</span>
            </div>
            <div style={{ height: 4, background: 'var(--bg-3)', borderRadius: 99, overflow: 'hidden' }}>
              <div style={{ width: `${pct}%`, height: '100%', background: colour }} />
            </div>
          </div>
        )
      })}
    </div>
  )
}

export function Outcome({ value, escalated }: { value: string; escalated: boolean }) {
  if (escalated || value === 'transferred') return <span className="chip" data-t="cost">passed to a person</span>
  if (value === 'completed') return <span className="chip" data-t="good">handled</span>
  if (value === 'abandoned') return <span className="chip" data-t="bad">caller hung up</span>
  if (value === 'in_progress') return <span className="chip" data-t="accent">in progress</span>
  return <span className="chip">{value}</span>
}

export function Mood({ value }: { value: string }) {
  const tone = value === 'positive' ? 'good' : value === 'negative' ? 'bad' : undefined
  return <span className="chip" data-t={tone}>{value}</span>
}
