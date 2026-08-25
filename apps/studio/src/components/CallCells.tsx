import type { CallRow } from '../api'
import { Icon } from './Icon'

/* The cells that say what a call DID.

   SHARED BECAUSE THEY WERE NOT, and the two screens drifted apart. Call history was rewritten to
   show what a call was about and what it achieved; the dashboard kept the older pair -- `outcome`,
   which records how the SOCKET closed and reads "handled" on every call ever placed, and
   `sentiment`, which reads "neutral" on almost all of them. Two constant columns on the screen an
   operator opens first, next to a quote of the caller's opening line.

   Living here rather than in either view also removes a cycle: Calls imported Mood from
   Dashboard, and Dashboard would have had to import Happened back. */

export function Happened({ call }: { call: CallRow }) {
  if (call.result === 'booked') {
    const at = call.booked_for ? new Date(call.booked_for) : null
    return (
      <div className="happened" data-t="booked">
        <Icon name="check" size={13} />
        <span>
          Booked
          {at && <em>{at.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })}, {at.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}</em>}
        </span>
        <code className="ref">{call.booked_reference}</code>
      </div>
    )
  }
  if (call.result === 'passed on') {
    return <div className="happened" data-t="warn"><Icon name="user" size={13} /><span>Passed to a person</span></div>
  }
  if (call.result === 'no speech') {
    return <div className="happened" data-t="quiet"><span>Nobody spoke</span></div>
  }
  if (call.result === 'abandoned') {
    return <div className="happened" data-t="warn"><Icon name="x" size={13} /><span>Hung up</span></div>
  }
  return <div className="happened"><Icon name="chat" size={13} /><span>Questions answered</span></div>
}

export function Mood({ value }: { value: string }) {
  const tone = value === 'positive' ? 'good' : value === 'negative' ? 'bad' : undefined
  return <span className="chip" data-t={tone}>{value}</span>
}

export function Outcome({ value, escalated }: { value: string; escalated: boolean }) {
  if (escalated || value === 'transferred') return <span className="chip" data-t="cost">passed to a person</span>
  if (value === 'completed') return <span className="chip" data-t="good">handled</span>
  if (value === 'abandoned') return <span className="chip" data-t="bad">caller hung up</span>
  if (value === 'in_progress') return <span className="chip" data-t="accent">in progress</span>
  return <span className="chip">{value}</span>
}
