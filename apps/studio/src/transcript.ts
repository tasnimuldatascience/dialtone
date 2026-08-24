/* Cleaning up what the recogniser heard, before it reaches the agent.
 *
 * People say "um". Speech recognition writes it down, and a raw transcript reads like this:
 *
 *     hi uh how are you um doing
 *
 * That is bad in three ways: it looks careless in the call record, it wastes the agent's very
 * limited context, and it makes the model answer the disfluency rather than the question.
 *
 * FILLERS ARE STRIPPED FOR THE AGENT AND KEPT FOR THE ENDPOINTER, and that split is the whole
 * point of this file. "um" is one of the strongest signals in the entire system: a caller who has
 * just said it is still composing, and the agent must wait. Removing it before the turn-taking
 * decision would throw away exactly the evidence that stops the agent interrupting people.
 *
 * So the raw transcript decides WHEN to answer, and the cleaned one decides WHAT is answered.
 */

/**
 * Words that carry no content on their own.
 *
 * Deliberately short. Every entry is a sound rather than a word — "like", "well", "so" and
 * "actually" are all used as fillers and are also perfectly ordinary words ("I would like an
 * appointment", "so how much is it"), and removing those changes meaning.
 */
const FILLERS = new Set([
  'uh', 'um', 'umm', 'uhh', 'er', 'erm', 'ah', 'ahh', 'eh', 'hmm', 'hm', 'mmm', 'mm',
])

/** Sounds that mean "I am listening", not "here is my question". */
const BACKCHANNELS = new Set([
  'mhm', 'mmhm', 'uh-huh', 'uhhuh', 'mm-hmm', 'mmhmm',
])

/**
 * Words that repeat on purpose.
 *
 * Collapsing every repeated word treats "that is very very expensive" as a stutter and quietly
 * removes the caller's emphasis. These are the ones English genuinely doubles for effect, and
 * they are a short enough list to name -- unlike the stutters, which can be any word at all.
 */
const REPEATABLE = new Set([
  'very', 'really', 'so', 'no', 'yes', 'much', 'far', 'way', 'ha', 'never', 'many',
])

const WORD = /[\w'’-]+/g

/**
 * Remove fillers and stutters from a transcript.
 *
 * Returns an empty string when nothing of substance remains, which is a meaningful answer: a
 * turn that was entirely "um" is not a turn, and sending it would have the agent reply to a
 * noise.
 */
export function cleanTranscript(raw: string): string {
  const words = raw.match(WORD) ?? []
  const kept: string[] = []

  for (const word of words) {
    const lower = word.toLowerCase()
    if (FILLERS.has(lower) || BACKCHANNELS.has(lower)) continue

    // A repeated word is usually a stutter: "I I want", "can can you". Recognition produces
    // these constantly and they read as a transcription fault rather than as speech -- except
    // for the handful of words English doubles deliberately, where the repetition IS the point.
    const previous = kept.length ? kept[kept.length - 1].toLowerCase() : ''
    if (previous === lower && !REPEATABLE.has(lower)) continue

    kept.push(word)
  }

  if (!kept.length) return ''

  // Restore the sentence shape: recognition emits lower case, and a bare lower-case sentence in
  // a call record looks like a bug even though it is faithful.
  const text = kept.join(' ')
  return text.charAt(0).toUpperCase() + text.slice(1)
}

/** True when a transcript is nothing but fillers, so there is no turn to take. */
export function isAllFiller(raw: string): boolean {
  return !cleanTranscript(raw)
}

/**
 * Did the caller just trail off on a filler?
 *
 * Used to hold a turn open. Someone who has just said "um" is mid-thought, whatever the silence
 * detector thinks, and this is the cheapest possible way to avoid interrupting them.
 */
export function endsOnFiller(raw: string): boolean {
  const words = raw.match(WORD) ?? []
  const last = words[words.length - 1]?.toLowerCase()
  return last ? FILLERS.has(last) : false
}
