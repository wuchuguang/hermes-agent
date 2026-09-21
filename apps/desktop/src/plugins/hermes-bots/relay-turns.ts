/**
 * Cross-connection relay turn tracking — the "grok build" progress surface
 * for cross-profile bot work.
 *
 * The drain loop in `relay.ts` is the ONE place a cross-connection turn
 * passes through, so it reports here: a `bot_relay.deliver` call opens a
 * turn, the posted reply (success or classified failure) closes it. The
 * store is display-only presentation state — never persisted, never alters
 * delivery — mirroring `$botAttention`'s contract in `data.ts`.
 *
 * The statusbar pill (`relay-turns-pill.tsx`) renders the live set; history
 * entries linger briefly so failures stay readable, then self-clear.
 */

import { atom } from '@hermes/plugin-sdk'

export type RelayTurnPhase = 'delivering' | 'done' | 'failed'

export interface RelayTurn {
  /** Delivery-turn id — the outbox envelope this turn serves. */
  id: string
  /** Connection the target bot lives on. */
  connectionId: string
  /** Profile name of the bot doing the work. */
  targetProfile: string
  /** Bot that asked for the work (the sender). */
  fromProfile: string
  /** First line of the request message — the human-readable task label. */
  preview: string
  phase: RelayTurnPhase
  startedAt: number
  /** Set when the turn closes; drives the brief linger + failure detail. */
  finishedAt?: number
  /** Classified failure code forwarded from bot_relay.deliver, if any. */
  reason?: string
}

/** Live + recently-finished turns, keyed by envelope id. */
export const $relayTurns = atom<Record<string, RelayTurn>>({})

/** Finished turns linger so a failure's reason stays readable; success goes
 *  quick. Mirrors composer-status's SUCCESS/FAILURE linger split. */
const SUCCESS_LINGER_MS = 6_000
const FAILURE_LINGER_MS = 15_000
/** Hard cap so a stalled UI can never grow the map unbounded. */
const MAX_TURNS = 24
const PREVIEW_MAX = 120

const clearTimers = new Map<string, ReturnType<typeof setTimeout>>()

export function relayTurnPreview(message: unknown): string {
  const firstLine = String(message || '')
    .split('\n')
    .map(line => line.trim())
    .find(line => line.length > 0)

  const flat = (firstLine || '').replace(/\s+/g, ' ').trim()

  return flat.length > PREVIEW_MAX ? `${flat.slice(0, PREVIEW_MAX - 1)}…` : flat
}

function pruneOverflow(turns: Record<string, RelayTurn>): Record<string, RelayTurn> {
  const ids = Object.keys(turns)

  if (ids.length <= MAX_TURNS) {
    return turns
  }

  const finished = ids
    .filter(id => turns[id].finishedAt)
    .sort((a, b) => (turns[a].finishedAt ?? 0) - (turns[b].finishedAt ?? 0))

  for (const id of finished.slice(0, ids.length - MAX_TURNS)) {
    delete turns[id]
  }

  return turns
}

/** Open a turn when the drain hands an envelope to `bot_relay.deliver`. */
export function beginRelayTurn(input: {
  connectionId: string
  envelopeId: string
  fromProfile: string
  message: string
  targetProfile: string
}) {
  const existing = $relayTurns.get()[input.envelopeId]

  if (existing && existing.phase === 'delivering') {
    return
  }

  const timer = clearTimers.get(input.envelopeId)

  if (timer) {
    clearTimeout(timer)
    clearTimers.delete(input.envelopeId)
  }

  $relayTurns.set(
    pruneOverflow({
      ...$relayTurns.get(),
      [input.envelopeId]: {
        id: input.envelopeId,
        connectionId: input.connectionId,
        targetProfile: input.targetProfile,
        fromProfile: input.fromProfile,
        preview: relayTurnPreview(input.message),
        phase: 'delivering',
        startedAt: Date.now()
      }
    })
  )
}

function settleTurn(id: string, phase: 'done' | 'failed', reason?: string) {
  const turns = $relayTurns.get()
  const turn = turns[id]

  if (!turn || turn.phase !== 'delivering') {
    return
  }

  $relayTurns.set({
    ...turns,
    [id]: {
      ...turn,
      phase,
      finishedAt: Date.now(),
      ...(reason
        ? { reason }
        : {})
    }
  })

  clearTimers.set(
    id,
    setTimeout(() => endRelayTurn(id), phase === 'done' ? SUCCESS_LINGER_MS : FAILURE_LINGER_MS)
  )
}

/** The reply reached the sender — the turn is over (success or typed error). */
export function settleRelayTurnDone(id: string) {
  settleTurn(id, 'done')
}

/** Delivery failed; `reason` is bot_relay.deliver's classified code when present. */
export function settleRelayTurnFailed(id: string, reason?: string) {
  settleTurn(id, 'failed', reason || undefined)
}

/** Manual X (or linger expiry) — drop the row at once. */
export function endRelayTurn(id: string) {
  const timer = clearTimers.get(id)

  if (timer) {
    clearTimeout(timer)
    clearTimers.delete(id)
  }

  const turns = $relayTurns.get()

  if (!turns[id]) {
    return
  }

  const next = { ...turns }

  delete next[id]
  $relayTurns.set(next)
}
