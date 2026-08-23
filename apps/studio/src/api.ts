/* Typed client for the gateway.
 *
 * Every shape here mirrors a Python dataclass on the other side. They are written out rather
 * than generated because the surface is small and a generator is another thing to keep running;
 * if this grew past ~15 endpoints the tradeoff would flip.
 */

export interface BenchmarkResult {
  label: string
  false_cutoff_rate: number
  median_latency_ms: number
  p90_latency_ms: number
  completion_recall: number
  n_complete: number
  n_incomplete: number
  failures: string[]
}

export interface CorpusItem {
  id: string
  transcript: string
  complete: boolean
  pause_ms: number
  note: string
  completion_score: number
  reason: string
}

export interface FlowEdge { to: string; when: string; condition: string | null }

export interface FlowNode {
  id: string
  kind: 'speak' | 'collect' | 'branch' | 'tool' | 'transfer' | 'end'
  objective: string
  collects: string | null
  pattern: string | null
  tools: string[]
  max_attempts: number
  edges: FlowEdge[]
}

export interface FlowPayload {
  name: string
  start: string
  global_tools: string[]
  nodes: FlowNode[]
  problems: string[]
  paths: string[][]
}

export interface ToolInfo {
  name: string
  description: string
  input_schema: Record<string, unknown>
  latency: 'instant' | 'fast' | 'slow' | 'background'
  idempotent: boolean
  cover: string | null
}

export interface Finding {
  rule: string
  sensitivity: 'strip' | 'tag'
  start: number
  end: number
  preview: string
}

export interface RedactResult { text: string; clean: boolean; findings: Finding[] }

export interface Scenario { id: string; title: string; description: string; turns: number }

export interface CallEvent {
  at_ms: number
  kind: 'endpoint' | 'reply' | 'spoke' | 'barge_in' | 'backchannel' | 'false_cutoff'
  [key: string]: unknown
}

export interface CallSummary {
  turns: number
  median_endpoint_ms: number
  baseline_median_ms: number
  speedup: number
  false_cutoffs: number
  interruptions: number
  backchannels: number
  redactions: number
  packet_loss: number
}

export interface CallResult {
  scenario: { id: string; title: string; description: string }
  events: CallEvent[]
  transcript: { role: string; content: string }[]
  redactions: { at_ms: number; rules: string[]; safe_text: string }[]
  summary: CallSummary
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(path)
  if (!response.ok) throw new Error(`${path} → ${response.status}`)
  return response.json() as Promise<T>
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error(`${path} → ${response.status}`)
  return response.json() as Promise<T>
}

export const api = {
  ablation: () => get<{ results: BenchmarkResult[] }>('/api/benchmark/ablation'),
  sweep: () => get<{ results: BenchmarkResult[] }>('/api/benchmark/sweep'),
  corpus: () => get<{ items: CorpusItem[] }>('/api/benchmark/corpus'),
  custom: (body: { base_silence_ms: number; enable_semantic: boolean; enable_prosody: boolean }) =>
    post<{ result: BenchmarkResult }>('/api/benchmark/custom', body),
  flow: () => get<FlowPayload>('/api/flow'),
  tools: () => get<{ tools: ToolInfo[] }>('/api/tools'),
  redact: (text: string) => post<RedactResult>('/api/redact', { text }),
  scenarios: () => get<{ scenarios: Scenario[] }>('/api/calls/scenarios'),
  runCall: (id: string) => post<CallResult>(`/api/calls/${id}/run`, {}),
}

/** Live call monitor. Returns a disposer; the caller owns the socket's lifetime. */
export function watchCall(
  scenarioId: string,
  onMessage: (message: Record<string, unknown>) => void,
  onError?: (error: string) => void,
): () => void {
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws'
  const socket = new WebSocket(`${protocol}://${location.host}/ws/call/${scenarioId}`)
  socket.onmessage = (event) => onMessage(JSON.parse(event.data as string))
  socket.onerror = () => onError?.('lost connection to the gateway')
  return () => {
    // readyState is checked because closing a socket still in CONNECTING throws in Safari,
    // and this disposer runs on every unmount including the one React 18 StrictMode fires
    // immediately after mount.
    if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
      socket.close()
    }
  }
}
