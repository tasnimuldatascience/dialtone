import type { BenchResult } from '../api'

/* Latency against interruptions, which is the argument the whole product rests on.
 *
 * Both axes are costs, so DOWN AND LEFT IS BETTER. Any latency figure is reachable by lowering
 * the silence threshold; a system is only genuinely better if it moves the whole curve toward
 * the origin rather than sliding along it. A single published number cannot express that, which
 * is exactly why every vendor publishes one.
 *
 * THE X-AXIS IS SQUARE-ROOT SCALED. Every adaptive setting lands at or near 0% and the fixed
 * thresholds bunch at 93-100%, so on a linear axis the entire adaptive family occupies two
 * pixels at the left edge — the headline result becomes the least legible mark on the chart. The
 * root scale expands the region where the differences are, keeps 0 at 0 and 100% at 100%, and is
 * labelled on the axis. No plotted value changes.
 *
 * Hand-drawn SVG: ~130 lines, no dependency, and the decisions that matter here — the scale, the
 * shaded corner, labels placed off their own lines — are the ones a charting library makes
 * hardest to override.
 */

const PAD = { top: 26, right: 26, bottom: 50, left: 54 }

export function TradeoffChart({
  results, live, height = 320,
}: { results: BenchResult[]; live?: BenchResult | null; height?: number }) {
  const width = 620
  const plotW = width - PAD.left - PAD.right
  const plotH = height - PAD.top - PAD.bottom

  const points = results.filter((r) => Number.isFinite(r.median_latency_ms))
  const all = live ? [...points, live] : points
  if (!all.length) return <div className="empty">no results</div>

  const maxLatency = Math.max(900, ...all.map((r) => r.median_latency_ms)) * 1.08
  const x = (rate: number) => PAD.left + Math.sqrt(Math.max(0, rate)) * plotW
  const y = (ms: number) => PAD.top + plotH - (ms / maxLatency) * plotH

  const fixed = points.filter((r) => r.label.startsWith('fixed'))
  const adaptive = points.filter((r) => r.label.startsWith('adaptive'))

  const path = (series: BenchResult[]) =>
    series
      .slice()
      .sort((a, b) => a.median_latency_ms - b.median_latency_ms)
      .map((r, i) => `${i ? 'L' : 'M'}${x(r.false_cutoff_rate).toFixed(1)},${y(r.median_latency_ms).toFixed(1)}`)
      .join(' ')

  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((f) => Math.round((maxLatency * f) / 50) * 50)
  const xTicks = [0, 0.05, 0.2, 0.5, 1]
  const baseline = fixed.find((r) => r.label === 'fixed 700ms')
  const best = adaptive.slice().sort((a, b) => a.median_latency_ms - b.median_latency_ms)[0]

  return (
    <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height} role="img"
         aria-label="Reply speed against how often the agent interrupts">
      <defs>
        <linearGradient id="better" x1="0" y1="1" x2="1" y2="0">
          <stop offset="0%" stopColor="#35e0d0" stopOpacity="0.13" />
          <stop offset="100%" stopColor="#35e0d0" stopOpacity="0" />
        </linearGradient>
        <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="3" result="b" />
          <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
        </filter>
      </defs>

      {/* The direction of improvement is the one thing a reader must take away, and a legend
          entry does not achieve it. */}
      <rect x={PAD.left} y={PAD.top + plotH * 0.5} width={plotW * 0.3} height={plotH * 0.5} fill="url(#better)" />
      <text x={PAD.left + 7} y={PAD.top + plotH - 9} fill="#35e0d0" fontSize="9.5" fontWeight="700" letterSpacing="0.11em" opacity="0.7">BETTER</text>

      {yTicks.map((ms) => (
        <g key={ms}>
          <line x1={PAD.left} y1={y(ms)} x2={width - PAD.right} y2={y(ms)} stroke="#1e232d" />
          <text x={PAD.left - 8} y={y(ms) + 3.5} fill="#4b5668" fontSize="10" textAnchor="end" fontFamily="var(--mono)">{ms}</text>
        </g>
      ))}
      {xTicks.map((rate) => (
        <g key={rate}>
          <line x1={x(rate)} y1={PAD.top} x2={x(rate)} y2={PAD.top + plotH} stroke="#14181f" />
          <text x={x(rate)} y={height - PAD.bottom + 17} fill="#4b5668" fontSize="10" textAnchor="middle" fontFamily="var(--mono)">{Math.round(rate * 100)}%</text>
        </g>
      ))}

      <text x={PAD.left + plotW / 2} y={height - 20} fill="#9aa7ba" fontSize="11" textAnchor="middle">
        how often the agent talks over the caller
      </text>
      <text x={PAD.left + plotW / 2} y={height - 7} fill="#4b5668" fontSize="9.5" textAnchor="middle">
        square-root scale, to separate the settings bunched near zero
      </text>
      <text x={13} y={PAD.top + plotH / 2} fill="#9aa7ba" fontSize="11" textAnchor="middle"
            transform={`rotate(-90 13 ${PAD.top + plotH / 2})`}>
        reply delay (ms)
      </text>

      <path d={path(fixed)} fill="none" stroke="#ffb340" strokeWidth="1.6" strokeOpacity="0.5" strokeDasharray="5 4" />
      <path d={path(adaptive)} fill="none" stroke="#35e0d0" strokeWidth="2.4" filter="url(#glow)" />

      {fixed.map((r) => (
        <circle key={r.label} cx={x(r.false_cutoff_rate)} cy={y(r.median_latency_ms)} r="3.4" fill="#08090c" stroke="#ffb340" strokeWidth="1.7">
          <title>{`${r.label}: ${r.median_latency_ms.toFixed(0)}ms, interrupts ${(r.false_cutoff_rate * 100).toFixed(1)}%`}</title>
        </circle>
      ))}
      {adaptive.map((r) => (
        <circle key={r.label} cx={x(r.false_cutoff_rate)} cy={y(r.median_latency_ms)} r="4" fill="#35e0d0">
          <title>{`${r.label}: ${r.median_latency_ms.toFixed(0)}ms, interrupts ${(r.false_cutoff_rate * 100).toFixed(1)}%`}</title>
        </circle>
      ))}

      {/* Labels sit clear of their own lines. Drawn level with the marker they read as a
          strikethrough, which is what the first version of this chart did. */}
      {best && (
        <text x={x(best.false_cutoff_rate) + 13} y={y(best.median_latency_ms) - 11} fill="#35e0d0" fontSize="10.5" fontWeight="600">
          dialtone — never interrupts
        </text>
      )}
      {baseline && (
        <g>
          <circle cx={x(baseline.false_cutoff_rate)} cy={y(baseline.median_latency_ms)} r="8" fill="none" stroke="#ffb340" strokeWidth="1" strokeOpacity="0.45" />
          <text x={x(baseline.false_cutoff_rate) + 13} y={y(baseline.median_latency_ms) - 23} fill="#ffb340" fontSize="10.5" fontWeight="600">typical fixed 700ms</text>
          <text x={x(baseline.false_cutoff_rate) + 13} y={y(baseline.median_latency_ms) - 11} fill="#8a7143" fontSize="9.5">
            interrupts {(baseline.false_cutoff_rate * 100).toFixed(1)}% of callers
          </text>
        </g>
      )}

      {live && Number.isFinite(live.median_latency_ms) && (
        <g>
          <circle cx={x(live.false_cutoff_rate)} cy={y(live.median_latency_ms)} r="7" fill="none" stroke="#a78bfa" strokeWidth="2" />
          <circle cx={x(live.false_cutoff_rate)} cy={y(live.median_latency_ms)} r="2.4" fill="#a78bfa" />
          <text x={x(live.false_cutoff_rate) + 12} y={y(live.median_latency_ms) + 16} fill="#a78bfa" fontSize="10.5" fontWeight="600">your setting</text>
        </g>
      )}
    </svg>
  )
}
