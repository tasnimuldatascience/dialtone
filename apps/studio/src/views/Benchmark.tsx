import { useEffect, useMemo, useState } from 'react'
import { api, type BenchResult } from '../api'
import { TradeoffChart } from '../components/TradeoffChart'
import { Icon } from '../components/Icon'

/* The turn-taking measurement.
 *
 * This is the one screen that is not about running a contact centre. It is the evidence for the
 * claim the rest of the product rests on: the agent replies faster AND interrupts people less,
 * and both halves are measured on a published test set rather than asserted.
 */
export function Benchmark() {
  const [sweep, setSweep] = useState<BenchResult[]>([])
  const [ablation, setAblation] = useState<BenchResult[]>([])
  const [threshold, setThreshold] = useState(520)
  const [syntax, setSyntax] = useState(true)
  const [prosody, setProsody] = useState(true)
  const [live, setLive] = useState<BenchResult | null>(null)
  const [probe, setProbe] = useState('my account number is four two')
  const [verdict, setVerdict] = useState<{ completion: number; reason: string; threshold_ms: number; reading: string } | null>(null)

  useEffect(() => {
    Promise.all([api.sweep(), api.ablation()])
      .then(([s, a]) => { setSweep(s.results); setAblation(a.results) })
      .catch(() => undefined)
  }, [])

  // Debounced: the slider fires continuously while dragging and each request replays the whole
  // test set. Without this the gateway takes about sixty benchmark runs per drag.
  useEffect(() => {
    const timer = window.setTimeout(() => {
      api.benchCustom({ base_silence_ms: threshold, enable_semantic: syntax, enable_prosody: prosody })
        .then((r) => setLive(r.result))
        .catch(() => undefined)
    }, 130)
    return () => window.clearTimeout(timer)
  }, [threshold, syntax, prosody])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (probe.trim()) api.benchScore(probe).then(setVerdict).catch(() => undefined)
    }, 250)
    return () => window.clearTimeout(timer)
  }, [probe])

  const baseline = useMemo(() => ablation.find((r) => r.label.startsWith('baseline')), [ablation])
  const best = useMemo(() => ablation.find((r) => r.label.includes('both')), [ablation])

  return (
    <div className="page">
      <div className="head">
        <h1>Turn-taking</h1>
        <p>
          The hard part of a phone agent is knowing when the caller has finished. Reply too slowly
          and it feels broken; too quickly and you cut people off. Both are measured here, on a
          test set that ships with the product.
        </p>
      </div>

      {best && baseline && (
        <div className="grid g4" style={{ marginBottom: 14 }}>
          <Metric k="Reply delay" v={`${best.median_latency_ms.toFixed(0)}ms`}
                  s={`${(baseline.median_latency_ms / best.median_latency_ms).toFixed(1)}× faster than a fixed 700ms`} tone="var(--accent)" />
          <Metric k="Interruptions" v={`${(best.false_cutoff_rate * 100).toFixed(1)}%`}
                  s={`a fixed rule interrupts ${(baseline.false_cutoff_rate * 100).toFixed(1)}%`} tone="var(--good)" />
          <Metric k="Slow tail" v={`${best.p90_latency_ms.toFixed(0)}ms`} s="p90, not just the median" tone="var(--agent)" />
          <Metric k="Turns answered" v={`${(best.completion_recall * 100).toFixed(0)}%`}
                  s={`of ${best.n_complete + best.n_incomplete} test sentences`} tone="var(--info)" />
        </div>
      )}

      <div className="grid g2" style={{ alignItems: 'start' }}>
        <div className="card">
          <h2 className="card-h">Speed against interruptions</h2>
          <p className="card-sub">
            Both axes are costs, so down and left is better. A system is only genuinely better if
            it moves the whole curve — not if it slides along one.
          </p>
          <TradeoffChart results={sweep} live={live} />
          <div className="lat-key" style={{ marginTop: 8 }}>
            <span><i className="sw" style={{ background: '#ffb340' }} /> fixed rule</span>
            <span><i className="sw" style={{ background: '#35e0d0' }} /> dialtone</span>
            <span><i className="sw" style={{ background: '#a78bfa' }} /> your setting</span>
          </div>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div className="card">
            <h2 className="card-h">Move the dial yourself</h2>
            <p className="card-sub">
              Both numbers are recomputed on every change. Turn both checks off and it becomes an
              ordinary fixed rule — which is the honest way to show where the gain comes from.
            </p>

            <div className="row" style={{ marginBottom: 12 }}>
              <span className="num" style={{ minWidth: 58, color: 'var(--accent)', fontWeight: 600 }}>{threshold}ms</span>
              <input type="range" min={200} max={1200} step={20} value={threshold} onChange={(e) => setThreshold(Number(e.target.value))} />
            </div>

            <div className="row" style={{ marginBottom: 14, flexWrap: 'wrap' }}>
              <label className="switch">
                <input type="checkbox" checked={syntax} onChange={(e) => setSyntax(e.target.checked)} />
                grammar — can the sentence end here?
              </label>
              <label className="switch">
                <input type="checkbox" checked={prosody} onChange={(e) => setProsody(e.target.checked)} />
                tone — is the voice falling?
              </label>
            </div>

            {live && (
              <div className="grid g2" style={{ gap: 10 }}>
                <Metric k="Replies in" v={`${live.median_latency_ms.toFixed(0)}ms`} s={`p90 ${live.p90_latency_ms.toFixed(0)}ms`} tone="var(--accent)" />
                <Metric
                  k="Talks over the caller" v={`${(live.false_cutoff_rate * 100).toFixed(1)}%`}
                  s={`${Math.round(live.false_cutoff_rate * live.n_incomplete)} of ${live.n_incomplete} unfinished sentences`}
                  tone={live.false_cutoff_rate > 0.05 ? 'var(--bad)' : 'var(--good)'}
                />
              </div>
            )}

            {live && live.failures.length === 0 && (
              <div className="note" style={{ marginTop: 12 }}>
                <b>Nobody was interrupted.</b> At this setting it holds through every unfinished
                sentence in the test set, including the ones reading a number aloud.
              </div>
            )}
            {live && live.failures.length > 0 && (
              <div className="note" data-t="warn" style={{ marginTop: 12 }}>
                <b>Interrupted these callers:</b>
                {live.failures.slice(0, 4).map((f) => (
                  <div key={f} className="num" style={{ fontSize: 11, marginTop: 3 }}>{f}</div>
                ))}
              </div>
            )}
          </div>

          <div className="card">
            <h2 className="card-h">Try a sentence</h2>
            <p className="card-sub">How long would the agent wait before replying to this?</p>
            <input type="text" value={probe} onChange={(e) => setProbe(e.target.value)} />
            {verdict && (
              <div style={{ marginTop: 12 }}>
                <div className="row-between" style={{ marginBottom: 8 }}>
                  <span className="chip" data-t={verdict.reading === 'complete' ? 'good' : 'cost'}>
                    sounds {verdict.reading === 'complete' ? 'finished' : 'unfinished'}
                  </span>
                  <span className="num" style={{ fontSize: 19, color: 'var(--accent)', fontWeight: 600 }}>
                    {verdict.threshold_ms}ms
                  </span>
                </div>
                <div style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.55 }}>{verdict.reason}</div>
              </div>
            )}
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 12 }}>
              {['my account number is four two', 'what appointments do you have', 'yes', 'can I also get', 'my postcode is SW1A'].map((s) => (
                <button key={s} className="btn btn-ghost btn-sm" onClick={() => setProbe(s)}>{s}</button>
              ))}
            </div>
          </div>
        </div>
      </div>

      <div className="card card-pad-0" style={{ marginTop: 14 }}>
        <div style={{ padding: '14px 16px 10px' }}>
          <h2 className="card-h">Which check is doing the work?</h2>
          <p className="card-sub" style={{ margin: 0 }}>
            Read the second row before the last. Adjusting the timing <b>without</b> the checks is
            worse than the plain fixed rule — it interrupts every unfinished sentence. That row is
            the control, and it is what makes the last row attributable to the checks.
          </p>
        </div>
        <table>
          <thead>
            <tr>
              <th>Setup</th>
              <th style={{ textAlign: 'right' }}>Reply delay</th>
              <th style={{ textAlign: 'right' }}>Slow tail</th>
              <th style={{ textAlign: 'right' }}>Interrupts</th>
              <th style={{ textAlign: 'right' }}>Answered</th>
            </tr>
          </thead>
          <tbody>
            {ablation.map((r) => (
              <tr key={r.label} style={r.label.includes('both') ? { background: '#0c1a1c' } : undefined}>
                <td style={{ fontWeight: r.label.includes('both') ? 600 : 400 }}>{PLAIN[r.label] ?? r.label}</td>
                <td className="n">{r.median_latency_ms.toFixed(0)}ms</td>
                <td className="n">{r.p90_latency_ms.toFixed(0)}ms</td>
                <td className="n">
                  <span className="chip" data-t={r.false_cutoff_rate === 0 ? 'good' : 'cost'}>
                    {(r.false_cutoff_rate * 100).toFixed(1)}%
                  </span>
                </td>
                <td className="n">{(r.completion_recall * 100).toFixed(0)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="note" style={{ marginTop: 14 }}>
        <Icon name="sparkle" size={13} /> <b>Everything here is recomputed from source on every
        request.</b> Nothing is read from a saved results file, because a benchmark you can only
        reproduce by trusting a checked-in JSON file is a claim rather than a measurement.
      </div>
    </div>
  )
}

const PLAIN: Record<string, string> = {
  'baseline fixed 700ms': 'Ordinary fixed 700ms rule',
  'adaptive, no signals': 'Adjusts timing, but no checks',
  '+ syntax only': 'Grammar check only',
  '+ prosody only': 'Tone check only',
  '+ both (default)': 'Both checks — what ships',
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
