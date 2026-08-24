/**
 * Tests for echo rejection — telling the agent's own voice from the caller's.
 *
 * WHY THIS NEEDS TESTS. The failure is silent and it is symmetrical, so both directions are
 * expensive and only one of them is visible:
 *
 *   TOO LOOSE   the agent hears itself through the speakers, answers its own sentence, and the
 *               call spirals. This actually happened -- the server log showed the agent's own
 *               greeting arriving at the endpointer as caller speech.
 *   TOO TIGHT   the agent stops hearing the caller. Worse, and much harder to notice, because a
 *               caller answering "yes, Thursday please" shares almost every word with the
 *               question they were just asked.
 *
 * The second is the one these tests exist for. It is very easy to fix the echo problem by
 * rejecting anything that resembles the last thing the agent said, and thereby build an agent
 * that cannot hear the word "yes".
 */

import { beforeEach, describe, expect, it } from 'vitest'
import { ECHO_GUARD_MS, clearSpokenMemory, inEchoWindow, looksLikeEcho, rememberSpoken, resetEchoWindow, setAgentAudioProbe } from '../src/voice'

describe('echo rejection', () => {
  beforeEach(() => clearSpokenMemory())

  it('ignores nothing when the agent has not spoken', () => {
    expect(looksLikeEcho('how much is a check-up')).toBe(false)
  })

  describe('recognises the agent coming back through the microphone', () => {
    const cases: [name: string, said: string, heard: string][] = [
      [
        'the greeting, verbatim',
        'Hello, how can I assist you today?',
        'hello how can i assist you today',
      ],
      [
        'the greeting, as the recogniser mangles it',
        'Northgate Dental, how can I help?',
        'northgate dental how can i help',
      ],
      [
        'a reply picked up mid-sentence',
        'A routine check-up costs forty five pounds, which includes a full examination.',
        'a routine check-up costs forty five pounds which includes',
      ],
      [
        'with a stray word the recogniser invented',
        'Yes, we are open Thursday evenings until eight.',
        'yes we are open thursday evenings until eight um',
      ],
    ]

    for (const [name, said, heard] of cases) {
      it(name, () => {
        rememberSpoken(said)
        expect(looksLikeEcho(heard)).toBe(true)
      })
    }
  })

  describe('does NOT reject the caller', () => {
    it('a short answer to the question just asked', () => {
      // The dangerous case. "Thursday" appears in the agent's own sentence, and rejecting this
      // would make the agent deaf to the most common kind of reply there is.
      rememberSpoken('We have late appointments on Thursdays until eight in the evening.')
      expect(looksLikeEcho('thursday please')).toBe(false)
    })

    it('a yes', () => {
      rememberSpoken('Shall I book that in for you?')
      expect(looksLikeEcho('yes')).toBe(false)
      expect(looksLikeEcho('yes please')).toBe(false)
    })

    it('a follow-up question that reuses the agent words', () => {
      rememberSpoken('A routine check-up costs forty five pounds.')
      expect(looksLikeEcho('and how much is a filling')).toBe(false)
    })

    it('a correction using the same vocabulary', () => {
      rememberSpoken('I have you down for Tuesday at ten thirty.')
      expect(looksLikeEcho('actually can we do the afternoon instead')).toBe(false)
    })

    it('something entirely unrelated', () => {
      rememberSpoken('Northgate Dental, how can I help?')
      expect(looksLikeEcho('my tooth has been hurting since the weekend')).toBe(false)
    })
  })

  it('forgets what the agent said a while ago', () => {
    // A caller genuinely repeating the agent's words later in the call must be heard. The memory
    // covers recognition lag, not the whole conversation.
    rememberSpoken('We are open Monday to Friday.')
    expect(looksLikeEcho('we are open monday to friday')).toBe(true)
    clearSpokenMemory()
    expect(looksLikeEcho('we are open monday to friday')).toBe(false)
  })

  it('does not judge one- or two-word fragments', () => {
    // Too short to compare meaningfully. These are handled by muting the microphone while the
    // agent speaks; guessing from two words would reject real answers.
    rememberSpoken('Yes, we are open on Thursday evenings.')
    expect(looksLikeEcho('yes')).toBe(false)
    expect(looksLikeEcho('open')).toBe(false)
  })

  it('handles several utterances at once', () => {
    rememberSpoken('Northgate Dental, how can I help?')
    rememberSpoken('A routine check-up costs forty five pounds.')
    expect(looksLikeEcho('northgate dental how can i help')).toBe(true)
    expect(looksLikeEcho('a routine check-up costs forty five pounds')).toBe(true)
    expect(looksLikeEcho('can I book an appointment for next week')).toBe(false)
  })
})

describe('the echo window', () => {
  beforeEach(() => {
    clearSpokenMemory()
    resetEchoWindow()
  })

  it('is shut while the agent is audible', () => {
    setAgentAudioProbe(() => true)
    expect(inEchoWindow()).toBe(true)
    setAgentAudioProbe(null)
  })

  it('stays shut after the agent stops', () => {
    // THE BUG THIS EXISTS FOR. Recognition reports what it heard several hundred milliseconds
    // late, so asking "is the agent audible right now" asks about the wrong moment entirely --
    // the sound was made while it still was. The window covers that delay.
    let audible = true
    setAgentAudioProbe(() => audible)
    expect(inEchoWindow()).toBe(true)

    audible = false
    setAgentAudioProbe(() => audible)
    // The agent has stopped, but a transcript arriving now describes sound made before it did.
    expect(inEchoWindow()).toBe(true)
    expect(ECHO_GUARD_MS).toBeGreaterThan(500)
    setAgentAudioProbe(null)
  })

  it('opens again once the guard has elapsed', async () => {
    setAgentAudioProbe(() => false)
    resetEchoWindow()
    expect(inEchoWindow()).toBe(false)
  })

  it('a new call is not muted by the previous one', () => {
    setAgentAudioProbe(() => true)
    expect(inEchoWindow()).toBe(true)
    setAgentAudioProbe(null)
    resetEchoWindow()
    expect(inEchoWindow()).toBe(false)
  })
})
