const DAY_MS = 24 * 60 * 60 * 1000

function startOfDay(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
}

/**
 * The label over the first message of a day: "today" and "yesterday" in words, otherwise the date in the person's
 * language — with the year only when it is not this one. `null` for a date that cannot be read, so nothing is shown.
 */
export function dayLabel(
  value: Date | number | string,
  now: Date,
  intlLocale: string,
  words: { today: string; yesterday: string },
): string | null {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null

  // Calendar days, not 24-hour spans: a message from 23:50 is "yesterday" at 00:10.
  const days = Math.round((startOfDay(now) - startOfDay(date)) / DAY_MS)
  if (days === 0) return words.today
  if (days === 1) return words.yesterday

  const sameYear = date.getFullYear() === now.getFullYear()
  return new Intl.DateTimeFormat(intlLocale, sameYear ? { day: 'numeric', month: 'long' } : { day: 'numeric', month: 'long', year: 'numeric' }).format(date)
}
