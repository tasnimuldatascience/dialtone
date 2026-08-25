import { useCallback, useEffect, useState } from 'react'
import type { ViewProps } from '../App'
import { api, type CallDetail, type CallRow, type Timing } from '../api'
import { Icon } from '../components/Icon'
import { Mood, Outcome } from './Dashboard'

/* Reading a call back.
 *
 * The transcript is the obvious half. The other half is everything the transcript does not show:
 * which document each answer came from, where the time went, which numbers had no source, and
 * whether the model tried to jump a step it was not allowed to.
 *
 * That is the difference between a call log and a tool for finding out why a call went wrong.
 */
export function Calls({ initialCallId }: ViewProps & { initialCallId?: string }) {
  const [calls, setCalls] = useState<CallRow[]>([])
  const [selected, setSelected] = useState<CallDetail | null>(null)
  const [filter, setFilter] = useState<string>('')

  const load = useCallback(async () => {
    const { calls: rows } = await api.calls({ limit: 200, outcome: filter || undefined })
    setCalls(rows)
  }, [filter])

  useEffect(() => { void load() }, [load])

  useEffect(() => {
    if (initialCallId) void api.call(initialCallId).then(setSelected).catch(() => undefined)
  }, [initialCallId])

  const open = async (id: string) => {
    setSelected(await api.call(id))
  }

  if (selected) {
    return <Detail call={selected} onBack={() => setSelected(null)} />
  }

  return (
    <div className="page">
      <div className="head row-between">
        <div>
          <h1>Call history</h1>
          <p>Every call, with its transcript, timings and checks.</p>
        </div>
        <div className="row">
          {['', 'completed', 'transferred', 'abandoned'].map((f) => (
            <button key={f} className="btn btn-ghost btn-sm" data-active={filter === f} onClick={() => setFilter(f)}>
              {f === '' ? 'All' : f === 'transferred' ? 'Passed on' : f === 'abandoned' ? 'Hung up' : 'Handled'}
            </button>
          ))}
        </div>
      </div>

      {calls.length === 0 ? (
        <div className="card"><div className="empty"><h3>No calls yet</h3>Start one from <b>Live call</b>.</div></div>
      ) : (
        <div className="t-wrap">
          <table>
            <thead>
              <tr>
                <th>What they wanted</th>
                <th>Agent</th>
                <th>Channel</th>
                <th>Outcome</th>
                <th>Mood</th>
                <th style={{ textAlign: 'right' }}>Turns</th>
                <th style={{ textAlign: 'right' }}>Length</th>
                <th style={{ textAlign: 'right' }}>When</th>
              </tr>
            </thead>
            <tbody>
              {calls.map((c) => (
                <tr key={c.id} data-clickable onClick={() => void open(c.id)}>
                  <td style={{ maxWidth: 340 }}>{c.summary || <span style={{ color: 'var(--text-3)' }}>no speech</span>}</td>
                  <td style={{ color: 'var(--text-2)' }}>{c.agent_name}</td>
                  <td><span className="chip" data-t={c.channel === 'voice' ? 'agent' : undefined}>{c.channel}</span></td>
                  <td><Outcome value={c.outcome} escalated={!!c.escalated} /></td>
                  <td><Mood value={c.sentiment} /></td>
                  <td className="n">{c.turn_count}</td>
                  <td className="n">{(c.duration_ms / 1000).toFixed(1)}s</td>
                  <td className="n" style={{ color: 'var(--text-dim)' }}>{c.started_at.slice(11, 16)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function Detail({ call, onBack }: { call: CallDetail; onBack: () => void }) {
  const latencies = call.turns.map((t) => t.timing?.total_ms ?? 0).filter(Boolean).sort((a, b) => a - b)
  const median = latencies.length ? latencies[Math.floor(latencies.length / 2)] : 0
  const flagged = call.turns.filter((t) => t.grounding && !t.grounding.ok).length
  const redactions = call.turns.filter((t) => t.redacted?.length).length

  return (
    <div className="page">
      <div className="head">
        <button className="btn btn-ghost btn-sm" onClick={onBack} style={{ marginBottom: 12 }}>
          ← All calls
        </button>
        <h1>{call.summary || 'Call'}</h1>
        <p>
          {call.agent_name} · {call.started_at.replace('T', ' ').slice(0, 16)} ·{' '}
          {(call.duration_ms / 1000).toFixed(1)}s · {call.turns.length}{' '}
          {call.turns.length === 1 ? 'turn' : 'turns'}
        </p>
      </div>

      <CallOutcome call={call} />

      <div className="grid g4" style={{ marginBottom: 16 }}>
        <Metric k="Turns" v={String(call.turns.length)} s="exchanges" tone="var(--accent)" />
        <Metric k="Median reply" v={`${Math.round(median)}ms`} s={`slowest ${Math.round(latencies[latencies.length - 1] ?? 0)}ms`} tone="var(--agent)" />
        <Metric
          k="Unverified numbers" v={String(flagged)}
          s={flagged ? 'figures with no source' : 'every number had a source'}
          tone={flagged ? 'var(--cost)' : 'var(--good)'}
        />
        <Metric
          k="Redactions" v={String(redactions)}
          s={redactions ? 'removed before storage' : 'nothing sensitive'}
          tone={redactions ? 'var(--bad)' : 'var(--text-dim)'}
        />
      </div>

      <div className="grid g2" style={{ alignItems: 'start' }}>
        <div className="card">
          <h2 className="card-h">Transcript</h2>
          <p className="card-sub">Stored redacted — anything sensitive was removed before it reached the database.</p>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            {call.turns.map((t, i) => (
              <div key={i} style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {t.caller && (
                  <div className="bubble" data-who="caller" style={{ maxWidth: '90%' }}>
                    <div className="av" data-who="caller">You</div>
                    <div className="msg">{t.caller}</div>
                  </div>
                )}
                <div className="bubble" data-who="agent" style={{ maxWidth: '90%' }}>
                  <div className="av" data-who="agent">AI</div>
                  <div style={{ minWidth: 0 }}>
                    <div className="msg">{t.agent}</div>
                    {t.spoken && t.spoken !== t.agent && (
                      <div className="msg-meta" title="What the voice engine was given">
                        spoken as: {t.spoken}
                      </div>
                    )}
                    {t.grounding && !t.grounding.ok && (
                      <div className="note" data-t="warn" style={{ marginTop: 7, fontSize: 11.5 }}>
                        {t.grounding.findings.map((f, j) => (
                          <div key={j}><b>{f.value}</b> appears in no document the agent was given</div>
                        ))}
                        {t.grounding.hedged && <div>estimated a price the documents state exactly</div>}
                      </div>
                    )}
                    {t.refused && (
                      <div className="note" data-t="bad" style={{ marginTop: 7, fontSize: 11.5 }}>
                        The model proposed a step the flow does not allow, and was refused.
                      </div>
                    )}
                    <div className="msg-meta">
                      {t.timing && <span>{Math.round(t.timing.total_ms)}ms</span>}
                      {t.node && <span>{t.node}</span>}
                      {t.moved_to && <span style={{ color: 'var(--accent)' }}>→ {t.moved_to}</span>}
                      {t.citations?.map((c, j) => <span key={j} style={{ color: 'var(--info)' }}>{c.document}</span>)}
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="card">
          <h2 className="card-h">Where the time went</h2>
          <p className="card-sub">Per turn, measured — not estimated.</p>
          {call.turns.map((t, i) => (
            <TurnBar key={i} index={i} timing={t.timing} />
          ))}
        </div>
      </div>
    </div>
  )
}

const STAGES: { key: keyof Timing; label: string; colour: string }[] = [
  { key: 'redact', label: 'redact', colour: '#ff6b81' },
  { key: 'knowledge', label: 'knowledge', colour: '#60a5fa' },
  { key: 'think', label: 'think', colour: '#a78bfa' },
  { key: 'speak', label: 'generate', colour: '#35e0d0' },
  { key: 'tools', label: 'tools', colour: '#ffb340' },
]

function TurnBar({ index, timing }: { index: number; timing?: Timing }) {
  if (!timing) return null
  return (
    <div style={{ marginBottom: 11 }}>
      <div className="row-between" style={{ fontSize: 11.5, color: 'var(--text-dim)' }}>
        <span>turn {index + 1}</span>
        <span className="num">{Math.round(timing.total_ms)}ms</span>
      </div>
      <div className="lat">
        {STAGES.map((s) => {
          const value = Number(timing[s.key] ?? 0)
          const width = timing.total_ms ? (value / timing.total_ms) * 100 : 0
          return width > 0.5 ? (
            <i key={s.key} style={{ width: `${width}%`, background: s.colour }} title={`${s.label} ${Math.round(value)}ms`} />
          ) : null
        })}
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

export { Icon }


/* ── what the call actually DID ──────────────────────────────────────────────
 *
 * The heading above is `call.summary`, which is the caller's opening sentence verbatim -- a
 * quote, not a summary. Useful for recognising a call in a list; useless for the first question
 * anyone asks of one, which is "did it work?".
 *
 * Answering that meant reading the whole transcript, and on a thirty-turn call nobody does.
 * So the outcome is stated: booked and its reference, passed to a person, or nothing agreed.
 */
function CallOutcome({ call }: { call: CallDetail }) {
  const appointment = call.appointment
  const at = appointment ? new Date(appointment.starts_at) : null

  if (appointment && at) {
    return (
      <div className="outcome" data-t="good">
        <Icon name="check" size={17} />
        <div>
          <b>Appointment booked</b>
          <p>
            {at.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' })}
            {' at '}
            {at.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}
            {appointment.patient_name && <> · {appointment.patient_name}</>}
            {appointment.reason && <> · {appointment.reason}</>}
          </p>
        </div>
        <code className="ref">{appointment.reference}</code>
      </div>
    )
  }

  if (call.escalated) {
    return (
      <div className="outcome" data-t="warn">
        <Icon name="user" size={17} />
        <div>
          <b>Passed to a person</b>
          <p>The agent handed over rather than finishing the call itself.</p>
        </div>
      </div>
    )
  }

  return (
    <div className="outcome">
      <Icon name="chat" size={17} />
      <div>
        <b>{call.turns.length ? 'Nothing was booked' : 'Nobody spoke'}</b>
        <p>
          {call.turns.length
            ? 'The caller asked questions and the call ended without an appointment.'
            : 'The call connected but no caller speech was recorded.'}
        </p>
      </div>
    </div>
  )
}
