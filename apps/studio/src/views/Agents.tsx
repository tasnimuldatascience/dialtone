import { useEffect, useState } from 'react'
import type { ViewProps } from '../App'
import { api, type Agent } from '../api'
import { Icon } from '../components/Icon'

/* Configuring an agent.
 *
 * Everything here is what the agent IS — who it says it is, how it opens, how varied its wording
 * is allowed to be. What it may DO lives in the flow, and what it KNOWS lives in the knowledge
 * base. Keeping the three apart is what makes each one reviewable on its own.
 */
export function Agents({ agents, agentId, setAgentId, reloadAgents, toast, navigate }: ViewProps) {
  const [draft, setDraft] = useState<Agent | null>(null)
  const [saving, setSaving] = useState(false)

  const selected = agents.find((a) => a.id === agentId) ?? null

  useEffect(() => {
    setDraft(selected ? { ...selected } : null)
  }, [selected?.id, selected?.updated_at])

  const dirty =
    draft && selected
      ? (['name', 'business', 'persona', 'greeting', 'voice', 'temperature', 'use_knowledge', 'status'] as const)
          .some((k) => draft[k] !== selected[k])
      : false

  const save = async () => {
    if (!draft) return
    setSaving(true)
    try {
      await api.updateAgent(draft.id, {
        name: draft.name,
        business: draft.business,
        persona: draft.persona,
        greeting: draft.greeting,
        voice: draft.voice,
        temperature: draft.temperature,
        use_knowledge: draft.use_knowledge,
        status: draft.status,
      })
      await reloadAgents()
      toast('Agent saved')
    } catch (error) {
      toast(`Could not save: ${String(error)}`, 'bad')
    } finally {
      setSaving(false)
    }
  }

  const create = async () => {
    try {
      const agent = await api.createAgent({ name: 'New agent', business: 'Acme' })
      await reloadAgents()
      setAgentId(agent.id)
      toast('Agent created')
    } catch (error) {
      toast(String(error), 'bad')
    }
  }

  const remove = async () => {
    if (!selected || agents.length <= 1) return
    await api.deleteAgent(selected.id)
    await reloadAgents()
    setAgentId(agents.find((a) => a.id !== selected.id)?.id ?? '')
    toast('Agent deleted')
  }

  return (
    <div className="page">
      <div className="head row-between">
        <div>
          <h1>Agents</h1>
          <p>Who the agent says it is, and how it opens a call.</p>
        </div>
        <button className="btn btn-primary" onClick={() => void create()}>
          <Icon name="plus" /> New agent
        </button>
      </div>

      <div className="grid" style={{ gridTemplateColumns: '236px 1fr', alignItems: 'start' }}>
        <div className="card card-pad-0">
          {agents.map((a) => (
            <button
              key={a.id}
              onClick={() => setAgentId(a.id)}
              style={{
                display: 'block', width: '100%', textAlign: 'left', background: a.id === agentId ? 'var(--bg-3)' : 'none',
                border: 0, borderBottom: '1px solid var(--line)', padding: '11px 14px', cursor: 'pointer',
                font: 'inherit', color: 'inherit',
                boxShadow: a.id === agentId ? 'inset 2px 0 0 var(--accent)' : 'none',
              }}
            >
              <div style={{ fontWeight: 560, fontSize: 13 }}>{a.name}</div>
              <div style={{ fontSize: 11.5, color: 'var(--text-dim)', marginTop: 1 }}>{a.business}</div>
              <div className="row" style={{ marginTop: 6, gap: 6 }}>
                <span className="chip" data-t={a.status === 'live' ? 'live' : undefined}>{a.status}</span>
                <span style={{ fontSize: 11, color: 'var(--text-3)' }}>{a.doc_count ?? 0} docs · {a.call_count ?? 0} calls</span>
              </div>
            </button>
          ))}
        </div>

        {draft && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div className="card">
              <h2 className="card-h">Identity</h2>
              <p className="card-sub">What the agent tells callers it is.</p>

              <div className="grid g2" style={{ gap: 0, columnGap: 14 }}>
                <div className="field">
                  <label>Agent name</label>
                  <input type="text" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
                  <span className="hint">Internal only — callers never hear this.</span>
                </div>
                <div className="field">
                  <label>Business</label>
                  <input type="text" value={draft.business} onChange={(e) => setDraft({ ...draft, business: e.target.value })} />
                  <span className="hint">Spoken to the caller.</span>
                </div>
              </div>

              <div className="field">
                <label>Personality</label>
                <input type="text" value={draft.persona} onChange={(e) => setDraft({ ...draft, persona: e.target.value })} />
                <span className="hint">A description, not a script — "a warm, efficient receptionist".</span>
              </div>

              <div className="field">
                <label>Opening line</label>
                <input type="text" value={draft.greeting} onChange={(e) => setDraft({ ...draft, greeting: e.target.value })} />
                <span className="hint">
                  Fixed rather than generated. It is the one line where any delay is fully audible,
                  and the one most worth controlling word for word.
                </span>
              </div>
            </div>

            <div className="card">
              <h2 className="card-h">Behaviour</h2>
              <p className="card-sub">How much the agent is allowed to improvise, and what it may draw on.</p>

              <div className="field">
                <label>Wording variety — {draft.temperature.toFixed(2)}</label>
                <input
                  type="range" min={0} max={1} step={0.05} value={draft.temperature}
                  onChange={(e) => setDraft({ ...draft, temperature: Number(e.target.value) })}
                />
                <span className="hint">
                  {draft.temperature <= 0.2
                    ? 'Nearly fixed wording. Recommended: a price is a commitment, and variety lands on the numbers too.'
                    : draft.temperature <= 0.5
                      ? 'Some variety. Watch the unverified-number count on live calls.'
                      : 'Highly varied — and measurably more likely to invent a figure. Not advisable for anything quoting prices.'}
                </span>
              </div>

              <div className="grid g2" style={{ gap: 0, columnGap: 14 }}>
                <div className="field">
                  <label>Voice</label>
                  <select value={draft.voice} onChange={(e) => setDraft({ ...draft, voice: e.target.value })}>
                    <option value="female-warm">Female, warm</option>
                    <option value="female-clear">Female, clear</option>
                    <option value="male-warm">Male, warm</option>
                    <option value="male-clear">Male, clear</option>
                  </select>
                </div>
                <div className="field">
                  <label>Status</label>
                  <select value={draft.status} onChange={(e) => setDraft({ ...draft, status: e.target.value })}>
                    <option value="draft">Draft</option>
                    <option value="live">Live</option>
                    <option value="paused">Paused</option>
                  </select>
                </div>
              </div>

              <label className="switch" style={{ marginTop: 4 }}>
                <input
                  type="checkbox" checked={draft.use_knowledge}
                  onChange={(e) => setDraft({ ...draft, use_knowledge: e.target.checked })}
                />
                Answer from the knowledge base
              </label>
              <div className="hint" style={{ marginTop: 5 }}>
                Off makes a faster agent that can only chat. On, it answers from your documents and
                says it will check when it finds nothing.
              </div>
            </div>

            <div className="row-between">
              <div className="row">
                <button className="btn btn-ghost" onClick={() => navigate({ view: 'knowledge' })}>
                  <Icon name="book" /> Knowledge
                </button>
                <button className="btn btn-ghost" onClick={() => navigate({ view: 'flow' })}>
                  <Icon name="flow" /> Flow
                </button>
                <button className="btn btn-ghost" onClick={() => navigate({ view: 'live' })}>
                  <Icon name="phone" /> Test call
                </button>
              </div>
              <div className="row">
                <button className="btn btn-danger" onClick={() => void remove()} disabled={agents.length <= 1}>
                  <Icon name="trash" /> Delete
                </button>
                <button className="btn btn-primary" onClick={() => void save()} disabled={!dirty || saving}>
                  {saving ? 'Saving…' : dirty ? 'Save changes' : 'Saved'}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
