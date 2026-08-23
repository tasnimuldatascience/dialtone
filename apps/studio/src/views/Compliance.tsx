import { useEffect, useMemo, useState } from 'react'
import { api, type Finding, type RedactResult } from '../api'

const SAMPLES = [
  {
    label: 'card, read aloud',
    text: 'sure the card is four two four two four two four two four two four two four two four two and the name on it is Sam Hasan',
  },
  {
    label: 'card, typed',
    text: 'the card number is 4539 1488 0343 6467 expiry 04 28',
  },
  {
    label: 'order number (must survive)',
    text: 'my order number is 4242 4242 4242 4241 can you check it',
  },
  {
    label: 'security code',
    text: 'and the cvv is 737 does that work',
  },
  {
    label: 'address and contact',
    text: 'I am at SW1A 1AA and my email is sam.hasan@example.com',
  },
]

/* The redaction playground.
 *
 * The interesting sample is the first one. Nobody says "4242424242424242" on a phone call — they
 * read it out — so a redactor built on digit patterns catches nothing that matters while
 * reporting a clean compliance record, which is the worst failure mode this system has.
 */
export function Compliance() {
  const [text, setText] = useState(SAMPLES[0].text)
  const [result, setResult] = useState<RedactResult | null>(null)

  useEffect(() => {
    const timer = setTimeout(() => {
      api.redact(text).then(setResult).catch(() => undefined)
    }, 160)
    return () => clearTimeout(timer)
  }, [text])

  const highlighted = useMemo(() => (result ? highlight(text, result.findings) : null), [text, result])

  return (
    <div className="page">
      <div className="page-head">
        <div className="eyebrow">Compliance</div>
        <h1>Removed before it is stored, and before the model ever sees it</h1>
        <p>
          On a call, sensitive data arrives one word at a time and is <em>spoken</em>, not typed.
          Both halves break the usual approach: by the time sixteen digits are visible the first
          eight are already logged, and none of them looked like digits to begin with.
        </p>
      </div>

      <div className="grid grid-2" style={{ alignItems: 'start' }}>
        <div className="panel">
          <h2 className="panel-title">Transcript in</h2>
          <p className="panel-note">As the recogniser produced it. Edit freely.</p>
          <div className="control-row" style={{ marginBottom: 12 }}>
            {SAMPLES.map((sample) => (
              <button key={sample.label} className="ghost" data-active={sample.text === text}
                      onClick={() => setText(sample.text)}>
                {sample.label}
              </button>
            ))}
          </div>
          <textarea rows={6} value={text} onChange={(e) => setText(e.target.value)}
                    aria-label="Transcript to redact" />

          {highlighted && (
            <>
              <div className="eyebrow" style={{ marginTop: 18 }}>What was found</div>
              <div className="redact-out">{highlighted}</div>
            </>
          )}
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div className="panel">
            <h2 className="panel-title">What the model receives</h2>
            <p className="panel-note">
              A model that never receives a card number cannot repeat one. That is a structural
              guarantee, not an instruction it might ignore.
            </p>
            <div className="redact-out">{result?.text ?? '…'}</div>
            {result && (
              <div style={{ marginTop: 14 }}>
                <span className="chip" data-tone={result.clean ? 'good' : 'bad'}>
                  {result.clean ? 'nothing removed' : `${result.findings.filter((f) => f.sensitivity === 'strip').length} removed`}
                </span>
              </div>
            )}
          </div>

          {result && result.findings.length > 0 && (
            <div className="panel">
              <h2 className="panel-title">Findings</h2>
              <p className="panel-note">
                A removed item carries no preview. A findings record that holds the last four
                digits is not a compliance record, it is a second copy of the breach.
              </p>
              <table>
                <thead>
                  <tr><th>rule</th><th>handling</th><th>preview</th></tr>
                </thead>
                <tbody>
                  {result.findings.map((finding, index) => (
                    <tr key={index}>
                      <td className="num">{finding.rule}</td>
                      <td>
                        <span className="chip" data-tone={finding.sensitivity === 'strip' ? 'bad' : 'cost'}>
                          {finding.sensitivity === 'strip' ? 'removed entirely' : 'kept, tagged'}
                        </span>
                      </td>
                      <td className="num" style={{ color: 'var(--text-faint)' }}>{finding.preview || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="panel">
            <h2 className="panel-title">Why an order number survives</h2>
            <p className="panel-note" style={{ marginBottom: 0 }}>
              Sixteen digits are not necessarily a card. Every real card passes the Luhn check and
              roughly nine in ten arbitrary digit runs do not — so the check is what separates
              “the caller read their card” from “the caller read their order number”. Without it
              the redactor destroys the reference the agent needs to do its job, and is unusable.
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}

/** Render the original text with each finding wrapped, using the server's spans. */
function highlight(text: string, findings: Finding[]) {
  // Server spans can overlap (the phone rule matches inside a card). Sorting and skipping any
  // span that starts before the previous one ended keeps the output well-formed; STRIP findings
  // come first from the server, so the stronger classification is the one that survives.
  const ordered = [...findings].sort((a, b) => a.start - b.start || b.end - a.end)
  const parts: React.ReactNode[] = []
  let cursor = 0

  ordered.forEach((finding, index) => {
    if (finding.start < cursor) return
    if (finding.start > cursor) parts.push(text.slice(cursor, finding.start))
    parts.push(
      <mark key={index} className={finding.sensitivity}>
        {text.slice(finding.start, finding.end)}
      </mark>,
    )
    cursor = finding.end
  })
  if (cursor < text.length) parts.push(text.slice(cursor))
  return parts
}
