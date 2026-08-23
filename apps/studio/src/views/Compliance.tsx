import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'

/* Keeping card numbers out of the system.
 *
 * The point of the first sample is that it contains no digits at all. People read card numbers
 * ALOUD, so a redactor built on digit patterns finds nothing and reports the call clean — which
 * is the worst failure this system has, because the clean report is believed.
 */

interface Finding { rule: string; sensitivity: string; start: number; end: number; preview: string }

const SAMPLES: { label: string; text: string }[] = [
  {
    label: 'card, read aloud',
    text: 'sure the card is four two four two four two four two four two four two four two four two and the name on it is Sam Hasan',
  },
  { label: 'card, typed', text: 'the card number is 4539 1488 0343 6467 expiry 04 28' },
  { label: 'order number (must survive)', text: 'my order number is 4242 4242 4242 4241 can you check it' },
  { label: 'security code', text: 'and the cvv is 737 does that work' },
  { label: 'address and contact', text: 'I am at SW1A 1AA and my email is sam.hasan@example.com' },
]

export function Compliance() {
  const [text, setText] = useState(SAMPLES[0].text)
  const [result, setResult] = useState<{ text: string; clean: boolean; findings: Finding[] } | null>(null)

  useEffect(() => {
    const timer = window.setTimeout(() => {
      api.redact(text).then(setResult).catch(() => undefined)
    }, 160)
    return () => window.clearTimeout(timer)
  }, [text])

  const marked = useMemo(() => (result ? highlight(text, result.findings) : null), [text, result])
  const stripped = result?.findings.filter((f) => f.sensitivity === 'strip').length ?? 0

  return (
    <div className="page">
      <div className="head">
        <h1>Compliance</h1>
        <p>
          On a call, sensitive data arrives one word at a time and is <em>spoken</em>, not typed.
          Both halves break the usual approach.
        </p>
      </div>

      <div className="grid g2" style={{ alignItems: 'start' }}>
        <div className="card">
          <h2 className="card-h">What the caller said</h2>
          <p className="card-sub">As the speech recogniser produced it. Edit freely.</p>

          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 12 }}>
            {SAMPLES.map((s) => (
              <button key={s.label} className="btn btn-ghost btn-sm" data-active={s.text === text} onClick={() => setText(s.text)}>
                {s.label}
              </button>
            ))}
          </div>

          <textarea rows={5} value={text} onChange={(e) => setText(e.target.value)} />

          {marked && (
            <>
              <div style={{ fontSize: 10.5, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--text-3)', fontWeight: 620, margin: '16px 0 7px' }}>
                What was found
              </div>
              <div style={{ fontFamily: 'var(--mono)', fontSize: 12.5, lineHeight: 1.8, background: 'var(--bg-0)', border: '1px solid var(--line-2)', borderRadius: 'var(--r-sm)', padding: 12, wordBreak: 'break-word' }}>
                {marked}
              </div>
            </>
          )}
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div className="card">
            <h2 className="card-h">What the AI receives</h2>
            <p className="card-sub">
              A model that never receives a card number cannot repeat one. That is a structural
              guarantee, not an instruction it might ignore.
            </p>
            <div style={{ fontFamily: 'var(--mono)', fontSize: 12.5, lineHeight: 1.8, background: 'var(--bg-0)', border: '1px solid var(--line-2)', borderRadius: 'var(--r-sm)', padding: 12, wordBreak: 'break-word' }}>
              {result?.text ?? '…'}
            </div>
            {result && (
              <div style={{ marginTop: 12 }}>
                <span className="chip" data-t={result.clean ? 'good' : 'bad'}>
                  {result.clean ? 'nothing removed' : `${stripped} removed`}
                </span>
              </div>
            )}
          </div>

          {result && result.findings.length > 0 && (
            <div className="card card-pad-0">
              <div style={{ padding: '14px 16px 10px' }}>
                <h2 className="card-h">Findings</h2>
                <p className="card-sub" style={{ margin: 0 }}>
                  A removed item carries no preview. A record holding the last four digits is not a
                  compliance record, it is a second copy of the breach.
                </p>
              </div>
              <table>
                <thead><tr><th>Rule</th><th>Handling</th><th>Preview</th></tr></thead>
                <tbody>
                  {result.findings.map((f, i) => (
                    <tr key={i}>
                      <td className="num">{f.rule}</td>
                      <td>
                        <span className="chip" data-t={f.sensitivity === 'strip' ? 'bad' : 'cost'}>
                          {f.sensitivity === 'strip' ? 'removed entirely' : 'kept, tagged'}
                        </span>
                      </td>
                      <td className="num" style={{ color: 'var(--text-3)' }}>{f.preview || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="card">
            <h2 className="card-h">Why an order number survives</h2>
            <p className="card-sub" style={{ margin: 0 }}>
              Sixteen digits are not necessarily a card. Every real card passes the Luhn check and
              roughly nine in ten arbitrary digit runs do not — so the check separates "the caller
              read their card" from "the caller read their order number". Without it the redactor
              destroys the reference the agent needs to help them, and is unusable.
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}

/** Wrap each finding using the server's spans. Overlaps are dropped rather than nested: the
 *  phone rule matches inside a card number, and STRIP findings come first from the server, so
 *  the stronger classification is the one that survives. */
function highlight(text: string, findings: Finding[]) {
  const ordered = [...findings].sort((a, b) => a.start - b.start || b.end - a.end)
  const parts: React.ReactNode[] = []
  let cursor = 0

  ordered.forEach((f, i) => {
    if (f.start < cursor) return
    if (f.start > cursor) parts.push(text.slice(cursor, f.start))
    parts.push(
      <mark
        key={i}
        style={{
          background: f.sensitivity === 'strip' ? '#3a1119' : '#2a2110',
          color: f.sensitivity === 'strip' ? 'var(--bad)' : 'var(--cost)',
          padding: '1px 5px', borderRadius: 4, fontWeight: f.sensitivity === 'strip' ? 620 : 400,
        }}
      >
        {text.slice(f.start, f.end)}
      </mark>,
    )
    cursor = f.end
  })
  if (cursor < text.length) parts.push(text.slice(cursor))
  return parts
}
