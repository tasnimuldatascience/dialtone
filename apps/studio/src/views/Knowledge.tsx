import { useCallback, useEffect, useRef, useState } from 'react'
import type { ViewProps } from '../App'
import { api, type Doc, type Hit, type IndexStats } from '../api'
import { Icon } from '../components/Icon'

/* The company's own information.
 *
 * The search box is not a convenience feature. "Why did it say that?" is the most common question
 * an operator has about an AI agent, and being able to type the caller's question and see exactly
 * which passages the agent would be handed answers it more directly than any log.
 *
 * It is also the only way to find out that a question retrieves NOTHING — which is not a failure,
 * it is the agent correctly declining to guess, and it is invisible from anywhere else.
 */
export function Knowledge({ agent, agentId, toast }: ViewProps) {
  const [docs, setDocs] = useState<Doc[]>([])
  const [index, setIndex] = useState<IndexStats | null>(null)
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [busy, setBusy] = useState(false)
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<Hit[] | null>(null)
  const [searchMs, setSearchMs] = useState(0)
  const fileInput = useRef<HTMLInputElement>(null)

  const load = useCallback(async () => {
    if (!agentId) return
    const { documents, index: stats } = await api.docs(agentId)
    setDocs(documents)
    setIndex(stats)
  }, [agentId])

  useEffect(() => { void load() }, [load])

  const add = async () => {
    if (!title.trim() || !body.trim()) return
    setBusy(true)
    try {
      const result = await api.addDoc(agentId, title.trim(), body.trim())
      toast(`Indexed into ${result.chunks} passage${result.chunks === 1 ? '' : 's'}`)
      setTitle('')
      setBody('')
      await load()
    } catch (error) {
      toast(String(error), 'bad')
    } finally {
      setBusy(false)
    }
  }

  const onFile = async (file: File) => {
    const text = await file.text()
    setTitle(file.name.replace(/\.[^.]+$/, ''))
    setBody(text)
  }

  const remove = async (docId: string) => {
    await api.deleteDoc(agentId, docId)
    await load()
    setHits(null)
    toast('Document removed')
  }

  const search = async () => {
    if (!query.trim()) return
    const result = await api.searchKnowledge(agentId, query.trim())
    setHits(result.hits)
    setSearchMs(result.ms)
  }

  return (
    <div className="page">
      <div className="head">
        <h1>Knowledge</h1>
        <p>
          What {agent?.business ?? 'the agent'} knows. The agent answers only from these documents
          and says it will check when it finds nothing relevant.
        </p>
      </div>

      <div className="grid g4" style={{ marginBottom: 14 }}>
        <Metric k="Documents" v={String(index?.documents ?? 0)} s="uploaded" tone="var(--info)" />
        <Metric k="Passages" v={String(index?.chunks ?? 0)} s="searchable pieces" tone="var(--accent)" />
        <Metric k="Words" v={String(Math.round((index?.characters ?? 0) / 5))} s="approximately" tone="var(--agent)" />
        <Metric
          k="Search index"
          v={index?.embeddings_ready ? 'ready' : 'building'}
          s={index?.embeddings_ready ? 'answers immediately' : 'first use loads the encoder'}
          tone={index?.embeddings_ready ? 'var(--good)' : 'var(--cost)'}
        />
      </div>

      <div className="grid g2" style={{ alignItems: 'start' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div className="card">
            <h2 className="card-h">Add a document</h2>
            <p className="card-sub">
              Opening hours, prices, policies, anything a caller might ask. Plain text or markdown.
            </p>

            <div className="field">
              <label>Title</label>
              <input type="text" value={title} placeholder="Refund policy" onChange={(e) => setTitle(e.target.value)} />
            </div>
            <div className="field">
              <label>Content</label>
              <textarea
                rows={8}
                value={body}
                placeholder={'Write each fact as its own paragraph.\n\nBlank lines separate passages, and the agent quotes them one at a time.'}
                onChange={(e) => setBody(e.target.value)}
              />
              <span className="hint">
                Write numbers the way they are said aloud — "forty five pounds" rather than "£45".
                The agent repeats what it reads, and a voice engine says the two differently.
              </span>
            </div>

            <div className="row">
              <input
                ref={fileInput} type="file" accept=".txt,.md,.csv,.json" style={{ display: 'none' }}
                onChange={(e) => { const f = e.target.files?.[0]; if (f) void onFile(f) }}
              />
              <button className="btn btn-ghost" onClick={() => fileInput.current?.click()}>
                Upload a file
              </button>
              <div className="spacer" />
              <button className="btn btn-primary" onClick={() => void add()} disabled={busy || !title.trim() || !body.trim()}>
                {busy ? 'Indexing…' : 'Add and index'}
              </button>
            </div>
          </div>

          <div className="card card-pad-0">
            <div style={{ padding: '14px 16px 10px' }}>
              <h2 className="card-h">Documents</h2>
              <p className="card-sub" style={{ margin: 0 }}>Each is split into passages the agent can quote.</p>
            </div>
            {docs.length === 0 ? (
              <div className="empty">Nothing uploaded yet.</div>
            ) : (
              <table>
                <thead>
                  <tr><th>Title</th><th>Source</th><th style={{ textAlign: 'right' }}>Passages</th><th /></tr>
                </thead>
                <tbody>
                  {docs.map((d) => (
                    <tr key={d.id}>
                      <td style={{ fontWeight: 540 }}>{d.title}</td>
                      <td><span className="chip">{d.source}</span></td>
                      <td className="n">{d.chunks}</td>
                      <td style={{ width: 40, textAlign: 'right' }}>
                        <button className="btn btn-ghost btn-sm" onClick={() => void remove(d.id)} title="Remove">
                          <Icon name="trash" size={13} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        <div className="card">
          <h2 className="card-h">What would the agent find?</h2>
          <p className="card-sub">
            Type a caller's question and see exactly which passages the agent would be given —
            before a real caller asks it.
          </p>

          <div className="row" style={{ marginBottom: 12 }}>
            <input
              type="text" value={query} placeholder="how much is a check-up?"
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && void search()}
            />
            <button className="btn btn-ghost" onClick={() => void search()} disabled={!query.trim()}>
              <Icon name="search" />
            </button>
          </div>

          {hits === null && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {['how much is a check-up?', 'are you open on saturday?', 'my tooth just broke', 'do you sell dog food?'].map((q) => (
                <button key={q} className="btn btn-ghost btn-sm" onClick={() => { setQuery(q); void api.searchKnowledge(agentId, q).then((r) => { setHits(r.hits); setSearchMs(r.ms) }) }}>
                  {q}
                </button>
              ))}
            </div>
          )}

          {hits !== null && (
            <>
              <div className="row-between" style={{ marginBottom: 10 }}>
                <span style={{ fontSize: 11.5, color: 'var(--text-dim)' }}>
                  {hits.length} passage{hits.length === 1 ? '' : 's'}
                </span>
                <span className="num" style={{ fontSize: 11.5, color: 'var(--text-dim)' }}>{searchMs}ms</span>
              </div>

              {hits.length === 0 ? (
                <div className="note">
                  <b>Nothing relevant.</b> The agent would say it does not know and offer to check —
                  which is the right answer, and the reason retrieval has an absolute relevance
                  floor rather than always returning its best guess.
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 9 }}>
                  {hits.map((h, i) => (
                    <div key={i} style={{ border: '1px solid var(--line)', borderRadius: 'var(--r-sm)', padding: 11, background: 'var(--bg-2)' }}>
                      <div className="row-between" style={{ marginBottom: 5 }}>
                        <span className="chip" data-t="info">{h.document}</span>
                        <span className="row" style={{ gap: 7 }}>
                          <span className="chip">{h.via}</span>
                          <span className="num" style={{ fontSize: 11.5, color: 'var(--accent)' }}>{h.score.toFixed(2)}</span>
                        </span>
                      </div>
                      <div style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.55 }}>{h.text}</div>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
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
