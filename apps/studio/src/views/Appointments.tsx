import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ViewProps } from '../App'
import { api, type Appointment, type Slot } from '../api'
import { Icon } from '../components/Icon'

/* The diary.
 *
 * WHY THIS SCREEN IS THE POINT. Everything else in this studio measures the call — how fast the
 * first token arrived, whether the agent interrupted, which document a price came from. All of
 * that is instrumentation. This is the only screen showing something that outlived the call, and
 * an AI receptionist that leaves nothing here is a demo of a conversation, not a receptionist.
 *
 * The two halves are deliberately side by side. WHAT IS BOOKED and WHAT IS FREE are the same
 * question asked twice, and the agent answers it from exactly the endpoint feeding this page —
 * so if the screen and the voice ever disagree, the bug is visible here rather than only in a
 * caller's memory of what they were told.
 */
export function Appointments({ agents, agentId, setAgentId }: ViewProps) {
  const [rows, setRows] = useState<Appointment[]>([])
  const [slots, setSlots] = useState<Slot[]>([])
  const [totalOpen, setTotalOpen] = useState(0)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    if (!agentId) return
    setLoading(true)
    try {
      const [booked, free] = await Promise.all([
        api.appointments(agentId),
        api.availability(agentId),
      ])
      setRows(booked.appointments)
      setSlots(free.open)
      setTotalOpen(free.total_open)
    } catch {
      /* the empty state says the same thing a error banner would, without the alarm */
    } finally {
      setLoading(false)
    }
  }, [agentId])

  useEffect(() => { void load() }, [load])

  const cancel = useCallback(async (row: Appointment) => {
    setBusy(row.id)
    try {
      await api.cancelAppointment(row.id)
      // Reloaded rather than spliced out of local state: cancelling frees the slot, and the
      // availability list beside it would otherwise still be showing it as taken.
      await load()
    } finally {
      setBusy('')
    }
  }, [load])

  // Grouped by day, because that is how anybody reads a diary. A flat list sorted by timestamp
  // is correct and unusable.
  const days = useMemo(() => {
    const out = new Map<string, Appointment[]>()
    for (const row of [...rows].sort((a, b) => a.starts_at.localeCompare(b.starts_at))) {
      const key = row.starts_at.slice(0, 10)
      out.set(key, [...(out.get(key) ?? []), row])
    }
    return [...out.entries()]
  }, [rows])

  const today = new Date().toISOString().slice(0, 10)
  const upcoming = rows.filter((r) => r.starts_at.slice(0, 10) >= today).length

  return (
    <div className="page page-wide">
      <div className="row-between" style={{ marginBottom: 16 }}>
        <div className="row">
          <select value={agentId} onChange={(e) => setAgentId(e.target.value)} style={{ width: 230 }}>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>{a.name} — {a.business}</option>
            ))}
          </select>
          <span className="chip">{upcoming} upcoming</span>
          <span className="chip" data-t="accent">{totalOpen} slots free</span>
        </div>
        <button className="btn" onClick={() => void load()}>
          <Icon name="clock" /> Refresh
        </button>
      </div>

      <div className="appt-grid">
        <div className="panel">
          <div className="panel-h"><Icon name="calendar" size={13} /> Booked</div>
          <div className="panel-b">
            {loading && <div className="skeleton-list">{[0, 1, 2].map((i) => <i key={i} />)}</div>}

            {!loading && rows.length === 0 && (
              <div className="empty">
                <h3>Nothing booked yet</h3>
                Appointments made on a call land here. Start one from <b>Live call</b>, agree a
                time, and it will appear with a reference the caller can quote.
              </div>
            )}

            {!loading && days.map(([day, items]) => (
              <div key={day} className="appt-day">
                <div className="appt-day-h">
                  {new Date(`${day}T00:00`).toLocaleDateString(undefined, {
                    weekday: 'long', day: 'numeric', month: 'long',
                  })}
                  {day === today && <span className="chip" data-t="accent">today</span>}
                </div>

                {items.map((row) => (
                  <div key={row.id} className="appt" data-status={row.status}>
                    <div className="appt-time">{row.starts_at.slice(11, 16)}</div>
                    <div style={{ minWidth: 0 }}>
                      <div className="appt-who">{row.patient_name || 'No name given'}</div>
                      <div className="appt-meta">
                        <span>{row.reason || 'appointment'}</span>
                        {row.phone && <span>{row.phone}</span>}
                        {row.email && <span className="trunc">{row.email}</span>}
                      </div>
                    </div>
                    <div className="appt-right">
                      <code className="ref">{row.reference}</code>
                      <button
                        className="btn btn-ghost btn-sm"
                        disabled={busy === row.id}
                        onClick={() => void cancel(row)}
                        title="Cancel and free the slot"
                      >
                        <Icon name="x" size={13} />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>

        <div className="panel">
          <div className="panel-h"><Icon name="clock" size={13} /> Free</div>
          <div className="panel-b">
            <p className="panel-note">
              Weekdays, 8:30 to 6, closed over lunch, late on Thursdays. This is the list the
              agent reads from on a live call — the same function, not a copy of it.
            </p>
            <div className="slots">
              {slots.slice(0, 40).map((s) => (
                <span className="slot" key={s.iso} title={s.spoken}>
                  {new Date(`${s.date}T${s.time}`).toLocaleDateString(undefined, {
                    weekday: 'short', day: 'numeric',
                  })}
                  <b>{s.time}</b>
                </span>
              ))}
            </div>
            {totalOpen > 40 && (
              <p className="panel-note" style={{ marginTop: 10, marginBottom: 0 }}>
                and {totalOpen - 40} more within the fortnight.
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
