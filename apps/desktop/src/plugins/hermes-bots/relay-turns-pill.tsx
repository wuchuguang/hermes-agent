/**
 * The statusbar pill for cross-connection bot turns — the "grok build"
 * progress surface for cross-profile work.
 *
 * Rendered as a plugin statusbar contribution (a `render()` item, so it owns
 * its own state and popover). Hidden entirely when no turns are tracked;
 * a spinner + count while work runs, a dim dot for lingering results.
 * The popover lists each turn: target bot ← sender, the task's first line,
 * phase, elapsed time, and an X to dismiss a lingering entry.
 */

import { Codicon, GlyphSpinner, Popover, PopoverContent, PopoverTrigger, useValue } from '@hermes/plugin-sdk'
import { useState } from 'react'

import { useBots } from './i18n'
import { $relayTurns, endRelayTurn, type RelayTurn } from './relay-turns'

const PHASE_ICON: Record<RelayTurn['phase'], { name: string; spinning?: boolean }> = {
  delivering: { name: 'loading', spinning: true },
  done: { name: 'check' },
  failed: { name: 'error' }
}

function RelayTurnRow({ turn }: { turn: RelayTurn }) {
  const b = useBots()
  const icon = PHASE_ICON[turn.phase]

  return (
    <div className="flex items-start gap-2 px-2.5 py-1.5" data-phase={turn.phase} data-slot="relay-turn-row">
      <span className="mt-0.5 shrink-0 text-(--ui-text-secondary)">
        {icon.spinning ? <GlyphSpinner className="text-[10px]" /> : <Codicon aria-hidden name={icon.name} size={13} />}
      </span>
      <div className="min-w-0 flex-1">
        <div className="truncate text-[0.7rem] font-medium text-(--ui-text-primary)">
          {b.relay.turnRowTitle(turn.targetProfile, turn.fromProfile)}
        </div>
        {turn.preview && (
          <div className="truncate text-[0.7rem] text-(--ui-text-tertiary)" title={turn.preview}>
            {turn.preview}
          </div>
        )}
        <div className="text-[0.65rem] text-(--ui-text-tertiary)">
          {turn.phase === 'delivering'
            ? b.relay.phaseDelivering(elapsedSeconds(turn.startedAt))
            : turn.phase === 'failed'
              ? b.relay.phaseFailed(turn.reason || undefined)
              : b.relay.phaseDone}
        </div>
      </div>
      {turn.phase !== 'delivering' && (
        <button
          aria-label={b.relay.dismiss}
          className="mt-0.5 shrink-0 rounded p-0.5 text-(--ui-text-tertiary) hover:bg-(--ui-hover-background) hover:text-(--ui-text-primary)"
          onClick={() => endRelayTurn(turn.id)}
          type="button"
        >
          <Codicon aria-hidden name="close" size={12} />
        </button>
      )}
    </div>
  )
}

/** Whole seconds since `startedAt`, recomputed on each parent render (the pill
 *  already re-renders on store changes; a second ticker adds nothing while a
 *  turn is young and the popover is closed). */
function elapsedSeconds(startedAt: number): number {
  return Math.max(0, Math.round((Date.now() - startedAt) / 1000))
}

export function RelayTurnsPill() {
  const b = useBots()
  const turnsById = useValue($relayTurns)
  const [open, setOpen] = useState(false)

  const turns = Object.values(turnsById).sort((x, y) => y.startedAt - x.startedAt)
  const running = turns.filter(turn => turn.phase === 'delivering')

  if (turns.length === 0) {
    return null
  }

  const label = running.length > 0 ? b.relay.pillRunning(running.length) : b.relay.pillFinished(turns.length)

  return (
    <Popover onOpenChange={setOpen} open={open}>
      <PopoverTrigger asChild>
        <button
          aria-label={label}
          className="flex h-5 items-center gap-1 rounded px-1.5 text-[0.7rem] text-(--ui-text-secondary) hover:bg-(--ui-hover-background) hover:text-(--ui-text-primary)"
          data-running={running.length > 0 ? '' : undefined}
          data-slot="relay-turns-pill"
          type="button"
        >
          {running.length > 0 ? (
            <GlyphSpinner className="text-[10px]" />
          ) : (
            <Codicon aria-hidden className="text-(--ui-text-tertiary)" name="check" size={12} />
          )}
          <span className="max-w-40 truncate">{label}</span>
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 p-0">
        <div className="border-b border-(--ui-stroke-secondary) px-2.5 py-1.5 text-[0.7rem] font-medium text-(--ui-text-secondary)">
          {b.relay.popoverTitle(running.length, turns.length)}
        </div>
        <div className="max-h-72 overflow-y-auto">
          {turns.map(turn => (
            <RelayTurnRow key={turn.id} turn={turn} />
          ))}
        </div>
      </PopoverContent>
    </Popover>
  )
}
