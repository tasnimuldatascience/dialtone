/* Deciding when the caller has finished speaking, in the browser.
 *
 * This is the same question `turn/endpointing.py` answers on the server, asked under a harder
 * constraint: the browser has two clocks that disagree.
 *
 *   THE MICROPHONE IS REAL TIME. The level meter knows the room went quiet the instant it did.
 *   THE TRANSCRIPT IS NOT. Web Speech emits interim results a few hundred milliseconds after the
 *   words were spoken, and in bursts rather than smoothly.
 *
 * Using either one alone fails, and both failures were shipped before this file existed:
 *
 *   TRANSCRIPT ONLY   "has it stopped changing?" is not silence detection. A recogniser pausing
 *                     to process looks exactly like a caller who has finished, so the agent cut
 *                     people off mid-sentence.
 *   AUDIO ONLY        the room goes quiet between two words of one sentence, and the recogniser
 *                     has not caught up yet. "Hi, how are you doing?" was sent as four separate
 *                     turns -- "hi", "hi uh", "hi how are you doing", "Hi, how are you doing?" --
 *                     and answered four times.
 *
 * So a turn ends when BOTH clocks agree, and only on words that have not already been sent. The
 * logic lives here as a pure function so it can be tested against recorded timings instead of
 * being verified by talking to a laptop and hoping.
 */

/** How far behind the audio the transcript runs. Measured against Chrome and Edge. */
export const RECOGNITION_LAG_MS = 420

/**
 * A floor under the endpointer's own threshold.
 *
 * The endpointer can legitimately ask for as little as 160ms of silence -- correct on the server,
 * where silence means silence. In a browser it does not: a threshold below the recognition lag
 * would end the turn on words the recogniser has not written down yet. "hi" scores as a complete
 * short turn and earns ~180ms, which is exactly how a greeting became its own turn while the
 * caller was still mid-sentence.
 */
export const MIN_SILENCE_MS = 320

export interface TurnInput {
  /** The full transcript so far, as the recogniser currently has it. */
  transcript: string
  /** What has already been dispatched to the agent this turn. */
  alreadySent: string
  /** Milliseconds since the microphone last heard speech. */
  quietForMs: number
  /** Milliseconds since the transcript last changed. */
  settledForMs: number
  /** What the endpointer asked for, given what was said. */
  thresholdMs: number
  /** True while the agent is speaking; nothing heard then belongs to the caller. */
  agentSpeaking?: boolean
  /** False until the caller has actually made a sound, so room noise cannot start a turn. */
  heardSpeech?: boolean
}

export interface TurnDecision {
  send: boolean
  /** The part of the transcript that has not been sent yet. */
  text: string
  /** Why, in words. Rendered in the studio so the decision is inspectable rather than magic. */
  reason: string
}

/**
 * Should this turn end now, and if so, with what text?
 *
 * Conditions are checked cheapest-first, and each returns its own reason — when an agent
 * interrupts someone, "why" needs to be a line you can read rather than a threshold you infer.
 */
export function decideTurn(input: TurnInput): TurnDecision {
  const transcript = input.transcript.trim()

  if (!transcript) {
    return { send: false, text: '', reason: 'nothing said yet' }
  }
  if (input.agentSpeaking) {
    return { send: false, text: '', reason: 'the agent is speaking' }
  }
  if (input.heardSpeech === false) {
    return { send: false, text: '', reason: 'no speech heard yet' }
  }

  // Only ever send what is new. The interim result repeats everything since the last final
  // result, so without this one spoken sentence becomes a turn per word.
  const sent = input.alreadySent.trim()
  const fresh = transcript.startsWith(sent) ? transcript.slice(sent.length).trim() : transcript
  if (!fresh) {
    return { send: false, text: '', reason: 'nothing new since the last turn' }
  }

  const threshold = Math.max(input.thresholdMs, MIN_SILENCE_MS)

  if (input.quietForMs < threshold) {
    return {
      send: false,
      text: fresh,
      reason: `quiet for ${Math.round(input.quietForMs)}ms of ${Math.round(threshold)}ms`,
    }
  }

  // The room is quiet, but the recogniser may still be writing. Sending here truncates the
  // sentence, which is the failure that produced four turns from one greeting.
  if (input.settledForMs < RECOGNITION_LAG_MS) {
    return {
      send: false,
      text: fresh,
      reason: `waiting for the transcript to catch up (${Math.round(input.settledForMs)}ms of ${RECOGNITION_LAG_MS}ms)`,
    }
  }

  return {
    send: true,
    text: fresh,
    reason: `silent for ${Math.round(input.quietForMs)}ms and the transcript has settled`,
  }
}
