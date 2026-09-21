/**
 * The room's rolling memory. The retained log window (`trimGroupChatLog`)
 * DROPS old entries silently — fine for a chat, fatal for a project: the
 * conclusions of week one vanish from every member's turn prompt. The digest
 * is a heuristic rolling summary (speaker → counts + last statement, user
 * asks verbatim) captured AT TRIM TIME, injected into every turn prompt ahead
 * of the delta, so members keep their bearings without any model call.
 *
 * Heuristic on purpose: an LLM summary here would run inside `updateGroupChat`
 * — a synchronous store mutation on the hot append path — and add cost,
 * latency, and a failure mode to every message. The digested facts (who said
 * what, what was asked) are exactly what an agent needs to re-orient; the
 * LEADER's richer "current status" lives in its own turn output, which the
 * delta carries while it stays in the window.
 */

import type { GroupMessage } from './types'

const DIGEST_MAX_CHARS = 2048
const USER_ASK_MAX = 160

/** A user line worth carrying: it asks, assigns, or decides. Kept verbatim
 *  (truncated) because paraphrase loses the assignment. */
function userAskText(text: string): null | string {
  const flat = String(text || '').replace(/\s+/g, ' ').trim()

  if (!flat) {
    return null
  }

  return flat.length > USER_ASK_MAX ? `${flat.slice(0, USER_ASK_MAX - 1)}…` : flat
}

interface SpeakerTurns {
  last: string
  turns: number
}

/**
 * Summarize the entries about to leave the retained window into a digest
 * block. Returns a stable, compact string; empty input or no summarizable
 * content returns '' (the prompt then omits the section entirely).
 */
export function buildGroupDigest(entries: GroupMessage[]): string {
  const speakerTurns = new Map<string, SpeakerTurns>()
  const userAsks: string[] = []

  for (const entry of entries) {
    const text = String(entry?.text || '').trim()

    if (!text) {
      continue
    }

    if (entry.from?.kind === 'user') {
      const ask = userAskText(text)

      if (ask) {
        userAsks.push(ask)
      }

      continue
    }

    const name = String(entry.from?.name || 'bot')

    if (text.length <= 200) {
      const prior = speakerTurns.get(name)

      speakerTurns.set(name, {
        last: text,
        turns: (prior?.turns || 0) + 1
      })
    } else {
      // A long message is substantive work; its FIRST LINE is its headline —
      // flattened, so the digest stays one line per speaker.
      const prior = speakerTurns.get(name)
      const headline = userAskText(text.split('\n').find(line => line.trim()) || text)

      const next: SpeakerTurns = {
        last: headline || (prior?.last ?? ''),
        turns: (prior?.turns || 0) + 1
      }

      speakerTurns.set(name, next)
    }
  }

  if (!speakerTurns.size && !userAsks.length) {
    return ''
  }

  const lines: string[] = []

  if (userAsks.length) {
    lines.push('Earlier requests/decisions (oldest first):')

    for (const ask of userAsks) {
      lines.push(`  ${ask}`)
    }
  }

  if (speakerTurns.size) {
    if (lines.length) {
      lines.push('')
    }

    lines.push('Earlier member activity:')

    for (const [name, info] of speakerTurns) {
      lines.push(`  ${name}: ${info.turns} message(s); last: ${info.last}`)
    }
  }

  const digest = lines.join('\n')

  return digest.length > DIGEST_MAX_CHARS ? `${digest.slice(0, DIGEST_MAX_CHARS - 1)}…` : digest
}

/**
 * Fold outgoing entries into the room's existing digest. Concatenation is
 * monotone (never loses an earlier ask); the char cap trades oldest asks
 * first, matching the window's own recency bias. Returns '' when both sides
 * are empty so the room record stores null, not an empty string.
 */
export function mergeGroupDigest(existing: null | string | undefined, outgoing: GroupMessage[]): string {
  const fresh = buildGroupDigest(outgoing)

  if (!fresh) {
    return typeof existing === 'string' && existing ? existing : ''
  }

  if (!existing) {
    return fresh
  }

  const merged = `${existing}\n\n${fresh}`

  // Over the cap keep the NEWEST tail (drop the oldest head) — recency bias
  // matches the retained window's own.
  return merged.length > DIGEST_MAX_CHARS ? `${merged.slice(-DIGEST_MAX_CHARS)}` : merged
}
