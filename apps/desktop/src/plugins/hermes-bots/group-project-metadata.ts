interface GroupProjectMetadata {
  brief?: null | string
  leaderKey?: null | string
  digest?: null | string
}

/** Keep project metadata normalization identical across durable write paths. */
export function durableGroupProjectMetadata(room: GroupProjectMetadata) {
  return {
    brief: typeof room.brief === 'string' && room.brief ? room.brief : null,
    leaderKey: typeof room.leaderKey === 'string' && room.leaderKey ? room.leaderKey : null,
    digest: typeof room.digest === 'string' && room.digest ? room.digest : null
  }
}

/** Sync omits empty fields; only snapshot publication applies payload bounds. */
export function syncGroupProjectMetadata(room: GroupProjectMetadata = {}, bounded = false) {
  const fields = durableGroupProjectMetadata(room)
  const limits: Record<string, number> = { brief: 512, leaderKey: 160, digest: 2048 }

  return Object.fromEntries(
    Object.entries(fields)
      .filter((entry): entry is [string, string] => Boolean(entry[1]))
      .map(([key, value]) => [key, bounded ? value.slice(0, limits[key]) : value])
  ) as Partial<Record<keyof GroupProjectMetadata, string>>
}