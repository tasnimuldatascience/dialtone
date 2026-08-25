import { useCallback, useEffect, useState } from 'react'
import type { ViewProps } from '../App'
import { api, type CallDetail, type CallRow, type Timing } from '../api'
import { Icon } from '../components/Icon'
import { Happened, Mood } from '../components/CallCells'

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
        {/* Filters on what HAPPENED, with counts, so the tab itself tells you whether it is
            worth opening. "Handled / Passed on / Hung up" filtered on the socket's outcome, which
            was "completed" for every call, so two of the three tabs were always empty. */}
        <div className="row">
          {RESULTS.map((r) => {
            const n = r.key === '' ? calls.length : calls.filter((c) => c.result === r.key).length
            return (
              <button
                key={r.key}
                className="btn btn-ghost btn-sm"
                data-active={filter === r.key}
                disabled={n === 0 && r.key !== ''}
                onClick={() => setFilter(r.key)}
              >
                {r.label}
                <b className="tab-n">{n}</b>
              </button>
            )
          })}
        </div>
      </div>

      {calls.length === 0 ? (
        <div className="card"><div className="empty"><h3>No calls yet</h3>Start one from <b>Live call</b>.</div></div>
      ) : (
        <div className="t-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: '38%' }}>What they wanted</th>
                <th>What happened</th>
                <th>Channel</th>
                <th style={{ textAlign: 'right' }}>Turns</th>
                <th style={{ textAlign: 'right' }}>Length</th>
                <th style={{ textAlign: 'right' }}>When</th>
              </tr>
            </thead>
            <tbody>
              {calls.map((c) => (
                <tr key={c.id} data-clickable onClick={() => void open(c.id)} data-quiet={c.result === 'no speech' || undefined}>
                  <td>
                    {/* What it was about, then what they said. The opening line alone was the
                        whole column, and on anything longer than one turn it describes where the
                        call STARTED rather than what it was for. */}
                    {c.wanted && <div className="wanted">{c.wanted}</div>}
                    <div className={c.wanted ? 'said' : 'wanted'}>
                      {c.summary
                        ? (c.wanted ? `“${c.summary}”` : c.summary)
                        : <span style={{ color: 'var(--text-3)' }}>nobody spoke</span>}
                    </div>
                    {/* Mood only when it is NOT neutral. It was neutral on every row, and a
                        badge that always says the same thing trains the eye to skip it. */}
                    {c.sentiment && c.sentiment !== 'neutral' && (
                      <Mood value={c.sentiment} />
                    )}
                  </td>
                  <td><Happened call={c} /></td>
                  <td><span className="chip" data-t={c.channel === 'voice' ? 'agent' : undefined}>{c.channel}</span></td>
                  <td className="n">{c.turn_count || <span style={{ color: 'var(--text-3)' }}>—</span>}</td>
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

const RESULTS: { key: string; label: string }[] = [
  { key: '', label: 'All' },
  { key: 'booked', label: 'Booked' },
  { key: 'answered', label: 'Answered' },
  { key: 'passed on', label: 'Passed on' },
  { key: 'no speech', label: 'Nobody spoke' },
]

/* What the call DID, in one cell.
 *
 * Replaces two columns that carried nothing. "Outcome" read `completed` on all sixteen calls on
 * screen — it records how the socket ended, not what the call achieved — and "Mood" read
 * `neutral` on all sixteen, which is a coarse word-list guess doing its honest best. Three of the
 * six columns were constant, and a constant column teaches the eye to stop reading that strip.
 *
 * A booked call carries its reference, because that is the thing somebody quotes back.
 */

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
        <h1>{call.wanted || call.summary || 'Call'}</h1>
        {call.wanted && call.summary && (
          <p className="said" style={{ marginBottom: 6 }}>“{call.summary}”</p>
        )}
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
                      {/* By DOCUMENT, deduped. Retrieval returns passages, and two passages
                          from the same page are two citations -- which rendered as
                          "Emergencies  Emergencies" under a reply and reads as a bug. What
                          the line answers is "where did this come from", and that is the page. */}
                      {[...new Set((t.citations ?? []).map((c) => c.document))].map((doc) => (
                        <span key={doc} style={{ color: 'var(--info)' }}>{doc}</span>
                      ))}
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
