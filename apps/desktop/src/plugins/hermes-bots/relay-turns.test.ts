/**
 * The relay turn store's lifecycle contract: open on deliver, close on the
 * posted reply, linger briefly after finishing, dismiss at once on demand,
 * and never resurrect a live turn from a duplicate envelope.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $relayTurns,
  beginRelayTurn,
  endRelayTurn,
  relayTurnPreview,
  settleRelayTurnDone,
  settleRelayTurnFailed
} from './relay-turns'

function begin(id: string, overrides: Record<string, unknown> = {}) {
  beginRelayTurn({
    connectionId: 'conn-b',
    envelopeId: id,
    fromProfile: 'default',
    message: 'check the deploy status',
    targetProfile: 'dev',
    ...overrides
  })
}

beforeEach(() => {
  $relayTurns.set({})
  vi.useFakeTimers()
})

describe('relay turns', () => {
  it('opens a turn in delivering state with a one-line preview', () => {
    begin('e1')

    const turn = $relayTurns.get().e1

    expect(turn).toMatchObject({
      connectionId: 'conn-b',
      fromProfile: 'default',
      phase: 'delivering',
      preview: 'check the deploy status',
      targetProfile: 'dev'
    })
    expect(turn?.finishedAt).toBeUndefined()
  })

  it('previews the first non-empty line, collapsed and capped', () => {
    expect(relayTurnPreview('\n\n  hello   world \nsecond line')).toBe('hello world')
    expect(relayTurnPreview('x'.repeat(200))).toHaveLength(120)
    expect(relayTurnPreview('x'.repeat(200)).endsWith('…')).toBe(true)
    expect(relayTurnPreview('')).toBe('')
  })

  it('does not resurrect a live turn when the same envelope re-reports', () => {
    begin('e1')
    const first = $relayTurns.get().e1

    begin('e1', { message: 'a different message' })

    expect($relayTurns.get().e1).toBe(first)
  })

  it('settles done and lingers briefly, then self-clears', () => {
    begin('e1')
    settleRelayTurnDone('e1')

    expect($relayTurns.get().e1).toMatchObject({ phase: 'done' })
    expect($relayTurns.get().e1?.finishedAt).toBeDefined()

    vi.advanceTimersByTime(6_000)

    expect($relayTurns.get().e1).toBeUndefined()
  })

  it('settles failed with the classified reason and lingers longer', () => {
    begin('e1')
    settleRelayTurnFailed('e1', 'runtime_offline')

    expect($relayTurns.get().e1).toMatchObject({ phase: 'failed', reason: 'runtime_offline' })

    vi.advanceTimersByTime(6_000)

    expect($relayTurns.get().e1).toBeDefined()

    vi.advanceTimersByTime(9_000)

    expect($relayTurns.get().e1).toBeUndefined()
  })

  it('settling an unknown or already-settled id changes nothing', () => {
    settleRelayTurnDone('missing')
    begin('e1')
    settleRelayTurnFailed('e1')
    const settled = $relayTurns.get().e1
    settleRelayTurnDone('e1')

    expect($relayTurns.get().e1).toBe(settled)
  })

  it('dismisses a lingering entry at once', () => {
    begin('e1')
    settleRelayTurnDone('e1')
    endRelayTurn('e1')

    expect($relayTurns.get().e1).toBeUndefined()
  })

  it('a re-delivered envelope id starts a fresh turn after settling', () => {
    begin('e1')
    settleRelayTurnDone('e1')
    begin('e1', { message: 'round two' })

    expect($relayTurns.get().e1).toMatchObject({ phase: 'delivering', preview: 'round two' })
  })

  it('caps the map by dropping the oldest finished turns', () => {
    for (let i = 0; i < 30; i += 1) {
      begin(`e${i}`)
      settleRelayTurnDone(`e${i}`)
    }

    const turns = $relayTurns.get()

    expect(Object.keys(turns)).toHaveLength(24)
    expect(turns.e0).toBeUndefined()
    expect(turns.e29).toBeDefined()
  })
})
