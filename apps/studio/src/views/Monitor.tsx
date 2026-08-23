import { useCallback, useEffect, useRef, useState } from 'react'
import { api, watchCall, type CallEvent, type CallResult, type Scenario } from '../api'

/* The live call monitor.
 *
 * A voice agent's behaviour is only legible in time. A summary can tell you the agent
 * interrupted someone; only a timeline shows you it happened 180ms into a number being read
 * aloud, which is the difference between a metric and a diagnosis.
 */
export function Monitor() {
  const [scenarios, setScenarios] = useState<Scenario[]>([])
  const [selected, setSelected] = useState('')
  const [events, setEvents] = useState<CallEvent[]>([])
  const [result, setResult] = useState<CallResult | null>(null)
  const [streaming, setStreaming] = useState(false)
  const disposer = useRef<(() => void) | null>(null)
  const scroller = useRef<HTMLDivElement>(null)

  useEffect(() => {
    api.scenarios().then((s) => {
      setScenarios(s.scenarios)
      setSelected((current) => current || s.scenarios[0]?.id || '')
    })
  }, [])

  // Closing the socket on unmount is not optional: React 18 StrictMode mounts twice in
  // development, and without this every run leaves an orphaned stream writing into a dead
  // component's state.
  useEffect(() => () => disposer.current?.(), [])

  useEffect(() => {
    // Scroll the CONTAINER, not the document. `scrollIntoView` walks up to the nearest
    // scrollable ancestor -- which is the page -- so every arriving event dragged the whole
    // document downward and pulled the header out of view mid-call.
    const box = scroller.current
    if (box) box.scrollTop = box.scrollHeight
  }, [events.length])

  const start = useCallback(() => {
    if (!selected) return
    disposer.current?.()
    setEvents([])
    setResult(null)
    setStreaming(true)

    disposer.current = watchCall(selected, (message) => {
      if (message.type === 'event') {
        setEvents((current) => [...current, message as unknown as CallEvent])
      } else if (message.type === 'done') {
        setStreaming(false)
        // The stream carries events only; the full transcript and redaction record come from
        // the same replay via REST, so the panel below is never assembled from partial state.
        api.runCall(selected).then(setResult).catch(() => undefined)
      }
    }, () => setStreaming(false))
  }, [selected])

  const scenario = scenarios.find((s) => s.id === selected)

  return (
    <div className="page">
      <div className="page-head">
        <div className="eyebrow">Call monitor</div>
        <h1>Every decision, on a timeline</h1>
        <p>
          These are scripted callers replayed through the real endpointer, barge-in detector and
          redactor — only the speech services are stubbed, and each stub carries the measured
          latency of the thing it replaces. Reproducing any of these on a live line takes a
          person, a phone and luck; reproducing one twice takes more luck than that.
        </p>
      </div>

      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="control-row">
          {scenarios.map((s) => (
            <button key={s.id} className="ghost" data-active={s.id === selected}
                    onClick={() => setSelected(s.id)} disabled={streaming}>
              {s.title}
            </button>
          ))}
          <button className="action" onClick={start} disabled={streaming || !selected}
                  style={{ marginLeft: 'auto' }}>
            {streaming ? 'running…' : 'Replay call'}
          </button>
        </div>
        {scenario && <p className="panel-note" style={{ marginTop: 14, marginBottom: 0 }}>{scenario.description}</p>}
      </div>

      {result && (
        <div className="grid grid-4" style={{ marginBottom: 16 }}>
          <Stat label="Median endpoint" value={`${result.summary.median_endpoint_ms.toFixed(0)}ms`}
                sub={`fixed 700ms would take ${result.summary.baseline_median_ms.toFixed(0)}ms`}
                accent="var(--latency)" />
          <Stat label="Talked over the caller" value={`${result.summary.false_cutoffs}`}
                sub="times the agent cut in mid-sentence"
                accent={result.summary.false_cutoffs ? 'var(--bad)' : 'var(--good)'} />
          <Stat label="Interruptions handled" value={`${result.summary.interruptions}`}
                sub="history truncated to what was heard" accent="var(--violet)" />
          <Stat label="Redacted" value={`${result.summary.redactions}`}
                sub="before storage and before the model"
                accent={result.summary.redactions ? 'var(--cost)' : 'var(--text-faint)'} />
        </div>
      )}

      <div className="grid grid-2" style={{ alignItems: 'start' }}>
        <div className="panel">
          <h2 className="panel-title">Timeline</h2>
          <p className="panel-note">
            Paced to preserve the relative spacing of events. When they happened matters as much
            as what happened.
          </p>
          {events.length === 0 && !streaming && (
            <div className="empty">pick a scenario and press replay</div>
          )}
          <div className="timeline" ref={scroller}>
            {events.map((event, index) => (
              <TimelineRow key={index} event={event} />
            ))}
          </div>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          {result && (
            <div className="panel">
              <h2 className="panel-title">What the model sees next turn</h2>
              <p className="panel-note">
                The assistant lines are what the caller <em>heard</em>, not what was generated. An
                agent that believes it said something the caller never heard produces answers that
                make no sense two turns later, and nothing in the logs explains why.
              </p>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 9 }}>
                {result.transcript.map((message, index) => (
                  <div key={index} style={{ display: 'flex', gap: 11 }}>
                    <span className="chip" data-tone={message.role === 'user' ? undefined : 'latency'}
                          style={{ flexShrink: 0, minWidth: 74, justifyContent: 'center' }}>
                      {message.role}
                    </span>
                    <span style={{ color: message.content.endsWith('…') ? 'var(--bad)' : 'var(--text)' }}>
                      {message.content}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {result && result.redactions.length > 0 && (
            <div className="panel">
              <h2 className="panel-title">Removed before anything stored it</h2>
              <p className="panel-note">
                The model never receives these. A model that never sees a card number cannot
                repeat one, which is a stronger guarantee than instructing it not to.
              </p>
              {result.redactions.map((r, index) => (
                <div key={index} style={{ marginBottom: 10 }}>
                  <span className="chip" data-tone="bad">{r.rules.join(', ')}</span>
                  <div className="redact-out" style={{ marginTop: 7 }}>{r.safe_text}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

const KIND_LABEL: Record<string, string> = {
  endpoint: 'turn ended',
  reply: 'generating',
  spoke: 'spoke',
  barge_in: 'caller interrupted',
  backchannel: 'backchannel',
  false_cutoff: 'talked over the caller',
}

function TimelineRow({ event }: { event: CallEvent }) {
  const seconds = (event.at_ms / 1000).toFixed(2)
  return (
    <div className="tl-row" data-kind={event.kind}>
      <div className="tl-time">{seconds}s</div>
      <div className="tl-dot" />
      <div>
        <div className="tl-kind">{KIND_LABEL[event.kind] ?? event.kind}</div>
        <div className="tl-body">{renderBody(event)}</div>
      </div>
    </div>
  )
}

function renderBody(event: CallEvent) {
  switch (event.kind) {
    case 'endpoint':
      return (
        <>
          <strong>“{String(event.transcript)}”</strong>
          <div className="tl-reason">
            responded after {Number(event.latency_ms).toFixed(0)}ms — {String(event.reason)}
          </div>
        </>
      )
    case 'reply': {
      const budget = event.budget as Record<string, number> | undefined
      return (
        <>
          <strong>“{String(event.text)}”</strong>
          {budget && (
            <div className="tl-reason">
              endpoint {budget.endpoint?.toFixed(0)}ms · stt {budget.stt?.toFixed(0)}ms · llm{' '}
              {budget.llm?.toFixed(0)}ms · tts {budget.tts?.toFixed(0)}ms ={' '}
              <span style={{ color: budget.within_budget ? 'var(--good)' : 'var(--bad)' }}>
                {budget.total_ms?.toFixed(0)}ms
              </span>
            </div>
          )}
        </>
      )
    }
    case 'barge_in':
      return (
        <>
          <div>
            generated <span style={{ color: 'var(--text-faint)' }}>“{String(event.generated)}”</span>
          </div>
          <div style={{ marginTop: 4 }}>
            heard <strong style={{ color: 'var(--bad)' }}>“{String(event.heard)}”</strong>
          </div>
          <div className="tl-reason">
            {(Number(event.fraction_played) * 100).toFixed(0)}% played — {String(event.reason)}
          </div>
        </>
      )
    case 'backchannel':
      return (
        <>
          <strong>“{String(event.transcript)}”</strong>
          <div className="tl-reason">{String(event.reason ?? 'agreement — the agent keeps talking')}</div>
        </>
      )
    case 'false_cutoff':
      return (
        <>
          <div>heard only <strong>“{String(event.heard)}”</strong></div>
          <div className="tl-reason">caller was saying “{String(event.caller_was_saying)}”</div>
        </>
      )
    default:
      return <strong>“{String(event.text ?? '')}”</strong>
  }
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
