/**
 * Making a raw transcript readable without changing what the caller said.
 *
 * Two risks, pulling in opposite directions. Do too little and the caller sees
 * "hi uh i i wanted to ask how much is a check up" and assumes the software is broken. Do too
 * much and you silently edit their sentence, and they get an answer to a question they did not
 * ask. Nearly every test here is about the second one.
 */

import { describe, expect, it } from 'vitest'
import { looksLikeAQuestion, polish } from '../src/transcript'

describe('polishing a transcript', () => {
  it('turns a raw recognition stream into a sentence', () => {
    expect(polish('hi uh i i wanted to ask how much is a check up')).toBe(
      'Hi I wanted to ask how much is a check up.',
    )
  })

  it('capitalises the pronoun recognition always gets wrong', () => {
    // The single most obvious tell that nothing has touched a transcript.
    expect(polish('i think i can make it')).toBe('I think I can make it.')
    expect(polish("i'm free on tuesday")).toBe("I'm free on tuesday.")
    expect(polish("i've been before and i'd like to come back")).toBe(
      "I've been before and I'd like to come back.",
    )
  })

  it('does not capitalise an i inside a word', () => {
    expect(polish('this is a filling')).toBe('This is a filling.')
    expect(polish('is it in the diary')).toBe('Is it in the diary?')
  })

  it('ends a question with a question mark', () => {
    expect(polish('how much is a check up')).toBe('How much is a check up?')
    expect(polish('are you open on saturday')).toBe('Are you open on saturday?')
    expect(polish('can i come tomorrow')).toBe('Can I come tomorrow?')
  })

  it('ends a statement with a full stop', () => {
    expect(polish('my tooth hurts')).toBe('My tooth hurts.')
    expect(polish('tomorrow works for me')).toBe('Tomorrow works for me.')
  })

  it('finds the question word after a lead-in', () => {
    expect(polish('so how much is it')).toBe('So how much is it?')
    expect(polish('and can i pay by card')).toBe('And can I pay by card?')
  })

  it('leaves punctuation the caller already has', () => {
    expect(polish('yes, that works!')).toBe('Yes, that works!')
    expect(polish('how much?')).toBe('How much?')
  })

  it('drops a trailing comma rather than closing on it', () => {
    // Recognition emits these constantly and they read as the sentence being cut off.
    expect(polish('i need an appointment,')).toBe('I need an appointment.')
    expect(polish('tomorrow morning -')).toBe('Tomorrow morning.')
  })

  it('capitalises after a sentence ending', () => {
    expect(polish('my tooth hurts. can i come in today')).toBe(
      'My tooth hurts. Can I come in today?',
    )
  })

  it('closes on the last sentence, not the first', () => {
    // A statement followed by a question. Reading the opening word would close it with a full
    // stop and turn the caller's question into a remark.
    expect(polish('that is fine. what time do you open')).toBe(
      'That is fine. What time do you open?',
    )
    expect(polish('how much is it. i will take it')).toBe('How much is it. I will take it.')
  })
})

/* THE PART THAT MATTERS. A previous version rebuilt the text out of /[\w'’-]+/ matches and
 * joined them with spaces, which deleted every character that was not a letter or a digit --
 * an email address and a price, destroyed, in the one place where being exact matters most. */
describe('does not destroy what it does not understand', () => {
  const intact: [string, string][] = [
    ['my email is sam@example.com', 'My email is sam@example.com.'],
    ["it's £120, isn't it", "It's £120, isn't it."],
    ['a check-up is $45', 'A check-up is $45.'],
    ['call me on 212-555-0142', 'Call me on 212-555-0142.'],
    ['my number is (212) 555-0142', 'My number is (212) 555-0142.'],
    ['we open at 8:30', 'We open at 8:30.'],
    ['it costs 50% more', 'It costs 50% more.'],
    ['ref NG5EA086', 'Ref NG5EA086.'],
  ]
  for (const [input, expected] of intact) {
    it(input, () => expect(polish(input)).toBe(expected))
  }
})

describe('leaves real words alone', () => {
  const keep: [string, string][] = [
    // Every one of these is used as a filler in some other sentence and is a content word
    // here. Removing them would change what the caller asked.
    ['I would like an appointment', 'I would like an appointment.'],
    ['well that is expensive', 'Well that is expensive.'],
    ['actually can we do friday', 'Actually can we do friday.'],
    // Not a stutter: the repetition is the meaning.
    ['that is very very expensive', 'That is very very expensive.'],
    // Numbers read out loud. The endpointer's whole reason for existing — and every word of it
    // has to survive.
    ['my account number is four two four two', 'My account number is four two four two.'],
  ]
  for (const [input, expected] of keep) {
    it(input, () => expect(polish(input)).toBe(expected))
  }
})

describe('while the caller is still speaking', () => {
  it('does not close a sentence that is still being said', () => {
    // A full stop that appears and then jumps as more words arrive is worse than none.
    expect(polish('how much is a', { punctuate: false })).toBe('How much is a')
    expect(polish('i need an', { punctuate: false })).toBe('I need an')
  })

  it('still tidies the words', () => {
    expect(polish('uh i i need an', { punctuate: false })).toBe('I need an')
  })
})

describe('spoken punctuation, in dictation only', () => {
  it('writes the marks a person dictates', () => {
    expect(polish('hello comma how are you question mark', { spokenMarks: true })).toBe(
      'Hello, how are you?',
    )
    expect(polish('book me in full stop thanks', { spokenMarks: true })).toBe(
      'Book me in. Thanks.',
    )
  })

  it('is off on a live call', () => {
    // "Period" said down a phone line is a length of time, not a full stop. Rewriting it would
    // delete a word the caller meant.
    expect(polish('the period of the guarantee')).toBe('The period of the guarantee.')
    expect(polish('a comma is a punctuation mark')).toBe('A comma is a punctuation mark.')
  })
})

describe('nothing to say', () => {
  it('returns nothing for noise', () => {
    expect(polish('um')).toBe('')
    expect(polish('uh um er')).toBe('')
    expect(polish('')).toBe('')
    expect(polish('   ')).toBe('')
  })
})

describe('spotting a question', () => {
  it('recognises the openers', () => {
    expect(looksLikeAQuestion('how much is it')).toBe(true)
    expect(looksLikeAQuestion('would tuesday work')).toBe(true)
    expect(looksLikeAQuestion('my tooth hurts')).toBe(false)
    expect(looksLikeAQuestion('')).toBe(false)
  })
})
