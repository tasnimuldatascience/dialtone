/**
 * Talking over the agent.
 *
 * Two failures, pulling against each other, and the tests are split between them:
 *
 *   TOO EAGER    the agent stops mid-word because a chair moved, or because its own voice came
 *                back through the microphone. Unusable — worse than not being interruptible.
 *   TOO SLOW     the caller says "no, not that one" and is ignored until the agent has finished
 *                reading out four appointment times. That is the bug this feature exists to fix.
 *
 * Every test below is a frame-by-frame trace, because the property being checked is about time
 * and a single-frame assertion cannot express it.
 */

import { describe, expect, it } from 'vitest'
import { BARGE_LEVEL, BARGE_MS, trackBargeIn } from '../src/bargein'

/** Run a level trace at 60ms per frame and report when, if ever, it interrupted. */
function trace(
  frames: { level: number; agentAudible?: boolean }[],
  step = 60,
): { firedAt: number | null; count: number; reasons: string[] } {
  let loudSince = 0
  let alreadyInterrupted = false
  let firedAt: number | null = null
  let count = 0
  const reasons: string[] = []

  frames.forEach((frame, i) => {
    const now = i * step
    const decision = trackBargeIn({
      agentAudible: frame.agentAudible ?? true,
      level: frame.level,
      now,
      loudSince,
      alreadyInterrupted,
    })
    loudSince = decision.loudSince
    reasons.push(decision.reason)
    if (decision.interrupt) {
      count += 1
      if (firedAt === null) firedAt = now
      alreadyInterrupted = true
    }
  })
  return { firedAt, count, reasons }
}

const loud = (n: number) => Array.from({ length: n }, () => ({ level: 0.4 }))
const quiet = (n: number) => Array.from({ length: n }, () => ({ level: 0.02 }))
/** Echo cancellation leaves a residue. It must sit below the threshold. */
const residue = (n: number) => Array.from({ length: n }, () => ({ level: 0.09 }))

describe('interrupting the agent', () => {
  it('gives way to someone talking over it', () => {
    const { firedAt } = trace([...quiet(3), ...loud(12)])
    expect(firedAt).not.toBeNull()
  })

  it('waits long enough to be sure', () => {
    // Anything faster and a cough stops the agent mid-word.
    const { firedAt } = trace([...loud(20)])
    expect(firedAt).toBeGreaterThanOrEqual(BARGE_MS - 60)
  })

  it('does not wait so long that the point is lost', () => {
    // The whole value is answering quickly. Half a second of shouting before it reacts is not
    // meaningfully better than waiting for it to finish.
    const { firedAt } = trace([...loud(20)])
    expect(firedAt).toBeLessThan(700)
  })

  it('fires once per interruption, not once per frame', () => {
    // The loop runs ~16 times a second. Without the latch, one interruption sends a burst of
    // messages and the history is truncated repeatedly, each time to less than before.
    const { count } = trace([...loud(30)])
    expect(count).toBe(1)
  })
})

describe('does not fire when it should not', () => {
  it('ignores the agent coming back through the microphone', () => {
    // THE FAILURE THAT MADE HALF-DUPLEX NECESSARY IN THE FIRST PLACE. Echo cancellation leaves a
    // residue; if that residue could trigger this, the agent would interrupt itself and the call
    // would spiral. There is no recovery from that, so the threshold has to clear it.
    const { firedAt } = trace([...residue(40)])
    expect(firedAt).toBeNull()
  })

  it('ignores a single loud frame', () => {
    // A door, a keyboard, a chair.
    const { firedAt } = trace([...quiet(4), { level: 0.9 }, ...quiet(20)])
    expect(firedAt).toBeNull()
  })

  it('ignores a short burst that stops', () => {
    const short = Math.floor(BARGE_MS / 60) - 2
    const { firedAt } = trace([...quiet(2), ...loud(short), ...quiet(20)])
    expect(firedAt).toBeNull()
  })

  it('needs the run to be unbroken', () => {
    // Two half-length bursts either side of a gap are not one interruption. Letting them add up
    // is how room noise eventually crosses any threshold you pick.
    const half = Math.floor(BARGE_MS / 60 / 2)
    const { firedAt } = trace([...loud(half), ...quiet(3), ...loud(half), ...quiet(10)])
    expect(firedAt).toBeNull()
  })

  it('does nothing at all while the agent is silent', () => {
    // There is nothing to interrupt. The ordinary endpointer owns this case, and firing here
    // would truncate a reply the caller heard in full.
    const frames = Array.from({ length: 30 }, () => ({ level: 0.6, agentAudible: false }))
    expect(trace(frames).firedAt).toBeNull()
  })

  it('forgets a run once the agent stops', () => {
    // Loudness that began during agent audio must not fire a beat after it ended — by then the
    // reply was fully heard, and truncating it would tell the model it said less than it did.
    const almost = Math.floor(BARGE_MS / 60) - 1
    const frames = [
      ...loud(almost),
      ...Array.from({ length: 10 }, () => ({ level: 0.6, agentAudible: false })),
    ]
    expect(trace(frames).firedAt).toBeNull()
  })
})

describe('what it tells the operator', () => {
  it('counts down while someone is talking over', () => {
    const { reasons } = trace([...loud(3)])
    expect(reasons.some((r) => /talking over/.test(r))).toBe(true)
  })

  it('says so once it has given way', () => {
    const { reasons } = trace([...loud(12)])
    expect(reasons).toContain('you interrupted')
  })

  it('is silent when there is nothing to say', () => {
    const frames = Array.from({ length: 5 }, () => ({ level: 0.02, agentAudible: false }))
    expect(trace(frames).reasons.every((r) => r === '')).toBe(true)
  })
})

describe('the thresholds themselves', () => {
  it('sits above what echo cancellation leaves behind', () => {
    // Measured on a laptop with speakers at a normal volume: residue peaks around 0.10.
    expect(BARGE_LEVEL).toBeGreaterThan(0.12)
  })

  it('is short enough to feel like being listened to', () => {
    expect(BARGE_MS).toBeLessThanOrEqual(500)
  })

  it('is long enough to outlast a noise', () => {
    expect(BARGE_MS).toBeGreaterThanOrEqual(250)
  })
})
