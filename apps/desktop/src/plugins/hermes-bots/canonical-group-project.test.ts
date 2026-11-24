import { describe, expect, it, vi } from 'vitest'

const request = vi.hoisted(() => vi.fn())

vi.mock('@hermes/plugin-sdk', () => ({ randomUuid: () => 'settings-event' }))
vi.mock('./canonical-groups', () => ({ canonicalGroupRequest: request }))

import { canonicalProjectContext, saveCanonicalGroupProject } from './canonical-group-project'
import type { CanonicalRoom } from './canonical-groups'

const room: CanonicalRoom = {
  room_id: 'room-1',
  name: 'Project',
  members: [
    { member_id: 'dev-local', profile: 'dev', handle: 'dev', target: { kind: 'local', profile: 'dev' } },
    { member_id: 'dev-peer', profile: 'dev', handle: 'dev-other', target: { kind: 'peer', installation_id: 'other', profile: 'dev' } }
  ]
}

describe('canonical group project settings', () => {
  it('keeps the elected peer distinct from the same profile on this machine', () => {
    expect(canonicalProjectContext(room, 'dev-peer', '  Ship checkout  ')).toEqual({
      purpose: 'Ship checkout', notes: '', routing: 'lead', lead_member_id: 'dev-peer'
    })
  })

  it('never elects a missing member and respects the backend purpose limit', () => {
    const context = canonicalProjectContext(room, 'gone', 'x'.repeat(600))

    expect(context.routing).toBe('everyone')
    expect(context.lead_member_id).toBe('')
    expect(context.purpose).toHaveLength(500)
  })

  it('saves the execution settings on the room authority', async () => {
    request.mockResolvedValueOnce({})
    const route = { connectionId: 'remote-owner', profile: 'pm' }

    await saveCanonicalGroupProject(route, room, 'dev-peer', 'Goal')
    expect(request).toHaveBeenLastCalledWith(route, 'groups.context', {
      room_id: 'room-1', event_id: 'settings-event', purpose: 'Goal', notes: '',
      routing: 'lead', lead_member_id: 'dev-peer'
    })
  })

  it('surfaces rejected settings so the creation UI can warn without creating another room', async () => {
    request.mockRejectedValueOnce(new Error('settings unavailable'))
    await expect(saveCanonicalGroupProject({ connectionId: 'local', profile: 'pm' }, room, 'dev-local', 'Goal'))
      .rejects.toThrow('settings unavailable')
  })
})
