/* The chart the whole project argues for.
 *
 * x = false cutoff rate (how often the agent talks over someone)
 * y = median endpoint latency (how long it waits before replying)
 *
 * Both axes are costs, so DOWN AND LEFT IS BETTER, and that is the entire point: any latency
 * number is achievable by sliding along one curve, and a system is only actually better if it
 * moves the whole curve toward the origin. Plotting a single number — which is what every
 * vendor publishes — cannot express that, and hides that the number was bought by interrupting
 * people more often.
 *
 * WHY THE X-AXIS IS SQUARE-ROOT SCALED. Every adaptive configuration lands at or near 0% false
 * cutoff, and the fixed thresholds are bunched at 93–100%. On a linear axis that puts the whole
 * adaptive family inside two pixels at the left edge — the headline result becomes the least
 * legible mark on the chart. A √ scale expands the near-zero region where the differences
 * actually are, keeps 0 at 0 and 100% at 100%, and is labelled on the axis so nobody has to
 * infer it. It does not change any plotted value.
 *
 * Hand-drawn SVG rather than a charting library: the whole thing is ~140 lines, it has no
 * dependency to keep current, and the decisions that matter here — the scale, the "better"
 * corner, labels placed off their own lines — are exactly the ones a general-purpose library
 * makes hardest to override.
 */

import type { BenchmarkResult } from '../api'

interface Props {
  results: BenchmarkResult[]
  /** Optional live point from the interactive slider. */
  live?: BenchmarkResult | null
  height?: number
}

const PAD = { top: 30, right: 30, bottom: 52, left: 60 }

export function TradeoffChart({ results, live, height = 360 }: Props) {
  const width = 660
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

  const path = (series: BenchmarkResult[]) =>
    series
      .slice()
      .sort((a, b) => a.median_latency_ms - b.median_latency_ms)
      .map((r, i) => `${i ? 'L' : 'M'}${x(r.false_cutoff_rate).toFixed(1)},${y(r.median_latency_ms).toFixed(1)}`)
      .join(' ')

  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((f) => Math.round((maxLatency * f) / 50) * 50)
  // Chosen for the √ scale: evenly spaced in the transformed space, so the labels are not
  // crowded at one end.
  const xTicks = [0, 0.05, 0.2, 0.5, 1]

  const baseline = fixed.find((r) => r.label === 'fixed 700ms')
  const bestAdaptive = adaptive.slice().sort((a, b) => a.median_latency_ms - b.median_latency_ms)[0]

  return (
    <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height} role="img"
         aria-label="Endpoint latency against false cutoff rate">
      <defs>
        <linearGradient id="better" x1="0" y1="1" x2="1" y2="0">
          <stop offset="0%" stopColor="#35e0d0" stopOpacity="0.14" />
          <stop offset="100%" stopColor="#35e0d0" stopOpacity="0" />
        </linearGradient>
        <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="3.2" result="b" />
          <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
        </filter>
      </defs>

      {/* The "better" corner. Shaded because the direction of improvement is the one thing a
          reader must take away, and a legend entry does not achieve that. */}
      <rect x={PAD.left} y={PAD.top + plotH * 0.5} width={plotW * 0.3} height={plotH * 0.5}
            fill="url(#better)" />
      <text x={PAD.left + 7} y={PAD.top + plotH - 9} fill="#35e0d0" fontSize="10"
            fontWeight="700" letterSpacing="0.1em" opacity="0.75">BETTER</text>

      {yTicks.map((ms) => (
        <g key={ms}>
          <line x1={PAD.left} y1={y(ms)} x2={width - PAD.right} y2={y(ms)} stroke="#1b232e" />
          <text x={PAD.left - 9} y={y(ms) + 3.5} fill="#55677d" fontSize="10.5" textAnchor="end"
                fontFamily="var(--mono)">{ms}</text>
        </g>
      ))}
      {xTicks.map((rate) => (
        <g key={rate}>
          <line x1={x(rate)} y1={PAD.top} x2={x(rate)} y2={PAD.top + plotH} stroke="#141b24" />
          <text x={x(rate)} y={height - PAD.bottom + 18} fill="#55677d" fontSize="10.5"
                textAnchor="middle" fontFamily="var(--mono)">{Math.round(rate * 100)}%</text>
        </g>
      ))}

      <text x={PAD.left + plotW / 2} y={height - 22} fill="#8ea0b5" fontSize="11.5" textAnchor="middle">
        false cutoff rate — how often the agent talks over the caller
      </text>
      <text x={PAD.left + plotW / 2} y={height - 7} fill="#55677d" fontSize="10" textAnchor="middle">
        square-root scale, to separate the configurations bunched near zero
      </text>
      <text x={14} y={PAD.top + plotH / 2} fill="#8ea0b5" fontSize="11.5" textAnchor="middle"
            transform={`rotate(-90 14 ${PAD.top + plotH / 2})`}>
        median endpoint latency (ms)
      </text>

      <path d={path(fixed)} fill="none" stroke="#ffab3d" strokeWidth="1.75" strokeOpacity="0.5"
            strokeDasharray="5 4" />
      <path d={path(adaptive)} fill="none" stroke="#35e0d0" strokeWidth="2.5" filter="url(#glow)" />

      {fixed.map((r) => (
        <circle key={r.label} cx={x(r.false_cutoff_rate)} cy={y(r.median_latency_ms)} r="3.6"
                fill="#07090c" stroke="#ffab3d" strokeWidth="1.75">
          <title>{`${r.label}: ${r.median_latency_ms.toFixed(0)}ms, ${(r.false_cutoff_rate * 100).toFixed(1)}% false cutoff`}</title>
        </circle>
      ))}
      {adaptive.map((r) => (
        <circle key={r.label} cx={x(r.false_cutoff_rate)} cy={y(r.median_latency_ms)} r="4.2"
                fill="#35e0d0">
          <title>{`${r.label}: ${r.median_latency_ms.toFixed(0)}ms, ${(r.false_cutoff_rate * 100).toFixed(1)}% false cutoff`}</title>
        </circle>
      ))}

      {/* Both families named on the plot itself. Labels are offset to the RIGHT of their series
          and vertically clear of it — a label drawn along its own line reads as a strikethrough,
          which is what the first version of this chart did. */}
      {bestAdaptive && (
        <text x={x(bestAdaptive.false_cutoff_rate) + 14} y={y(bestAdaptive.median_latency_ms) - 12}
              fill="#35e0d0" fontSize="11" fontWeight="600">
          adaptive — 0% false cutoff
        </text>
      )}
      {baseline && (
        <g>
          <circle cx={x(baseline.false_cutoff_rate)} cy={y(baseline.median_latency_ms)} r="8.5"
                  fill="none" stroke="#ffab3d" strokeWidth="1" strokeOpacity="0.45" />
          {/* Both lines stacked ABOVE the point: the fixed curve passes through it diagonally,
              so anything placed level with the marker crosses the line it belongs to. */}
          <text x={x(baseline.false_cutoff_rate) + 14} y={y(baseline.median_latency_ms) - 24}
                fill="#ffab3d" fontSize="11" fontWeight="600">typical fixed 700ms</text>
          <text x={x(baseline.false_cutoff_rate) + 14} y={y(baseline.median_latency_ms) - 11}
                fill="#8a7143" fontSize="10">
            {(baseline.false_cutoff_rate * 100).toFixed(1)}% of callers talked over
          </text>
        </g>
      )}

      {live && Number.isFinite(live.median_latency_ms) && (
        <g>
          <circle cx={x(live.false_cutoff_rate)} cy={y(live.median_latency_ms)} r="7.5" fill="none"
                  stroke="#a78bfa" strokeWidth="2" />
          <circle cx={x(live.false_cutoff_rate)} cy={y(live.median_latency_ms)} r="2.5" fill="#a78bfa" />
          {/* Placed below the point, because the adaptive family's own label sits above it and
              the two collide at the default slider position. */}
          <text x={x(live.false_cutoff_rate) + 13} y={y(live.median_latency_ms) + 17}
                fill="#a78bfa" fontSize="11" fontWeight="600">your setting</text>
        </g>
      )}
    </svg>
  )
}
