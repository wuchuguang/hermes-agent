import { randomUuid } from '@hermes/plugin-sdk'

import { canonicalGroupRequest } from './canonical-groups'
import type { CanonicalGroupRoute, CanonicalRoom, CanonicalRoomContext } from './canonical-groups'

/** Resolve the elected leader by the id in the frozen creation request, never by display name. */
export function canonicalProjectContext(room: CanonicalRoom, requestedLeaderId: string | undefined, brief: string): CanonicalRoomContext {
  const leader = room.members.find(member => member.member_id === requestedLeaderId)

  return {
    purpose: brief.trim().slice(0, 500),
    notes: '',
    routing: leader ? 'lead' : 'everyone',
    lead_member_id: leader?.member_id ?? ''
  }
}

/** New gateway-owned rooms execute with the same project goal and leader chosen in the dialog. */
export async function saveCanonicalGroupProject(route: CanonicalGroupRoute, room: CanonicalRoom, requestedLeaderId: string | undefined, brief: string) {
  const context = canonicalProjectContext(room, requestedLeaderId, brief)

  await canonicalGroupRequest(route, 'groups.context', { room_id: room.room_id, event_id: randomUuid(), ...context })
}
