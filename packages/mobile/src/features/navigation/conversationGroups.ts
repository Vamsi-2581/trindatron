import type { Session } from '@/services/api/types'
import { parseServerDate } from '@/utils/dates'

export type ConversationGroupKey = 'today' | 'yesterday' | 'week' | 'older'

export interface ConversationGroup {
  key: ConversationGroupKey
  sessions: Session[]
}

const ORDER: ConversationGroupKey[] = ['today', 'yesterday', 'week', 'older']
const DAY_MS = 24 * 60 * 60 * 1000

function startOfLocalDay(time: number): number {
  const date = new Date(time)
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
}

/** Which group a conversation last touched at `updatedAt` belongs to, by calendar day on this device. */
export function conversationGroup(updatedAt: string, now = Date.now()): ConversationGroupKey {
  const days = Math.round((startOfLocalDay(now) - startOfLocalDay(parseServerDate(updatedAt).getTime())) / DAY_MS)
  if (days <= 0) return 'today'
  if (days === 1) return 'yesterday'
  if (days < 7) return 'week'
  return 'older'
}

/**
 * The history as it is read: Hoje, Ontem, the last 7 days, and everything before — newest first inside each
 * group, empty groups left out. Grouping is display only; the order and the list itself are the Core's.
 */
export function groupConversations(sessions: readonly Session[], now = Date.now()): ConversationGroup[] {
  const buckets = new Map<ConversationGroupKey, Session[]>()
  for (const session of [...sessions].sort((a, b) => parseServerDate(b.updated_at).getTime() - parseServerDate(a.updated_at).getTime())) {
    const key = conversationGroup(session.updated_at, now)
    buckets.set(key, [...(buckets.get(key) ?? []), session])
  }
  return ORDER.filter((key) => buckets.has(key)).map((key) => ({ key, sessions: buckets.get(key)! }))
}
