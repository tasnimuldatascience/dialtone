/**
 * Knowing when the caller has finished — tested against recorded timings.
 *
 * The one thing this product claims to do well, so it is verified rather than hoped for. Each
 * scenario below is a trace of what the browser actually reports over time: the microphone level
 * in real time, the transcript arriving a few hundred milliseconds late, and in bursts.
 *
 * The headline case is the bug a real call produced. One spoken sentence, "Hi, how are you
 * doing?", was sent as FOUR turns and answered four times:
 *
 *     You: hi
 *     You: hi uh
 *     You: hi how are you doing
 *     You: Hi, how are you doing?
 *
 * It is reproduced here as `a greeting spoken in one breath`, and it must produce exactly one.
 */

import { describe, expect, it } from 'vitest'
import { decideTurn, MIN_SILENCE_MS, RECOGNITION_LAG_MS } from '../src/turntaking'

/** One moment in a call: what the microphone and the recogniser each reported. */
interface Frame {
  at: number
  /** Transcript as the recogniser has it now. Unchanged frames repeat the previous value. */
  transcript: string
  /** True while the caller is actually making sound. */
  voice: boolean
}

/**
 * Replay a trace through the decision, exactly as the live loop does.
 *
 * Returns every turn it would have sent, so a test can assert on the COUNT as well as the
 * content — sending the right words four times is still wrong.
 */
function replay(frames: Frame[], thresholdMs: number): string[] {
  const sent: string[] = []
  let alreadySent = ''
  let lastVoiceAt = 0
  let lastChangeAt = 0
  let previous = ''
  let heardSpeech = false

  for (const frame of frames) {
    if (frame.voice) {
      lastVoiceAt = frame.at
      heardSpeech = true
    }
    if (frame.transcript !== previous) {
      lastChangeAt = frame.at
      previous = frame.transcript
      // A changing transcript is proof of speech even if the level meter missed it.
      lastVoiceAt = frame.at
      heardSpeech = true
    }

    const decision = decideTurn({
      transcript: frame.transcript,
      alreadySent,
      quietForMs: frame.at - lastVoiceAt,
      settledForMs: frame.at - lastChangeAt,
      thresholdMs,
      heardSpeech,
    })

    if (decision.send) {
      sent.push(decision.text)
      alreadySent = frame.transcript
    }
  }
  return sent
}

/** Build a trace: words arrive at `wordAt`, the transcript lags behind by `lag`. */
function trace(
  words: { text: string; spokenAt: number }[],
  { lag = 400, tailMs = 3000, step = 60 } = {},
): Frame[] {
  const frames: Frame[] = []
  const end = (words.at(-1)?.spokenAt ?? 0) + tailMs
  for (let at = 0; at <= end; at += step) {
    // Everything spoken up to `at - lag` has reached the recogniser by now.
    const heard = words.filter((w) => w.spokenAt + lag <= at).map((w) => w.text)
    // Voice is present for ~260ms around each spoken word.
    const voice = words.some((w) => at >= w.spokenAt && at < w.spokenAt + 260)
    frames.push({ at, transcript: heard.join(' '), voice })
  }
  return frames
}

describe('deciding when to answer', () => {
  it('a greeting spoken in one breath is ONE turn, not four', () => {
    // The exact failure from a live call. Natural gaps between words, and the transcript
    // arriving 400ms late, previously produced a turn per word.
    const frames = trace([
      { text: 'hi', spokenAt: 0 },
      { text: 'how', spokenAt: 520 },
      { text: 'are', spokenAt: 700 },
      { text: 'you', spokenAt: 860 },
      { text: 'doing', spokenAt: 1040 },
    ])

    // "hi" scores as a complete short turn and earns a very short threshold. That is correct on
    // the server and is exactly the value that broke this in the browser.
    const sent = replay(frames, 180)

    expect(sent).toHaveLength(1)
    expect(sent[0]).toBe('hi how are you doing')
  })

  it('does not answer during a mid-sentence pause', () => {
    // Someone reading out a number, thinking between groups. The pause is longer than the
    // threshold, and answering into it is the failure the whole project exists to prevent.
    const frames = trace([
      { text: 'my', spokenAt: 0 },
      { text: 'account', spokenAt: 200 },
      { text: 'number', spokenAt: 420 },
      { text: 'is', spokenAt: 620 },
      { text: 'four', spokenAt: 800 },
      { text: 'two', spokenAt: 980 },
      // 900ms of thinking, mid-number.
      { text: 'four', spokenAt: 1880 },
      { text: 'two', spokenAt: 2060 },
    ])

    // What the endpointer gives a sentence ending mid-number.
    const sent = replay(frames, 1600)
    expect(sent).toHaveLength(1)
    expect(sent[0]).toBe('my account number is four two four two')
  })

  it('answers a finished question promptly', () => {
    const frames = trace([
      { text: 'how', spokenAt: 0 },
      { text: 'much', spokenAt: 180 },
      { text: 'is', spokenAt: 340 },
      { text: 'a', spokenAt: 460 },
      { text: 'check-up', spokenAt: 580 },
    ])
    const sent = replay(frames, 250)
    expect(sent).toEqual(['how much is a check-up'])
  })

  it('treats two separated sentences as two turns', () => {
    // A genuine gap -- the caller finished, the agent should have answered, and they carried on.
    // The second sentence must not repeat the first.
    const frames = trace([
      { text: 'hello', spokenAt: 0 },
      { text: 'there', spokenAt: 180 },
      { text: 'can', spokenAt: 2600 },
      { text: 'you', spokenAt: 2760 },
      { text: 'help', spokenAt: 2920 },
    ])
    const sent = replay(frames, 250)
    expect(sent).toHaveLength(2)
    expect(sent[0]).toBe('hello there')
    expect(sent[1]).toBe('can you help')
  })

  it('never repeats words it has already sent', () => {
    const frames = trace([
      { text: 'yes', spokenAt: 0 },
      { text: 'please', spokenAt: 2400 },
    ])
    const sent = replay(frames, 200)
    expect(sent.join(' ')).toBe('yes please')
    expect(sent.every((turn) => turn.trim().length > 0)).toBe(true)
  })

  it('is silent when the caller is', () => {
    const frames = Array.from({ length: 60 }, (_, i) => ({
      at: i * 60, transcript: '', voice: false,
    }))
    expect(replay(frames, 300)).toEqual([])
  })
})

describe('the decision itself', () => {
  const base = {
    transcript: 'how much is a check-up',
    alreadySent: '',
    quietForMs: 900,
    settledForMs: 900,
    thresholdMs: 250,
    heardSpeech: true,
  }

  it('sends when both clocks agree', () => {
    expect(decideTurn(base).send).toBe(true)
  })

  it('waits while the room is still noisy', () => {
    const d = decideTurn({ ...base, quietForMs: 100 })
    expect(d.send).toBe(false)
    expect(d.reason).toContain('quiet for')
  })

  it('waits for the transcript even when the room is silent', () => {
    // The case audio-only detection gets wrong.
    const d = decideTurn({ ...base, settledForMs: 100 })
    expect(d.send).toBe(false)
    expect(d.reason).toContain('catch up')
  })

  it('never waits less than the recognition lag, whatever the endpointer asks', () => {
    // The endpointer may legitimately ask for 160ms. In a browser that is below the transcript's
    // own delivery delay, so it would end the turn on words not yet written down.
    const d = decideTurn({ ...base, thresholdMs: 160, quietForMs: 200, settledForMs: 900 })
    expect(d.send).toBe(false)
    expect(MIN_SILENCE_MS).toBeGreaterThan(160)
  })

  it('stays quiet while the agent is talking', () => {
    expect(decideTurn({ ...base, agentSpeaking: true }).send).toBe(false)
  })

  it('does not start a turn from room noise alone', () => {
    expect(decideTurn({ ...base, heardSpeech: false }).send).toBe(false)
  })

  it('sends only the new words', () => {
    const d = decideTurn({ ...base, transcript: 'yes please book it', alreadySent: 'yes please' })
    expect(d.send).toBe(true)
    expect(d.text).toBe('book it')
  })

  it('sends nothing when there is nothing new', () => {
    const d = decideTurn({ ...base, transcript: 'yes please', alreadySent: 'yes please' })
    expect(d.send).toBe(false)
    expect(d.reason).toContain('nothing new')
  })

  it('recovers if the recogniser rewrites the sentence', () => {
    // Web Speech revises what it thought it heard. When the new transcript is not an extension
    // of what was sent, the whole thing is fresh -- dropping it would lose the turn entirely.
    const d = decideTurn({ ...base, transcript: 'can I book a hygienist', alreadySent: 'yes please' })
    expect(d.send).toBe(true)
    expect(d.text).toBe('can I book a hygienist')
  })

  it('reports a reason a person can read', () => {
    expect(decideTurn({ ...base, quietForMs: 50 }).reason).toMatch(/\d+ms/)
    expect(RECOGNITION_LAG_MS).toBeGreaterThan(0)
  })
})

describe('recognition revising what it already gave us', () => {
  it('does not send a sentence again when it is finalised with punctuation', () => {
    // The fourth reply in the original bug report. Web Speech delivers "hi how are you doing" as
    // an interim, then finalises it as "Hi, how are you doing?" -- same sentence, different
    // string. Compared raw, that reads as a brand new turn.
    const d = decideTurn({
      transcript: 'Hi, how are you doing?',
      alreadySent: 'hi how are you doing',
      quietForMs: 2000,
      settledForMs: 2000,
      thresholdMs: 250,
      heardSpeech: true,
    })
    expect(d.send).toBe(false)
  })

  it('still sends genuinely new words after a revision', () => {
    const d = decideTurn({
      transcript: 'Hi, how are you doing? I need an appointment.',
      alreadySent: 'hi how are you doing',
      quietForMs: 2000,
      settledForMs: 2000,
      thresholdMs: 250,
      heardSpeech: true,
    })
    expect(d.send).toBe(true)
    expect(d.text.toLowerCase()).toContain('appointment')
    expect(d.text.toLowerCase()).not.toContain('how are you')
  })

  it('sends a genuinely different sentence even if it shares a word or two', () => {
    const d = decideTurn({
      transcript: 'can I book a hygienist appointment',
      alreadySent: 'how much is a check-up',
      quietForMs: 2000,
      settledForMs: 2000,
      thresholdMs: 250,
      heardSpeech: true,
    })
    expect(d.send).toBe(true)
    expect(d.text).toBe('can I book a hygienist appointment')
  })
})
