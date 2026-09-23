/**
 * The statusbar pill renders the relay turn store: hidden while empty,
 * spinner + count while running, finished state during the linger, and a
 * popover listing each turn with its phase. The X on a finished row drops
 * it via the REAL store (relay-turns is not mocked here).
 *
 * SDK and i18n are replaced with literal factories — a partial
 * `importOriginal` mock of `./i18n` deadlocks module init (the real i18n
 * pulls the whole plugin-sdk chain back in), so copy here is literal and the
 * label-shape contract is pinned by i18n.test.ts instead.
 */

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $relayTurns, beginRelayTurn, endRelayTurn, settleRelayTurnDone, settleRelayTurnFailed } from './relay-turns'

vi.mock('@hermes/plugin-sdk', async () => {
  const nanostores = await import('nanostores')

  return {
    Codicon: ({ name }: { name: string }) => <span aria-hidden data-icon={name} />,
    GlyphSpinner: () => <span aria-hidden data-icon="spinner" />,
    // relay-turns calls atom() at module top level — the real nanostores one.
    atom: nanostores.atom,
    host: {},
    useValue: <T,>(store: { get: () => T }) => store.get(),
    Popover: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
    PopoverContent: ({ children }: { children?: React.ReactNode }) => <div data-slot="popover-content">{children}</div>,
    PopoverTrigger: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
    usePluginI18n: () => (key: string) => key
  }
})

vi.mock('./i18n', () => ({
  useBots: () => ({
    relay: {
      dismiss: 'Dismiss',
      phaseDelivering: (seconds: number) => `working · ${seconds}s`,
      phaseDone: 'done',
      phaseFailed: (reason?: string) => (reason ? `failed · ${reason}` : 'failed'),
      pillFinished: (count: number) => `Bot work · ${count} finished`,
      pillRunning: (count: number) => `Bot work · ${count} running`,
      popoverTitle: (running: number, total: number) =>
        running > 0 ? `Cross-bot tasks — ${running} running` : `Cross-bot tasks — ${total} finished`,
      turnRowTitle: (target: string, from: string) => `${target} ← ${from}`
    }
  })
}))

import { RelayTurnsPill } from './relay-turns-pill'

function pill() {
  return render(<RelayTurnsPill />)
}

function begin(id: string, message: string) {
  beginRelayTurn({
    connectionId: 'conn-b',
    envelopeId: id,
    fromProfile: 'default',
    message,
    targetProfile: 'dev'
  })
}

beforeEach(() => {
  $relayTurns.set({})
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('RelayTurnsPill', () => {
  it('renders nothing while no turns are tracked', () => {
    const { container } = pill()

    expect(container.children).toHaveLength(0)
  })

  it('shows the running count while a turn is delivering', () => {
    begin('e1', 'check the deploy status')

    const { container } = pill()

    expect(screen.getByText('Bot work · 1 running')).toBeDefined()
    expect(container.querySelector('[data-running]')).toBeDefined()
    expect(container.querySelector('[data-icon="spinner"]')).toBeDefined()
  })

  it('lists the turn with target, sender, preview, and phase', () => {
    begin('e1', 'check the deploy status')

    pill()

    expect(screen.getByText('dev ← default')).toBeDefined()
    expect(screen.getByText('check the deploy status')).toBeDefined()
    expect(screen.getByText(/working ·/)).toBeDefined()
  })

  it('switches to the finished label during the linger and the X drops the row', () => {
    begin('e1', 'check the deploy status')
    settleRelayTurnDone('e1')

    pill()

    expect(screen.getByText('Bot work · 1 finished')).toBeDefined()

    screen.getByRole('button', { name: 'Dismiss' }).click()

    expect($relayTurns.get().e1).toBeUndefined()
  })

  it('shows the classified failure reason on a failed turn', () => {
    begin('e1', 'check the deploy status')
    settleRelayTurnFailed('e1', 'runtime_offline')

    pill()

    expect(screen.getByText('failed · runtime_offline')).toBeDefined()
  })

  it('newest turn sorts first in the popover', () => {
    vi.useFakeTimers()
    beginRelayTurn({
      connectionId: 'conn-b',
      envelopeId: 'e1',
      fromProfile: 'default',
      message: 'older task',
      targetProfile: 'dev'
    })
    vi.advanceTimersByTime(1_000)
    beginRelayTurn({
      connectionId: 'conn-b',
      envelopeId: 'e2',
      fromProfile: 'pm',
      message: 'newer task',
      targetProfile: 'qa'
    })

    const { container } = pill()
    const rows = [...container.querySelectorAll('[data-slot="relay-turn-row"]')]

    expect(rows).toHaveLength(2)
    expect(rows[0].textContent).toContain('qa ← pm')
    expect(rows[0].textContent).toContain('newer task')
    expect(rows[1].textContent).toContain('dev ← default')
  })

  it('the exported endRelayTurn clears the live store (wiring sanity)', () => {
    begin('e1', 'task')
    endRelayTurn('e1')

    expect($relayTurns.get().e1).toBeUndefined()
  })
})
