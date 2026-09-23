/**
 * The drain loop reports every cross-connection turn into the relay turn
 * store: deliver opens it, the posted reply closes it (done or failed with
 * the classified reason), and a no-target envelope never opens one.
 *
 * Drives the REAL drain through startBotRelay with the same mock shape
 * relay.test.ts uses; only the store is observed directly.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ProfileRoute } from './types'

const { clearBotAttentionMock, hostMock, noteBotAttentionMock, UnboundedCache } = vi.hoisted(() => ({
  clearBotAttentionMock: vi.fn(),
  hostMock: {
    onEvent: vi.fn(),
    profileRoutes: vi.fn(),
    requestProfile: vi.fn(),
    retainProfileSocket: vi.fn()
  } as Record<string, unknown>,
  noteBotAttentionMock: vi.fn(),
  UnboundedCache: class extends Map {
    constructor(_max: number) {
      super()
    }
  }
}))

vi.mock('@hermes/plugin-sdk', async () => {
  const nanostores = await import('nanostores')

  return { host: hostMock, LruCache: UnboundedCache, atom: nanostores.atom }
})

vi.mock('./data', () => ({
  botHandle: (name: string) => name,
  clearBotAttention: clearBotAttentionMock,
  noteBotAttention: noteBotAttentionMock
}))

const RELAY_PUSH_DEBOUNCE_MS = 250

const route = (id: string): ProfileRoute => ({
  connectionId: id,
  mode: 'remote',
  profile: 'default',
  targetProfile: 'default'
})

interface RelayCall {
  connectionId: string
  method: string
  params: Record<string, unknown>
}

function respondWith(handler: (call: RelayCall) => unknown) {
  const calls: RelayCall[] = []

  ;(hostMock.requestProfile as ReturnType<typeof vi.fn>).mockImplementation(
    async (target: ProfileRoute, method: string, params: Record<string, unknown>) => {
      const call = { connectionId: target.connectionId, method, params: structuredClone(params ?? {}) }

      calls.push(call)

      return handler(call)
    }
  )

  return calls
}

async function loadRelay() {
  vi.resetModules()

  // resetModules gives ./relay a FRESH ./relay-turns instance (a new atom),
  // so the test must import BOTH after the reset or it observes a store the
  // drain never writes to.
  const [relay, turns] = await Promise.all([import('./relay'), import('./relay-turns')])

  return { relay, $relayTurns: turns.$relayTurns }
}

async function pushAndSettle() {
  const listener = (hostMock.onEvent as ReturnType<typeof vi.fn>).mock.calls.at(-1)?.[1] as () => void

  listener()
  await vi.advanceTimersByTimeAsync(RELAY_PUSH_DEBOUNCE_MS + 10)
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  hostMock.onEvent = vi.fn(() => vi.fn())
  hostMock.profileRoutes = vi.fn(async () => [route('a'), route('b')])
  hostMock.requestProfile = vi.fn(async () => ({}))
  hostMock.retainProfileSocket = vi.fn(() => vi.fn())
})

afterEach(() => {
  vi.useRealTimers()
})

function envelope(overrides: Record<string, unknown> = {}) {
  return {
    from_handle: 'hermes',
    from_profile: 'default',
    id: 'env-1',
    message: 'please check the deploy status',
    target_connection: 'b',
    target_profile: 'dev',
    ...overrides
  }
}

describe('drain loop → relay turn store', () => {
  it('a delivered envelope opens a turn and the reply settles it done', async () => {
    let resolveDeliver!: (value: { reply: string }) => void

    const calls = respondWith(call => {
      if (call.method === 'bot_relay.deliver') {
        return new Promise(resolve => {
          resolveDeliver = resolve
        })
      }

      // Only ONE sender holds the envelope — both connections returning it
      // would deliver the same turn twice (the second reopen resets phase).
      return call.method === 'bot_relay.outbox.drain' && call.connectionId === 'a' ? { envelopes: [envelope()] } : {}
    })

    const {
      relay: { startBotRelay, stopBotRelay },
      $relayTurns
    } = await loadRelay()

    startBotRelay()
    await vi.advanceTimersByTimeAsync(0)
    await pushAndSettle()
    await vi.advanceTimersByTimeAsync(0)

    expect($relayTurns.get()['env-1']).toMatchObject({
      fromProfile: 'default',
      phase: 'delivering',
      preview: 'please check the deploy status',
      targetProfile: 'dev'
    })

    // Settle the deliver turn; the drain continuation (settle → reply RPC)
    // finishes over a few microtask hops.
    resolveDeliver({ reply: 'all good' })

    for (let i = 0; i < 6; i += 1) {
      await vi.advanceTimersByTimeAsync(0)
    }

    expect($relayTurns.get()['env-1']).toMatchObject({ phase: 'done' })
    expect(calls.filter(call => call.method === 'bot_relay.reply')).toHaveLength(1)

    stopBotRelay()
  })

  it('a failed deliver settles the turn with the classified reason', async () => {
    respondWith(call => {
      if (call.method === 'bot_relay.deliver') {
        throw { data: { reason: 'runtime_offline' }, message: 'delivery failed' }
      }

      return call.method === 'bot_relay.outbox.drain' ? { envelopes: [envelope()] } : {}
    })

    const {
      relay: { startBotRelay, stopBotRelay },
      $relayTurns
    } = await loadRelay()

    startBotRelay()
    await vi.advanceTimersByTimeAsync(0)
    await pushAndSettle()
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(0)

    expect($relayTurns.get()['env-1']).toMatchObject({ phase: 'failed', reason: 'runtime_offline' })

    stopBotRelay()
  })

  it('an envelope with no reachable target never opens a turn', async () => {
    respondWith(call =>
      call.method === 'bot_relay.outbox.drain' ? { envelopes: [envelope({ target_connection: 'ghost' })] } : {}
    )

    const {
      relay: { startBotRelay, stopBotRelay },
      $relayTurns
    } = await loadRelay()

    startBotRelay()
    await vi.advanceTimersByTimeAsync(0)
    await pushAndSettle()
    await vi.advanceTimersByTimeAsync(0)

    expect($relayTurns.get()).toEqual({})

    stopBotRelay()
  })
})
