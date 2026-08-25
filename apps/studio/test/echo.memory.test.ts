/**
 * Recognising the agent's own voice coming back through the microphone.
 *
 * THE CALL THAT CAUSED THIS FILE. Eleven turns, on speakers, and from turn six onward the agent
 * was answering itself:
 *
 *     agent:  "Sure, our usual working hours are from eight thirty to six in the evening."
 *     caller: "Our usual working hours are from 8:30 in the morning to six in the eve..."
 *     agent:  "We regret to inform you that we are currently unable to accommodate..."
 *     caller: "We are currently unable to accommodate your request for an appointment"
 *
 * The content check was not the problem — those three transcripts score 0.79, 0.88 and 1.00
 * against what the agent said, well over the threshold. The problem was that by the time they
 * arrived, the memory of having said them had expired.
 *
 * `rememberSpoken` was called when a chunk was SCHEDULED and expired six seconds later. Every
 * chunk of a reply is scheduled within a second or two; they play out over the following ten. So
 * the last sentence of a long reply reached the speaker, echoed back, and was checked against a
 * memory that had lapsed seconds earlier.
 *
 * That is the same mistake as the speaking flag that went stale between chunks, in a different
 * variable: anything timed from when audio was QUEUED is measuring the wrong clock.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SPOKEN_MEMORY_MS, clearSpokenMemory, looksLikeEcho, rememberSpoken } from '../src/voice'

beforeEach(() => {
  clearSpokenMemory()
  vi.useFakeTimers()
})
afterEach(() => vi.useRealTimers())

/** The three transcripts from the real call, with what the agent had actually said. */
const REAL = [
  {
    said: 'Sure, our usual working hours are from eight thirty in the morning to six in the evening. We are closed at weekends.',
    heard: 'Our usual working hours are from 8:30 in the morning to six in the evening',
  },
  {
    said: 'We regret to inform you that we are currently unable to accommodate your request for an appointment at that time.',
    heard: 'We are currently unable to accommodate your request for an appointment',
  },
  {
    said: 'Monday at five thirty sounds good. Is there anything specific you need to discuss?',
    heard: 'Monday at 5:30 sounds good. Is there anything specific you need to discuss',
  },
]

describe('the agent hearing itself', () => {
  it.each(REAL)('catches $heard', ({ said, heard }) => {
    rememberSpoken(said)
    expect(looksLikeEcho(heard)).toBe(true)
  })

  it('still catches it after a long reply has finished playing', () => {
    // THE REGRESSION. A twelve-second reply: every chunk is queued at the start, and the last
    // one is not heard until it has played. The hold has to be measured from THEN.
    rememberSpoken(REAL[0].said, 12_000 + SPOKEN_MEMORY_MS)
    vi.advanceTimersByTime(12_000)
    expect(looksLikeEcho(REAL[0].heard)).toBe(true)
  })

  it('would have missed it on the old six-second timer', () => {
    // Documents the bug rather than the fix: with no hold, the memory is gone before the audio
    // has finished coming out of the speaker.
    rememberSpoken(REAL[0].said)
    vi.advanceTimersByTime(SPOKEN_MEMORY_MS + 500)
    expect(looksLikeEcho(REAL[0].heard)).toBe(false)
  })

  it('holds for at least the guard even when nothing is queued behind it', () => {
    rememberSpoken(REAL[1].said, 0)
    vi.advanceTimersByTime(SPOKEN_MEMORY_MS - 500)
    expect(looksLikeEcho(REAL[1].heard)).toBe(true)
  })

  it('sweeps entries that are genuinely stale', () => {
    rememberSpoken(REAL[0].said, 1000)
    vi.advanceTimersByTime(60_000)
    rememberSpoken('something else entirely about parking')
    expect(looksLikeEcho(REAL[0].heard)).toBe(false)
  })

  it('does not expire an entry because a LATER one was shorter', () => {
    // The sweep used to stop at the head of the list, which is ordered by insertion and not by
    // expiry. A long-held entry queued first could be dropped by a short one queued after it.
    rememberSpoken(REAL[0].said, 30_000)
    rememberSpoken('a short trailing clause', 200)
    vi.advanceTimersByTime(1_000)
    expect(looksLikeEcho(REAL[0].heard)).toBe(true)
  })
})

describe('the caller is still heard', () => {
  it('does not swallow an ordinary answer', () => {
    rememberSpoken('I have got Tuesday at nine, or Thursday at two in the afternoon.')
    for (const said of ['Thursday at two please', 'yes that works', 'can I come earlier']) {
      expect(looksLikeEcho(said)).toBe(false)
    }
  })

  it('does not swallow a caller picking one of the offered slots', () => {
    // THE FALSE POSITIVE THIS FILE FOUND. "Thursday at two please" is four words, three of them
    // the agent's, which scored 0.75 and was treated as echo. An agent deaf to the answer it
    // just asked for is worse than one that occasionally hears itself.
    rememberSpoken('I have got Tuesday at nine, or Thursday at two in the afternoon.')
    for (const said of ['Thursday at two please', 'Thursday at two in the afternoon',
                        'the second one', 'nine on Tuesday']) {
      expect(looksLikeEcho(said), said).toBe(false)
    }
  })

  it('does not judge something too short to judge', () => {
    rememberSpoken('We are open Monday through Friday from eight thirty until six.')
    expect(looksLikeEcho('open')).toBe(false)
    expect(looksLikeEcho('until six')).toBe(false)
  })

  it('lets the caller repeat the agent once the memory has lapsed', () => {
    // A caller genuinely reading a time back should be heard. The window is long enough to cover
    // the audio and short enough not to deafen the agent for the rest of the call.
    rememberSpoken('Thursday at two in the afternoon', 1000)
    vi.advanceTimersByTime(1000 + SPOKEN_MEMORY_MS + 100)
    expect(looksLikeEcho('Thursday at two in the afternoon then')).toBe(false)
  })
})
