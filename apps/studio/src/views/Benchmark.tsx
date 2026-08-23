import { useEffect, useMemo, useState } from 'react'
import { api, type BenchmarkResult } from '../api'
import { TradeoffChart } from '../components/TradeoffChart'

export function Benchmark() {
  const [sweep, setSweep] = useState<BenchmarkResult[]>([])
  const [ablation, setAblation] = useState<BenchmarkResult[]>([])
  const [threshold, setThreshold] = useState(520)
  const [semantic, setSemantic] = useState(true)
  const [prosody, setProsody] = useState(true)
  const [live, setLive] = useState<BenchmarkResult | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    Promise.all([api.sweep(), api.ablation()])
      .then(([s, a]) => {
        setSweep(s.results)
        setAblation(a.results)
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  useEffect(() => {
    // Debounced: the slider fires continuously while dragging, and each request replays the
    // whole corpus. Without this the gateway receives ~60 benchmark runs per drag.
    const timer = setTimeout(() => {
      api
        .custom({ base_silence_ms: threshold, enable_semantic: semantic, enable_prosody: prosody })
        .then((r) => setLive(r.result))
        .catch(() => undefined)
    }, 130)
    return () => clearTimeout(timer)
  }, [threshold, semantic, prosody])

  const baseline = useMemo(() => ablation.find((r) => r.label.startsWith('baseline')), [ablation])
  const best = useMemo(() => ablation.find((r) => r.label.includes('both')), [ablation])

  if (error) return <div className="empty">could not reach the gateway — {error}</div>

  return (
    <div className="page">
      <div className="page-head">
        <div className="eyebrow">Endpointing benchmark</div>
        <h1>Latency is not free, and the price is measured here</h1>
        <p>
          Every voice vendor publishes a latency number. None publishes the false-cutoff rate that
          came with it — yet they are the same dial. Any latency figure is reachable by lowering
          the silence threshold; the only question is how often you interrupt someone to get it.
        </p>
      </div>

      {best && baseline && (
        <div className="grid grid-4" style={{ marginBottom: 18 }}>
          <Stat label="Median response" value={`${best.median_latency_ms.toFixed(0)}ms`}
                sub={`${(baseline.median_latency_ms / best.median_latency_ms).toFixed(1)}× faster than a fixed 700ms`}
                accent="var(--latency)" />
          <Stat label="False cutoff" value={`${(best.false_cutoff_rate * 100).toFixed(1)}%`}
                sub={`baseline interrupts on ${(baseline.false_cutoff_rate * 100).toFixed(1)}%`}
                accent="var(--good)" />
          <Stat label="p90 response" value={`${best.p90_latency_ms.toFixed(0)}ms`}
                sub="the slow tail, not just the median" accent="var(--latency)" />
          <Stat label="Turns answered" value={`${(best.completion_recall * 100).toFixed(0)}%`}
                sub={`over ${best.n_complete + best.n_incomplete} labelled turns`}
                accent="var(--violet)" />
        </div>
      )}

      <div className="grid grid-2" style={{ alignItems: 'start' }}>
        <div className="panel">
          <h2 className="panel-title">The trade-off curve</h2>
          <p className="panel-note">
            A system is better only if it moves the whole curve toward the origin — not if it
            slides along one. That distinction is what a single published number cannot express.
          </p>
          <TradeoffChart results={sweep} live={live} />
          <div className="legend" style={{ marginTop: 10 }}>
            <span><i className="swatch" style={{ background: '#ffab3d' }} /> fixed threshold</span>
            <span><i className="swatch" style={{ background: '#35e0d0' }} /> adaptive</span>
            <span><i className="swatch" style={{ background: '#a78bfa' }} /> your setting</span>
          </div>
        </div>

        <div className="panel">
          <h2 className="panel-title">Move the dial yourself</h2>
          <p className="panel-note">
            Both numbers are recomputed against the labelled corpus on every change. Turn both
            signals off and the adaptive endpointer becomes a fixed one — which is the honest way
            to show that the gain comes from the signals, not from a luckier default.
          </p>

          <div className="control-row" style={{ marginBottom: 14 }}>
            <span className="num" style={{ minWidth: 62, color: 'var(--latency)', fontWeight: 600 }}>
              {threshold}ms
            </span>
            <input type="range" min={200} max={1200} step={20} value={threshold}
                   onChange={(e) => setThreshold(Number(e.target.value))}
                   aria-label="Base silence threshold" />
          </div>

          <div className="control-row" style={{ marginBottom: 20 }}>
            <label className="toggle">
              <input type="checkbox" checked={semantic} onChange={(e) => setSemantic(e.target.checked)} />
              syntax — is the sentence finished?
            </label>
            <label className="toggle">
              <input type="checkbox" checked={prosody} onChange={(e) => setProsody(e.target.checked)} />
              prosody — is the pitch falling?
            </label>
          </div>

          {live && (
            <div className="grid grid-2" style={{ gap: 12, marginBottom: 18 }}>
              <Stat label="Responds in" value={`${live.median_latency_ms.toFixed(0)}ms`}
                    sub={`p90 ${live.p90_latency_ms.toFixed(0)}ms`} accent="var(--latency)" />
              <Stat label="Talks over the caller"
                    value={`${(live.false_cutoff_rate * 100).toFixed(1)}%`}
                    sub={`${Math.round(live.false_cutoff_rate * live.n_incomplete)} of ${live.n_incomplete} unfinished turns`}
                    accent={live.false_cutoff_rate > 0.05 ? 'var(--bad)' : 'var(--good)'} />
            </div>
          )}

          {live && live.failures.length > 0 && (
            <>
              <div className="eyebrow">Who it interrupted</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {live.failures.slice(0, 6).map((f) => (
                  <div key={f} className="num" style={{ fontSize: 12, color: 'var(--cost)' }}>{f}</div>
                ))}
              </div>
            </>
          )}
          {live && live.failures.length === 0 && (
            <div className="callout">
              <strong>No caller interrupted.</strong> At this setting the endpointer holds through
              every unfinished turn in the corpus, including the ones reading a number aloud.
            </div>
          )}
        </div>
      </div>

      <div className="panel" style={{ marginTop: 16 }}>
        <h2 className="panel-title">Which signal is doing the work?</h2>
        <p className="panel-note">
          An adaptive endpointer that wins only because its base threshold happens to be tuned is
          not adaptive, it is tuned. Turning each signal off in turn is the only way to tell them
          apart — and the row that matters is the second one, where adaptivity without signals is
          strictly worse than the baseline it replaced.
        </p>
        <div className="scroll-x">
          <table>
            <thead>
              <tr>
                <th>configuration</th>
                <th style={{ textAlign: 'right' }}>median</th>
                <th style={{ textAlign: 'right' }}>p90</th>
                <th style={{ textAlign: 'right' }}>false cutoff</th>
                <th style={{ textAlign: 'right' }}>turns answered</th>
              </tr>
            </thead>
            <tbody>
              {ablation.map((r) => (
                <tr key={r.label} data-highlight={r.label.includes('both')}>
                  <td>{r.label}</td>
                  <td className="n">{r.median_latency_ms.toFixed(0)}ms</td>
                  <td className="n">{r.p90_latency_ms.toFixed(0)}ms</td>
                  <td className="n">
                    <span className="chip" data-tone={r.false_cutoff_rate === 0 ? 'good' : 'cost'}>
                      {(r.false_cutoff_rate * 100).toFixed(1)}%
                    </span>
                  </td>
                  <td className="n">{(r.completion_recall * 100).toFixed(0)}%</td>
                </tr>
              ))}
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
