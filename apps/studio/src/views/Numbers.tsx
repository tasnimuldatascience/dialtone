import { useCallback, useEffect, useState } from 'react'
import type { ViewProps } from '../App'
import { api, type PhoneNumber } from '../api'
import { Icon } from '../components/Icon'

/* Phone numbers, and which agent answers them.
 *
 * The provider column is honest about what this is: numbers here are simulated. Wiring a real
 * carrier means implementing six methods behind the telephony interface, and pretending otherwise
 * on a screen an operator will read is how a demo becomes a lie.
 */
export function Numbers({ agents, toast }: ViewProps) {
  const [numbers, setNumbers] = useState<PhoneNumber[]>([])
  const [e164, setE164] = useState('')
  const [label, setLabel] = useState('')
  const [assignTo, setAssignTo] = useState('')

  const load = useCallback(async () => {
    const { numbers: rows } = await api.numbers()
    setNumbers(rows)
  }, [])

  useEffect(() => { void load() }, [load])
  useEffect(() => { setAssignTo((c) => c || agents[0]?.id || '') }, [agents])

  const add = async () => {
    if (!e164.trim()) return
    try {
      await api.addNumber(e164.trim(), label.trim(), assignTo || null)
      setE164('')
      setLabel('')
      await load()
      toast('Number added')
    } catch (error) {
      toast(String(error), 'bad')
    }
  }

  const assign = async (id: string, agentId: string) => {
    await api.assignNumber(id, agentId || null)
    await load()
  }

  return (
    <div className="page">
      <div className="head">
        <h1>Phone numbers</h1>
        <p>Which agent answers which line.</p>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <h2 className="card-h">Add a number</h2>
        <p className="card-sub">
          Numbers here are simulated. Connecting a real carrier — Twilio, Telnyx, a SIP trunk —
          means implementing six methods behind the telephony interface; nothing above it changes.
        </p>
        <div className="row" style={{ alignItems: 'flex-end' }}>
          <div className="field" style={{ flex: 1, marginBottom: 0 }}>
            <label>Number</label>
            <input type="text" value={e164} placeholder="+441134960003" onChange={(e) => setE164(e.target.value)} />
          </div>
          <div className="field" style={{ flex: 1, marginBottom: 0 }}>
            <label>Label</label>
            <input type="text" value={label} placeholder="Bookings" onChange={(e) => setLabel(e.target.value)} />
          </div>
          <div className="field" style={{ flex: 1, marginBottom: 0 }}>
            <label>Answered by</label>
            <select value={assignTo} onChange={(e) => setAssignTo(e.target.value)}>
              <option value="">Nobody</option>
              {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select>
          </div>
          <button className="btn btn-primary" onClick={() => void add()} disabled={!e164.trim()}>
            <Icon name="plus" /> Add
          </button>
        </div>
      </div>

      <div className="t-wrap">
        <table>
          <thead>
            <tr><th>Number</th><th>Label</th><th>Answered by</th><th>Provider</th></tr>
          </thead>
          <tbody>
            {numbers.length === 0 && (
              <tr><td colSpan={4}><div className="empty">No numbers yet.</div></td></tr>
            )}
            {numbers.map((n) => (
              <tr key={n.id}>
                <td className="num" style={{ fontWeight: 560 }}>{n.e164}</td>
                <td style={{ color: 'var(--text-2)' }}>{n.label || '—'}</td>
                <td>
                  <select
                    value={n.agent_id ?? ''}
                    onChange={(e) => void assign(n.id, e.target.value)}
                    style={{ width: 190, padding: '4px 9px', fontSize: 12 }}
                  >
                    <option value="">Nobody</option>
                    {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                  </select>
                </td>
                <td><span className="chip">{n.provider}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
