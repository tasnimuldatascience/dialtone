/* Turning what the recogniser heard into what the caller meant.
 *
 * Speech recognition gives you a lower-case stream of words with no punctuation, every "um"
 * faithfully written down, and every stutter preserved:
 *
 *     hi uh i i wanted to ask how much is a check up um
 *
 * A person reading that assumes the software is broken. What they expect — what WhisperFlow and
 * every good dictation tool does — is:
 *
 *     Hi, I wanted to ask, how much is a check-up?
 *
 * THE SPLIT THAT MAKES THIS SAFE. Fillers are removed for the AGENT and kept for the ENDPOINTER,
 * and that is the whole architecture of this file. "um" is one of the strongest signals in the
 * system: a caller who has just said it is still composing, and the agent must wait. Strip it
 * before the turn-taking decision and you throw away the evidence that stops the agent talking
 * over people. So the RAW transcript decides WHEN to answer, and the POLISHED one decides WHAT
 * is answered and what appears on screen.
 *
 * NOTHING HERE REBUILDS THE TEXT FROM WORD MATCHES. The previous version did — it collected
 * `/[\w'’-]+/g` and joined the results with spaces — and that silently deleted every character
 * that is not a letter or a digit:
 *
 *     "my email is sam@example.com"  ->  "my email is sam example com"
 *     "it's £120, isn't it?"         ->  "it's 120 isn't it"
 *
 * An email address and a price, both destroyed, in the one place where being exact matters most.
 * Everything below edits the token stream in place and leaves anything it does not recognise
 * exactly as it found it.
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
 * they are a short enough list to name — unlike the stutters, which can be any word at all.
 */
const REPEATABLE = new Set([
  'very', 'really', 'so', 'no', 'yes', 'much', 'far', 'way', 'ha', 'never', 'many',
])

/**
 * Openers that make a sentence a question.
 *
 * Used only to choose between "?" and "." at the end. Getting it wrong costs a punctuation mark,
 * so the list can afford to be plain rather than clever.
 */
const QUESTION_OPENERS = new Set([
  'what', 'where', 'when', 'why', 'who', 'whom', 'whose', 'which', 'how',
  'do', 'does', 'did', 'can', 'could', 'will', 'would', 'shall', 'should',
  'is', 'are', 'am', 'was', 'were', 'have', 'has', 'had', 'may', 'might',
])

const WORD = /[\w'’-]+/g

/** The letters and digits inside a token, with any surrounding punctuation removed. */
function core(token: string): string {
  return token.replace(/^[^\w'’]+/, '').replace(/[^\w'’]+$/, '').toLowerCase()
}

/**
 * Remove fillers and stutters, keeping everything else byte for byte.
 *
 * Returns an empty string when nothing of substance remains, which is a meaningful answer: a
 * turn that was entirely "um" is not a turn, and sending it would have the agent reply to a
 * noise.
 */
function strip(raw: string): string {
  const kept: string[] = []
  let previous = ''

  for (const token of raw.split(/\s+/)) {
    if (!token) continue
    const word = core(token)
    if (!word) {
      // Punctuation on its own. Keep it — it came from somewhere.
      kept.push(token)
      continue
    }
    if (FILLERS.has(word) || BACKCHANNELS.has(word)) continue

    // A repeated word is usually a stutter: "I I want", "can can you". Recognition produces
    // these constantly and they read as a transcription fault rather than as speech — except
    // for the handful of words English doubles deliberately, where the repetition IS the point.
    if (word === previous && !REPEATABLE.has(word)) continue

    previous = word
    kept.push(token)
  }

  return kept.join(' ').trim()
}

/** Restore the capital that recognition never emits. */
function capitalise(text: string): string {
  return text
    // The opening letter, wherever it is — the sentence may start with a quote or a bracket.
    .replace(/^(\W*)(\w)/, (_, lead: string, letter: string) => lead + letter.toUpperCase())
    // And after every sentence ending.
    .replace(/([.!?]\s+)(\w)/g, (_, end: string, letter: string) => end + letter.toUpperCase())
}

/**
 * The one word English always capitalises.
 *
 * Chrome's recogniser writes "i" and "i'm" in lower case in the middle of a sentence, which is
 * the single most obvious tell that a transcript has not been touched by anything.
 */
function fixPronoun(text: string): string {
  return text.replace(/\bi\b(['’](?:m|ve|ll|d))?/g, (_, suffix: string | undefined) =>
    `I${suffix ?? ''}`)
}

/** Does this read as a question? Used only to pick the closing mark. */
export function looksLikeAQuestion(text: string): boolean {
  const words = text.toLowerCase().match(WORD) ?? []
  const [first, second] = words
  if (!first) return false
  if (QUESTION_OPENERS.has(first)) return true
  // "and how much is it", "so can I come tomorrow" — the opener is one word further in.
  if (second && ['and', 'so', 'but', 'ok', 'okay', 'right'].includes(first)) {
    return QUESTION_OPENERS.has(second)
  }
  return false
}

/** Close the sentence, if the caller has not closed it themselves. */
function punctuate(text: string): string {
  if (!text) return text
  if (/[.!?]$/.test(text)) return text
  // A trailing comma or dash is the recogniser's, not the caller's, and reads as truncation.
  const trimmed = text.replace(/[,;:–—-]+$/, '').trimEnd()
  if (!trimmed) return ''
  // The mark belongs to the LAST sentence, not the first. "My tooth hurts. Can I come in
  // today" is a statement followed by a question, and reading the opening word would close it
  // with a full stop.
  const lastSentence = trimmed.split(/(?<=[.!?])\s+/).pop() ?? trimmed
  return trimmed + (looksLikeAQuestion(lastSentence) ? '?' : '.')
}

/**
 * Spoken punctuation, for dictation only.
 *
 * A person dictating into a box says "comma" and means one. A person on a phone call says
 * "period" and means a length of time, so this is never applied to a live call — which is why
 * it is an option rather than part of the main path.
 */
const SPOKEN_MARKS: [RegExp, string][] = [
  [/\s+(?:full stop|period)\b/gi, '.'],
  [/\s+comma\b/gi, ','],
  [/\s+question mark\b/gi, '?'],
  [/\s+exclamation (?:mark|point)\b/gi, '!'],
  [/\s+(?:semicolon|semi colon)\b/gi, ';'],
  [/\s+colon\b/gi, ':'],
  [/\s+(?:new line|newline|new paragraph)\b/gi, '\n'],
]

function applySpokenMarks(text: string): string {
  let out = text
  for (const [pattern, mark] of SPOKEN_MARKS) out = out.replace(pattern, mark)
  // "hello , there" -> "hello, there"
  return out.replace(/\s+([,.!?;:])/g, '$1').replace(/\n\s+/g, '\n')
}

export interface PolishOptions {
  /**
   * Close the sentence. Off while the caller is still speaking: a full stop that appears and
   * then moves as more words arrive is worse than none at all.
   */
  punctuate?: boolean
  /** Turn spoken "comma" and "full stop" into marks. Dictation only — see SPOKEN_MARKS. */
  spokenMarks?: boolean
}

/**
 * The whole pass: fillers out, stutters out, capitals and punctuation back in.
 *
 * This is what the caller sees and what the agent is sent. The raw string it was given is
 * untouched and is what the endpointer keeps reading.
 */
export function polish(raw: string, options: PolishOptions = {}): string {
  let text = raw.trim()
  if (!text) return ''
  if (options.spokenMarks) text = applySpokenMarks(text)

  text = strip(text)
  if (!text) return ''

  text = fixPronoun(text)
  if (options.punctuate !== false) text = punctuate(text)
  return capitalise(text)
}

/**
 * Remove fillers and stutters, without adding punctuation.
 *
 * The narrower half of `polish`, kept separate because the tests for "did it change the
 * caller's meaning?" are about this step alone.
 */
export function cleanTranscript(raw: string): string {
  return polish(raw, { punctuate: false })
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
