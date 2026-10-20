import { currentIntlLocale, t } from '@/i18n'

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit++
  }
  const number = new Intl.NumberFormat(currentIntlLocale(), { maximumFractionDigits: value >= 10 ? 0 : 1 }).format(value)
  return `${number} ${units[unit]}`
}

/** Display name for a plan id. Product names are the same in every language. */
export function planLabel(plan: string | null | undefined): string {
  if (!plan) return '—'
  return plan.charAt(0).toUpperCase() + plan.slice(1)
}

export function formatPrice(amount: string, currency: string): string {
  const value = Number(amount)
  if (!Number.isFinite(value)) return `${amount} ${currency}`
  try {
    return new Intl.NumberFormat(currentIntlLocale(), { style: 'currency', currency }).format(value)
  } catch {
    return `${value.toFixed(2)} ${currency}`
  }
}

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${Math.round(value)}%`
}

// The Core's default title for a conversation that has not been named yet.
const CORE_DEFAULT_TITLE = 'New conversation'

export function conversationTitle(title: string | null | undefined): string {
  const trimmed = title?.trim()
  return !trimmed || trimmed === CORE_DEFAULT_TITLE ? t('chat.untitled') : trimmed
}

export function firstName(fullName: string | null | undefined): string | null {
  const name = fullName?.trim().split(/\s+/)[0]
  return name ? name : null
}
