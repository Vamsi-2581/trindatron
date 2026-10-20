import { currentIntlLocale, t } from '@/i18n'

const HAS_OFFSET = /(Z|[+-]\d{2}:?\d{2})$/i

/**
 * The Core serialises many timestamps as naive UTC ("2026-09-13T18:41:58.14").
 * `new Date()` would read those as device-local time, so they get an explicit Z.
 */
export function parseServerDate(value: string): Date {
  return new Date(HAS_OFFSET.test(value) ? value : `${value}Z`)
}

const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

export function formatRelativeTime(value: string, now = Date.now()): string {
  const date = parseServerDate(value)
  const diff = now - date.getTime()
  if (diff < MINUTE) return t('dates.justNow')
  if (diff < HOUR) return t('dates.minutesAgo', { count: Math.floor(diff / MINUTE) })
  if (diff < DAY) return t('dates.hoursAgo', { count: Math.floor(diff / HOUR) })
  if (diff < 2 * DAY) return t('dates.yesterday')
  if (diff < 7 * DAY) return t('dates.daysAgo', { count: Math.floor(diff / DAY) })
  return new Intl.DateTimeFormat(currentIntlLocale(), { day: '2-digit', month: 'short', year: 'numeric' }).format(date)
}

export function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat(currentIntlLocale(), {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  }).format(parseServerDate(value))
}

/** "em 3 h", "in 2 days" — for a renewal instant decided by the Core. */
export function formatTimeUntil(value: string, now = Date.now()): string {
  const diff = parseServerDate(value).getTime() - now
  if (diff <= MINUTE) return t('dates.inMoments')
  if (diff < HOUR) return t('dates.inMinutes', { count: Math.ceil(diff / MINUTE) })
  if (diff < DAY) return t('dates.inHours', { count: Math.round(diff / HOUR) })
  const days = Math.round(diff / DAY)
  return days === 1 ? t('dates.inOneDay') : t('dates.inDays', { count: days })
}

/** Local wall-clock time in the user's language, e.g. "14:30" or "2:30 PM". */
export function formatClock(value: string): string {
  return new Intl.DateTimeFormat(currentIntlLocale(), { hour: '2-digit', minute: '2-digit' }).format(parseServerDate(value))
}

export function formatWeekday(value: string): string {
  return new Intl.DateTimeFormat(currentIntlLocale(), { weekday: 'long' }).format(parseServerDate(value))
}

export function isSameLocalDay(value: string, now = Date.now()): boolean {
  return parseServerDate(value).toDateString() === new Date(now).toDateString()
}
