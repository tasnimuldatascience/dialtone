import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ViewProps } from '../App'
import { api, type FlowNode, type FlowPayload } from '../api'
import { Icon } from '../components/Icon'

/* The conversation graph, on a canvas you can drag.
 *
 * WHAT THE GRAPH IS FOR. The model chooses the words; the graph chooses what is possible. Which
 * tools exist at this step, which transitions are legal, what has to be collected before moving
 * on. A model that proposes a step the graph does not declare gets a refusal rather than a state
 * change, and that refusal is visible on the call afterwards.
 *
 * LAYOUT IS COMPUTED, THEN DRAGGABLE. Nodes are placed by breadth-first depth from the start, so
 * a flow that has never been opened still reads correctly. Dragging is for rearranging, not for
 * doing the layout by hand — a graph whose shape depends on someone having tidied it is a graph
 * that looks broken to the next person.
 */

const KIND_COLOUR: Record<string, string> = {
  speak: '#647085',
  collect: '#35e0d0',
  branch: '#60a5fa',
  tool: '#a78bfa',
  transfer: '#ffb340',
  end: '#4ade80',
}

interface Placed extends FlowNode { x: number; y: number }

export function FlowStudio({ agentId }: ViewProps) {
  const [flow, setFlow] = useState<FlowPayload | null>(null)
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({})
  const [selected, setSelected] = useState<string | null>(null)
  const dragging = useRef<{ id: string; dx: number; dy: number } | null>(null)
  const canvas = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!agentId) return
    void api.flow(agentId).then((f) => {
      setFlow(f)
      setPositions(autoLayout(f))
      setSelected(null)
    })
  }, [agentId])

  const nodes: Placed[] = useMemo(
    () => (flow?.nodes ?? []).map((n) => ({ ...n, ...(positions[n.id] ?? { x: 40, y: 40 }) })),
    [flow, positions],
  )

  const onMove = useCallback((e: React.MouseEvent) => {
    const drag = dragging.current
    const box = canvas.current?.getBoundingClientRect()
    if (!drag || !box) return
    setPositions((p) => ({
      ...p,
      [drag.id]: {
        x: Math.max(8, e.clientX - box.left + (canvas.current?.scrollLeft ?? 0) - drag.dx),
        y: Math.max(8, e.clientY - box.top + (canvas.current?.scrollTop ?? 0) - drag.dy),
      },
    }))
  }, [])

  const node = nodes.find((n) => n.id === selected) ?? null
  const problems = flow?.problems ?? []

  const height = Math.max(460, ...nodes.map((n) => n.y + 130))
  const width = Math.max(900, ...nodes.map((n) => n.x + 220))

  return (
    <div className="page page-wide">
      <div className="head row-between">
        <div>
          <h1>Conversation flow</h1>
          <p>
            The model picks the words. This picks what is possible — which tools exist at each
            step, and which moves are legal.
          </p>
        </div>
        <div className="row">
          <span className="chip" data-t={problems.length ? 'bad' : 'good'}>
            {problems.length ? `${problems.length} problem${problems.length === 1 ? '' : 's'}` : 'valid'}
          </span>
          <button className="btn btn-ghost btn-sm" onClick={() => flow && setPositions(autoLayout(flow))}>
            Tidy up
          </button>
        </div>
      </div>

      {problems.length > 0 && (
        <div className="note" data-t="bad" style={{ marginBottom: 12 }}>
          <b>This flow would be refused.</b> Every problem below is a call that would dead-end on a
          customer.
          {problems.map((p) => <div key={p} className="num" style={{ fontSize: 11.5, marginTop: 4 }}>— {p}</div>)}
        </div>
      )}

      <div className="grid" style={{ gridTemplateColumns: '1fr 316px', alignItems: 'start' }}>
        <div
          className="canvas"
          ref={canvas}
          // Sized to the graph, capped to the viewport. A fixed viewport-height canvas leaves
          // a screen of empty grid under a six-node flow, which reads as something failing to
          // load rather than as space.
          style={{ height: Math.min(height + 24, window.innerHeight - 250), minHeight: 380 }}
          onMouseMove={onMove}
          onMouseUp={() => { dragging.current = null }}
          onMouseLeave={() => { dragging.current = null }}
        >
          <div style={{ position: 'relative', width, height }}>
            <svg width={width} height={height} style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
              <defs>
                <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
                  <path d="M0 0 L10 5 L0 10 z" fill="#2a303c" />
                </marker>
              </defs>
              {nodes.flatMap((n) =>
                n.edges.map((edge) => {
                  const to = nodes.find((m) => m.id === edge.to)
                  if (!to) return null
                  const active = selected === n.id || selected === to.id
                  // Cubic curve out of the right edge into the left edge. Straight lines cross
                  // each other constantly on a graph with re-ask loops; curves stay readable.
                  const x1 = n.x + 178
                  const y1 = n.y + 30
                  const x2 = to.x
                  const y2 = to.y + 30
                  const mid = Math.max(40, Math.abs(x2 - x1) / 2)
                  return (
                    <path
                      key={`${n.id}-${edge.to}`}
                      d={`M${x1},${y1} C${x1 + mid},${y1} ${x2 - mid},${y2} ${x2},${y2}`}
                      fill="none"
                      stroke={active ? '#35e0d0' : '#2a303c'}
                      strokeWidth={active ? 1.8 : 1.2}
                      markerEnd="url(#arrow)"
                    />
                  )
                }),
              )}
            </svg>

            {nodes.map((n) => (
              <div
                key={n.id}
                className="node"
                data-sel={selected === n.id}
                style={{ left: n.x, top: n.y }}
                onMouseDown={(e) => {
                  const box = canvas.current?.getBoundingClientRect()
                  if (!box) return
                  dragging.current = {
                    id: n.id,
                    dx: e.clientX - box.left + (canvas.current?.scrollLeft ?? 0) - n.x,
                    dy: e.clientY - box.top + (canvas.current?.scrollTop ?? 0) - n.y,
                  }
                  setSelected(n.id)
                }}
              >
                <div className="node-t">
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{n.id}</span>
                  <i className="node-kind" style={{ background: KIND_COLOUR[n.kind] }} title={n.kind} />
                </div>
                <div className="node-o">{n.objective}</div>
                {n.tools.length > 0 && (
                  <div style={{ marginTop: 7, display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                    {n.tools.map((t) => <span key={t} className="chip" data-t="agent" style={{ fontSize: 10 }}>{t}</span>)}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div className="panel">
            <div className="panel-h">
              <Icon name="flow" size={13} />
              {node ? node.id : 'Select a step'}
            </div>
            <div className="panel-b">
              {!node && (
                <div style={{ fontSize: 12.5, color: 'var(--text-dim)', lineHeight: 1.6 }}>
                  Click a box to see what it may do — and, more to the point, what it may not.
                  Drag to rearrange.
                </div>
              )}
              {node && (
                <>
                  <div style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.6, marginBottom: 12 }}>
                    {node.objective}
                  </div>

                  <Line k="Kind" v={node.kind} />
                  {node.collects && <Line k="Must collect" v={node.collects} />}
                  {node.pattern && <Line k="Accepted form" v={node.pattern} />}
                  <Line k="Re-asks before escalating" v={String(node.max_attempts)} />
                  <Line k="Tools here" v={[...node.tools, ...(flow?.global_tools ?? [])].join(', ') || 'none'} />

                  <div style={{ fontSize: 10.5, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--text-3)', fontWeight: 620, marginTop: 14, marginBottom: 6 }}>
                    Legal moves
                  </div>
                  {node.edges.length === 0 && (
                    <div style={{ fontSize: 12.5, color: 'var(--text-dim)' }}>
                      Terminal — the call ends or leaves the agent here.
                    </div>
                  )}
                  {node.edges.map((edge) => (
                    <button
                      key={edge.to}
                      onClick={() => setSelected(edge.to)}
                      style={{ display: 'block', width: '100%', textAlign: 'left', background: 'none', border: 0, borderBottom: '1px solid #12161d', padding: '7px 0', cursor: 'pointer', font: 'inherit', color: 'inherit' }}
                    >
                      <span className="num" style={{ color: 'var(--accent)', fontSize: 12 }}>→ {edge.to}</span>
                      <div style={{ fontSize: 11.5, color: 'var(--text-dim)' }}>{edge.when}</div>
                    </button>
                  ))}

                  <div className="note" style={{ marginTop: 14, fontSize: 11.5 }}>
                    Anything not listed is <b>refused</b>, not discouraged. A tool absent from the
                    list cannot be called by a model that has decided it should be.
                  </div>
                </>
              )}
            </div>
          </div>

          <div className="panel">
            <div className="panel-h">Every path a caller can take</div>
            <div className="panel-b">
              {(flow?.paths ?? []).map((path, i) => (
                <div key={i} className="num" style={{ fontSize: 11.5, padding: '4px 0', borderBottom: '1px solid #12161d', color: 'var(--text-dim)' }}>
                  {path.map((step, j) => (
                    <span key={j}>
                      {j > 0 && ' → '}
                      <span style={{ color: j === path.length - 1 ? 'var(--accent)' : 'var(--text-2)' }}>{step}</span>
                    </span>
                  ))}
                </div>
              ))}
              <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 10, lineHeight: 1.55 }}>
                This is what a graph buys that a prompt cannot: paths can be listed, walked and
                asserted on.
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

function Line({ k, v }: { k: string; v: string }) {
  return (
    <div style={{ padding: '6px 0', borderBottom: '1px solid #12161d' }}>
      <div style={{ fontSize: 10.5, color: 'var(--text-3)', textTransform: 'uppercase', letterSpacing: '.07em' }}>{k}</div>
      <div className="num" style={{ fontSize: 12, marginTop: 2, wordBreak: 'break-word' }}>{v}</div>
    </div>
  )
}

/** Breadth-first columns from the start node. A node keeps its SHALLOWEST depth so a re-ask loop
 *  does not push a step rightwards past the one it feeds. */
function autoLayout(flow: FlowPayload): Record<string, { x: number; y: number }> {
  const byId = new Map(flow.nodes.map((n) => [n.id, n]))
  const depth = new Map<string, number>([[flow.start, 0]])
  const queue = [flow.start]

  while (queue.length) {
    const id = queue.shift() as string
    for (const edge of byId.get(id)?.edges ?? []) {
      if (!depth.has(edge.to)) {
        depth.set(edge.to, (depth.get(id) ?? 0) + 1)
        queue.push(edge.to)
      }
    }
  }

  const columns = new Map<number, string[]>()
  for (const n of flow.nodes) {
    const level = depth.get(n.id) ?? 0
    columns.set(level, [...(columns.get(level) ?? []), n.id])
  }

  const out: Record<string, { x: number; y: number }> = {}
  for (const [level, ids] of columns) {
    ids.forEach((id, row) => {
      out[id] = { x: 40 + level * 232, y: 30 + row * 128 }
    })
  }
  return out
}
