/* Deciding that the caller has talked over the agent.
 *
 * WHY THIS IS DECIDED FROM LOUDNESS AND NOT FROM WORDS. There are two microphone streams on this
 * page and only one of them can be trusted while the agent is speaking:
 *
 *   MicLevel      our own getUserMedia stream, opened with `echoCancellation: true`. The browser
 *                 removes most of what is coming out of the speakers, so what is left above the
 *                 floor is the caller.
 *   Web Speech    opens its own stream internally. We cannot pass it constraints, so during agent
 *                 audio its transcript is partly the agent's own words — which is exactly how an
 *                 earlier version ended up in conversation with itself.
 *
 * So the decision is made from the stream with echo cancellation, and the transcript is only
 * believed once the agent has stopped. Reading the words to decide whether to stop talking would
 * mean reading the agent's own sentence and treating it as an interruption.
 *
 * WHY IT HAS TO BE SUSTAINED. A single loud frame is a cough, a door, a keyboard, a chair. An
 * agent that stops mid-word every time something happens in the room is worse than one that
 * cannot be interrupted at all, because the caller cannot tell what they did wrong.
 *
 * WHAT THIS DELIBERATELY GETS WRONG. "Mm-hmm" is about as long and as loud as the beginning of a
 * real interruption, and nothing in an energy signal separates them. So a backchannel will
 * sometimes stop the audio. That is the cheaper error: the recovery is that the transcript turns
 * out to be nothing but a backchannel, no turn is taken, and the agent carries on — a small
 * stumble. The other way round, a caller shouting "no, not that one" is ignored until the agent
 * finishes reading out four appointment times.
 */

/**
 * How loud counts as the caller, while the agent is audible.
 *
 * Roughly double the threshold used when the agent is silent. Echo cancellation leaves a
 * residue, and the whole point of this number is to sit above it.
 */
export const BARGE_LEVEL = 0.17

/** How long that has to hold. About one syllable of overlap before the agent gives way. */
export const BARGE_MS = 380

export interface BargeInput {
  /** Is the agent making sound right now? Nothing here applies when it is silent. */
  agentAudible: boolean
  /** Current microphone level, 0..1, from the echo-cancelled stream. */
  level: number
  /** performance.now() */
  now: number
  /** When the current run of loud frames began, or 0 if the last frame was quiet. */
  loudSince: number
  /** Set once an interruption has been sent, so one utterance cannot fire twice. */
  alreadyInterrupted: boolean
}

export interface BargeDecision {
  /** Carry this back in on the next frame. */
  loudSince: number
  /** Stop the audio and tell the gateway. */
  interrupt: boolean
  /** Why, for the on-screen turn-taking readout. */
  reason: string
}

export function trackBargeIn(input: BargeInput): BargeDecision {
  const { agentAudible, level, now, loudSince, alreadyInterrupted } = input

  // Nothing to interrupt. Reset, so a run of loudness that began while the agent was talking
  // cannot fire a moment after it has stopped.
  if (!agentAudible) return { loudSince: 0, interrupt: false, reason: '' }

  if (level < BARGE_LEVEL) {
    return { loudSince: 0, interrupt: false, reason: 'agent speaking' }
  }

  const since = loudSince || now
  const held = now - since

  if (alreadyInterrupted) {
    return { loudSince: since, interrupt: false, reason: 'interrupted — listening' }
  }
  if (held >= BARGE_MS) {
    return { loudSince: since, interrupt: true, reason: 'you interrupted' }
  }
  return {
    loudSince: since,
    interrupt: false,
    reason: `someone is talking over — ${Math.round(BARGE_MS - held)}ms`,
  }
}
