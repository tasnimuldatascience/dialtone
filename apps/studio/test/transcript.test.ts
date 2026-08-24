/**
 * Cleaning the transcript without changing what the caller meant.
 *
 * The risk is entirely in the second half. Stripping "um" is easy; stripping it too
 * enthusiastically removes real words, and a caller whose sentence has been quietly edited gets
 * an answer to a question they did not ask.
 */

import { describe, expect, it } from 'vitest'
import { cleanTranscript, endsOnFiller, isAllFiller } from '../src/transcript'

describe('cleaning a transcript', () => {
  it('removes fillers', () => {
    expect(cleanTranscript('hi uh how are you um doing')).toBe('Hi how are you doing')
  })

  it('removes stutters', () => {
    expect(cleanTranscript('can can you help me')).toBe('Can you help me')
    expect(cleanTranscript('I I I need an appointment')).toBe('I need an appointment')
  })

  it('capitalises the opening word', () => {
    // Recognition emits lower case throughout, which reads as a fault in a call record.
    expect(cleanTranscript('how much is a check-up')).toBe('How much is a check-up')
  })

  it('reports a turn that was nothing but noise', () => {
    // Not a turn. Sending it would have the agent reply to a sound.
    expect(isAllFiller('um')).toBe(true)
    expect(isAllFiller('uh um er')).toBe(true)
    expect(isAllFiller('mm-hmm')).toBe(true)
    expect(isAllFiller('hi')).toBe(false)
  })

  describe('leaves real words alone', () => {
    const keep: [string, string][] = [
      // Every one of these is used as a filler in some other sentence and is a content word
      // here. Removing them would change what the caller asked.
      ['I would like an appointment', 'I would like an appointment'],
      ['so how much is it', 'So how much is it'],
      ['well that is expensive', 'Well that is expensive'],
      ['actually can we do friday', 'Actually can we do friday'],
      ['is that the same price', 'Is that the same price'],
      // Not a stutter: the repetition is the meaning.
      ['that is very very expensive', 'That is very very expensive'],
    ]
    for (const [input, expected] of keep) {
      it(input, () => expect(cleanTranscript(input)).toBe(expected))
    }
  })

  it('keeps everything else intact', () => {
    expect(cleanTranscript('my account number is four two four two')).toBe(
      'My account number is four two four two',
    )
  })

  it('handles an empty transcript', () => {
    expect(cleanTranscript('')).toBe('')
    expect(cleanTranscript('   ')).toBe('')
  })
})

describe('trailing fillers', () => {
  it('spots a caller trailing off mid-thought', () => {
    // The cheapest possible way to avoid interrupting someone. Whatever the silence detector
    // believes, a caller who has just said "um" has not finished.
    expect(endsOnFiller('I think the problem is um')).toBe(true)
    expect(endsOnFiller('let me see uh')).toBe(true)
  })

  it('does not fire on a finished sentence', () => {
    expect(endsOnFiller('how much is a check-up')).toBe(false)
    expect(endsOnFiller('yes please')).toBe(false)
  })

  it('does not fire on a filler in the middle', () => {
    expect(endsOnFiller('hi um how are you')).toBe(false)
  })
})
