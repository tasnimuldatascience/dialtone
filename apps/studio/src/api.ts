/* Typed client for the gateway.
 *
 * Written out rather than generated: the surface is ~35 endpoints and a generator is another
 * thing to keep running. If it doubles, that trade flips.
 */

export interface Health {
  ok: boolean
  status: 'starting' | 'ready'
  model: string
  warm_seconds: number
  live_calls: number
  version: string
}

export interface Overview {
  calls: number
  live: number
  agents: number
  documents: number
  resolved: number
  escalated: number
  abandoned: number
  containment: number
  escalation_rate: number
  avg_duration_ms: number
  median_turn_ms: number
  p90_turn_ms: number
  by_day: { day: string; calls: number; resolved: number }[]
  sentiment: Record<string, number>
  status: string
}

export interface Agent {
  id: string
  name: string
  business: string
  persona: string
  greeting: string
  voice: string
  temperature: number
  use_knowledge: boolean
  status: string
  created_at: string
  updated_at: string
  call_count?: number
  doc_count?: number
  flow?: FlowPayload | null
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
  problems?: string[]
  paths?: string[][]
}

export interface Doc {
  id: string
  title: string
  source: string
  chunks: number
  size?: number
  created_at: string
}

export interface IndexStats {
  documents: number
  chunks: number
  characters: number
  embeddings_ready: boolean
}

export interface Hit { document: string; document_id: string; text: string; score: number; via: string }

export interface PhoneNumber {
  id: string
  e164: string
  label: string
  agent_id: string | null
  agent_name: string | null
  provider: string
}

export interface CallRow {
  id: string
  agent_id: string
  agent_name: string
  direction: string
  from_number: string
  started_at: string
  ended_at: string | null
  duration_ms: number
  outcome: string
  resolved: number
  escalated: number
  sentiment: string
  summary: string
  channel: string
  turn_count: number
}

export interface Timing {
  redact?: number
  knowledge?: number
  think?: number
  speak?: number
  tools?: number
  total_ms: number
}

export interface GroundingFinding { value: number; context: string; kind: string }
export interface Grounding { ok: boolean; verified: number[]; hedged: boolean; findings: GroundingFinding[] }

export interface CallTurn {
  ordinal: number
  caller: string
  agent: string
  spoken: string
  node: string
  moved_to: string
  timing: Timing
  citations: Hit[]
  tools: { name: string; ok: boolean; ms: number }[]
  redacted: string[]
  refused: string
  grounding?: Grounding
}

export interface CallDetail extends CallRow { turns: CallTurn[] }

export interface Campaign {
  id: string
  agent_id: string
  agent_name: string
  name: string
  status: string
  contacts: number
  reached: number
}

export interface BenchResult {
  label: string
  false_cutoff_rate: number
  median_latency_ms: number
  p90_latency_ms: number
  completion_recall: number
  n_complete: number
  n_incomplete: number
  failures: string[]
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: init?.body
      ? { 'content-type': 'application/json', ...(init?.headers ?? {}) }
      : init?.headers,
  })
  if (!res.ok) {
    let detail = String(res.status)
    try {
      detail = JSON.stringify((await res.json()).detail ?? detail)
    } catch {
      /* the body was not JSON; the status code is all we have */
    }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

const get = <T,>(p: string) => req<T>(p)
const post = <T,>(p: string, body?: unknown) => req<T>(p, { method: 'POST', body: JSON.stringify(body ?? {}) })
const patch = <T,>(p: string, body: unknown) => req<T>(p, { method: 'PATCH', body: JSON.stringify(body) })
const put = <T,>(p: string, body: unknown) => req<T>(p, { method: 'PUT', body: JSON.stringify(body) })
const del = <T,>(p: string) => req<T>(p, { method: 'DELETE' })

export const api = {
  health: () => get<Health>('/api/health'),
  overview: () => get<Overview>('/api/overview'),

  agents: () => get<{ agents: Agent[] }>('/api/agents'),
  agent: (id: string) => get<Agent>(`/api/agents/${id}`),
  createAgent: (body: Partial<Agent>) => post<Agent>('/api/agents', body),
  updateAgent: (id: string, body: Partial<Agent>) => patch<Agent>(`/api/agents/${id}`, body),
  deleteAgent: (id: string) => del<{ deleted: boolean }>(`/api/agents/${id}`),

  flow: (id: string) => get<FlowPayload>(`/api/agents/${id}/flow`),
  saveFlow: (id: string, body: FlowPayload) =>
    put<{ saved: boolean; paths: string[][] }>(`/api/agents/${id}/flow`, body),

  docs: (id: string) => get<{ documents: Doc[]; index: IndexStats }>(`/api/agents/${id}/documents`),
  addDoc: (id: string, title: string, body: string) =>
    post<Doc & { index: IndexStats }>(`/api/agents/${id}/documents`, { title, body }),
  deleteDoc: (id: string, docId: string) =>
    del<{ deleted: boolean }>(`/api/agents/${id}/documents/${docId}`),
  searchKnowledge: (id: string, query: string) =>
    post<{ query: string; ms: number; hits: Hit[] }>(`/api/agents/${id}/knowledge/search`, { query }),

  numbers: () => get<{ numbers: PhoneNumber[] }>('/api/numbers'),
  addNumber: (e164: string, label: string, agent_id: string | null) =>
    post<PhoneNumber>('/api/numbers', { e164, label, agent_id }),
  assignNumber: (id: string, agent_id: string | null) =>
    patch<{ assigned: boolean }>(`/api/numbers/${id}`, { agent_id }),

  calls: (params: { agent_id?: string; limit?: number; outcome?: string } = {}) => {
    const entries = Object.entries(params).filter(([, v]) => v != null).map(([k, v]) => [k, String(v)])
    const q = new URLSearchParams(entries as [string, string][])
    return get<{ calls: CallRow[] }>(`/api/calls${q.toString() ? `?${q}` : ''}`)
  },
  call: (id: string) => get<CallDetail>(`/api/calls/${id}`),
  startCall: (agentId: string, channel = 'text') =>
    post<{ call_id: string; greeting: string }>('/api/calls', { agent_id: agentId, channel }),

  campaigns: () => get<{ campaigns: Campaign[] }>('/api/campaigns'),
  createCampaign: (agentId: string, name: string) =>
    post<Campaign>('/api/campaigns', { agent_id: agentId, name }),
  addContacts: (id: string, contacts: { name: string; e164: string }[]) =>
    post<{ added: number }>(`/api/campaigns/${id}/contacts`, { contacts }),

  ablation: () => get<{ results: BenchResult[] }>('/api/benchmark/ablation'),
  sweep: () => get<{ results: BenchResult[] }>('/api/benchmark/sweep'),
  benchCustom: (b: { base_silence_ms: number; enable_semantic: boolean; enable_prosody: boolean }) =>
    post<{ result: BenchResult }>('/api/benchmark/custom', b),
  benchScore: (text: string) =>
    get<{ text: string; completion: number; reason: string; threshold_ms: number; reading: string }>(
      `/api/benchmark/score?text=${encodeURIComponent(text)}`,
    ),
  corpus: () =>
    get<{ items: { id: string; transcript: string; complete: boolean; completion_score: number; reason: string; note: string }[] }>(
      '/api/benchmark/corpus',
    ),

  redact: (text: string) =>
    post<{ text: string; clean: boolean; findings: { rule: string; sensitivity: string; start: number; end: number; preview: string }[] }>(
      '/api/redact',
      { text },
    ),
  tools: () =>
    get<{ tools: { name: string; description: string; latency: string; idempotent: boolean; cover: string | null }[] }>(
      '/api/tools',
    ),
}

/** Open the live-call socket. The caller owns its lifetime. */
export function openCall(
  callId: string,
  onEvent: (e: Record<string, unknown>) => void,
  onClose?: () => void,
) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const ws = new WebSocket(`${proto}://${location.host}/ws/call/${callId}`)
  ws.onmessage = (e) => onEvent(JSON.parse(e.data as string))
  ws.onclose = () => onClose?.()
  return {
    say: (text: string) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'say', text }))
    },
    hangup: () => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'hangup' }))
    },
    close: () => {
      // CONNECTING is checked too: closing a socket mid-handshake throws in Safari, and this
      // runs on every unmount including the one React StrictMode fires right after mount.
      if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) ws.close()
    },
  }
}
