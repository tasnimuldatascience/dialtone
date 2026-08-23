import { useEffect, useState } from 'react'
import { api, type FlowNode, type FlowPayload, type ToolInfo } from '../api'

/* The flow inspector.
 *
 * Laid out in reachability layers rather than as a free-form canvas. A drag-and-drop node editor
 * is the obvious thing to build and it is the wrong thing to look at: what an operator needs to
 * see is which tools are reachable where, and every path a caller can take. Both are properties
 * of the graph, and a hand-arranged canvas obscures them the moment someone moves a box.
 */
export function FlowView() {
  const [flow, setFlow] = useState<FlowPayload | null>(null)
  const [tools, setTools] = useState<ToolInfo[]>([])
  const [focused, setFocused] = useState<string | null>(null)

  useEffect(() => {
    api.flow().then(setFlow).catch(() => undefined)
    api.tools().then((t) => setTools(t.tools)).catch(() => undefined)
  }, [])

  if (!flow) return <div className="empty">loading the flow…</div>

  const layers = buildLayers(flow)
  const byId = new Map(flow.nodes.map((n) => [n.id, n]))
  const focusedNode = focused ? byId.get(focused) : null

  return (
    <div className="page">
      <div className="page-head">
        <div className="eyebrow">Conversation flow</div>
        <h1>The model picks the words. The graph picks what is possible.</h1>
        <p>
          A prompt cannot give you determinism where it matters, testability, or an answer to
          “what was it doing when the call went wrong?”. A graph can: every transition is either
          triggered deterministically or proposed by the model and validated against the declared
          edges, so a hallucinated transition gets a refusal rather than a state change.
        </p>
      </div>

      <div className="grid grid-4" style={{ marginBottom: 18 }}>
        <Stat label="Nodes" value={String(flow.nodes.length)} sub={flow.name} accent="var(--latency)" />
        <Stat label="Distinct paths" value={String(flow.paths.length)}
              sub="each one is a conversation you can assert on" accent="var(--violet)" />
        <Stat label="Structural problems" value={String(flow.problems.length)}
              sub={flow.problems.length ? 'this flow would be refused' : 'validated before it can load'}
              accent={flow.problems.length ? 'var(--bad)' : 'var(--good)'} />
        <Stat label="Tools" value={String(tools.length)}
              sub={`${flow.global_tools.length} reachable everywhere`} accent="var(--cost)" />
      </div>

      {flow.problems.length > 0 && (
        <div className="panel" style={{ marginBottom: 16, borderColor: '#46212b' }}>
          <h2 className="panel-title" style={{ color: 'var(--bad)' }}>This flow would not load</h2>
          {flow.problems.map((p) => (
            <div key={p} className="num" style={{ fontSize: 12.5, color: 'var(--bad)' }}>— {p}</div>
          ))}
        </div>
      )}

      <div className="panel" style={{ marginBottom: 16 }}>
        <h2 className="panel-title">The graph</h2>
        <p className="panel-note">
          Arranged by distance from the start. Click a node to see what it may do — and, more to
          the point, what it may not.
        </p>
        <div className="scroll-x">
          <div style={{ display: 'flex', gap: 18, alignItems: 'flex-start', minWidth: 'min-content', paddingBottom: 6 }}>
            {layers.map((layer, index) => (
              <div key={index} style={{ display: 'flex', flexDirection: 'column', gap: 12, minWidth: 186 }}>
                <div className="eyebrow" style={{ marginBottom: 0 }}>step {index + 1}</div>
                {layer.map((node) => (
                  <button key={node.id} className="node-card" data-kind={node.kind}
                          onClick={() => setFocused(node.id === focused ? null : node.id)}
                          style={{
                            textAlign: 'left',
                            font: 'inherit',
                            cursor: 'pointer',
                            outline: node.id === focused ? '1px solid var(--latency)' : 'none',
                          }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, justifyContent: 'space-between' }}>
                      <span className="node-id">{node.id}</span>
                      <span className="chip" data-tone={toneFor(node.kind)}>{node.kind}</span>
                    </div>
                    <div className="node-obj clamp-3">{node.objective}</div>
                    {node.tools.length > 0 && (
                      <div style={{ marginTop: 8, display: 'flex', gap: 5, flexWrap: 'wrap' }}>
                        {node.tools.map((t) => (
                          <span key={t} className="chip" data-tone="violet">{t}</span>
                        ))}
                      </div>
                    )}
                  </button>
                ))}
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="grid grid-2" style={{ alignItems: 'start' }}>
        <div className="panel">
          <h2 className="panel-title">
            {focusedNode ? `${focusedNode.id} — what it may do` : 'Every path a caller can take'}
          </h2>
          {focusedNode ? (
            <>
              <p className="panel-note">{focusedNode.objective}</p>
              <Detail label="Tools reachable here"
                      value={[...focusedNode.tools, ...flow.global_tools].join(', ') || 'none'} />
              {focusedNode.collects && (
                <Detail label="Must collect before leaving" value={focusedNode.collects} />
              )}
              {focusedNode.pattern && <Detail label="Accepted form" value={focusedNode.pattern} />}
              <Detail label="Re-asks before escalating" value={String(focusedNode.max_attempts)} />
              <div className="eyebrow" style={{ marginTop: 16 }}>Legal transitions</div>
              {focusedNode.edges.length === 0 && (
                <div style={{ color: 'var(--text-faint)', fontSize: 13 }}>
                  terminal — the call ends or leaves the agent here
                </div>
              )}
              {focusedNode.edges.map((edge) => (
                <div key={edge.to} style={{ padding: '7px 0', borderBottom: '1px solid #131a22' }}>
                  <span className="num" style={{ color: 'var(--latency)' }}>→ {edge.to}</span>
                  <div style={{ color: 'var(--text-dim)', fontSize: 12.5 }}>{edge.when}</div>
                </div>
              ))}
              <div className="callout" style={{ marginTop: 16 }}>
                Anything not listed above is <strong>refused</strong>, not discouraged. A tool
                absent from the schema cannot be called by a model that has decided it should be.
              </div>
            </>
          ) : (
            <>
              <p className="panel-note">
                This is what a graph buys that a prompt cannot: paths can be enumerated, walked
                and asserted. A prompt's regression suite is a person reading transcripts.
              </p>
              {flow.paths.map((path, index) => (
                <div key={index} className="num"
                     style={{ fontSize: 12.5, padding: '6px 0', borderBottom: '1px solid #131a22' }}>
                  {path.map((step, i) => (
                    <span key={i}>
                      {i > 0 && <span style={{ color: 'var(--text-faint)' }}> → </span>}
                      <span style={{ color: i === path.length - 1 ? 'var(--latency)' : 'var(--text-dim)' }}>
                        {step}
                      </span>
                    </span>
                  ))}
                </div>
              ))}
            </>
          )}
        </div>

        <div className="panel">
          <h2 className="panel-title">Tools, and what each costs the caller in silence</h2>
          <p className="panel-note">
            On a call a slow tool is not a spinner, it is dead air — and dead air is
            indistinguishable from a dropped line. Anything past ~800ms has to be covered with
            speech that starts <em>before</em> the tool does, which is why the class is declared
            rather than measured after the fact.
          </p>
          <div className="scroll-x">
            <table>
              <thead>
                <tr>
                  <th>tool</th>
                  <th>latency</th>
                  <th>retry-safe</th>
                  <th>spoken while it runs</th>
                </tr>
              </thead>
              <tbody>
                {tools.map((tool) => (
                  <tr key={tool.name}>
                    <td className="num">{tool.name}</td>
                    <td>
                      <span className="chip" data-tone={tool.latency === 'slow' || tool.latency === 'background' ? 'cost' : 'latency'}>
                        {tool.latency}
                      </span>
                    </td>
                    <td>
                      {tool.idempotent ? (
                        <span className="chip" data-tone="good">yes</span>
                      ) : (
                        <span className="chip" data-tone="bad">deduped on key</span>
                      )}
                    </td>
                    <td style={{ color: 'var(--text-dim)', fontSize: 12.5 }}>{tool.cover ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  )
}

/** Breadth-first layers from the start node, so the render order reflects reachability. */
function buildLayers(flow: FlowPayload): FlowNode[][] {
  const byId = new Map(flow.nodes.map((n) => [n.id, n]))
  const depth = new Map<string, number>([[flow.start, 0]])
  const queue = [flow.start]

  while (queue.length) {
    const id = queue.shift() as string
    const node = byId.get(id)
    if (!node) continue
    for (const edge of node.edges) {
      // A node keeps its SHALLOWEST depth. Re-ask edges loop backwards, and letting a later
      // visit overwrite the depth would push a node rightward past the nodes it feeds.
      if (!depth.has(edge.to)) {
        depth.set(edge.to, (depth.get(id) ?? 0) + 1)
        queue.push(edge.to)
      }
    }
  }

  const layers: FlowNode[][] = []
  for (const node of flow.nodes) {
    const level = depth.get(node.id) ?? 0
    ;(layers[level] ??= []).push(node)
  }
  return layers.filter(Boolean)
}

function toneFor(kind: string) {
  if (kind === 'end') return 'good'
  if (kind === 'transfer') return 'cost'
  if (kind === 'tool') return 'violet'
  if (kind === 'collect') return 'latency'
  return undefined
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ padding: '7px 0', borderBottom: '1px solid #131a22' }}>
      <div className="stat-label">{label}</div>
      <div className="num" style={{ fontSize: 12.5, marginTop: 2 }}>{value}</div>
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
