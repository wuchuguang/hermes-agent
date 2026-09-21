/**
 * The room's rolling memory: heuristic digest captured at trim time, injected
 * into every turn prompt. Pins the whole cycle — entries leave the retained
 * window → digest captures them → the next member prompt carries them.
 */

import { describe, expect, it } from 'vitest'

import { buildGroupDigest, mergeGroupDigest } from './group-digest'
import type { GroupMessage } from './types'

function msg(from: 'user' | 'member', name: string, text: string, at = 0): GroupMessage {
  return {
    at,
    from: from === 'user' ? { kind: 'user', name } : { kind: 'member', name },
    text
  }
}

describe('buildGroupDigest', () => {
  it('summarizes empty input to nothing', () => {
    expect(buildGroupDigest([])).toBe('')
  })

  it('keeps user asks verbatim (truncated) and member activity as last-statement', () => {
    const digest = buildGroupDigest([
      msg('user', 'You', 'please ship the checkout redesign'),
      msg('member', 'dev', 'on it — starting with the cart page', 1),
      msg('member', 'qa', 'I will write the acceptance criteria', 2)
    ])

    expect(digest).toContain('please ship the checkout redesign')
    expect(digest).toContain('dev: 1 message(s); last: on it — starting with the cart page')
    expect(digest).toContain('qa: 1 message(s); last: I will write the acceptance criteria')
  })

  it('uses the first line as the headline for long member messages', () => {
    const digest = buildGroupDigest([
      msg('member', 'dev', `Foundings report:\nsecond line of detail\n${'x'.repeat(220)}`, 1)
    ])

    expect(digest).toContain('last: Foundings report:')
    expect(digest).not.toContain('second line of detail')
  })

  it('counts multiple turns per speaker', () => {
    const digest = buildGroupDigest([
      msg('member', 'dev', 'first', 1),
      msg('member', 'dev', 'second', 2)
    ])

    expect(digest).toContain('dev: 2 message(s); last: second')
  })

  it('caps the digest length', () => {
    const entries = Array.from({ length: 40 }, (_, i) => msg('user', 'You', `request number ${i} — ${'x'.repeat(100)}`, i))

    expect(buildGroupDigest(entries).length).toBeLessThanOrEqual(2048)
  })
})

describe('mergeGroupDigest', () => {
  it('returns empty when both sides are empty', () => {
    expect(mergeGroupDigest(null, [])).toBe('')
  })

  it('keeps the existing digest when nothing new leaves', () => {
    expect(mergeGroupDigest('earlier summary', [])).toBe('earlier summary')
  })

  it('returns the fresh digest when there is no existing one', () => {
    const merged = mergeGroupDigest(null, [msg('user', 'You', 'the ask')])

    expect(merged).toContain('the ask')
  })

  it('concatenates old then new, and keeps the NEWEST tail when over the cap', () => {
    const first = mergeGroupDigest(null, [msg('user', 'You', `old ask — ${'a'.repeat(1800)}`)])
    const merged = mergeGroupDigest(first, [msg('user', 'You', 'new ask that must survive')])

    expect(merged).toContain('new ask that must survive')
    expect(merged.length).toBeLessThanOrEqual(2048)
    // Over the cap, the OLDEST head is truncated away — recency bias matches
    // the window's own. (No ellipsis marker: the cut is mid-content by
    // design, and a marker would cost a char of the budget every merge.)
    expect(merged.startsWith('old ask')).toBe(false)
  })
})
